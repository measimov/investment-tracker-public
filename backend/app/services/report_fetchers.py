"""财报原文/基本面获取的纯 HTTP 层（cninfo 巨潮 + SEC EDGAR + Yahoo 港股）。

不碰 DB、不做文本解析——只负责检索/下载/限速/缓存映射。所有函数可被
测试整体 monkeypatch。数据源为公开免费接口（2026-08-03 实测连通），带
UA 与保守限速；被封禁时上层落 failed 行并降级，不重试轰炸。

EDGAR 合规要求：UA 须携带联系方式（settings.edgar_user_agent），限速
≤10 req/s（本层 0.15s 间隔）。
"""

import json
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import requests
import requests.adapters
import urllib3
import urllib3.connection

from ..core.logging import get_app_logger
from . import report_cache
from .http_source import throttle as _throttle

logger = get_app_logger(__name__)

# 全链路 HTTPS：明文 HTTP 下映射、检索响应与 PDF 字节都可被中间人替换，
# 最终污染投资分析。巨潮三个端点均已支持 HTTPS（2026-08-03 实测 200/206）
_CNINFO_BASE = "https://www.cninfo.com.cn"
_CNINFO_STATIC = "https://static.cninfo.com.cn"
_CNINFO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
}

# 保守限速：cninfo 每次请求间隔 ≥1s（全模块共享，含 PDF 下载）
_CNINFO_MIN_INTERVAL_SECONDS = 1.0
# 分源限速：实现在 http_source（#281，语义见那里）；按模块名调用，测试可整体替换 _throttle

PDF_DOWNLOAD_TIMEOUT_SECONDS = 60
PDF_MAX_BYTES = 50 * 1024 * 1024
# 下载总时长与最低速度：`timeout=` 只管「多久没有任何字节」，连接退化成涓流时（2026-09-26 实测
# 披露易某个 Akamai 边缘节点每秒 1.8KB，正常 500-700KB/s）字节一直在来、永远不超时，一份
# 大 PDF 要下几个小时并卡死整个补跑。超过总时长或宽限期后平均速度过低即放弃这条连接并换新
# 连接重试一次；仍失败抛 requests.Timeout（调用方按瞬时失败处理，不烧 attempts）
PDF_DOWNLOAD_DEADLINE_SECONDS = 180
PDF_MIN_SPEED_GRACE_SECONDS = 30
PDF_MIN_BYTES_PER_SECOND = 32 * 1024
PDF_DOWNLOAD_ATTEMPTS = 2
_PDF_CHUNK_BYTES = 64 * 1024
# 主线程监督下载的轮询间隔（墙钟上限的精度）
_PDF_WATCHDOG_POLL_SECONDS = 0.5
# 打断后等待工作线程收尾的上限（shutdown 后 recv 立即失败，正常毫秒级）
_PDF_WORKER_JOIN_SECONDS = 5.0
_monotonic = time.monotonic

# A股报告 category → 报告类型标记
CNINFO_CATEGORIES = {
    "annual": "category_ndbg_szsh",
    "semi": "category_bndbg_szsh",
}

# orgId 全量映射（code → orgId），进程内缓存；超过 _MAPPING_MAX_AGE_SECONDS 重新拉取——
# 常驻进程里新上市/新加入自选的标的否则永远查不到（PR #309 评审 P2-1）
_MAPPING_MAX_AGE_SECONDS = 24 * 3600
_org_id_cache: Dict[str, str] = {}
_org_id_cache_loaded = False
_org_id_cache_loaded_at = 0.0


