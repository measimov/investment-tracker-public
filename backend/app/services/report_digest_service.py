"""财报摘要编排：规划目标（十年年报+最新中报）→ 下载/抽节/摘要 → 永久缓存。

分层缓存（均落 security_profile_data 通用行）：
- report_section：原文节选（下载+抽取的缓存，重摘要免重下载）
- report_digest：LLM 摘要（map 层产物，永久复用）

失败语义：下载/抽取/LLM 确定性失败均落 failed 行并计 attempts，attempts≥2
自动路径永久跳过（可观测可人工清除）；任何失败不阻断主分析——分析输入
携带 report_digest_gaps 让模型如实注明缺口。
"""

import io
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import pdfplumber
import requests
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .llm_client import LLMClientError, LLMNotConfiguredError, chat_completion
from .report_digest_prompts import (
    DEFAULT_TIER as DEFAULT_DIGEST_TIER,
    DIGEST_PROMPT_VERSION,
    assign_digest_tiers,
    build_digest_messages,
    parse_digest_output,
    tier_spec,
)
from .report_fetchers import cninfo_search_reports, download_report_pdf
from .report_sections import (
    SECTION_EXTRACTOR_VERSION,
    budget_section,
    extract_cn_sections,
    pages_to_text,
    share_budget,
)
from .security_profile_service import upsert_profile_row

logger = get_app_logger(__name__)

# 覆盖深度（owner 拍板：至少十年）：十年年报 + 最新一份半年报（A股）；
# 美股为近十份 10-K
ANNUAL_YEARS = 10
MAX_ATTEMPTS = 2

# 空清单（源站故障或该标的确实无年报）的短 TTL：不遮住新披露、也不轰炸源站
REPORT_TARGET_PLAN_EMPTY_TTL_HOURS = 1

# 支持财报摘要的市场（回填 job 与 API 校验共用）
REPORT_MARKETS = ("A股", "美股", "港股")


def _pack_for_digest(
    sections: Dict[str, str], *, tier: str
) -> tuple[Dict[str, str], Dict[str, Any]]:
    """按档裁剪 digest 输入，并返回**可落库**的裁剪元数据。

    预算控制只在这里发生一次。旧实现是"头 24k + 尾 6k"，配上抽取期的 50k 硬切
    就是双重截断：拿到的"尾部"其实是 50k 切点前的内容，而风险要点/展望恰恰依赖
    章节真尾。`budget_section` 按小节权重装箱、保证真尾，并如实报告省了什么。

    元数据必须落库：此前裁剪完全不可观测，线上看到的 20% 截断率低估了真实的
    30%——而"被截掉"和"公司没披露"在摘要里长得一模一样。

    档位预算是**整份报告**的上限，由 `share_budget` 在各章节间分配：每节各发
    一份完整预算的话，A 档三节就是 120k 而非声明的 40k，分档省下的 token 也
    就无从谈起。
    """
    spec = tier_spec(tier)
    wanted = [name for name in spec["sections"] if sections.get(name)]
    # business 缺失时用登记信息页兜底；prompt 侧的标签已写明它不是业务概要
    if "business" in spec["sections"] and "business" not in wanted:
        if sections.get("company_profile"):
            wanted.append("company_profile")
    quotas = share_budget({name: len(sections[name]) for name in wanted}, spec["budget"])

    packed: Dict[str, str] = {}
    meta: Dict[str, Any] = {}
    for name, budget in quotas.items():
        text, budget_meta = budget_section(sections[name], budget=budget)
        packed[name] = text
        meta[name] = {
            "original_chars": budget_meta.original_chars,
            "kept_chars": budget_meta.kept_chars,
            "strategy": budget_meta.strategy,
            "omitted_chars": budget_meta.omitted_chars,
            "dropped_subsections": list(budget_meta.dropped_subsections),
            "sliced_subsections": list(budget_meta.sliced_subsections),
        }
    return packed, meta


def _load_row(
    db: Session, symbol: str, market: str, dataset: str, period_key: str
) -> Optional[SecurityProfileData]:
    return (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == dataset,
            SecurityProfileData.period_key == period_key,
        )
        .first()
    )


def _upsert(db: Session, symbol: str, market: str, dataset: str,
            period_key: str, payload: Dict[str, Any]) -> None:
    upsert_profile_row(db, symbol, market, dataset, period_key, payload)


