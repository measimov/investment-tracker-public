"""港股年报/中报 PDF → 三张合并报表 → 归一 26 科目 → security_profile_data.report_statements。

港股结构化基本面此前只有 Yahoo fundamentals-timeseries（非官方、仅近 3-5 年、429 频发）。
披露易年报（t2code 40100）与中期报告（40200）是官方一手来源，每份含本期与比较期两列，
十年只需约 20 份 PDF/只。管线：

  plan_statement_targets（披露易清单，年报+中报，同期取公告日最新）
  → ensure_report_statements：判缺 → download_report_pdf → pdfplumber 逐页文本
    → report_statements.locate_statements（纯函数定位三张表）
    → LLM 只做**科目→行 id** 映射（report_statement_prompts，JSON mode）
    → 代码按 id 取原文数字、按单位放大、按表头年份定列 → 写 report_statements 行

两个数据集：
- report_statement_extract：按**报告**（period_key = 报告期|annual/interim）缓存状态、
  源指纹、双版本、解析出的结构化行（prompt bump 后无需重下载 PDF 即可重映射）与映射；
- report_statements：按**会计期**（period_key = 末日|FY/H1）的科目行，形状与 Yahoo 透视行
  一致（end_date / fp / currency / 26 科目），`market_statements` 港股分支优先消费它，
  Yahoo 只补 PDF 没覆盖的年份。本报告期的行（is_comparative=False）是权威；比较期行只填空
  或替换更早报告写下的比较期行，绝不覆盖某份报告自己的本期行。

失败语义与 report_digest_service 一致：瞬时错误不消耗 attempts；确定性错误（定位失败、
映射不合约定）计 attempts，MAX_ATTEMPTS 后封顶跳过；无 Key / 401-403 / 429 为 fatal。
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Callable, Any, Dict, List, Optional, Tuple

import pdfplumber
from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .llm_client import LLMClientError, LLMNotConfiguredError, chat_completion
from .report_digest_service import (
    MAX_ATTEMPTS,
    _extract_report_year,
    _hkex_sort_key,
    _infer_hk_fiscal_end,
    _is_transient,
    _load_row,
    _parse_hkex_datetime,
    _upsert,
    source_fingerprint,
)
from .report_fetchers import download_report_pdf, hkex_reports
from .report_statement_checks import (
    CROSS_CHECK_FIELDS,
    STATEMENT_VALIDATION_VERSION,
    cross_check_row,
    finalize_validation,
    hard_failures,
    validation_current,
    validation_summary,
)
from .report_statement_prompts import (
    DERIVED_SUM_FIELDS,
    EXPENSE_MAGNITUDE_FIELDS,
    FIELD_KIND,
    PER_SHARE_FIELDS,
    STATEMENT_PROMPT_VERSION,
    build_statement_messages,
    parse_statement_mapping,
    statement_row_current,
)
from .report_statements import (
    STATEMENT_EXTRACTOR_VERSION,
    ParsedStatement,
    locate_statements,
    period_columns,
    resolve_value,
    statement_rows_for_prompt,
    years_consistent,
    detect_period_end,
)

logger = get_app_logger(__name__)

STATEMENT_MARKETS = ("港股",)
ANNUAL_YEARS = 10
INTERIM_YEARS = 10
EXTRACT_DATASET = "report_statement_extract"
STATEMENT_DATASET = "report_statements"
# 港股报表币种缺失时不猜（HKD/CNY/USD 都常见）：留 None，由消费方按缺失处理
_INTERIM_NOISE = ("摘要", "補充", "补充", "更正", "英文", "季度", "季報", "季报")
_ANNUAL_NOISE = ("摘要", "補充", "补充", "更正", "英文")


# ---------------------------------------------------------------------------- 目标规划


def _infer_hk_interim_end(title: str, ann_date: str) -> Optional[str]:
    """中期报告标题年份 + 公告日 → 中期末日。中报须在期末后 1-4 个月内刊发：在标题年份的
    四个季末里取公告日之前 1-4 个月那一个（6/30 财年 12 月的公司中期末是 12/31、3 月财年
    是 9/30）。落不进窗口退回 6/30。"""
    year = _extract_report_year(title)
    if year is None:
        return None
    ann = _parse_hkex_datetime(ann_date)
    if ann is None:
        return f"{year}0630"
    # 标题年份既可能是期末所在年（"2025 中期報告" = 2025-06-30），也可能是财年（6 月财年的
    # 01023 "2026 中期報告" = 截至 2025-12-31 止六個月，刊发于 2026 年 2-3 月）：两个年份的
    # 季末都试，取落在刊发窗口里的那个
    for candidate_year in (year, year - 1):
        for month, day in ((6, "30"), (9, "30"), (12, "31"), (3, "31")):
            months_before = (ann.year - candidate_year) * 12 + (ann.month - month)
            if 1 <= months_before <= 4:
                return f"{candidate_year}{month:02d}{day}"
    return f"{year}0630"


def plan_statement_targets(symbol: str, market: str) -> Dict[str, Any]:
    """年报 + 中报目标（按 end_date 倒序）：{"targets", "complete", "failed_kinds"}。"""
    if market not in STATEMENT_MARKETS:
        return {"targets": [], "complete": True, "failed_kinds": []}
    targets: List[Dict[str, Any]] = []
    failed: List[str] = []
    for report_type, years, noise in (
        ("annual", ANNUAL_YEARS, _ANNUAL_NOISE),
        ("interim", INTERIM_YEARS, _INTERIM_NOISE),
    ):
        try:
            reports = hkex_reports(symbol, report_type=report_type, limit=years + 4)
        except Exception as exc:  # noqa: BLE001 - 清单失败记 failed_kinds，另一类继续
            logger.warning("披露易 %s 清单获取失败 %s: %s", report_type, symbol, str(exc)[:150])
            failed.append(report_type)
            continue
        by_period: Dict[str, Dict[str, Any]] = {}
        for row in reports:
            title = row["title"]
            if any(word in title for word in noise):
                continue
            end_date = (
                _infer_hk_fiscal_end(title, row["ann_date"])
                if report_type == "annual"
                else _infer_hk_interim_end(title, row["ann_date"])
            )
            if not end_date:
                continue
            existing = by_period.get(end_date)
            if existing is None or _hkex_sort_key(row["ann_date"]) > _hkex_sort_key(
                existing["ann_date"]
            ):
                by_period[end_date] = row
        for end_date in sorted(by_period, reverse=True)[:years]:
            row = by_period[end_date]
            targets.append({
                "period_key": f"{end_date}|{report_type}",
                "report_type": report_type,
                "end_date": end_date,
                "title": row["title"],
                "ann_date": row["ann_date"],
                "url": row["url"],
            })
    targets.sort(key=lambda t: (t["end_date"], t["report_type"] == "annual"), reverse=True)
    return {"targets": targets, "complete": not failed, "failed_kinds": failed}


# ---------------------------------------------------------------------------- 抽取与映射


def _drop_overdrawn(chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """被后画的文字覆盖的字符串整段丢掉（画家模型：后画的盖住先画的）。

    時代集團 01023 的报表页在标题位置先画了模板页眉「綜合財務報表附註」再画「綜合損益表」，
    渲染出来只见后者，但两串字符位置逐字重合，pdfplumber 按 x 排序后拼成
    「綜綜合合財損務益報表表附註」，标题永远匹配不上。按内容流把字符切成「同一行连续绘制」
    的串；一串里过半字符槽位被后面的串覆盖，就整串视为被盖住（露出的尾巴「表附註」渲染时
    也不可见——它和被盖住的部分属于同一次绘制）。同文重画（加粗效果）也顺带去重。"""
    if not chars:
        return chars
    slot = [
        (round(float(ch["x0"]) * 2), round(float(ch["top"]) * 2), round(float(ch.get("size") or 0) * 2))
        for ch in chars
    ]
    last_index: Dict[Tuple[int, int, int], int] = {}
    for index, key in enumerate(slot):
        last_index[key] = index
    if len(last_index) == len(chars):
        return chars
    # 同一行（top/size 相同）、内容流里连续且 x 单调向右的字符 = 一次绘制的串；x 回退
    # 说明另一串从左边重新开始画（同一位置重画的加粗、或盖在上面的新标题）
    runs: List[List[int]] = [[0]]
    for index in range(1, len(chars)):
        if slot[index][1:] == slot[index - 1][1:] and slot[index][0] > slot[index - 1][0]:
            runs[-1].append(index)
        else:
            runs.append([index])
    dropped: set = set()
    for run in runs:
        covered = sum(1 for index in run if last_index[slot[index]] != index)
        if covered >= 2 and covered * 2 >= len(run):
            dropped.update(run)
        else:
            dropped.update(index for index in run if last_index[slot[index]] != index)
    return [ch for index, ch in enumerate(chars) if index not in dropped]


def baseline_text(chars: List[Dict[str, Any]], *, page_height: float) -> str:
    """按**基线**而不是字形框顶边聚行后抽文本。

    pdfplumber 默认按 char 的 top 聚行，而 top 来自字体的 ascent/descent 度量：申洲國際
    02313 的年报正文是 Source Han Sans（字形框整体落在基线之下）配 Helvetica 数字（正常
    度量），同一行的科目名与数字 top 相差 7.5pt（半行），被拆成两行——58 行现金流量表全部
    「无标签」，上一行的科目名进了下一行的上下文，模型推理 2.3 万 token 后仍把页脚「2025 43」
    映射成经营现金流。两者的文本矩阵 f 分量（基线 y）完全相同，所以把每个字符的 top/bottom
    改写成「基线 − 0.8×字号 / 基线 + 0.2×字号」再交给 pdfplumber 自己的聚行与排版，正常字体
    的页面输出与 `page.extract_text()` 逐行一致。缺 matrix 或非直立文字的页面退回默认抽取。"""
    adjusted: List[Dict[str, Any]] = []
    for ch in _drop_overdrawn(chars):
        matrix = ch.get("matrix")
        if not matrix or len(matrix) < 6 or not ch.get("upright", True):
            return pdfplumber.utils.extract_text(chars)
        size = float(ch.get("size") or 0) or float(ch["bottom"] - ch["top"])
        top = page_height - float(matrix[5]) - size * 0.8
        shift = top - float(ch["top"])
        copy = dict(ch)
        copy["top"] = top
        copy["bottom"] = top + size
        copy["doctop"] = float(ch["doctop"]) + shift
        copy["y1"] = page_height - top
        copy["y0"] = page_height - top - size
        adjusted.append(copy)
    return pdfplumber.utils.extract_text(adjusted)


def _page_text(page) -> str:
    return baseline_text(page.chars, page_height=float(page.height))


def _extract_pages(pdf_bytes: bytes) -> List[str]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [_page_text(page) for page in pdf.pages]


def _locate(pages: List[str], target: Dict[str, Any]) -> Dict[str, ParsedStatement]:
    found = locate_statements(pages, report_type=target["report_type"])
    located = {kind: parsed for kind, parsed in found.items() if parsed is not None}
    missing = [kind for kind in ("income", "balance") if kind not in located]
    if missing:
        raise ValueError(f"未能定位报表: {'、'.join(missing)}")
    end_date = actual_period_end(located, target)
    for kind, parsed in located.items():
        if not years_consistent(parsed, end_date=end_date):
            raise ValueError(f"{kind} 表头年份 {parsed.years} 与报告期 {end_date} 不符")
    return located


def actual_period_end(located: Dict[str, ParsedStatement], target: Dict[str, Any]) -> str:
    """报表表头声明的期末日优先于清单猜出来的 end_date（清单只有标题+公告日，非 12 月
    财年的公司猜不准）。损益/现金流的「截至…止」是本期期末，資產負債表的「於…」亦然；
    与猜测年份相差超过一年视为读错，仍用猜测。"""
    guessed = target["end_date"]
    for kind in ("income", "cashflow", "balance"):
        parsed = located.get(kind)
        if parsed is None:
            continue
        detected = detect_period_end(parsed)
        if detected and abs(int(detected[:4]) - int(guessed[:4])) <= 1:
            if detected != guessed:
                logger.info(
                    "报表期末以表头为准 %s: 清单猜测 %s → 表头 %s", target.get("title"), guessed, detected
                )
            return detected
    return guessed


def _prompt_statements(
    located: Dict[str, ParsedStatement], target: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    payload: Dict[str, Dict[str, Any]] = {}
    for kind, parsed in located.items():
        columns = period_columns(parsed, report_type=target["report_type"], end_date=target["end_date"])
        payload[kind] = {
            "title": parsed.title,
            "unit_multiplier": parsed.unit_multiplier,
            "currency": parsed.currency,
            "columns": [f"{c.end_date} {c.fp}" for c in columns],
            "rows": statement_rows_for_prompt(parsed, columns=[c.column for c in columns]),
        }
    return payload


def _decimal_to_number(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def build_period_rows(
    located: Dict[str, ParsedStatement],
    mapping: Dict[str, Dict[str, List[str]]],
    target: Dict[str, Any],
    *,
    fingerprint: str,
) -> List[Dict[str, Any]]:
    """映射 + 解析行 → 各会计期的科目行（本期 is_comparative=False，比较期 True）。"""
    rows_by_period: Dict[str, Dict[str, Any]] = {}
    for kind, parsed in located.items():
        if not mapping.get(kind):
            continue  # 软必需科目缺失时整张表已被丢弃：不留空来源占位
        columns = period_columns(parsed, report_type=target["report_type"], end_date=target["end_date"])
        for col in columns:
            key = f"{col.end_date}|{col.fp}"
            row = rows_by_period.setdefault(key, {
                "end_date": col.end_date,
                "fp": col.fp,
                "currency": None,
                "is_comparative": not col.is_primary,
                "source_period_key": target["period_key"],
                "source_report_type": target["report_type"],
                "source_end_date": target["end_date"],
                "source_url": target["url"],
                "source_fingerprint": fingerprint,
                "source_pages": {},
                # 每张表的来源报告（比较期按表合并时判新旧）
                "source_by_kind": {},
                # 每张表各自识别出的币种/单位：三张表不一致是硬失败，不猜
                "currency_by_kind": {},
                "unit_by_kind": {},
                "extractor_version": STATEMENT_EXTRACTOR_VERSION,
                "prompt_version": STATEMENT_PROMPT_VERSION,
            })
            row["currency_by_kind"][kind] = parsed.currency
            row["unit_by_kind"][kind] = parsed.unit_multiplier
            row["source_pages"][kind] = [parsed.page_start, parsed.page_end]
            row["source_by_kind"][kind] = {
                "period_key": target["period_key"], "end_date": target["end_date"],
                "report_type": target["report_type"], "ann_date": target.get("ann_date"),
            }
            for field, row_ids in mapping.get(kind, {}).items():
                value = resolve_value(
                    parsed, row_ids, col.column, scale=field not in PER_SHARE_FIELDS
                )
                if value is None:
                    continue
                if field in EXPENSE_MAGNITUDE_FIELDS:
                    value = abs(value)
                row[field] = _decimal_to_number(value)
    rows: List[Dict[str, Any]] = []
    for row in rows_by_period.values():
        known = sorted({c for c in row["currency_by_kind"].values() if c})
        row["currency"] = known[0] if len(known) == 1 else None
        # 记下**实际**由代码推导的科目及其输入：清洗输入时派生值一并失效（评审 P1）
        row["derived_fields"] = {}
        for derived, addends in DERIVED_SUM_FIELDS.items():
            if row.get(derived) is None and all(row.get(f) is not None for f in addends):
                row[derived] = sum(row[f] for f in addends)
                row["derived_fields"][derived] = list(addends)
        cfo, capex = row.get("n_cashflow_act"), row.get("capex")
        if cfo is not None and capex is not None:
            # capex 按报表符号通常为负；个别报表写正数，按绝对值扣才不会把 FCF 算大
            row["free_cashflow"] = cfo - abs(capex)
            row["derived_fields"]["free_cashflow"] = ["n_cashflow_act", "capex"]
        failures = hard_failures(row)
        if failures:
            if not row["is_comparative"]:
                raise ValueError("报表校验失败: " + "；".join(failures))
            # 比较期行不可信就丢掉：相邻年份的报告会再写一次，垃圾比较行比没有更糟
            logger.warning(
                "丢弃比较期行 %s（%s）: %s", row["end_date"], target.get("period_key"), failures
            )
            continue
        finalize_validation(row)
        rows.append(row)
    return rows


def _source_rank(source: Optional[Dict[str, Any]]) -> tuple:
    """来源报告的新旧：报告期越新越优；同期年报优于中报；再同则公告日晚者（修订版）优。"""
    if not source:
        return ("", 0, "")
    return (
        str(source.get("end_date") or ""),
        1 if source.get("report_type") == "annual" else 0,
        _hkex_sort_key(source.get("ann_date") or ""),
    )


def merge_comparative_row(
    existing: Optional[Dict[str, Any]], incoming: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """比较期行的写入结果：None = 不写。

    - 目标期已有**本期权威行**（某份报告自己的报告期）→ 不写；
    - 目标期没有行 → 原样写；
    - 目标期已有比较期行（含旧版本行）→ **按表合并**：每张表取来源报告更新的那份，另一份只
      填该表缺失的科目。目标按报告期倒序处理，2025 年报先为 2024 写下三张表的比较期，随后
      2025 中报的资产负债表比较列也指向 2024——整行替换会把营收/现金流丢掉，而 2024 年报
      缺失/失败时它们永远回不来（PR #201 评审 P1）。"""
    if not existing:
        return incoming
    if not statement_row_current(existing):
        # 旧版本行（无论本期还是比较期）在重算成功前不算有效数据：不能挡住新版本的比较期，
        # 否则版本 bump 后该期在年报重算前一直无数据可读（每轮只重算 max_new 份）
        return incoming
    if not existing.get("is_comparative"):
        return None
    # 币种未知或不一致时**不得逐科目补数**（20 USD 会被贴成 20 CNY，元数据还指向另一份报告）：
    # 只能整行取单一来源——来源更新者胜，否则原样保留（PR #201 评审 P1）
    existing_currency, incoming_currency = existing.get("currency"), incoming.get("currency")
    if not existing_currency or not incoming_currency or existing_currency != incoming_currency:
        existing_rank = max(
            (_source_rank(src) for src in (existing.get("source_by_kind") or {}).values()),
            default=_source_rank(None),
        )
        incoming_rank = max(
            (_source_rank(src) for src in (incoming.get("source_by_kind") or {}).values()),
            default=_source_rank(None),
        )
        return incoming if incoming_rank > existing_rank else None
    merged = dict(existing)
    merged_sources = dict(existing.get("source_by_kind") or {})
    merged_pages = dict(existing.get("source_pages") or {})
    incoming_sources = incoming.get("source_by_kind") or {}
    for kind, source in incoming_sources.items():
        newer = _source_rank(source) >= _source_rank(merged_sources.get(kind))
        for field, value in incoming.items():
            if FIELD_KIND.get(field) != kind or value is None:
                continue
            if newer or merged.get(field) is None:
                merged[field] = value
        if newer:
            merged_sources[kind] = source
            merged_pages[kind] = (incoming.get("source_pages") or {}).get(kind)
    merged["source_by_kind"] = merged_sources
    merged["source_pages"] = merged_pages
    merged["currency"] = merged.get("currency") or incoming.get("currency")
    # 行级元数据取最新来源；版本号取当前（合并结果是当前代码写出的）
    newest_kind = max(merged_sources, key=lambda k: _source_rank(merged_sources[k]), default=None)
    if newest_kind:
        top = merged_sources[newest_kind]
        merged["source_period_key"] = top["period_key"]
        merged["source_report_type"] = top["report_type"]
        merged["source_end_date"] = top["end_date"]
    merged["is_comparative"] = True
    merged["extractor_version"] = STATEMENT_EXTRACTOR_VERSION
    merged["prompt_version"] = STATEMENT_PROMPT_VERSION
    # 合并结果的科目来自两份报告，原 validation 已失效
    finalize_validation(merged)
    return merged


