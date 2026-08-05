"""财报原文/基本面获取的纯 HTTP 层（cninfo 巨潮 + SEC EDGAR + Yahoo 港股）。

不碰 DB、不做文本解析——只负责检索/下载/限速/缓存映射。所有函数可被
测试整体 monkeypatch。数据源为公开免费接口（2026-08-03 实测连通），带
UA 与保守限速；被封禁时上层落 failed 行并降级，不重试轰炸。

EDGAR 合规要求：UA 须携带联系方式（settings.edgar_user_agent），限速
≤10 req/s（本层 0.15s 间隔）。
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from ..core.logging import get_app_logger

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
_rate_lock = threading.Lock()
_last_request_at: Dict[str, float] = {}

PDF_DOWNLOAD_TIMEOUT_SECONDS = 60
PDF_MAX_BYTES = 50 * 1024 * 1024

# A股报告 category → 报告类型标记
CNINFO_CATEGORIES = {
    "annual": "category_ndbg_szsh",
    "semi": "category_bndbg_szsh",
}

# orgId 全量映射（code → orgId），进程内缓存
_org_id_cache: Dict[str, str] = {}
_org_id_cache_loaded = False


def _throttle(source: str, min_interval: float) -> None:
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_at.get(source, 0.0)
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _last_request_at[source] = time.monotonic()


def _load_org_id_map() -> Dict[str, str]:
    global _org_id_cache_loaded
    if _org_id_cache_loaded:
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
    logger.info("cninfo orgId 映射加载完成：%d 条", len(_org_id_cache))
    return _org_id_cache


def cninfo_org_id(symbol: str) -> Optional[str]:
    """code → orgId；映射表加载失败时回退沪市惯例 gssh0{code}（实测有效）。"""
    try:
        org_id = _load_org_id_map().get(symbol)
        if org_id:
            return org_id
    except Exception as exc:
        logger.warning("cninfo orgId 映射加载失败，使用回退规则: %s", str(exc)[:120])
    if symbol.startswith(("6", "9")):
        return f"gssh0{symbol}"
    return None


def cninfo_search_reports(
    symbol: str, *, report_type: str, se_date: str
) -> List[Dict[str, Any]]:
    """检索年报/半年报公告列表（含修订版与摘要，由调用方过滤）。

    返回 [{title, ann_date(YYYY-MM-DD), url, adjunct_size_kb}]，按公告时间倒序。
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
            ann_date = (
                time.strftime("%Y-%m-%d", time.gmtime(ann_ts / 1000)) if ann_ts else ""
            )
            announcements.append({
                "title": str(row.get("announcementTitle") or ""),
                "ann_date": ann_date,
                "url": f"{_CNINFO_STATIC}/{adjunct}",
                "adjunct_size_kb": row.get("adjunctSize"),
            })
        if not body.get("hasMore") and len(rows) < 30:
            break
        page += 1
    return announcements


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
    _throttle(source, interval)
    response = requests.get(
        url,
        headers=headers,
        timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS,
        stream=True,
    )
    response.raise_for_status()
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=1 << 20):
        total += len(chunk)
        if total > PDF_MAX_BYTES:
            response.close()
            raise ValueError(f"PDF 超过大小上限 {PDF_MAX_BYTES // (1 << 20)}MB")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# SEC EDGAR（美股：官方免费；UA 须带联系方式，限速 ≤10 req/s）
# ---------------------------------------------------------------------------

_EDGAR_MIN_INTERVAL_SECONDS = 0.15

# symbol → {cik, title} 映射，进程内缓存
_cik_cache: Dict[str, Dict[str, Any]] = {}
_cik_cache_loaded = False


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
    global _cik_cache_loaded
    if _cik_cache_loaded:
        return _cik_cache
    data = _edgar_get_json("https://www.sec.gov/files/company_tickers.json")
    for entry in data.values():
        ticker = str(entry.get("ticker") or "").upper()
        if ticker:
            _cik_cache[ticker] = {
                "cik": int(entry.get("cik_str") or 0),
                "title": entry.get("title"),
            }
    _cik_cache_loaded = True
    logger.info("EDGAR CIK 映射加载完成：%d 条", len(_cik_cache))
    return _cik_cache


def edgar_lookup(symbol: str) -> Optional[Dict[str, Any]]:
    """美股 symbol → {cik, title}；未注册返回 None。"""
    return _load_cik_map().get(str(symbol or "").strip().upper())


_cik_reverse_cache: Dict[int, Dict[str, Any]] = {}