def plan_report_targets(symbol: str, market: str) -> List[Dict[str, Any]]:
    """规划待摘要报告（兼容旧调用方：只返回目标列表）。

    完整性状态见 plan_report_targets_detailed —— 缓存层必须用后者，否则会把
    部分成功的清单当成完整结果长期缓存。
    """
    return plan_report_targets_detailed(symbol, market)["targets"]


def plan_report_targets_detailed(symbol: str, market: str) -> Dict[str, Any]:
    """规划待摘要报告：近十年年报 + 最新半年报，并报告**完整性**。

    返回 {targets, complete, failed_kinds}。complete=False 表示某一类检索失败
    （例如 annual 请求失败而 semi 成功）——此时 targets 非空但缺了十年年报，
    调用方不得把它当作完整清单缓存，否则整轮批量分析会静默缺失年报。

    cninfo 检索（同报告期多版本取公告日最新=修订版胜；过滤摘要/英文版/已取消）。
    测试 monkeypatch 本函数或 plan_report_targets。
    """
    if market == "美股":
        return _plan_us_targets_detailed(symbol)
    if market == "港股":
        return _plan_hk_targets_detailed(symbol)
    if market != "A股":
        return {"targets": [], "complete": True, "failed_kinds": []}
    today = date.today()
    se_date = f"{today.year - ANNUAL_YEARS - 1}-01-01~{today.isoformat()}"

    targets: List[Dict[str, Any]] = []
    failed_kinds: List[str] = []
    for report_type, keep in (("annual", ANNUAL_YEARS), ("semi", 1)):
        try:
            announcements = cninfo_search_reports(
                symbol, report_type=report_type, se_date=se_date
            )
        except Exception as exc:
            logger.warning("cninfo 检索 %s %s 失败: %s", symbol, report_type, str(exc)[:150])
            failed_kinds.append(report_type)
            continue
        by_period: Dict[str, Dict[str, Any]] = {}
        for row in announcements:
            title = row["title"]
            if any(word in title for word in ("摘要", "英文", "已取消", "补充公告")):
                continue
            end_date = _infer_report_end_date(title, row["ann_date"], report_type)
            if not end_date:
                continue
            existing = by_period.get(end_date)
            if existing is None or row["ann_date"] > existing["ann_date"]:
                by_period[end_date] = row  # 修订版（公告日更新）胜
        for end_date in sorted(by_period, reverse=True)[:keep]:
            row = by_period[end_date]
            targets.append({
                "period_key": f"{end_date}|{report_type}",
                "report_type": report_type,
                "end_date": end_date,
                "title": row["title"],
                "ann_date": row["ann_date"],
                "url": row["url"],
            })
    targets.sort(key=lambda item: item["end_date"], reverse=True)
    return {
        "targets": targets,
        "complete": not failed_kinds,
        "failed_kinds": failed_kinds,
    }


def _plan_us_targets_detailed(symbol: str) -> Dict[str, Any]:
    """美股：EDGAR submissions 近十份年报（10-K 或 20-F；同样报告完整性）。"""
    from .report_fetchers import edgar_lookup, edgar_recent_annual_filings

    lookup = edgar_lookup(symbol)
    if not lookup:
        # 代码不在 SEC 注册表：这是确定的答案，不是失败
        logger.warning("美股 %s 未在 SEC 注册表中找到，跳过报告规划", symbol)
        return {"targets": [], "complete": True, "failed_kinds": []}
    targets = []
    try:
        filings = edgar_recent_annual_filings(lookup["cik"], limit=ANNUAL_YEARS)
    except Exception as exc:
        logger.warning("EDGAR 年报清单获取失败 %s: %s", symbol, str(exc)[:150])
        return {"targets": [], "complete": False, "failed_kinds": ["annual"]}
    for filing in filings:
        report_date = filing["report_date"].replace("-", "")
        if not report_date:
            continue
        form = filing.get("form") or "10-K"
        # period_key 带表单类型：同一家公司从 10-K 换成 20-F（或反之）时，
        # 两份报告的抽取规则不同，不能共用一个缓存键
        targets.append({
            "period_key": f"{report_date}|{form}",
            "report_type": form,
            "end_date": report_date,
            "title": f"{form} ({filing['report_date']})",
            "ann_date": filing["filing_date"],
            "url": {
                "cik": lookup["cik"],
                "accession": filing["accession"],
                "document": filing["primary_document"],
            },
        })
    targets.sort(key=lambda item: item["end_date"], reverse=True)
    return {"targets": targets, "complete": True, "failed_kinds": []}