def _yahoo_row(db: Session, symbol: str, market: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """同财年的 Yahoo 行（键是裸 end_date，没有 |FY 后缀——两侧键形不同，只在内存里按
    (end_date, fp) 对齐，见 earnings_quality.merge_hk_statement_rows）。中报没有 Yahoo 对照。"""
    if row.get("fp") != "FY":
        return None
    found = _load_row(db, symbol, market, "yahoo_fundamentals", str(row["end_date"]))
    return found.payload if found else None


def comparative_evidence(comparative: Dict[str, Any]) -> Dict[str, Any]:
    """另一份报告比较列里可重跑的证据切片：来源与币种 + 交叉核对科目。存在主行的
    `comparative_evidence` 上——比较列本身通常被主行覆盖、没有第二条行可回读，规则升版重校验
    时若不保存这份证据，先前由它判出的存疑会被静默清除（PR #207 评审 P2）。"""
    evidence = {
        "source_period_key": comparative.get("source_period_key"),
        "source_report_type": comparative.get("source_report_type"),
        "source_end_date": comparative.get("source_end_date"),
        "currency": comparative.get("currency"),
    }
    for field in CROSS_CHECK_FIELDS:
        if comparative.get(field) is not None:
            evidence[field] = comparative[field]
    return evidence


def _evidence_rank(evidence: Optional[Dict[str, Any]]) -> tuple:
    if not evidence:
        return _source_rank(None)
    return _source_rank({
        "end_date": evidence.get("source_end_date"),
        "report_type": evidence.get("source_report_type"),
    })


def attach_comparative_evidence(row: Dict[str, Any], comparative: Optional[Dict[str, Any]]) -> None:
    """把比较列证据挂到主行上；已有证据时只被来源更新（或同源）的替换。"""
    if not comparative:
        return
    incoming = comparative_evidence(comparative)
    if _evidence_rank(incoming) >= _evidence_rank(row.get("comparative_evidence")):
        row["comparative_evidence"] = incoming


def _cross_checks(
    db: Session, symbol: str, market: str, row: Dict[str, Any],
    *, comparative: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Yahoo 同财年行 + 比较列证据（传入的新证据，否则行上保存的）。"""
    attach_comparative_evidence(row, comparative)
    return cross_check_row(
        row,
        yahoo_row=_yahoo_row(db, symbol, market, row),
        comparative_row=row.get("comparative_evidence"),
    )


def _write_period_rows(
    db: Session, symbol: str, market: str, rows: List[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """写各期行并做交叉核对 → (写入的 period_key, 存疑的 period_key)。

    主行：与 Yahoo 同财年行、以及库里已有的**另一份报告的比较列**核对后覆盖写入。
    比较行：目标期已有主行时不写，但用这份报告的比较列反过来核对那条主行并重写它的
    validation——旧年份的主行由此被下一年的报告校验。"""
    written: List[str] = []
    suspect: List[str] = []
    for row in rows:
        period_key = f"{row['end_date']}|{row['fp']}"
        existing = _load_row(db, symbol, market, STATEMENT_DATASET, period_key)
        existing_payload = existing.payload if existing else None
        if row["is_comparative"]:
            payload = merge_comparative_row(existing_payload, row)
            if payload is None:
                if existing_payload and not existing_payload.get("is_comparative") and statement_row_current(
                    existing_payload
                ):
                    primary = dict(existing_payload)
                    finalize_validation(
                        primary, extra_checks=_cross_checks(db, symbol, market, primary, comparative=row)
                    )
                    _upsert(db, symbol, market, STATEMENT_DATASET, period_key, primary)
                    if primary["validation"]["status"] == "suspect":
                        suspect.append(period_key)
                continue
            finalize_validation(payload, extra_checks=_cross_checks(db, symbol, market, payload))
        else:
            comparative = None
            if existing_payload and existing_payload.get("is_comparative"):
                if existing_payload.get("source_period_key") != row.get("source_period_key"):
                    comparative = existing_payload
            elif existing_payload and existing_payload.get("comparative_evidence"):
                # 主行重抽：旧主行上保存的比较列证据随行延续（比较列本身早已被覆盖）
                row["comparative_evidence"] = existing_payload["comparative_evidence"]
            payload = row
            finalize_validation(
                payload, extra_checks=_cross_checks(db, symbol, market, payload, comparative=comparative)
            )
        _upsert(db, symbol, market, STATEMENT_DATASET, period_key, payload)
        written.append(period_key)
        if payload["validation"]["status"] == "suspect":
            suspect.append(period_key)
    return written, suspect


def revalidate_report_statements(db: Session, symbol: str, market: str) -> Dict[str, int]:
    """校验规则升版后零下载零 LLM 重算存量行的 validation：恒等式、Yahoo 核对，以及行上
    保存的比较列证据（`comparative_evidence`，写入时由另一份报告的比较列留下）——三者都
    重跑，规则升版不会把先前由比较列判出的存疑清掉。返回 {revalidated, suspect}。"""
    rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == STATEMENT_DATASET,
        )
        .all()
    )
    revalidated = suspect = 0
    for row in rows:
        payload = row.payload or {}
        if not statement_row_current(payload) or validation_current(payload):
            continue
        refreshed = dict(payload)
        finalize_validation(refreshed, extra_checks=_cross_checks(db, symbol, market, refreshed))
        _upsert(db, symbol, market, STATEMENT_DATASET, row.period_key, refreshed)
        revalidated += 1
        if refreshed["validation"]["status"] == "suspect":
            suspect += 1
    if revalidated:
        db.commit()
    return {"revalidated": revalidated, "suspect": suspect}


def _extract_row_current(payload: Dict[str, Any], fingerprint: str) -> bool:
    return (
        payload.get("source_fingerprint") == fingerprint
        and int(payload.get("extractor_version") or 1) == STATEMENT_EXTRACTOR_VERSION
    )


def ensure_report_statements(
    db: Session, symbol: str, market: str, *, max_new: int
) -> Dict[str, Any]:
    """判缺 → 下载 → 定位 → 映射 → 写行，单次最多处理 max_new 份报告（成本护栏）。
    返回结构与 ensure_report_digests 同形：{total, completed, generated, attempted, failed,
    remaining, pending_periods, gaps, permanently_failed, plan_incomplete, fatal}。"""
    result: Dict[str, Any] = {
        "total": 0, "completed": 0, "generated": 0, "attempted": 0, "failed": 0,
        "remaining": 0, "pending_periods": [], "gaps": [], "permanently_failed": 0,
        "plan_incomplete": False, "fatal": None, "suspect": 0, "suspect_periods": [],
    }
    if market not in STATEMENT_MARKETS:
        return result
    planned = plan_statement_targets(symbol, market)
    targets = planned["targets"]
    result["total"] = len(targets)
    result["plan_incomplete"] = not planned["complete"]
    if result["plan_incomplete"]:
        result["gaps"].append(
            "报告清单检索部分失败（披露易故障："
            + "、".join(planned["failed_kinds"]) + "），报表覆盖范围不可信"
        )

    for target in targets:
        period_key = target["period_key"]
        fingerprint = source_fingerprint(target)
        row = _load_row(db, symbol, market, EXTRACT_DATASET, period_key)
        payload = row.payload if row else None
        attempts = 0
        reusable: Optional[Dict[str, ParsedStatement]] = None
        if payload and _extract_row_current(payload, fingerprint):
            prompt_current = int(payload.get("prompt_version") or 1) == STATEMENT_PROMPT_VERSION
            if payload.get("status") == "ok" and prompt_current:
                result["completed"] += 1
                continue
            attempts = int(payload.get("attempts") or 0)
            if attempts >= MAX_ATTEMPTS and not (payload.get("status") == "ok" and not prompt_current):
                result["permanently_failed"] += 1
                result["gaps"].append(f"{target['end_date']} 报表抽取失败（已封顶）")
                continue
            if payload.get("statements"):
                # 抽取器版本没变、只是 prompt 变了：重映射无需重下载
                reusable = {
                    kind: ParsedStatement.from_payload(item)
                    for kind, item in (payload.get("statements") or {}).items()
                }
                if payload.get("status") == "ok" and not prompt_current:
                    attempts = 0
        if result["attempted"] >= max_new:
            result["pending_periods"].append(target["end_date"])
            result["remaining"] += 1
            continue

        result["attempted"] += 1
        fetched_bytes = int((payload or {}).get("fetched_pdf_bytes") or 0) if reusable else 0
        try:
            if reusable:
                located = reusable
            else:
                pdf_bytes = download_report_pdf(target["url"], source="hkexnews")
                fetched_bytes = len(pdf_bytes)
                located = _locate(_extract_pages(pdf_bytes), target)
            # period_key 是清单里这份报告的身份，不变；end_date 改按表头声明的期末日
            target = {**target, "end_date": actual_period_end(located, target)}
            prompt_payload = _prompt_statements(located, target)
            completion = chat_completion(
                build_statement_messages(
                    symbol=symbol, market=market, report_type=target["report_type"],
                    end_date=target["end_date"], statements=prompt_payload,
                ),
                response_format={"type": "json_object"},
            )
            mapping, unresolved = parse_statement_mapping(
                completion["content"],
                {kind: [r.row_id for r in parsed.rows] for kind, parsed in located.items()},
            )
            period_rows = build_period_rows(located, mapping, target, fingerprint=fingerprint)
            written, suspect_periods = _write_period_rows(db, symbol, market, period_rows)
            for period in suspect_periods:
                stored = _load_row(db, symbol, market, STATEMENT_DATASET, period)
                reason = validation_summary(stored.payload if stored else {})
                result["gaps"].append(f"{period.split('|')[0]} 报表科目校验存疑（{reason}）")
                if period not in result["suspect_periods"]:
                    result["suspect_periods"].append(period)
            result["suspect"] = len(result["suspect_periods"])
            usage = completion.get("usage", {})
            _upsert(db, symbol, market, EXTRACT_DATASET, period_key, {
                "status": "ok",
                "error": None,
                "attempts": attempts + 1,
                "source_fingerprint": fingerprint,
                "extractor_version": STATEMENT_EXTRACTOR_VERSION,
                "prompt_version": STATEMENT_PROMPT_VERSION,
                "report_type": target["report_type"],
                "end_date": target["end_date"],
                "title": target["title"],
                "ann_date": target["ann_date"],
                "source_url": target["url"],
                "statements": {kind: parsed.to_payload() for kind, parsed in located.items()},
                "mapping": mapping,
                "unresolved": unresolved,
                "periods_written": written,
                "suspect_periods": suspect_periods,
                "model": completion.get("model"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "fetched_pdf_bytes": fetched_bytes,
            })
            db.commit()
            result["generated"] += 1
            result["completed"] += 1
        except LLMNotConfiguredError as exc:
            db.rollback()
            result["gaps"].append("未配置 LLM API Key，无法归一报表科目")
            result["fatal"] = {
                "kind": "llm_not_configured",
                "message": str(exc) or "未配置 LLM API Key（LLM_REPORT_API_KEY）",
            }
            break
        except Exception as exc:  # noqa: BLE001 - 分类后落库，与摘要管线同语义
            db.rollback()
            transient = _is_transient(exc)
            logger.warning(
                "报表抽取失败 %s %s（%s）: %s",
                symbol, period_key, "瞬时" if transient else "确定性", str(exc)[:200],
            )
            _upsert(db, symbol, market, EXTRACT_DATASET, period_key, {
                "status": "failed",
                "error": str(exc)[:300],
                "attempts": attempts + (0 if transient else 1),
                "source_fingerprint": fingerprint,
                "extractor_version": STATEMENT_EXTRACTOR_VERSION,
                "prompt_version": STATEMENT_PROMPT_VERSION,
                "report_type": target["report_type"],
                "end_date": target["end_date"],
                "title": target["title"],
                "ann_date": target["ann_date"],
                "source_url": target["url"],
                # 定位成功但映射失败时保留结构化行，重试不必重下载
                "statements": {kind: p.to_payload() for kind, p in (reusable or {}).items()},
                "fetched_pdf_bytes": fetched_bytes,
            })
            db.commit()
            result["failed"] += 1
            result["gaps"].append(f"{target['end_date']} 报表抽取失败")
            if isinstance(exc, LLMClientError) and exc.status_code in (401, 402, 403, 429):
                result["fatal"] = {
                    "kind": "llm_auth" if exc.status_code != 429 else "llm_rate_limited",
                    "message": f"LLM 调用失败（HTTP {exc.status_code}）：{str(exc)[:150]}",
                }
                break
    # 校验规则升版后的存量行：零下载零 LLM 补算（≤ 二十几行，与本轮成本无关）
    try:
        refreshed = revalidate_report_statements(db, symbol, market)
        if refreshed["suspect"]:
            result["gaps"].append(f"另有 {refreshed['suspect']} 个会计期的存量报表行重新校验后存疑")
    except Exception as exc:  # noqa: BLE001 - 重校验失败不影响抽取结果
        db.rollback()
        logger.warning("报表行重校验失败 %s %s: %s", symbol, market, str(exc)[:200])
    if result["pending_periods"]:
        result["gaps"].insert(
            0,
            "以下报告期的报表尚未抽取（本轮成本护栏未处理，可再次触发续跑）："
            + "、".join(result["pending_periods"]),
        )
    return result


# ---------------------------------------------------------------------------- 读


def load_report_statement_rows(
    db: Session, symbol: str, market: str, *, limit: int = 24
) -> List[Dict[str, Any]]:
    """当前版本的科目行（旧版本行在重算成功前不参与任何读取，见 statement_row_current）。"""
    rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == STATEMENT_DATASET,
        )
        .order_by(SecurityProfileData.period_key.desc())
        .all()
    )
    current = [row.payload for row in rows if statement_row_current(row.payload or {})]
    return current[:limit]


def statement_progress(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """详情页「报表抽取」面板：零外呼。

    四类报告：ok（当前版本成功）/ stale（成功但版本过期，待重抽——**不是失败**，此前被算进
    失败数）/ failed（可重试）/ capped（attempts 封顶，永久跳过）；会计期按年报/中报分列，
    存疑期来自科目行的 validation；失败清单带原因，让用户看到"哪份、为什么"。"""
    extracts = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == EXTRACT_DATASET,
        )
        .all()
    )
    ok: List[SecurityProfileData] = []
    stale: List[SecurityProfileData] = []
    failed: List[SecurityProfileData] = []
    for row in extracts:
        payload = row.payload or {}
        if payload.get("status") == "ok":
            (ok if statement_row_current(payload) else stale).append(row)
        else:
            failed.append(row)
    failed_reports = sorted(
        (
            {
                "period_key": row.period_key,
                "end_date": (row.payload or {}).get("end_date"),
                "report_type": (row.payload or {}).get("report_type"),
                "error": (row.payload or {}).get("error"),
                "attempts": int((row.payload or {}).get("attempts") or 0),
                "capped": int((row.payload or {}).get("attempts") or 0) >= MAX_ATTEMPTS,
            }
            for row in failed
        ),
        key=lambda item: str(item["end_date"] or ""),
        reverse=True,
    )
    periods = load_report_statement_rows(db, symbol, market, limit=60)
    annual = sorted({p["end_date"] for p in periods if p.get("fp") == "FY"}, reverse=True)
    interim = sorted({p["end_date"] for p in periods if p.get("fp") == "H1"}, reverse=True)
    suspect_periods = sorted(
        {
            f"{p['end_date']}|{p.get('fp') or 'FY'}"
            for p in periods
            if (p.get("validation") or {}).get("status") == "suspect"
        },
        reverse=True,
    )
    fetched = [row.fetched_at for row in extracts if row.fetched_at is not None]
    return {
        "reports_ok": len(ok),
        "reports_stale": len(stale),
        "reports_failed": len(failed),
        "reports_capped": sum(1 for item in failed_reports if item["capped"]),
        "annual_periods": annual,
        "interim_periods": interim,
        "suspect_periods": suspect_periods,
        "suspect_count": len(suspect_periods),
        "failed_reports": failed_reports,
        "last_extracted_at": max(fetched).isoformat() if fetched else None,
        "validation_version": STATEMENT_VALIDATION_VERSION,
        "as_of": date.today().isoformat(),
    }


STATEMENT_OUTCOME_KEYS = (
    "total", "completed", "generated", "failed", "permanently_failed", "suspect", "remaining",
)


def attach_statement_outcome(
    db: Session, symbol: str, market: str, outcome: Dict[str, Any], *, max_new: int,
    ensure: Optional[Callable[..., Dict[str, Any]]] = None,
) -> None:
    """财报摘要之后顺带抽三张报表（同一成本护栏）：结果挂在 outcome["statements"]，缺口以
    「[报表抽取]」前缀并入 gaps，fatal 与摘要同词汇表向上传递。报表管线自身的意外异常不
    拖垮本标的的摘要结果。单标的回填与批量回填共用（此前只有批量做，单标的「补齐历史摘要」
    不抽报表）。`ensure` 供批量侧注入自己模块命名空间里的 ensure_report_statements（测试
    monkeypatch 该名字）。"""
    runner = ensure or ensure_report_statements
    try:
        statements = runner(db, symbol, market, max_new=max_new)
    except Exception as exc:  # noqa: BLE001
        logger.warning("报表抽取意外失败 %s/%s: %s", market, symbol, str(exc)[:200])
        outcome["gaps"] = list(outcome.get("gaps") or []) + ["[报表抽取] 管线异常，本轮未抽取报表"]
        return
    outcome["statements"] = {key: statements.get(key) for key in STATEMENT_OUTCOME_KEYS}
    outcome["gaps"] = list(outcome.get("gaps") or []) + [
        f"[报表抽取] {gap}" for gap in statements.get("gaps", [])
    ]
    if statements.get("fatal"):
        outcome["fatal"] = statements["fatal"]


