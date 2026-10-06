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
映射不合约定、推理耗尽输出额度的空输出）计 attempts，MAX_ATTEMPTS 后封顶跳过；无 Key /
401-403 / 429 为 fatal。

**构建层**（`STATEMENT_BUILD_VERSION`）：「映射 → 会计期行」这一段——EPS 单位（仙→元，跨报告
传播）、夹层权益、资产小计修复、重列标记——只依赖已存的抽取行，所以构建逻辑升版走
`rebuild_report_statements`：零下载、零 LLM，按计划顺序从抽取行重建。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import local_today
from ..models.security_profile import SecurityProfileData
from .background_job_store import JobOwnershipLostError
from .llm_client import (
    LLMClientError,
    LLMNotConfiguredError,
    chat_completion,
    is_output_truncated,
)
from .payload_versions import stored_version, versions_current
from .hk_report_catalog import hk_report_targets
from .report_digest_service import MAX_ATTEMPTS, is_transient_error, source_fingerprint
from .report_fetchers import download_report_pdf
from .report_pdf_text import extract_pages as _extract_pages
from .report_statement_build import (
    attach_comparative_evidence,
    build_period_rows,
    eps_unit_evidence,
    merge_comparative_row,
    propagate_eps_units,
)
from .profile_store import load_profile_row, upsert_profile_row
from .report_statement_checks import (
    STATEMENT_VALIDATION_VERSION,
    cross_check_row,
    finalize_validation,
    validation_current,
    validation_summary,
)
from .report_statement_prompts import (
    STATEMENT_BUILD_VERSION,
    STATEMENT_PROMPT_VERSION,
    build_statement_messages,
    is_revenue_label,
    parse_statement_mapping,
    statement_row_current,
)
from .report_statements import (
    STATEMENT_EXTRACTOR_VERSION,
    ParsedStatement,
    locate_statements,
    period_columns,
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
# 每个标的一行（period_key="current"）：最近一次完整清单计划到的年报/中报份数。
# 09618 这类第二上市公司不在披露易发中期报告——planned_interim=0 让进度说清楚「没有」而不是「缺」
PLAN_DATASET = "report_statement_plan"
PLAN_PERIOD_KEY = "current"
# 港股报表币种缺失时不猜（HKD/CNY/USD 都常见）：留 None，由消费方按缺失处理
# ---------------------------------------------------------------------------- 目标规划


def plan_statement_targets(symbol: str, market: str) -> Dict[str, Any]:
    """年报 + 中报目标（期末倒序、同期年报在前）：{"targets", "complete", "failed_kinds"}。
    清单与选取规则见 hk_report_catalog（与财报摘要同一份）。"""
    if market not in STATEMENT_MARKETS:
        return {"targets": [], "complete": True, "failed_kinds": []}
    targets: List[Dict[str, Any]] = []
    failed: List[str] = []
    for report_type, years in (("annual", ANNUAL_YEARS), ("interim", INTERIM_YEARS)):
        try:
            targets.extend(hk_report_targets(symbol, report_type, years))
        except Exception as exc:  # noqa: BLE001 - 清单失败记 failed_kinds，另一类继续
            logger.warning("披露易 %s 清单获取失败 %s: %s", report_type, symbol, str(exc)[:150])
            failed.append(report_type)
    targets.sort(key=lambda t: (t["end_date"], t["report_type"] == "annual"), reverse=True)
    return {"targets": targets, "complete": not failed, "failed_kinds": failed}


# ---------------------------------------------------------------------------- 抽取与映射


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
                    "报表期末以表头为准 %s: 清单猜测 %s → 表头 %s",
                    target.get("title"),
                    guessed,
                    detected,
                )
            return detected
    return guessed


def _prompt_statements(
    located: Dict[str, ParsedStatement], target: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    payload: Dict[str, Dict[str, Any]] = {}
    for kind, parsed in located.items():
        columns = period_columns(
            parsed, report_type=target["report_type"], end_date=target["end_date"]
        )
        payload[kind] = {
            "title": parsed.title,
            "unit_multiplier": parsed.unit_multiplier,
            "currency": parsed.currency,
            "columns": [f"{c.end_date} {c.fp}" for c in columns],
            "rows": statement_rows_for_prompt(parsed, columns=[c.column for c in columns]),
        }
    return payload


def _yahoo_eps_map(db: Session, symbol: str, market: str) -> Dict[str, Dict[str, Any]]:
    """雅虎年度行的 EPS（键 = 裸 end_date）：EPS 单位的外部证据。"""
    rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "yahoo_fundamentals",
        )
        .all()
    )
    return {
        row.period_key: {
            "currency": (row.payload or {}).get("currency"),
            "basic_eps": (row.payload or {}).get("basic_eps"),
        }
        for row in rows
        if (row.payload or {}).get("basic_eps")
    }