def _plan_hk_targets_detailed(symbol: str) -> Dict[str, Any]:
    """港股：披露易近十年年报（无中报——半年报在披露易另属一类，暂不纳入）。"""
    from .report_fetchers import hkex_annual_reports

    try:
        reports = hkex_annual_reports(symbol, limit=ANNUAL_YEARS + 2)
    except Exception as exc:
        logger.warning("披露易年报清单获取失败 %s: %s", symbol, str(exc)[:150])
        return {"targets": [], "complete": False, "failed_kinds": ["annual"]}

    by_period: Dict[str, Dict[str, Any]] = {}
    for row in reports:
        title = row["title"]
        if any(word in title for word in ("摘要", "補充", "补充", "更正", "英文")):
            continue
        end_date = _infer_hk_fiscal_end(title, row["ann_date"])
        if not end_date:
            continue
        existing = by_period.get(end_date)
        # 同一财年出现多份（修订/重刊）取公告日更新的。**必须先解析成日期**：
        # 披露易的格式是 DD/MM/YYYY，按字符串比会认为 "30/03/2025" 比
        # "02/04/2025" 新，于是缓存住原件、重刊永远进不来（源指纹也不会变）
        if existing is None or _hkex_sort_key(row["ann_date"]) > _hkex_sort_key(
            existing["ann_date"]
        ):
            by_period[end_date] = row
    targets = [
        {
            "period_key": f"{end_date}|annual",
            "report_type": "annual",
            "end_date": end_date,
            "title": by_period[end_date]["title"],
            "ann_date": by_period[end_date]["ann_date"],
            "url": by_period[end_date]["url"],
        }
        for end_date in sorted(by_period, reverse=True)[:ANNUAL_YEARS]
    ]
    return {"targets": targets, "complete": True, "failed_kinds": []}


# 常见财年结束月（港股财年不统一：多数 12/31，也有 3/31、6/30）
_HK_FISCAL_MONTHS = (12, 9, 6, 3)
_MONTH_END_DAY = {3: "31", 6: "30", 9: "30", 12: "31"}


def _infer_hk_fiscal_end(title: str, ann_date: str) -> Optional[str]:
    """港股年报标题年份 + 公告日 → 财年结束日（YYYYMMDD）。

    不能一律按 12/31：港股财年不统一（阿里 3/31），而结构化数据来自 Yahoo 的
    真实 asOfDate——两边对不上，LLM 拿到的就是同一年两个口径。

    判据是"年报须在财年结束后数月内刊发"：在标题年份的四个季末里，取公告日
    之前 2-6 个月那一个。落不进这个窗口就退回 12/31。
    """

    year = _extract_report_year(title)
    if year is None:
        return None
    ann = _parse_hkex_datetime(ann_date)
    if ann is None:
        return f"{year}1231"
    for month in _HK_FISCAL_MONTHS:
        months_before = (ann.year - year) * 12 + (ann.month - month)
        if 2 <= months_before <= 6:
            return f"{year}{month:02d}{_MONTH_END_DAY[month]}"
    return f"{year}1231"


def _hkex_sort_key(value: str) -> date:
    """公告日排序键；无法解析时给 date.min（确定性兜底：坏值永不胜出）。"""
    return _parse_hkex_datetime(value) or date.min


_CN_DIGITS = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
              "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _extract_report_year(title: str) -> Optional[int]:
    """标题里的报告年份，**中文数字年份必须认**。

    港股大量公司的年报标题是「二零二五年年報」（实测中海油 00883、绿城 03900
    连续多年全是这种写法）。只认阿拉伯数字年份会把它们的每一份年报都静默丢弃，
    清单变成 complete-empty——批量层把"检索正常但一份都没识别出来"当成
    "该标的无年报"记成功，整个标的无声消失。
    """
    import re

    match = re.search(r"(20\d{2})", title)
    if match:
        return int(match.group(1))
    cn_match = re.search(r"[零〇一二三四五六七八九]{4}", title)
    if cn_match:
        digits = "".join(_CN_DIGITS[ch] for ch in cn_match.group(0))
        if digits.startswith("20"):
            return int(digits)
    return None


def _parse_hkex_datetime(value: str) -> Optional[date]:
    """披露易公告时间 'DD/MM/YYYY HH:MM' → date。"""
    head = str(value or "").split(" ")[0]
    try:
        day, month, year = (int(part) for part in head.split("/"))
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