def _load_org_id_map() -> Dict[str, str]:
    global _org_id_cache_loaded, _org_id_cache_loaded_at
    if (
        _org_id_cache_loaded
        and time.monotonic() - _org_id_cache_loaded_at < _MAPPING_MAX_AGE_SECONDS
    ):
        return _org_id_cache
    _throttle("cninfo", _CNINFO_MIN_INTERVAL_SECONDS)
    response = requests.get(
        f"{_CNINFO_BASE}/new/data/szse_stock.json",
        headers=_CNINFO_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    for row in response.json().get("stockList", []):
        code = str(row.get("code") or "").strip()
        org_id = str(row.get("orgId") or "").strip()
        if code and org_id:
            _org_id_cache[code] = org_id
    _org_id_cache_loaded = True
    _org_id_cache_loaded_at = time.monotonic()
    logger.info("cninfo orgId 映射加载完成：%d 条", len(_org_id_cache))
    return _org_id_cache


def cninfo_org_id(symbol: str, *, strict: bool = False) -> Optional[str]:
    """code → orgId；映射表加载失败时回退沪市惯例 gssh0{code}（实测有效）。

    strict=True（公告同步用）：只有映射表**确认加载成功且查无此码**才返回 None；加载失败时
    已缓存的旧映射里有就用，没有就原样抛出——回退规则只覆盖 6/9 开头，深市代码会被误判
    为「无官方源」，B 股会退成按 B 股代码检索（恒为 0 条却记成功）。"""
    try:
        org_id = _load_org_id_map().get(symbol)
        if org_id or strict:
            return org_id
    except Exception as exc:
        cached = _org_id_cache.get(symbol)
        if cached:
            return cached
        if strict:
            raise
        logger.warning("cninfo orgId 映射加载失败，使用回退规则: %s", str(exc)[:120])
    if symbol.startswith(("6", "9")):
        return f"gssh0{symbol}"
    return None


def cninfo_local_date(ann_ts: Any) -> str:
    """巨潮公告时间戳（毫秒）→ 业务时区日期 YYYY-MM-DD；缺失返回空串。"""
    if not ann_ts:
        return ""
    from datetime import datetime, timezone

    from ..core.timeutil import business_timezone

    moment = datetime.fromtimestamp(int(ann_ts) / 1000, tz=timezone.utc)
    return moment.astimezone(business_timezone()).date().isoformat()


def cninfo_search_reports(symbol: str, *, report_type: str, se_date: str) -> List[Dict[str, Any]]:
    """检索年报/半年报公告列表（含修订版与摘要，由调用方过滤）。

    返回 [{title, ann_date(YYYY-MM-DD), ann_date_local, url, adjunct_size_kb}]，按公告时间倒序。

    `ann_date` 是 UTC 日期（北京时间零点的公告会早一天，#346-4），但它是摘要缓存
    `source_fingerprint` 的组成部分——直接改会让全部 A股 摘要指纹失效、全量重烧 LLM，所以
    原样保留；按业务时区换算的日期另给 `ann_date_local`，只供展示与「标题无年份」的年份兜底。
    """
    org_id = cninfo_org_id(symbol)
    stock = f"{symbol},{org_id}" if org_id else symbol
    announcements: List[Dict[str, Any]] = []
    page = 1
    while page <= 5:  # 十年年报翻页护栏
        _throttle("cninfo", _CNINFO_MIN_INTERVAL_SECONDS)
        response = requests.post(
            f"{_CNINFO_BASE}/new/hisAnnouncement/query",
            headers=_CNINFO_HEADERS,
            data={
                "pageNum": page,
                "pageSize": 30,
                "column": "szse",
                "tabName": "fulltext",
                "stock": stock,
                "category": CNINFO_CATEGORIES[report_type],
                "seDate": se_date,
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        rows = body.get("announcements") or []
        for row in rows:
            adjunct = str(row.get("adjunctUrl") or "")
            if not adjunct.lower().endswith(".pdf"):
                continue
            ann_ts = row.get("announcementTime")
            ann_date = time.strftime("%Y-%m-%d", time.gmtime(ann_ts / 1000)) if ann_ts else ""
            announcements.append(
                {
                    "title": str(row.get("announcementTitle") or ""),
                    "ann_date": ann_date,
                    "ann_date_local": cninfo_local_date(ann_ts),
                    "url": f"{_CNINFO_STATIC}/{adjunct}",
                    "adjunct_size_kb": row.get("adjunctSize"),
                }
            )
        if not body.get("hasMore") and len(rows) < 30:
            break
        page += 1
    return announcements


def cninfo_announcement_page(
    stock: str, *, se_date: str, page: int, page_size: int = 30
) -> Dict[str, Any]:
    """巨潮公告检索的一页原始响应（不限类别，含非 PDF 行）。

    stock 为 `代码,orgId`（B 股用 orgId 对应的 A 股代码，见 announcement_sources）；
    se_date 为 `YYYY-MM-DD~YYYY-MM-DD`。返回 {announcements: [...], hasMore, totalAnnouncement}。
    偶发返回非 JSON（实测 601899 一次）：重试一次，仍失败抛出由调用方记失败。"""
    last_exc: Optional[Exception] = None
    for _attempt in range(2):
        _throttle("cninfo", _CNINFO_MIN_INTERVAL_SECONDS)
        try:
            response = requests.post(
                f"{_CNINFO_BASE}/new/hisAnnouncement/query",
                headers=_CNINFO_HEADERS,
                data={
                    "pageNum": page,
                    "pageSize": page_size,
                    "column": "szse",
                    "tabName": "fulltext",
                    "stock": stock,
                    "seDate": se_date,
                },
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            continue
        if not isinstance(body, dict):
            last_exc = ValueError(f"巨潮响应结构异常: {str(body)[:120]}")
            continue
        return body
    assert last_exc is not None
    raise last_exc


def download_report_pdf(url: str, *, source: str = "cninfo") -> bytes:
    """流式下载 PDF（60s 超时、50MB 上限，超限即断）。

    只接受 HTTPS：库内可能残留旧版本写入的明文 URL，重放时不得降级
    （明文响应可被替换，PDF 内容最终进入投资分析）。

    `source` 决定限速桶与请求头——巨潮与披露易是两个站点，共用一个限速桶只会
    互相拖慢，而 Referer 写错会被对方拒绝。
    """
    if not str(url).lower().startswith("https://"):
        raise ValueError(f"拒绝以非 HTTPS 方式下载财报: {url}")
    headers, interval = (
        (_HKEX_HEADERS, _HKEX_MIN_INTERVAL_SECONDS)
        if source == "hkexnews"
        else (_CNINFO_HEADERS, _CNINFO_MIN_INTERVAL_SECONDS)
    )
    return _download_guarded(url, headers, source=source, interval=interval)


def _download_guarded(url: str, headers: Dict[str, str], *, source: str, interval: float) -> bytes:
    """按站点限速的流式下载：墙钟总时长 + 最低速率看门狗，慢连接换新连接重试一次。
    PDF（巨潮/披露易）与 EDGAR 主文档共用（#345：EDGAR 曾是一次性 `requests.get`，读完才查
    大小上限、也没有墙钟看门狗，涓流会让整轮摘要卡死）。"""
    # 原始报告本地缓存（report_cache）：解析规则升版重跑时命中本地，不再重新下载；未命中才联网
    return report_cache.cached_download(
        url, source, lambda: _download_with_retry(url, headers, source=source, interval=interval)
    )


def _download_with_retry(
    url: str, headers: Dict[str, str], *, source: str, interval: float
) -> bytes:
    last_error: Optional[requests.Timeout] = None
    for _attempt in range(PDF_DOWNLOAD_ATTEMPTS):
        _throttle(source, interval)
        try:
            return _download_once(url, headers)
        except requests.Timeout as exc:
            # 慢连接/总时长超限：换一条新连接（CDN 可能分到另一个边缘节点）再试
            last_error = exc
    assert last_error is not None
    raise last_error


def _tracking_session(sockets: List[socket.socket]) -> requests.Session:
    """每次下载一个独立 Session：连接池的连接类在**建出 socket 的那一刻**登记它的一个 dup。

    取消不能依赖 `requests.get` 返回——响应头阶段（服务端持续涓流不结束的响应头，每个字节都
    重置读超时）`response` 还是 None（PR #212 评审 P2）。也不能登记原始 socket 本身：HTTPS 的
    `wrap_socket` 会 detach 原始 socket 的 fd（之后 `fileno()` 为 -1，shutdown 只得到 EBADF），
    TLS 握手与响应头阶段都拿不到 SSLSocket（评审 P1）。`sock.dup()` 是指向**同一个内核
    socket** 的独立 fd：detach 动不到它，shutdown 它会让 SSLSocket 的阻塞 recv/握手立即失败，
    且 fd 归我们所有、不存在被复用成别的连接的风险。dup 在下载结束时由 `_close_sockets` 关闭。"""

    def track(conn_cls):
        class _Tracked(conn_cls):
            def _new_conn(self):  # noqa: D401 — urllib3 钩子
                sock = super()._new_conn()
                try:
                    sockets.append(sock.dup())
                except OSError:
                    pass
                return sock

        return _Tracked

    class _Pool(urllib3.HTTPConnectionPool):
        ConnectionCls = track(urllib3.connection.HTTPConnection)

    class _TlsPool(urllib3.HTTPSConnectionPool):
        ConnectionCls = track(urllib3.connection.HTTPSConnection)

    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    adapter.poolmanager.pool_classes_by_scheme = {"http": _Pool, "https": _TlsPool}
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _download_once(url: str, headers: Dict[str, str]) -> bytes:
    """一次下载，受**墙钟**总时长与最低速度约束。

    检查不能只放在「收到一块数据之后」：`iter_content` 攒满一块（或 EOF）才 yield，只要字节
    持续涓流进来，`timeout=` 的读超时也不触发——100B/s 时第一块就要十分钟，1B/s 要十几小时
    （PR #212 评审 P1，用真实流式服务复现）。所以读取放在工作线程里，主线程按墙钟监督：超过
    总时长或宽限期后平均速度过低，就 shutdown 底层 socket，让阻塞中的 recv 立即失败——等待
    响应头的阶段同样被覆盖。工作线程是 daemon，被放弃后随 socket 关闭自行结束。"""
    state: Dict[str, Any] = {"response": None, "total": 0, "result": None, "error": None}
    sockets: List[socket.socket] = []
    session = _tracking_session(sockets)
    abort = threading.Event()
    done = threading.Event()
    started = _monotonic()

    def check(total: int) -> None:
        elapsed = _monotonic() - started
        if elapsed > PDF_DOWNLOAD_DEADLINE_SECONDS:
            raise requests.Timeout(
                f"PDF 下载超过总时长 {PDF_DOWNLOAD_DEADLINE_SECONDS}s（已收 {total // 1024}KB）"
            )
        if elapsed > PDF_MIN_SPEED_GRACE_SECONDS and total / elapsed < PDF_MIN_BYTES_PER_SECOND:
            raise requests.Timeout(
                f"PDF 下载过慢（{int(total / elapsed / 1024)}KB/s < "
                f"{PDF_MIN_BYTES_PER_SECOND // 1024}KB/s），放弃这条连接"
            )

    def worker() -> None:
        response = None
        try:
            response = session.get(
                url, headers=headers, timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS, stream=True
            )
            state["response"] = response
            if abort.is_set():
                return
            response.raise_for_status()
            chunks: List[bytes] = []
            # 块要小：块越大，正常下载时工作线程内的检查越稀疏（墙钟上限由主线程兜底）
            for chunk in response.iter_content(chunk_size=_PDF_CHUNK_BYTES):
                if abort.is_set():
                    return
                state["total"] += len(chunk)
                if state["total"] > PDF_MAX_BYTES:
                    raise ValueError(f"PDF 超过大小上限 {PDF_MAX_BYTES // (1 << 20)}MB")
                chunks.append(chunk)
                check(state["total"])
            state["result"] = b"".join(chunks)
        except BaseException as exc:  # noqa: BLE001 — 原样交回主线程
            state["error"] = exc
        finally:
            if response is not None:
                response.close()
            session.close()
            done.set()

    thread = threading.Thread(target=worker, name="pdf-download", daemon=True)
    thread.start()
    try:
        while not done.wait(timeout=_PDF_WATCHDOG_POLL_SECONDS):
            try:
                check(state["total"])
            except requests.Timeout:
                abort.set()
                _shutdown_sockets(sockets)
                _force_close(state["response"])
                # 打断后工作线程应立即结束；等它收尾（有界），线程与连接真正释放而不是被遗弃
                thread.join(timeout=_PDF_WORKER_JOIN_SECONDS)
                raise
    finally:
        if not thread.is_alive():
            _close_sockets(sockets)
    if state["error"] is not None:
        raise state["error"]
    return state["result"]


def _shutdown_sockets(sockets: List[socket.socket]) -> None:
    for sock in list(sockets):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _close_sockets(sockets: List[socket.socket]) -> None:
    """关闭登记的 dup（工作线程已结束后才关：之前关掉就失去打断手段）。"""
    while sockets:
        try:
            sockets.pop().close()
        except OSError:
            pass


def _force_close(response: Optional[requests.Response]) -> None:
    """从另一线程打断阻塞中的读取：close() 在 Linux 上不保证唤醒正在 recv 的线程，
    shutdown(SHUT_RDWR) 才会。握手与响应头阶段 response 还是 None，由 `_shutdown_sockets` 按
    连接建立时登记的 dup 打断（见 `_tracking_session`），这里只做响应对象的收尾。"""
    if response is None:
        return
    raw = getattr(response, "raw", None)
    sock = getattr(getattr(raw, "_connection", None), "sock", None)
    if sock is None:
        fp = getattr(raw, "_fp", None)
        sock = getattr(getattr(getattr(fp, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        response.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# SEC EDGAR（美股：官方免费；UA 须带联系方式，限速 ≤10 req/s）
# ---------------------------------------------------------------------------

_EDGAR_MIN_INTERVAL_SECONDS = 0.15

# symbol → {cik, title} 映射，进程内缓存
_cik_cache: Dict[str, Dict[str, Any]] = {}
_cik_cache_loaded = False
_cik_cache_loaded_at = 0.0


def _edgar_headers() -> Dict[str, str]:
    from ..config import settings

    user_agent = (settings.edgar_user_agent or "").strip()
    if not user_agent:
        user_agent = "investment-tracker/1.0 (contact-not-configured@example.com)"
        logger.warning("EDGAR_USER_AGENT 未配置，使用占位 UA（建议配置联系方式）")
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}


def _edgar_get_json(url: str) -> Dict[str, Any]:
    _throttle("edgar", _EDGAR_MIN_INTERVAL_SECONDS)
    response = requests.get(url, headers=_edgar_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def _load_cik_map() -> Dict[str, Dict[str, Any]]:
    global _cik_cache_loaded, _cik_cache_loaded_at
    if _cik_cache_loaded and time.monotonic() - _cik_cache_loaded_at < _MAPPING_MAX_AGE_SECONDS:
        return _cik_cache
    data = _edgar_get_json("https://www.sec.gov/files/company_tickers.json")
    _cik_reverse_cache.clear()
    for entry in data.values():
        ticker = str(entry.get("ticker") or "").upper()
        if ticker:
            _cik_cache[ticker] = {
                "cik": int(entry.get("cik_str") or 0),
                "title": entry.get("title"),
            }
    _cik_cache_loaded = True
    _cik_cache_loaded_at = time.monotonic()
    logger.info("EDGAR CIK 映射加载完成：%d 条", len(_cik_cache))
    return _cik_cache


def edgar_lookup(symbol: str) -> Optional[Dict[str, Any]]:
    """美股 symbol → {cik, title}；未注册返回 None。映射表加载失败时旧缓存里有就用，
    没有就抛出（不当成「未注册」）。"""
    ticker = str(symbol or "").strip().upper()
    try:
        return _load_cik_map().get(ticker)
    except Exception:
        if ticker in _cik_cache:
            return _cik_cache[ticker]
        raise


_cik_reverse_cache: Dict[int, Dict[str, Any]] = {}


def edgar_reverse_lookup(cik: int) -> Optional[Dict[str, Any]]:
    """CIK → {symbol, title}；非上市 filer（无 ticker）返回 None。

    一 CIK 多 ticker（GOOG/GOOGL 股别）取 company_tickers 首见者（主类）。
    """
    if not _cik_reverse_cache:
        for ticker, entry in _load_cik_map().items():
            _cik_reverse_cache.setdefault(entry["cik"], {"symbol": ticker, "title": entry["title"]})
    return _cik_reverse_cache.get(int(cik))


def edgar_companyfacts(cik: int) -> Dict[str, Any]:
    return _edgar_get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")


# submissions 的短时进程缓存（#281）：一次分析任务里摘要规划、ADS 换算比、同业、行业会各拉一遍
# 同一份（几百 KB），SEC 公平访问要求 ≤10 req/s。只缓存成功响应；10 分钟足够覆盖一次任务，
# 又不会让公告同步（30 分钟一轮）错过新申报
_SUBMISSIONS_TTL_SECONDS = 600
_submissions_cache: Dict[int, tuple] = {}


def edgar_submissions(cik: int) -> Dict[str, Any]:
    cik = int(cik)
    cached = _submissions_cache.get(cik)
    if cached is not None and _monotonic() - cached[0] < _SUBMISSIONS_TTL_SECONDS:
        return cached[1]
    payload = _edgar_get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    _submissions_cache[cik] = (_monotonic(), payload)
    return payload


def clear_edgar_submissions_cache() -> None:
    _submissions_cache.clear()


# 年报表单：10-K 是美国本土发行人，20-F 是外国私人发行人（中概股几乎全是
# 20-F）。只认 10-K 会让 PDD/BABA 这类标的检索到 0 份年报——财报摘要与商业
# 画像整块空白，而表面上"没有报错"。
EDGAR_ANNUAL_FORMS = ("10-K", "20-F")


class EdgarListingIncomplete(Exception):
    """年报清单没读全（`filings.files` 的某一页取不到）：`filings` 是已读到的部分。"""

    def __init__(self, message: str, filings: List[Dict[str, Any]]):
        super().__init__(message)
        self.filings = filings


def _annual_filings_in(columns: Dict[str, Any], limit: int, out: List[Dict[str, Any]]) -> None:
    forms = columns.get("form") or []
    for index, form in enumerate(forms):
        if len(out) >= limit:
            return
        if form not in EDGAR_ANNUAL_FORMS:
            continue
        out.append(
            {
                "form": str(form),
                "accession": str(columns["accessionNumber"][index]),
                "primary_document": str(columns["primaryDocument"][index]),
                "filing_date": str(columns["filingDate"][index]),
                "report_date": str(columns["reportDate"][index]),
            }
        )


def edgar_recent_annual_filings(cik: int, *, limit: int = 10) -> List[Dict[str, Any]]:
    """近 N 份年报：[{form, accession, primary_document, filing_date, report_date}]。

    submissions 的 `filings.recent` 只保证「至少一年或 1000 条」，更早的申报在 `filings.files`
    分页里（#346-1：Form 4 / 6-K / 424B 多的发行人，recent 里可能只剩三份年报，十年覆盖静默
    缩水却标成完整）。recent 不够 limit 份时逐页读取；某页读不到抛 `EdgarListingIncomplete`
    （带已读到的部分），调用方按不完整清单处理，不得当成「只有这几份」。
    """
    submissions = edgar_submissions(cik)
    filings_block = submissions.get("filings") or {}
    filings: List[Dict[str, Any]] = []
    _annual_filings_in(filings_block.get("recent") or {}, limit, filings)
    for page in filings_block.get("files") or []:
        if len(filings) >= limit:
            break
        name = str((page or {}).get("name") or "")
        if not name:
            continue
        try:
            columns = _edgar_get_json(f"https://data.sec.gov/submissions/{name}")
        except Exception as exc:  # noqa: BLE001 - 如实报不完整
            raise EdgarListingIncomplete(
                f"EDGAR 申报分页 {name} 获取失败: {str(exc)[:120]}", filings
            ) from exc
        _annual_filings_in(columns, limit, filings)
    return filings


def edgar_filing_url(cik: Any, accession: str, document: str) -> str:
    """filing 主文档的下载 URL（原始报告缓存按它识别文件）。"""
    accession_nodash = str(accession).replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"


def edgar_download_filing(cik: int, accession: str, document: str) -> str:
    """下载 filing 主文档（HTML 文本）。"""
    url = edgar_filing_url(cik, accession, document)
    raw = _download_guarded(
        url, _edgar_headers(), source="edgar", interval=_EDGAR_MIN_INTERVAL_SECONDS
    )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # 老申报是 cp1252/latin-1：按 cp1252 解（HTML 实体之外的弯引号/破折号都在这个码页里）
        return raw.decode("cp1252", errors="replace")


def edgar_same_sic_companies(sic: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    """同 SIC 码公司 CIK 清单（browse-edgar atom）：[{cik}]；失败上层降级。

    2026-08 实测该端点 atom 输出的公司名损坏（Perl 未解引用的
    "ARRAY(0x...)" 占位），名称不可用——只解析 <cik>，名称与 ticker 由
    调用方经 edgar_reverse_lookup（company_tickers 反查）补齐。
    """
    _throttle("edgar", _EDGAR_MIN_INTERVAL_SECONDS)
    response = requests.get(
        "https://www.sec.gov/cgi-bin/browse-edgar",
        params={
            "action": "getcompany",
            "SIC": sic,
            "type": "10-K",
            "owner": "include",
            "count": limit,
            "output": "atom",
        },
        headers=_edgar_headers(),
        timeout=30,
    )
    response.raise_for_status()
    import re as _re

    companies = []
    seen: set = set()
    for match in _re.finditer(r"<cik>0*(\d+)</cik>", response.text):
        cik = int(match.group(1))
        if cik in seen:
            continue
        seen.add(cik)
        companies.append({"cik": cik})
    return companies


# ---------------------------------------------------------------------------
# 港股年报全文（披露易 HKEXnews；两步：代码→stockId→年报清单）
# ---------------------------------------------------------------------------

_HKEX_BASE = "https://www1.hkexnews.hk"
_HKEX_MIN_INTERVAL_SECONDS = 1.0
_HKEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": f"{_HKEX_BASE}/search/titlesearch.xhtml?lang=zh",
}
# 文件类别：t1code=40000 财务报表/环境社会及管治资料；t2code 40100 年报、40200 中期报告
# （2026-09 实测：腾讯 12 份中期报告回到 2015）
_HKEX_ANNUAL_T1 = 40000
_HKEX_ANNUAL_T2 = 40100
_HKEX_T2_BY_REPORT_TYPE = {"annual": 40100, "interim": 40200}

_hkex_stock_id_cache: Dict[str, Optional[str]] = {}


def hkex_stock_id(symbol: str) -> Optional[str]:
    """港股代码 → 披露易内部 stockId（**不是**股票代码，检索必须先换）。"""
    code = str(symbol or "").strip()
    if code in _hkex_stock_id_cache:
        return _hkex_stock_id_cache[code]
    _throttle("hkexnews", _HKEX_MIN_INTERVAL_SECONDS)
    response = requests.get(
        f"{_HKEX_BASE}/search/prefix.do",
        params={"callback": "c", "lang": "ZH", "type": "A", "name": code, "market": "SEHK"},
        headers=_HKEX_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    text = response.text
    # JSONP 响应：c({...})
    try:
        payload = json.loads(text[text.index("(") + 1 : text.rindex(")")])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"披露易 prefix 响应无法解析: {text[:120]}") from exc
    info = payload.get("stockInfo") or []
    stock_id = str(info[0]["stockId"]) if info else None
    # 只缓存命中：新上市标的在披露易建档前查无结果，缓存 None 会让常驻进程永远查不到
    if stock_id:
        _hkex_stock_id_cache[code] = stock_id
    return stock_id


def hkex_reports(
    symbol: str, *, report_type: str = "annual", limit: int = 12
) -> List[Dict[str, Any]]:
    """披露易报告清单：report_type=annual（年报）| interim（中期报告），公告日倒序，
    返回 [{title, ann_date, url}]。只保留 PDF 直链；去噪声与同期取舍在 hk_report_catalog。"""
    t2code = _HKEX_T2_BY_REPORT_TYPE.get(report_type)
    if t2code is None:
        raise ValueError(f"未知的披露易报告类别: {report_type}")
    stock_id = hkex_stock_id(symbol)
    if not stock_id:
        logger.warning("披露易未找到港股 %s 的 stockId", symbol)
        return []
    _throttle("hkexnews", _HKEX_MIN_INTERVAL_SECONDS)
    response = requests.get(
        f"{_HKEX_BASE}/search/titleSearchServlet.do",
        params={
            "sortDir": 0,
            "sortByOptions": "DateTime",
            "category": 0,
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": -1,
            "fromDate": "20150101",
            "toDate": "20991231",
            "title": "",
            "searchType": 1,
            "t1code": _HKEX_ANNUAL_T1,
            "t2Gcode": -2,
            "t2code": t2code,
            "rowRange": max(limit * 2, 20),
            "lang": "ZH",
        },
        headers=_HKEX_HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    rows = (response.json() or {}).get("result") or []
    if isinstance(rows, str):  # 该端点有时把结果作为 JSON 字符串再包一层
        rows = json.loads(rows)
    reports = []
    for row in rows:
        link = row.get("FILE_LINK") or ""
        if not link.lower().endswith(".pdf"):
            continue
        reports.append(
            {
                "title": (row.get("TITLE") or "").strip(),
                "ann_date": (row.get("DATE_TIME") or "").strip(),
                "url": f"{_HKEX_BASE}{link}",
            }
        )
    return reports[:limit]


def hkex_title_search(
    symbol: str,
    *,
    t1code: int,
    t2g_code: int,
    t2code: int,
    from_date: str,
    to_date: str,
    row_range: int = 100,
) -> List[Dict[str, Any]]:
    """披露易按文件类别检索（公告日倒序），返回 [{title, ann_date, url}]（仅 PDF）。

    与 `hkex_reports` 同一端点、同一限速桶与 Referer，只是类别由调用方给出——
    例如现金股息公告表格 t1code=10000 / t2Gcode=3 / t2code=13251。
    from_date/to_date 为 YYYYMMDD。stockId 找不到时返回空列表。
    """
    stock_id = hkex_stock_id(symbol)
    if not stock_id:
        logger.warning("披露易未找到港股 %s 的 stockId", symbol)
        return []
    _throttle("hkexnews", _HKEX_MIN_INTERVAL_SECONDS)
    response = requests.get(
        f"{_HKEX_BASE}/search/titleSearchServlet.do",
        params={
            "sortDir": 0,
            "sortByOptions": "DateTime",
            "category": 0,
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": -1,
            "fromDate": from_date,
            "toDate": to_date,
            "title": "",
            "searchType": 1,
            "t1code": t1code,
            "t2Gcode": t2g_code,
            "t2code": t2code,
            "rowRange": row_range,
            "lang": "ZH",
        },
        headers=_HKEX_HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    rows = (response.json() or {}).get("result") or []
    if isinstance(rows, str):
        rows = json.loads(rows)
    documents = []
    for row in rows:
        link = row.get("FILE_LINK") or ""
        if not link.lower().endswith(".pdf"):
            continue
        documents.append(
            {
                "title": (row.get("TITLE") or "").strip(),
                "ann_date": (row.get("DATE_TIME") or "").strip(),
                "url": f"{_HKEX_BASE}{link}",
            }
        )
    return documents


def hkex_announcements_raw(
    symbol: str, *, from_date: str, to_date: str, row_range: int = 500
) -> Optional[List[Dict[str, Any]]]:
    """披露易**全部类别**公告原始行（searchType=0、各级类别 -2），公告时间倒序。

    行字段：NEWS_ID（唯一）、DATE_TIME（dd/mm/YYYY HH:MM 香港时间）、TITLE、LONG_TEXT
    （官方分类「一级 - [二级 / 二级]」，HTML 实体未解码）、FILE_LINK、FILE_TYPE。
    from/to 为 YYYYMMDD；stockId 找不到返回 None（与「该区间无公告」的空列表区分）。
    rowRange 上限由调用方按窗口控制（公告公司一年可达数百条，调用方分段请求）。"""
    stock_id = hkex_stock_id(symbol)
    if not stock_id:
        return None
    _throttle("hkexnews", _HKEX_MIN_INTERVAL_SECONDS)
    response = requests.get(
        f"{_HKEX_BASE}/search/titleSearchServlet.do",
        params={
            "sortDir": 0,
            "sortByOptions": "DateTime",
            "category": 0,
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": -1,
            "fromDate": from_date,
            "toDate": to_date,
            "title": "",
            "searchType": 0,
            "t1code": -2,
            "t2Gcode": -2,
            "t2code": -2,
            "rowRange": row_range,
            "lang": "ZH",
        },
        headers=_HKEX_HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    rows = (response.json() or {}).get("result") or []
    if isinstance(rows, str):
        rows = json.loads(rows)
    return list(rows)


# ---------------------------------------------------------------------------
# 港股（Yahoo fundamentals-timeseries，免 crumb；非官方端点，失败上层降级）
# ---------------------------------------------------------------------------

_YAHOO_MIN_INTERVAL_SECONDS = 1.0
# 2026-08-03 实测：完整 Chrome UA 会被 Yahoo 429，精简 UA 正常——勿改回
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Yahoo 年度序列 → 内部科目名（与 EDGAR 透视行对齐，便于共用指标层）。
#
# 科目名刻意与 `pivot_rows_to_statements` 期待的键一致（cost_of_revenue /
# accounts_receiv / inventories / total_cur_assets / fix_assets /
# depr_fa_coga_dpba / sga_exp），因此扩这张表就直接让港股的毛利率、应收与存货
# 增速差、Beneish M-score 全部可算——此前算不出**不是数据源没有**，是当初只要
# 了 10 个字段。2026-08-04 实测这 26 个科目对 00700 与 02156 全部返回数据，
# 单次请求 URL 751 字符（无需分批）。
YAHOO_HK_FIELD_MAP: Dict[str, str] = {
    # 利润表
    "annualTotalRevenue": "total_revenue",
    "annualCostOfRevenue": "cost_of_revenue",
    "annualGrossProfit": "gross_profit",
    "annualOperatingIncome": "operating_income",
    "annualNetIncome": "n_income_attr_p",
    "annualPretaxIncome": "total_profit",
    "annualTaxProvision": "income_tax",
    "annualEBITDA": "ebitda",
    "annualSellingGeneralAndAdministration": "sga_exp",
    "annualInterestExpense": "int_exp",
    "annualBasicEPS": "basic_eps",
    "annualDilutedEPS": "diluted_eps",
    # 资产负债表
    "annualTotalAssets": "total_assets",
    "annualCurrentAssets": "total_cur_assets",
    "annualCurrentLiabilities": "total_cur_liab",
    "annualAccountsReceivable": "accounts_receiv",
    "annualInventory": "inventories",
    "annualNetPPE": "fix_assets",
    "annualCashAndCashEquivalents": "money_cap",
    "annualTotalLiabilitiesNetMinorityInterest": "total_liab",
    "annualStockholdersEquity": "total_hldr_eqy_exc_min_int",
    "annualTotalDebt": "total_debt",
    # 现金流量表
    "annualOperatingCashFlow": "n_cashflow_act",
    "annualFreeCashFlow": "free_cashflow",
    "annualCapitalExpenditure": "capex",
    "annualDepreciationAndAmortization": "depr_fa_coga_dpba",
    # 已付普通股股息（Yahoo 为负数现金流出，落库取量级，与 PDF 映射的 div_paid_owners 同口径；
    # 2026-09 实测 00700/00883/02313 返回近 4 年，与 annualCashDividendsPaid 相同）
    "annualCommonStockDividendPaid": "div_paid_owners",
}
# 落库取绝对值的科目（量级口径）
YAHOO_HK_MAGNITUDE_FIELDS = frozenset({"div_paid_owners"})


def to_yahoo_hk_code(symbol: str) -> str:
    """港股代码 → Yahoo 格式：'00700'→'0700.HK'、'09988'→'9988.HK'（四位补零）。"""
    digits = str(symbol or "").strip()
    if not digits.isdigit() or not int(digits):
        raise ValueError(f"非法港股代码: {symbol!r}")
    return f"{int(digits):04d}.HK"


def yahoo_hk_fundamentals(symbol: str) -> List[Dict[str, Any]]:
    """港股年度核心科目：一次 GET 全部序列，按 asOfDate 合并每年一行。

    行结构与 EDGAR 透视行对齐（end_date 8 位 + fp=FY 标记 + currency——
    港股公司报告币种不一（腾讯 CNY、汇丰 USD），必须透传给 LLM）。
    2026-08-03 实测仅返回近 4-5 年——年限边界由 prompt 明示，不在此层补。
    """
    code = to_yahoo_hk_code(symbol)
    _throttle("yahoo", _YAHOO_MIN_INTERVAL_SECONDS)
    now = int(time.time())
    response = requests.get(
        f"https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{code}",
        params={
            "type": ",".join(YAHOO_HK_FIELD_MAP),
            "period1": now - 20 * 365 * 86400,
            "period2": now,
        },
        headers=_YAHOO_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    results = ((response.json().get("timeseries") or {}).get("result")) or []

    merged: Dict[str, Dict[str, Any]] = {}
    for series in results:
        meta_types = (series.get("meta") or {}).get("type") or []
        field = YAHOO_HK_FIELD_MAP.get(meta_types[0]) if meta_types else None
        if not field:
            continue
        for item in series.get(meta_types[0]) or []:
            if not item:
                continue  # 序列缺年份时 Yahoo 以 null 占位
            as_of = str(item.get("asOfDate") or "")
            value = (item.get("reportedValue") or {}).get("raw")
            if not as_of or value is None:
                continue
            row = merged.setdefault(
                as_of,
                {
                    "end_date": as_of.replace("-", ""),
                    "fp": "FY",
                    "currency": item.get("currencyCode"),
                },
            )
            row[field] = abs(value) if field in YAHOO_HK_MAGNITUDE_FIELDS else value
    return sorted(merged.values(), key=lambda r: r["end_date"], reverse=True)