def _yahoo_row(
    db: Session, symbol: str, market: str, row: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """同财年的 Yahoo 行（键是裸 end_date，没有 |FY 后缀——两侧键形不同，只在内存里按
    (end_date, fp) 对齐，见 earnings_quality.merge_hk_statement_rows）。中报没有 Yahoo 对照。"""
    if row.get("fp") != "FY":
        return None
    found = load_profile_row(db, symbol, market, "yahoo_fundamentals", str(row["end_date"]))
    return found.payload if found else None


def _cross_checks(
    db: Session,
    symbol: str,
    market: str,
    row: Dict[str, Any],
    *,
    comparative: Optional[Dict[str, Any]] = None,
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
        existing = load_profile_row(db, symbol, market, STATEMENT_DATASET, period_key)
        existing_payload = existing.payload if existing else None
        if row["is_comparative"]:
            payload = merge_comparative_row(existing_payload, row)
            if payload is None:
                if (
                    existing_payload
                    and not existing_payload.get("is_comparative")
                    and statement_row_current(existing_payload)
                ):
                    primary = dict(existing_payload)
                    finalize_validation(
                        primary,
                        extra_checks=_cross_checks(db, symbol, market, primary, comparative=row),
                    )
                    upsert_profile_row(db, symbol, market, STATEMENT_DATASET, period_key, primary)
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
                payload,
                extra_checks=_cross_checks(db, symbol, market, payload, comparative=comparative),
            )
        upsert_profile_row(db, symbol, market, STATEMENT_DATASET, period_key, payload)
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
        upsert_profile_row(db, symbol, market, STATEMENT_DATASET, row.period_key, refreshed)
        revalidated += 1
        if refreshed["validation"]["status"] == "suspect":
            suspect += 1
    if revalidated:
        db.commit()
    return {"revalidated": revalidated, "suspect": suspect}


def _extract_target(period_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """抽取行 → build_period_rows 的 target（end_date 是抽取时按表头确定的实际期末）。"""
    return {
        "period_key": period_key,
        "report_type": payload.get("report_type"),
        "end_date": payload.get("end_date"),
        "title": payload.get("title"),
        "ann_date": payload.get("ann_date"),
        "url": payload.get("source_url"),
    }


def _rebuildable(payload: Dict[str, Any]) -> bool:
    """成功、抽取器与 prompt 均为当前版本、且存有结构化行与映射的抽取行才能零 LLM 重建。"""
    return (
        payload.get("status") == "ok"
        and versions_current(
            payload,
            extractor_version=STATEMENT_EXTRACTOR_VERSION,
            prompt_version=STATEMENT_PROMPT_VERSION,
        )
        and bool(payload.get("statements"))
        and isinstance(payload.get("mapping"), dict)
        and bool(payload.get("report_type"))
        and bool(payload.get("end_date"))
    )


def _located_of(payload: Dict[str, Any]) -> Dict[str, ParsedStatement]:
    return {
        kind: ParsedStatement.from_payload(item)
        for kind, item in (payload.get("statements") or {}).items()
    }


def plan_order_key(period_key: str) -> tuple:
    """`plan_statement_targets` 的处理顺序（期末倒序、同期年报先于中报）的排序键——重建按同一
    顺序处理，比较列合并与交叉核对证据才与首次抽取时一致（按 reverse=True 排序）。"""
    end_date, _, report_type = str(period_key).partition("|")
    return (end_date, report_type == "annual")


def _stored_eps_evidence(
    db: Session, symbol: str, market: str, *, exclude: Optional[str] = None
) -> List[Dict[str, Any]]:
    """库里全部可重建抽取行的 EPS 单位证据（纯读取，零外呼）。"""
    evidence: List[Dict[str, Any]] = []
    yahoo_eps = _yahoo_eps_map(db, symbol, market)
    for row in _load_extract_rows(db, symbol, market):
        payload = row.payload or {}
        if row.period_key == exclude or not _rebuildable(payload):
            continue
        try:
            item = eps_unit_evidence(
                _located_of(payload),
                payload["mapping"],
                _extract_target(row.period_key, payload),
                yahoo_eps=yahoo_eps,
            )
        except Exception as exc:  # noqa: BLE001 - 单份坏载荷不影响其他报告的单位判定
            logger.warning("EPS 单位证据读取失败 %s %s: %s", symbol, row.period_key, str(exc)[:150])
            continue
        if item:
            evidence.append(item)
    return evidence


def _load_extract_rows(db: Session, symbol: str, market: str) -> List[SecurityProfileData]:
    return (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == EXTRACT_DATASET,
        )
        .all()
    )


def _finish(db: Session, *, commit: bool) -> None:
    if commit:
        db.commit()
    else:
        # 演练模式：写入留在未提交事务里；Core upsert 不刷新 ORM 身份映射，读前必须 expire
        db.flush()
        db.expire_all()


def rebuild_report_statements(
    db: Session, symbol: str, market: str, *, force: bool = False, commit: bool = True
) -> Dict[str, Any]:
    """从已存抽取行（结构化行 + 映射）**零下载、零 LLM** 重建会计期科目行。

    对象：status=ok、抽取器与 prompt 为当前版本，且构建版本过期（`STATEMENT_BUILD_VERSION`）
    或 EPS 单位判定变了（新报告带来的「仙」证据沿报告链传播到旧报告）的抽取行；`force` 全部重建。
    按 `plan_statement_targets` 的顺序处理（期末倒序、年报先于中报），比较列合并与证据确定。
    单份重建失败（构建期硬失败）记 status=failed、attempts=1：下次 ensure 允许一次 LLM 重映射。
    `commit=False` 为演练：写入只 flush，由调用方回滚。

    返回 {rebuilt, failed, suspect_periods, eps_cents_reports, repaired_periods}。"""
    result: Dict[str, Any] = {
        "rebuilt": 0,
        "failed": 0,
        "suspect_periods": [],
        "eps_cents_reports": 0,
        "repaired_periods": [],
    }
    if market not in STATEMENT_MARKETS:
        return result
    usable: Dict[str, Tuple[Dict[str, Any], Dict[str, ParsedStatement], Dict[str, Any]]] = {}
    for row in _load_extract_rows(db, symbol, market):
        payload = dict(row.payload or {})
        if not _rebuildable(payload):
            continue
        try:
            located = _located_of(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽取行载荷无法解析 %s %s: %s", symbol, row.period_key, str(exc)[:150])
            continue
        usable[row.period_key] = (payload, located, _extract_target(row.period_key, payload))
    if not usable:
        return result
    evidence = []
    yahoo_eps = _yahoo_eps_map(db, symbol, market)
    for payload, located, target in usable.values():
        item = eps_unit_evidence(located, payload["mapping"], target, yahoo_eps=yahoo_eps)
        if item:
            evidence.append(item)
    units = propagate_eps_units(evidence)
    result["eps_cents_reports"] = sum(1 for unit in units.values() if unit["divisor"] != 1)

    def stale(period_key: str) -> bool:
        payload = usable[period_key][0]
        divisor = int((units.get(period_key) or {}).get("divisor") or 1)
        return (
            force
            or not versions_current(payload, build_version=STATEMENT_BUILD_VERSION)
            or int(payload.get("eps_divisor") or 1) != divisor
        )

    for period_key in sorted((pk for pk in usable if stale(pk)), key=plan_order_key, reverse=True):
        payload, located, target = usable[period_key]
        unit = units.get(period_key) or {"divisor": 1, "basis": None}
        savepoint = db.begin_nested()
        try:
            period_rows = build_period_rows(
                located,
                payload["mapping"],
                target,
                fingerprint=str(payload.get("source_fingerprint") or ""),
                eps_unit=unit,
            )
            written, suspect_periods = _write_period_rows(db, symbol, market, period_rows)
            upsert_profile_row(
                db,
                symbol,
                market,
                EXTRACT_DATASET,
                period_key,
                {
                    **payload,
                    "build_version": STATEMENT_BUILD_VERSION,
                    "eps_divisor": int(unit.get("divisor") or 1),
                    "eps_unit_basis": unit.get("basis"),
                    "periods_written": written,
                    "suspect_periods": suspect_periods,
                    "rebuilt_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮其他报告
            savepoint.rollback()
            logger.warning("报表重建失败 %s %s: %s", symbol, period_key, str(exc)[:200])
            upsert_profile_row(
                db,
                symbol,
                market,
                EXTRACT_DATASET,
                period_key,
                {
                    **payload,
                    "status": "failed",
                    "error": f"重建失败: {exc}"[:300],
                    # 已抽取成功过：给一次 LLM 重映射的机会（attempts 满 MAX_ATTEMPTS 才封顶）
                    "attempts": 1,
                },
            )
            _finish(db, commit=commit)
            result["failed"] += 1
            continue
        _finish(db, commit=commit)
        result["rebuilt"] += 1
        for period in suspect_periods:
            if period not in result["suspect_periods"]:
                result["suspect_periods"].append(period)
        for built in period_rows:
            if built.get("repaired_fields"):
                result["repaired_periods"].append(f"{built['end_date']}|{built['fp']}")
    return result


def statement_capped(payload: Dict[str, Any]) -> bool:
    """抽取记录是否已封顶（永久跳过）：失败次数达上限，且那些失败发生在**当前** prompt 版本下
    （#343-1：prompt 升版就是为了修映射契约问题，旧版本下的失败不能继续挡住重试）。"""
    return (
        int(payload.get("attempts") or 0) >= MAX_ATTEMPTS
        and payload.get("status") != "ok"
        and versions_current(payload, prompt_version=STATEMENT_PROMPT_VERSION)
    )


def no_revenue_line_stale(payload: Dict[str, Any]) -> bool:
    """按「表内无收入行」接受的旧映射，在当前收入行判据下表内其实有收入行（#342-3：09618「總收入」）
    ——不能沿用，须重新映射。只看存下的行标签，零下载零 LLM 即可判定。"""
    if "income.total_revenue:no_revenue_line" not in (payload.get("unresolved") or []):
        return False
    rows = ((payload.get("statements") or {}).get("income") or {}).get("rows") or []
    return any(is_revenue_label(row.get("label") or "") for row in rows)


def _extract_row_current(payload: Dict[str, Any], fingerprint: str) -> bool:
    return payload.get("source_fingerprint") == fingerprint and versions_current(
        payload, extractor_version=STATEMENT_EXTRACTOR_VERSION
    )


def _output_exhausted(exc: Exception) -> bool:
    """LLM 输出额度耗尽（finish_reason=length，content 为空或半截）：同样的输入重试结果相同。"""
    return is_output_truncated(exc)


def _safe_rebuild(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    try:
        return rebuild_report_statements(db, symbol, market)
    except Exception as exc:  # noqa: BLE001 - 重建失败不影响抽取
        db.rollback()
        logger.warning("报表重建失败 %s %s: %s", symbol, market, str(exc)[:200])
        return None


def _merge_rebuild(result: Dict[str, Any], rebuilt: Optional[Dict[str, Any]]) -> None:
    if not rebuilt:
        return
    result["rebuilt"] = result.get("rebuilt", 0) + rebuilt["rebuilt"]
    for period in rebuilt["suspect_periods"]:
        if period not in result["suspect_periods"]:
            result["suspect_periods"].append(period)
    result["suspect"] = len(result["suspect_periods"])
    if rebuilt["failed"]:
        result["gaps"].append(f"{rebuilt['failed']} 份报告按新构建口径重建失败，待重新映射")


def ensure_report_statements(
    db: Session, symbol: str, market: str, *, max_new: int
) -> Dict[str, Any]:
    """判缺 → 下载 → 定位 → 映射 → 写行，单次最多处理 max_new 份报告（成本护栏）。
    返回结构与 ensure_report_digests 同形：{total, completed, generated, attempted, failed,
    remaining, pending_periods, gaps, permanently_failed, plan_incomplete, fatal}；另有
    `mapping_reused`：抽取器升版后重定位结果与存量逐字节相同、沿用旧映射（零 LLM）的份数。"""
    result: Dict[str, Any] = {
        "total": 0,
        "completed": 0,
        "generated": 0,
        "attempted": 0,
        "failed": 0,
        "remaining": 0,
        "pending_periods": [],
        "gaps": [],
        "permanently_failed": 0,
        "plan_incomplete": False,
        "fatal": None,
        "suspect": 0,
        "suspect_periods": [],
        "rebuilt": 0,
        "mapping_reused": 0,
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
            + "、".join(planned["failed_kinds"])
            + "），报表覆盖范围不可信"
        )
    else:
        # 只记完整清单：部分失败时某类计 0 份会被误读成「公司不发中报」
        upsert_profile_row(
            db,
            symbol,
            market,
            PLAN_DATASET,
            PLAN_PERIOD_KEY,
            {
                "planned_annual": sum(1 for t in targets if t["report_type"] == "annual"),
                "planned_interim": sum(1 for t in targets if t["report_type"] == "interim"),
                "period_keys": [t["period_key"] for t in targets],
                "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        db.commit()
    # 构建逻辑升版（或 EPS 单位证据变化）的存量抽取行：零下载零 LLM 重建，不占 max_new
    _merge_rebuild(result, _safe_rebuild(db, symbol, market))

    for target in targets:
        period_key = target["period_key"]
        fingerprint = source_fingerprint(target)
        row = load_profile_row(db, symbol, market, EXTRACT_DATASET, period_key)
        payload = row.payload if row else None
        attempts = 0
        reusable: Optional[Dict[str, ParsedStatement]] = None
        if payload and _extract_row_current(payload, fingerprint):
            prompt_current = versions_current(payload, prompt_version=STATEMENT_PROMPT_VERSION)
            if payload.get("status") == "ok" and prompt_current:
                result["completed"] += 1
                continue
            # prompt 升版后旧版本下的失败不算数（#343-1）：靠 prompt 升版修复的映射契约问题
            # （缺必需科目、非法 JSON、输出截断）必须能到达已封顶的报告，否则重跑脚本永不收敛
            attempts = int(payload.get("attempts") or 0) if prompt_current else 0
            if statement_capped(payload):
                result["permanently_failed"] += 1
                result["gaps"].append(f"{target['end_date']} 报表抽取失败（已封顶）")
                continue
            if payload.get("statements"):
                # 抽取器版本没变、只是 prompt 变了：重映射无需重下载
                reusable = {
                    kind: ParsedStatement.from_payload(item)
                    for kind, item in (payload.get("statements") or {}).items()
                }

        # 抽取器升版而报告本身没变（同一源指纹、已成功、prompt 为当前版本）：重下载重定位后若解析
        # 结果与存量**逐字节相同**，沿用已存映射不再调用 LLM——抽取器修复通常只影响少数版式，
        # 其余报告重跑 LLM 只会引入映射抖动（已被交叉核对验证过的映射被随机改写）与成本
        prior = (
            payload
            if payload
            and not reusable
            and payload.get("status") == "ok"
            and payload.get("source_fingerprint") == fingerprint
            and versions_current(payload, prompt_version=STATEMENT_PROMPT_VERSION)
            and payload.get("statements")
            and isinstance(payload.get("mapping"), dict)
            and not no_revenue_line_stale(payload)
            else None
        )
        if result["attempted"] >= max_new:
            result["pending_periods"].append(target["end_date"])
            result["remaining"] += 1
            continue

        result["attempted"] += 1
        fetched_bytes = int((payload or {}).get("fetched_pdf_bytes") or 0) if reusable else 0
        located: Optional[Dict[str, ParsedStatement]] = None
        try:
            if reusable:
                located = reusable
            else:
                pdf_bytes = download_report_pdf(target["url"], source="hkexnews")
                fetched_bytes = len(pdf_bytes)
                located = _locate(_extract_pages(pdf_bytes), target)
            # period_key 是清单里这份报告的身份，不变；end_date 改按表头声明的期末日
            target = {**target, "end_date": actual_period_end(located, target)}
            statements_payload = {kind: parsed.to_payload() for kind, parsed in located.items()}
            if prior is not None and statements_payload == prior.get("statements"):
                mapping = prior["mapping"]
                unresolved = list(prior.get("unresolved") or [])
                completion = {
                    "model": prior.get("model"),
                    "usage": {
                        "prompt_tokens": prior.get("prompt_tokens"),
                        "completion_tokens": prior.get("completion_tokens"),
                    },
                }
                if "generation_meta" in prior:
                    completion["generation_meta"] = prior["generation_meta"]
                result["mapping_reused"] += 1
            else:
                prior = None
                completion = chat_completion(
                    build_statement_messages(
                        symbol=symbol,
                        market=market,
                        report_type=target["report_type"],
                        end_date=target["end_date"],
                        statements=_prompt_statements(located, target),
                    ),
                    # 大报表（03900 三份）推理会吃穿复盘报告的 16384 额度：报表映射单独配额
                    max_tokens=settings.statement_max_output_tokens,
                    response_format={"type": "json_object"},
                )
                mapping, unresolved = parse_statement_mapping(
                    completion["content"],
                    {kind: [r.row_id for r in parsed.rows] for kind, parsed in located.items()},
                    labels={
                        kind: [r.label for r in parsed.rows] for kind, parsed in located.items()
                    },
                )
            # EPS 单位：本报告的证据 + 库里其他报告的证据一起传播（链与隐含股数跨报告）
            evidence = _stored_eps_evidence(db, symbol, market, exclude=period_key)
            own = eps_unit_evidence(
                located, mapping, target, yahoo_eps=_yahoo_eps_map(db, symbol, market)
            )
            if own:
                evidence.append(own)
            eps_unit = propagate_eps_units(evidence).get(period_key) or {
                "divisor": 1,
                "basis": None,
            }
            period_rows = build_period_rows(
                located, mapping, target, fingerprint=fingerprint, eps_unit=eps_unit
            )
            written, suspect_periods = _write_period_rows(db, symbol, market, period_rows)
            for period in suspect_periods:
                stored = load_profile_row(db, symbol, market, STATEMENT_DATASET, period)
                reason = validation_summary(stored.payload if stored else {})
                result["gaps"].append(f"{period.split('|')[0]} 报表科目校验存疑（{reason}）")
                if period not in result["suspect_periods"]:
                    result["suspect_periods"].append(period)
            result["suspect"] = len(result["suspect_periods"])
            usage = completion.get("usage", {})
            upsert_profile_row(
                db,
                symbol,
                market,
                EXTRACT_DATASET,
                period_key,
                {
                    "status": "ok",
                    "error": None,
                    "attempts": attempts + 1,
                    "source_fingerprint": fingerprint,
                    "extractor_version": STATEMENT_EXTRACTOR_VERSION,
                    "prompt_version": STATEMENT_PROMPT_VERSION,
                    "build_version": STATEMENT_BUILD_VERSION,
                    "eps_divisor": int(eps_unit.get("divisor") or 1),
                    "eps_unit_basis": eps_unit.get("basis"),
                    "report_type": target["report_type"],
                    "end_date": target["end_date"],
                    "title": target["title"],
                    "ann_date": target["ann_date"],
                    "source_url": target["url"],
                    "statements": statements_payload,
                    "mapping": mapping,
                    "unresolved": unresolved,
                    "periods_written": written,
                    "suspect_periods": suspect_periods,
                    # 沿用旧映射时记下它来自哪个抽取器版本（解析结果逐字节相同才会沿用）
                    "mapping_from_extractor": (
                        int(
                            prior.get("mapping_from_extractor")
                            or stored_version(prior, "extractor_version")
                        )
                        if prior is not None
                        else STATEMENT_EXTRACTOR_VERSION
                    ),
                    "model": completion.get("model"),
                    **(
                        {"generation_meta": completion["generation_meta"]}
                        if "generation_meta" in completion
                        else {}
                    ),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "fetched_pdf_bytes": fetched_bytes,
                },
            )
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
        except JobOwnershipLostError:
            raise
        except Exception as exc:  # noqa: BLE001 - 分类后落库，与摘要管线同语义
            db.rollback()
            # 推理吃光输出额度的空/半截输出（finish_reason=length）重试也一样：确定性失败，两次封顶，
            # 不再每轮占用配额（is_transient_error 现已同样判定；此处显式保留，不依赖摘要侧的实现）
            transient = is_transient_error(exc) and not _output_exhausted(exc)
            logger.warning(
                "报表抽取失败 %s %s（%s）: %s",
                symbol,
                period_key,
                "瞬时" if transient else "确定性",
                str(exc)[:200],
            )
            upsert_profile_row(
                db,
                symbol,
                market,
                EXTRACT_DATASET,
                period_key,
                {
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
                    # 定位成功但映射失败时保留结构化行（本轮新下载定位的也存——此前只存复用来的，
                    # 首次 LLM 失败后重试必须重下 PDF，披露易不稳时代价大），重试不必重下载
                    "statements": {
                        kind: p.to_payload() for kind, p in (located or reusable or {}).items()
                    },
                    "fetched_pdf_bytes": fetched_bytes,
                },
            )
            db.commit()
            result["failed"] += 1
            result["gaps"].append(f"{target['end_date']} 报表抽取失败")
            if isinstance(exc, LLMClientError) and exc.status_code in (401, 402, 403, 429):
                result["fatal"] = {
                    "kind": "llm_auth" if exc.status_code != 429 else "llm_rate_limited",
                    "message": f"LLM 调用失败（HTTP {exc.status_code}）：{str(exc)[:150]}",
                }
                break
    if result["generated"]:
        # 新抽的报告可能带来「仙」证据（报告链/隐含股数），已构建的旧报告 EPS 单位随之改变
        _merge_rebuild(result, _safe_rebuild(db, symbol, market))
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


def load_statement_plan(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """最近一次**完整**清单的计划（`report_statement_plan`）；从未完整规划过返回 None。"""
    row = load_profile_row(db, symbol, market, PLAN_DATASET, PLAN_PERIOD_KEY)
    return (row.payload or None) if row else None


def outside_statement_window(period_key: str, plan: Optional[Dict[str, Any]]) -> bool:
    """这份抽取行是否已滚出 `plan_statement_targets` 的计划（永远不会再被处理）。

    判据是**计划的实际成员**，不是「最新期末 − 10 年」：计划按期末倒序取最近 10 份，报告缺年
    时计划会一路延伸到更早的年份（2025…2017 + 2015），按自然年截断会把仍在计划内的 2015 判到
    窗口外，重跑脚本跳过它、读取侧又因版本不符隐藏它（PR #230 评审 P2）。

    - 没有完整计划（从未规划过 / 清单检索部分失败）→ 保守：一律不算窗口外；
    - 在计划内 → 不算；
    - 不在计划内且**早于同类型全部计划期末** → 窗口外；不在计划内但不早于（清单改名、
      去重被替换等）→ 保守不算，照旧计入待重抽/失败。
    """
    if not plan or not plan.get("period_keys"):
        return False
    planned = set(plan["period_keys"])
    if period_key in planned:
        return False
    end_date, _, report_type = str(period_key).partition("|")
    same_type = [key.partition("|")[0] for key in planned if key.partition("|")[2] == report_type]
    if not same_type or not end_date:
        return False
    return end_date < min(same_type)


def statement_progress(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """详情页「报表抽取」面板：零外呼。

    四类报告：ok（当前版本成功）/ stale（成功但版本过期，待重抽——**不是失败**，此前被算进
    失败数）/ failed（可重试）/ capped（attempts 封顶，永久跳过）；会计期按年报/中报分列，
    存疑期来自科目行的 validation；失败清单带原因，让用户看到"哪份、为什么"。"""
    extracts = _load_extract_rows(db, symbol, market)
    plan = load_statement_plan(db, symbol, market) or {}
    ok: List[SecurityProfileData] = []
    stale: List[SecurityProfileData] = []
    failed: List[SecurityProfileData] = []
    out_of_window: List[SecurityProfileData] = []
    for row in extracts:
        payload = row.payload or {}
        if outside_statement_window(row.period_key, plan):
            # 滚出十年窗口的旧报告永远不会再被计划处理：不算待重抽/失败（01579 2016 中报）
            out_of_window.append(row)
        elif payload.get("status") == "ok":
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
                "capped": statement_capped(row.payload or {}),
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
        "reports_out_of_window": len(out_of_window),
        # 最近一次完整清单计划到的份数（未规划过为 None）：planned_interim=0 = 披露易上没有中报
        "planned_annual": plan.get("planned_annual"),
        "planned_interim": plan.get("planned_interim"),
        "planned_at": plan.get("planned_at"),
        "annual_periods": annual,
        "interim_periods": interim,
        "suspect_periods": suspect_periods,
        "suspect_count": len(suspect_periods),
        "failed_reports": failed_reports,
        "last_extracted_at": max(fetched).isoformat() if fetched else None,
        "validation_version": STATEMENT_VALIDATION_VERSION,
        "as_of": local_today().isoformat(),
    }


STATEMENT_OUTCOME_KEYS = (
    "total",
    "completed",
    "generated",
    "failed",
    "permanently_failed",
    "suspect",
    "remaining",
)


def attach_statement_outcome(
    db: Session,
    symbol: str,
    market: str,
    outcome: Dict[str, Any],
    *,
    max_new: int,
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
    except JobOwnershipLostError:
        raise
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