def _infer_report_end_date(title: str, ann_date: str, report_type: str) -> Optional[str]:
    """从标题推报告期（"2025年年度报告"→20251231）；标题无年份用公告年-1 兜底。"""
    import re

    match = re.search(r"(20\d{2})\s*年", title)
    if match:
        year = int(match.group(1))
    elif ann_date:
        year = int(ann_date[:4]) - (1 if report_type == "annual" else 0)
    else:
        return None
    return f"{year}1231" if report_type == "annual" else f"{year}0630"


def source_fingerprint(target: Dict[str, Any]) -> str:
    """报告源版本指纹（URL + 公告日）。

    缓存键只有 period_key，但同一报告期会出现修订版（新 URL / 新公告日）。
    没有指纹时旧版一旦 ok 就永久遮住修订版，节选与摘要都不再刷新。
    """
    url = target.get("url")
    url_text = json.dumps(url, sort_keys=True, ensure_ascii=False) if isinstance(url, dict) else str(url or "")
    return f"{url_text}|{target.get('ann_date') or ''}"


def _is_transient(exc: Exception) -> bool:
    """网络抖动/上游 5xx = 可恢复，不消耗永久重试额度；PDF 损坏、章节
    定位失败等确定性错误才计 attempts（与 LLM 侧失败语义保持一致）。"""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return status is None or status >= 500 or status == 429
    if isinstance(exc, LLMClientError):
        # LLM 走 httpx 而非 requests，上面两条一条都拦不住：不认的话一次
        # DeepSeek 5xx 就烧掉一次永久额度，两次之后该报告期永久跳过
        status = exc.status_code
        return status is None or status >= 500 or status == 429
    return False


_TIER_RICHNESS = {"C": 0, "B": 1, "A": 2}


def digest_versions_current(payload: Dict[str, Any]) -> bool:
    """该摘要行是否由当前版本的抽取器与 prompt 生成（缺字段 = 版本 1 的历史行）。

    读取路径（分析输入、进度）必须一起用它过滤：只看 `status == "ok"` 的话，
    一次回填最多重跑 4 份，其余旧版本的错误摘要仍是 ok，分析会把新旧混在一起，
    进度还会虚报已完成——版本号是为了让错误数据**消失**，不是只挡住重跑。
    """
    return (
        int(payload.get("extractor_version") or 1) == SECTION_EXTRACTOR_VERSION
        and int(payload.get("prompt_version") or 1) == DIGEST_PROMPT_VERSION
    )


def _digest_is_current(payload: Dict[str, Any], fingerprint: str, tier: str) -> bool:
    """缓存命中判据：源指纹 + 双版本 + 档位**不低于**当前所需。

    档位只在"缓存的比现在需要的更薄"时才重跑。报告变旧会从 A 降到 B 再降到
    C——那是降级，已有的 A 档摘要本就比 C 档更全，花钱重跑一次去换更少的字段
    是纯亏。C 档省的是**尚未生成**那些年份的钱，不是把已有的削一遍。
    """
    if payload.get("source_fingerprint") != fingerprint:
        return False
    if not digest_versions_current(payload):
        return False
    cached_tier = str(payload.get("digest_tier") or DEFAULT_DIGEST_TIER)
    return _TIER_RICHNESS.get(cached_tier, 1) >= _TIER_RICHNESS.get(tier, 1)


def cached_report_targets(
    db: Session, symbol: str, market: str, *, force_refresh: bool = False
) -> List[Dict[str, Any]]:
    """兼容薄层：只要目标列表。完整性语义见 cached_report_targets_detailed。"""
    return cached_report_targets_detailed(
        db, symbol, market, force_refresh=force_refresh
    )["targets"]