def edgar_reverse_lookup(cik: int) -> Optional[Dict[str, Any]]:
    """CIK → {symbol, title}；非上市 filer（无 ticker）返回 None。

    一 CIK 多 ticker（GOOG/GOOGL 股别）取 company_tickers 首见者（主类）。
    """
    if not _cik_reverse_cache:
        for ticker, entry in _load_cik_map().items():
            _cik_reverse_cache.setdefault(
                entry["cik"], {"symbol": ticker, "title": entry["title"]}
            )
    return _cik_reverse_cache.get(int(cik))


def edgar_companyfacts(cik: int) -> Dict[str, Any]:
    return _edgar_get_json(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    )


def edgar_submissions(cik: int) -> Dict[str, Any]:
    return _edgar_get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")


# 年报表单：10-K 是美国本土发行人，20-F 是外国私人发行人（中概股几乎全是
# 20-F）。只认 10-K 会让 PDD/BABA 这类标的检索到 0 份年报——财报摘要与商业
# 画像整块空白，而表面上"没有报错"。
EDGAR_ANNUAL_FORMS = ("10-K", "20-F")


def edgar_recent_annual_filings(cik: int, *, limit: int = 10) -> List[Dict[str, Any]]:
    """近 N 份年报：[{form, accession, primary_document, filing_date, report_date}]。"""
    submissions = edgar_submissions(cik)
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filings = []
    for index, form in enumerate(forms):
        if form not in EDGAR_ANNUAL_FORMS:
            continue
        filings.append({
            "form": str(form),
            "accession": str(recent["accessionNumber"][index]),
            "primary_document": str(recent["primaryDocument"][index]),
            "filing_date": str(recent["filingDate"][index]),
            "report_date": str(recent["reportDate"][index]),
        })
        if len(filings) >= limit:
            break
    return filings


def edgar_download_filing(cik: int, accession: str, document: str) -> str:
    """下载 filing 主文档（HTML 文本）。"""
    accession_nodash = accession.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"
    )
    _throttle("edgar", _EDGAR_MIN_INTERVAL_SECONDS)
    response = requests.get(url, headers=_edgar_headers(), timeout=60)
    response.raise_for_status()
    if len(response.content) > PDF_MAX_BYTES:
        raise ValueError("10-K 文档超过大小上限")
    return response.text


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
            "action": "getcompany", "SIC": sic, "type": "10-K",
            "owner": "include", "count": limit, "output": "atom",
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
# 文件类别：t1code=40000 财务报表/环境社会及管治资料，t2code=40100 年报
_HKEX_ANNUAL_T1 = 40000
_HKEX_ANNUAL_T2 = 40100

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
        payload = json.loads(text[text.index("(") + 1: text.rindex(")")])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"披露易 prefix 响应无法解析: {text[:120]}") from exc
    info = payload.get("stockInfo") or []
    stock_id = str(info[0]["stockId"]) if info else None
    _hkex_stock_id_cache[code] = stock_id
    return stock_id


def hkex_annual_reports(symbol: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    """披露易年报清单（公告日倒序），返回 [{title, ann_date, url}]。

    只保留 PDF 直链；标题含"摘要/補充/英文"等的由上层判重时处理。
    2026-08-04 实测：腾讯 12 份、小盘股 6-11 份，PDF 3-13MB 且**纯文本零空白
    页**（比 A股 招行那份 32MB 轻得多）。
    """
    stock_id = hkex_stock_id(symbol)
    if not stock_id:
        logger.warning("披露易未找到港股 %s 的 stockId", symbol)
        return []
    _throttle("hkexnews", _HKEX_MIN_INTERVAL_SECONDS)
    response = requests.get(
        f"{_HKEX_BASE}/search/titleSearchServlet.do",
        params={
            "sortDir": 0, "sortByOptions": "DateTime", "category": 0, "market": "SEHK",
            "stockId": stock_id, "documentType": -1,
            "fromDate": "20150101", "toDate": "20991231", "title": "",
            "searchType": 1, "t1code": _HKEX_ANNUAL_T1, "t2Gcode": -2,
            "t2code": _HKEX_ANNUAL_T2, "rowRange": max(limit * 2, 20), "lang": "ZH",
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
        reports.append({
            "title": (row.get("TITLE") or "").strip(),
            "ann_date": (row.get("DATE_TIME") or "").strip(),
            "url": f"{_HKEX_BASE}{link}",
        })
    return reports[:limit]


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
}


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
        "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/"
        f"finance/timeseries/{code}",
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
            row = merged.setdefault(as_of, {
                "end_date": as_of.replace("-", ""),
                "fp": "FY",
                "currency": item.get("currencyCode"),
            })
            row[field] = value
    return sorted(merged.values(), key=lambda r: r["end_date"], reverse=True)
