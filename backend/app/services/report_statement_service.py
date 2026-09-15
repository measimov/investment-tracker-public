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
from typing import Any, Dict, List, Optional

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
from .report_statement_prompts import (
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
    for month, day in ((6, "30"), (9, "30"), (12, "31"), (3, "31")):
        months_before = (ann.year - year) * 12 + (ann.month - month)
        if 1 <= months_before <= 4:
            return f"{year}{month:02d}{day}"
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


def _extract_pages(pdf_bytes: bytes) -> List[str]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def _locate(pages: List[str], target: Dict[str, Any]) -> Dict[str, ParsedStatement]:
    found = locate_statements(pages, report_type=target["report_type"])
    located = {kind: parsed for kind, parsed in found.items() if parsed is not None}
    missing = [kind for kind in ("income", "balance") if kind not in located]
    if missing:
        raise ValueError(f"未能定位报表: {'、'.join(missing)}")
    for kind, parsed in located.items():
        if not years_consistent(parsed, end_date=target["end_date"]):
            raise ValueError(
                f"{kind} 表头年份 {parsed.years} 与报告期 {target['end_date']} 不符"
            )
    return located


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
                "extractor_version": STATEMENT_EXTRACTOR_VERSION,
                "prompt_version": STATEMENT_PROMPT_VERSION,
            })
            row["currency"] = row["currency"] or parsed.currency
            row["source_pages"][kind] = [parsed.page_start, parsed.page_end]
            row["source_by_kind"][kind] = {
                "period_key": target["period_key"], "end_date": target["end_date"],
                "report_type": target["report_type"],
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
    for row in rows_by_period.values():
        cfo, capex = row.get("n_cashflow_act"), row.get("capex")
        if cfo is not None and capex is not None:
            row["free_cashflow"] = cfo + capex  # capex 按报表符号为负
    return list(rows_by_period.values())


def _source_rank(source: Optional[Dict[str, Any]]) -> tuple:
    """来源报告的新旧：报告期越新越优；同期年报优于中报。"""
    if not source:
        return ("", 0)
    return (str(source.get("end_date") or ""), 1 if source.get("report_type") == "annual" else 0)


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
    return merged


def _write_period_rows(db: Session, symbol: str, market: str, rows: List[Dict[str, Any]]) -> List[str]:
    written: List[str] = []
    for row in rows:
        period_key = f"{row['end_date']}|{row['fp']}"
        payload = row
        if row["is_comparative"]:
            existing = _load_row(db, symbol, market, STATEMENT_DATASET, period_key)
            payload = merge_comparative_row(existing.payload if existing else None, row)
            if payload is None:
                continue
        _upsert(db, symbol, market, STATEMENT_DATASET, period_key, payload)
        written.append(period_key)
    return written


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
        "plan_incomplete": False, "fatal": None,
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
            written = _write_period_rows(db, symbol, market, period_rows)
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
    """详情页展示：已抽取报告数、覆盖的会计期、失败/封顶数。零外呼。"""
    extracts = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == EXTRACT_DATASET,
        )
        .all()
    )
    # 旧版本的 ok 行不算完成（重算前不可用），与 ensure_report_statements 的判缺口径一致
    ok = [
        row for row in extracts
        if (row.payload or {}).get("status") == "ok" and statement_row_current(row.payload or {})
    ]
    failed = [row for row in extracts if row not in ok]
    periods = load_report_statement_rows(db, symbol, market, limit=60)
    annual = sorted({p["end_date"] for p in periods if p.get("fp") == "FY"}, reverse=True)
    interim = sorted({p["end_date"] for p in periods if p.get("fp") == "H1"}, reverse=True)
    return {
        "reports_ok": len(ok),
        "reports_failed": len(failed),
        "reports_capped": sum(
            1 for row in failed if int((row.payload or {}).get("attempts") or 0) >= MAX_ATTEMPTS
        ),
        "annual_periods": annual,
        "interim_periods": interim,
        "as_of": date.today().isoformat(),
    }