def cached_report_targets_detailed(
    db: Session, symbol: str, market: str, *, force_refresh: bool = False
) -> Dict[str, Any]:
    """年报清单缓存包装（dataset=report_target_plan, period_key=current）。

    返回 {targets, complete}。**complete 必须一路传播**："清单确实为空"与
    "清单检索失败"只看 targets 是分不开的——源站整体故障时两者都是空列表，
    把后者当成"该标的无年报"会让批量把全部持仓记成功、绿色完成、生成 0 份。

    `plan_report_targets` 每次都要打 2-10 次 cninfo（每次 1s 全局限速），而年报
    清单一整年不变——批量分析里这是纯浪费。缓存落库而非进程内：部署后"第一次
    点一键分析"恰恰是主场景，进程缓存那时必然是空的。

    空结果与**部分成功**都只缓存 1 小时：
    - 空结果：不让长 TTL 遮住新披露，也不让源站故障期被反复轰炸
    - 部分成功（例如 annual 检索失败、semi 成功）：targets 非空但缺了十年年报，
      按 24h 缓存会让整轮批量分析静默缺年报，随后 24h 新鲜度又跳过这份缺数据的
      分析——必须短 TTL 尽快重试
    """
    row = _load_row(db, symbol, market, "report_target_plan", "current")
    payload = row.payload if row else None
    if payload and not force_refresh:
        ttl_hours = (
            settings.report_target_plan_ttl_hours
            if payload.get("status") == "ok"
            else REPORT_TARGET_PLAN_EMPTY_TTL_HOURS  # empty / partial 都走短 TTL
        )
        planned_at = payload.get("planned_at")
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(planned_at)
            if age.total_seconds() < ttl_hours * 3600:
                return {
                    "targets": list(payload.get("targets") or []),
                    # 缓存的 partial 行同样是不完整清单（短 TTL 只保证尽快重试，
                    # 不改变它当下的性质）
                    "complete": payload.get("status") != "partial",
                }
        except (TypeError, ValueError):
            pass  # 时间戳损坏：按未命中处理，重新检索

    planned = plan_report_targets_detailed(symbol, market)
    targets = planned["targets"]
    if not planned["complete"]:
        status = "partial"
    elif targets:
        status = "ok"
    else:
        status = "empty"
    try:
        _upsert(db, symbol, market, "report_target_plan", "current", {
            "status": status,
            "failed_kinds": planned["failed_kinds"],
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "targets": targets,
        })
        db.commit()
    except Exception as exc:  # 缓存写失败不影响本次结果
        db.rollback()
        logger.warning("年报清单缓存写入失败 %s/%s: %s", symbol, market, str(exc)[:150])
    return {"targets": targets, "complete": bool(planned["complete"])}


def _ensure_section(
    db: Session, symbol: str, market: str, target: Dict[str, Any]
) -> Optional[Dict[str, str]]:
    """确保原文节选缓存存在；返回 sections dict 或 None（失败/封顶）。

    命中条件是**两个**版本维度同时匹配：源指纹（报告本身有没有出修订版）与
    抽取器版本（我们的定位逻辑有没有改）。只看源指纹的话，修好抽取缺陷后库里
    那批抽错章节的节选会永久命中，修复对存量数据完全无效。
    """
    period_key = target["period_key"]
    fingerprint = source_fingerprint(target)
    row = _load_row(db, symbol, market, "report_section", period_key)
    attempts = 0
    if row:
        payload = row.payload or {}
        fresh = (
            payload.get("source_fingerprint") == fingerprint
            # 缺字段 = 版本 1 的历史行，bump 后一律重抽
            and int(payload.get("extractor_version") or 1) == SECTION_EXTRACTOR_VERSION
        )
        if fresh:
            if payload.get("extract_status") == "ok":
                return payload.get("sections") or None
            if int(payload.get("attempts") or 0) >= MAX_ATTEMPTS:
                return None
            attempts = int(payload.get("attempts") or 0)
        # 指纹或抽取器版本变化 → attempts 归零重新抽取

    try:
        if market == "美股":
            from .report_fetchers import edgar_download_filing
            from .report_sections import extract_us_items

            ref = target["url"]
            html = edgar_download_filing(ref["cik"], ref["accession"], ref["document"])
            # 表单类型决定 Item 编号映射：20-F 的业务在 Item 4、MD&A 在 Item 5
            extracted = extract_us_items(
                html, form_type=str(target.get("report_type") or "10-K")
            )
            fetched_bytes = len(html.encode("utf-8", errors="ignore"))
        else:
            # 港股走披露易（不同站点=不同限速桶与 Referer），A股走巨潮
            pdf_bytes = download_report_pdf(
                target["url"], source="hkexnews" if market == "港股" else "cninfo"
            )
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = pages_to_text([page.extract_text() or "" for page in pdf.pages])
            extracted = extract_cn_sections(text)
            fetched_bytes = len(pdf_bytes)
        sections = {
            name: result.text
            for name, result in extracted.items()
            # unbounded_item = 终止标题没匹配到、这一段一路吃到文末，内容跨了
            # 好几章。当成成功节选去摘要，比缺这一节更糟
            if result and "unbounded_item" not in result.quality_flags
        }
        if not sections.get("mdna"):
            raise ValueError("未能定位管理层讨论与分析章节")
        _upsert(db, symbol, market, "report_section", period_key, {
            "source_url": target["url"],
            "source_fingerprint": fingerprint,
            "extractor_version": SECTION_EXTRACTOR_VERSION,
            "title": target["title"],
            "ann_date": target["ann_date"],
            "extract_status": "ok",
            "error": None,
            "attempts": attempts + 1,
            "sections": sections,
            "section_meta": {
                name: {
                    "chars": result.chars,
                    "truncated": result.truncated,
                    "locator": result.locator,
                    "confidence": round(result.confidence, 3),
                    "quality_flags": list(result.quality_flags),
                }
                for name, result in extracted.items()
                if result
            },
            "fetched_pdf_bytes": fetched_bytes,
        })
        db.commit()
        return sections
    except Exception as exc:
        db.rollback()
        transient = _is_transient(exc)
        logger.warning(
            "报告节选获取失败 %s %s（%s）: %s",
            symbol, period_key, "瞬时" if transient else "确定性", str(exc)[:200],
        )
        _upsert(db, symbol, market, "report_section", period_key, {
            "source_url": target["url"],
            "source_fingerprint": fingerprint,
            "extractor_version": SECTION_EXTRACTOR_VERSION,
            "title": target["title"],
            "ann_date": target["ann_date"],
            "extract_status": "failed",
            "error": str(exc)[:300],
            # 瞬时故障（超时/连接失败/5xx/429）不消耗永久额度
            "attempts": attempts + (0 if transient else 1),
            "sections": {},
        })
        db.commit()
        return None


def _section_permanently_failed(
    db: Session, symbol: str, market: str, target: Dict[str, Any]
) -> bool:
    """该报告的章节抽取是否已确定性封顶（同源指纹、同抽取器版本、attempts 满）。

    判据必须与 `_ensure_section` 的缓存命中条件保持一致：它命中封顶行时直接
    返回 None、零成本——这种"跳过"不是本轮的尝试，不该消耗预算或计入 failed。
    """
    row = _load_row(db, symbol, market, "report_section", target["period_key"])
    payload = row.payload if row else None
    if not payload:
        return False
    return (
        payload.get("source_fingerprint") == source_fingerprint(target)
        and int(payload.get("extractor_version") or 1) == SECTION_EXTRACTOR_VERSION
        and payload.get("extract_status") != "ok"
        and int(payload.get("attempts") or 0) >= MAX_ATTEMPTS
    )


def ensure_report_digests(
    db: Session, symbol: str, market: str, *, max_new: int
) -> Dict[str, Any]:
    """判缺→下载→抽节→摘要，单次最多生成 max_new 份（成本护栏）。

    返回 {total, completed, generated, remaining, pending_periods, gaps}。

    **待补齐的报告期必须进 gaps**：分析 job 只把 gaps 传进 LLM 输入，
    `remaining` 谁也不看。版本升级后若有 10 份旧摘要而单轮只补 2 份，模型会
    在完全不知道另外 8 个年份缺失的情况下写"跨年综述"。
    """
    planned = cached_report_targets_detailed(db, symbol, market)
    targets = planned["targets"]
    tiers = assign_digest_tiers(targets)
    result: Dict[str, Any] = {
        "total": len(targets), "completed": 0, "generated": 0,
        # attempted = 本轮实际做了工作的报告数（下载/抽取尝试或 LLM 调用，
        # 无论成败）。**成本护栏按它扣减**：按 generated 扣的话，失败不消耗
        # 预算，循环会把十年报告全试一遍——"每轮最多 4 份"的承诺静默失效。
        "attempted": 0,
        # failed = 本轮**尝试过但失败**的份数（含下载/章节抽取失败与 LLM 生成
        # 失败）。批量调用方靠它区分"全部命中缓存/无年报"（generated=0 且
        # failed=0，是成功）与"每一份都失败"（generated=0 且 failed>0，必须
        # 记失败）——只覆盖 LLM 段的话，数据源或解析器让所有报告都倒在下载/
        # 抽取段时，谎报成功的路径原样存在。
        "failed": 0,
        "remaining": 0, "pending_periods": [], "gaps": [],
        # permanently_failed = 已封顶的历史失败份数（摘要封顶 + section 封顶）。
        # **成本维度与结果维度必须拆开**：封顶行不消耗 attempted（零成本跳过），
        # 但它是确定性的失败结果——不单列的话，"全部封顶"的标的返回
        # generated=0/failed=0，批量层记成功、前端弹绿色"新生成 0 份"，
        # 用户完全看不到其实所有可回填报告都永久失败了。
        "permanently_failed": 0,
        # plan_incomplete = 年报清单检索部分失败（cninfo/披露易/EDGAR 上游故障）。
        # 此时 targets 可能为空或缺年份，都不代表"该标的无年报"——批量层必须
        # 把"零产出且清单不完整"记为失败，否则源站整体故障 = 全量绿色完成。
        "plan_incomplete": not planned["complete"],
        # fatal = 整批等价的确定性失败（结构化，kind 与 FATAL_ANALYSIS_ERROR_KINDS
        # 同一套词汇表）。**绝不能让调用方去匹配中文 gap 文案**：文案一改判据
        # 就静默失效，批量退化成"全跑一遍白转"。
        "fatal": None,
    }
    if result["plan_incomplete"]:
        result["gaps"].insert(
            0, "年报清单检索失败或不完整（数据源故障），本轮覆盖范围不可信"
        )
    for target in targets:
        period_key = target["period_key"]
        fingerprint = source_fingerprint(target)
        tier = tiers.get(period_key, "B")
        digest_row = _load_row(db, symbol, market, "report_digest", period_key)
        payload = digest_row.payload if digest_row else None
        if payload and not _digest_is_current(payload, fingerprint, tier):
            payload = None
        if payload and payload.get("status") == "ok":
            result["completed"] += 1
            continue
        if payload and int(payload.get("attempts") or 0) >= MAX_ATTEMPTS:
            result["permanently_failed"] += 1
            result["gaps"].append(f"{target['end_date']} 摘要生成失败（已封顶）")
            continue
        if _section_permanently_failed(db, symbol, market, target):
            # 历史封顶失败：零成本跳过（不消耗 attempted、不计本轮 failed），
            # 但作为**结果**计入 permanently_failed——见结构注释
            result["permanently_failed"] += 1
            result["gaps"].append(
                f"{target['end_date']} 报告下载或章节抽取失败（已封顶）"
            )
            continue
        if result["attempted"] >= max_new:
            # 本轮成本护栏用光：如实记下是哪些报告期还没补
            result["pending_periods"].append(target["end_date"])
            result["remaining"] += 1
            continue

        result["attempted"] += 1
        sections = _ensure_section(db, symbol, market, target)
        if not sections:
            result["failed"] += 1
            result["gaps"].append(f"{target['end_date']} 报告下载或章节抽取失败")
            continue

        attempts = int((payload or {}).get("attempts") or 0)
        packed, input_meta = _pack_for_digest(sections, tier=tier)
        try:
            completion = chat_completion(
                build_digest_messages(
                    symbol, market, target["report_type"], target["end_date"], packed,
                    tier=tier,
                ),
                response_format={"type": "json_object"},
            )
            digest = parse_digest_output(completion["content"], tier=tier)
            usage = completion.get("usage", {})
            _upsert(db, symbol, market, "report_digest", period_key, {
                "status": "ok",
                "error": None,
                "attempts": attempts + 1,
                "source_fingerprint": fingerprint,
                "extractor_version": SECTION_EXTRACTOR_VERSION,
                "prompt_version": DIGEST_PROMPT_VERSION,
                "digest_tier": tier,
                "report_type": target["report_type"],
                "end_date": target["end_date"],
                "source_url": target["url"],
                "sections_used": sorted(packed),
                "input_meta": input_meta,
                "digest": digest,
                "model": completion.get("model"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            })
            db.commit()
            result["generated"] += 1
            result["completed"] += 1
        except LLMNotConfiguredError as exc:
            result["gaps"].append("未配置 LLM API Key，无法生成报告摘要")
            result["fatal"] = {
                "kind": "llm_not_configured",
                "message": str(exc) or "未配置 LLM API Key（LLM_REPORT_API_KEY）",
            }
            break
        except (ValueError, LLMClientError) as exc:
            # ValueError = 输出不合约定（重试也一样）；LLM 侧按 _is_transient 判
            deterministic = isinstance(exc, ValueError) or not _is_transient(exc)
            db.rollback()
            _upsert(db, symbol, market, "report_digest", period_key, {
                "status": "failed",
                "error": str(exc)[:300],
                "source_fingerprint": fingerprint,
                "extractor_version": SECTION_EXTRACTOR_VERSION,
                "prompt_version": DIGEST_PROMPT_VERSION,
                "digest_tier": tier,
                # 确定性失败计 attempts（封顶跳过）；瞬时失败不计（下次可重试）
                "attempts": attempts + (1 if deterministic else 0),
                "report_type": target["report_type"],
                "end_date": target["end_date"],
                "source_url": target["url"],
            })
            db.commit()
            result["failed"] += 1
            result["gaps"].append(f"{target['end_date']} 摘要生成失败")
            # 401/402/403 认证或欠费、429 限流：换个标的照样失败，逐只重试
            # 只会继续消耗配额并把整批拖成"看起来在跑"的空转
            if isinstance(exc, LLMClientError) and exc.status_code in (
                401, 402, 403, 429
            ):
                result["fatal"] = {
                    "kind": "llm_auth" if exc.status_code != 429 else "llm_rate_limited",
                    "message": f"LLM 调用失败（HTTP {exc.status_code}）：{str(exc)[:150]}",
                }
                break
    if result["pending_periods"]:
        # 一行列出而不是每年一条：分析输入只取 gaps 前 6 条，逐条会被截断掉。
        # **插到最前面**：append 在已有失败缺口之后时，"6 条封顶失败 + 2 份新生成
        # + 2 份待续跑"这种分布会把它挤成第 7 条，正好落在截断线外——模型只看到
        # 失败，仍不知道还有年份没跑。缺失范围比单条失败详情更该活下来。
        result["gaps"].insert(
            0,
            "以下报告期尚未生成摘要（本轮成本护栏未处理，可再次触发续跑）："
            + "、".join(result["pending_periods"]),
        )
    return result


def load_report_digests(
    db: Session, symbol: str, market: str, *, limit: int = 12
) -> List[Dict[str, Any]]:
    """status=ok 的摘要（end_date 倒序）：分析输入与详情页共用。"""
    rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "report_digest",
        )
        .order_by(SecurityProfileData.period_key.desc())
        .limit(limit * 2)  # 含 failed 行，过滤后再截
        .all()
    )
    digests = []
    for row in rows:
        payload = row.payload or {}
        if payload.get("status") != "ok" or not digest_versions_current(payload):
            continue  # 版本过期 = 内容已知有误，宁可缺口如实上报也不混进分析
        digests.append({
            "period_key": row.period_key,
            "report_type": payload.get("report_type"),
            "end_date": payload.get("end_date"),
            "source_url": payload.get("source_url"),
            "digest": payload.get("digest"),
        })
        if len(digests) >= limit:
            break
    return digests


def digest_progress(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """详情页进度条：已摘要 X / 目标 Y（目标数用缓存的 targets 估算需外呼，
    这里只按库内行统计；total 由回填 job 结果或 targets 计算）。"""
    rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "report_digest",
        )
        .all()
    )
    ok = sum(
        1 for row in rows
        if (row.payload or {}).get("status") == "ok"
        and digest_versions_current(row.payload or {})
    )
    # 封顶失败同样要过版本：版本升级后 ensure 会重新尝试这些报告期，
    # 详情页却还在写"永久失败"——进度与实际行为对不上
    failed = sum(
        1 for row in rows
        if (row.payload or {}).get("status") == "failed"
        and int((row.payload or {}).get("attempts") or 0) >= MAX_ATTEMPTS
        and digest_versions_current(row.payload or {})
    )
    return {"digested": ok, "failed_capped": failed}


def serialize_digest_for_analysis(
    digests: List[Dict[str, Any]], *, compact_older_than_years: int = 5
) -> List[Dict[str, Any]]:
    """分析输入版摘要：旧于 N 年的压缩为核心四字段（预算控制第一档）。"""
    cutoff_year = date.today().year - compact_older_than_years
    compacted = []
    for entry in digests:
        end_date = str(entry.get("end_date") or "")
        digest = entry.get("digest") or {}
        if end_date[:4].isdigit() and int(end_date[:4]) < cutoff_year:
            digest = {
                key: digest.get(key)
                for key in ("主营收入结构", "一次性项目", "会计信号", "关键数字")
                if digest.get(key)
            }
        compacted.append({
            "end_date": entry.get("end_date"),
            "report_type": entry.get("report_type"),
            "digest": digest,
        })
    return compacted
