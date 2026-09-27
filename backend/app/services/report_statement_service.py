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

import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Any, Dict, List, Optional, Sequence, Tuple

import pdfplumber
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .llm_client import (
    LLMClientError,
    LLMNotConfiguredError,
    chat_completion,
    is_output_truncated,
)
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
    EPS_CENTS_RATIO_RANGE,
    EPS_CHECK_FIELDS,
    IDENTITY_REL_TOL,
    STATEMENT_VALIDATION_VERSION,
    cross_check_row,
    finalize_validation,
    hard_failures,
    header_restated,
    validation_current,
    validation_summary,
)
from .report_statement_prompts import (
    DERIVED_SUM_FIELDS,
    EXPENSE_MAGNITUDE_FIELDS,
    FIELD_KIND,
    PER_SHARE_FIELDS,
    STATEMENT_BUILD_VERSION,
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
# 每个标的一行（period_key="current"）：最近一次完整清单计划到的年报/中报份数。
# 09618 这类第二上市公司不在披露易发中期报告——planned_interim=0 让进度说清楚「没有」而不是「缺」
PLAN_DATASET = "report_statement_plan"
PLAN_PERIOD_KEY = "current"
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


# ---------------------------------------------------------------------------- 构建：EPS 单位（#223）

# 每股盈利以「仙」（1/100 元）列示的标记：「基本（港仙）」「每股港仙/人民幣仙」「人民幣分」「cents」
CENTS_RE = re.compile(r"仙|人民幣分|人民币分|\bcents?\b", re.I)
# 映射行之前的邻行只在像每股盈利小标题时才看（「每股盈利（港仙）」下面跟「基本」「攤薄」）；
# 股息行（「擬派末期股息每股 5 港仙」）不是 EPS 的单位
_EPS_CONTEXT_RE = re.compile(r"每股(?:盈利|收益|虧損|亏损)|per\s+share|基本|攤薄|摊薄|basic|diluted", re.I)
_DIVIDEND_RE = re.compile(r"股息|dividend", re.I)
EPS_NEIGHBOR_ROWS = 3
# 隐含股数（归母净利 / 每股盈利）相差在 2 倍内视为同一单位：仙与元差 100 倍，不会混淆
EPS_SHARES_RATIO = 2.0


def per_share_divisor(statement: ParsedStatement, row_ids: Sequence[str]) -> int:
    """损益表里映射到 EPS 的行按「仙」列示时返回 100，否则 1。

    证据：映射行本身的标签/上下文（01023「基本（港仙）」）、紧邻其上的每股盈利小标题行
    （「每股盈利（港仙）」→「基本」「攤薄」），以及表头（「以每股港仙列示」）。"""
    index = {row.row_id: i for i, row in enumerate(statement.rows)}
    texts: List[str] = []
    for row_id in row_ids:
        i = index.get(row_id)
        if i is None:
            continue
        row = statement.rows[i]
        texts.append(row.label)
        texts.extend(row.context)
        for neighbor in statement.rows[max(0, i - EPS_NEIGHBOR_ROWS):i]:
            if _EPS_CONTEXT_RE.search(neighbor.label) and not _DIVIDEND_RE.search(neighbor.label):
                texts.append(neighbor.label)
    texts.extend(line for line in statement.header if not _DIVIDEND_RE.search(line))
    return 100 if any(CENTS_RE.search(text or "") for text in texts) else 1


# ---------------------------------------------------------------------------- 构建：EPS 附注号守卫（build v2）

# 「每股盈利 13」「母公司普通股股權持有人應佔每股盈利 14」「OF THE COMPANY 10」这类小标题行的
# 唯一数值是**附注号**：抽取器 v9 之前币种包裹的「HK$4.889港元」不认，基本/摊薄行整行丢失，
# 模型只能把小标题映射成 EPS（00148/00799/02313/03900/09926 共 33 份报告，basic_eps = 13.0）。
# 判据全部确定性：映射行只有**一个 token**（多列报表里单 token 行不可能同时是本期与比较期）、
# 是 1-2 位不带小数点的整数（附注号形态）、标签不含基本/摊薄；行名或上下文是每股盈利小标题。
# 其后几行里有基本（摊薄）行则改指向它，否则丢弃该科目交给雅虎补缺——宁缺毋滥
EPS_HEADING_RE = re.compile(r"每股(?:盈利|收益|虧損|亏损|（虧損）|（亏损）)|per\s+share|\bEPS\b", re.I)
_EPS_KIND_RE = {
    "basic_eps": re.compile(r"基本|basic", re.I),
    # 「攤簿」是 03900 2017 中报的原文错字
    "diluted_eps": re.compile(r"攤薄|摊薄|攤簿|diluted", re.I),
}
EPS_REDIRECT_ROWS = 4


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _is_eps_note_row(statement: ParsedStatement, row) -> bool:
    if statement.column_count < 2 or len(row.values) != 1:
        return False
    value = row.values[0]
    if value is None or value.as_tuple().exponent != 0 or not 1 <= value <= 99:
        return False
    if any(pattern.search(_squash(row.label)) for pattern in _EPS_KIND_RE.values()):
        return False
    return bool(EPS_HEADING_RE.search(" ".join([row.label, *row.context])))


def _eps_redirect_target(statement: ParsedStatement, index: int, field: str) -> Optional[str]:
    """小标题之后（同一块内，最多 EPS_REDIRECT_ROWS 行）第一条标签或上下文写明该类别、且至少两个
    数值的行。「基本及攤薄」同时满足两类。上下文里的类别只认独占一行的「基本 Basic」提示，
    所以这里看的是 _EPS_KIND_RE 能否在 标签 或 context 中命中。"""
    pattern = _EPS_KIND_RE[field]
    for candidate in statement.rows[index + 1:index + 1 + EPS_REDIRECT_ROWS]:
        if sum(1 for v in candidate.values if v is not None) < 2:
            continue
        texts = [_squash(candidate.label), *(_squash(c) for c in candidate.context)]
        if any(pattern.search(text) for text in texts):
            return candidate.row_id
    return None


def repair_eps_note_mapping(
    income: Optional[ParsedStatement], income_mapping: Dict[str, List[str]]
) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """basic/diluted_eps 映射到附注号小标题 → 改指向其后的基本（摊薄）行或丢弃。
    返回 (修复后的损益表映射, {field: 修复记录})；不需要修复原样返回、记录为空。"""
    if income is None or not income_mapping:
        return income_mapping, {}
    index = {row.row_id: i for i, row in enumerate(income.rows)}
    fixed = dict(income_mapping)
    repairs: Dict[str, Dict[str, Any]] = {}
    for field in ("basic_eps", "diluted_eps"):
        ids = list(income_mapping.get(field) or [])
        if len(ids) != 1 or ids[0] not in index:
            continue
        i = index[ids[0]]
        row = income.rows[i]
        if not _is_eps_note_row(income, row):
            continue
        target = _eps_redirect_target(income, i, field)
        if target:
            fixed[field] = [target]
        else:
            fixed.pop(field, None)
        repairs[field] = {
            "reason": "eps_note_number", "from_row": ids[0], "to_row": target,
            "note_number": str(row.values[0]),
        }
    return fixed, repairs


def effective_mapping(
    located: Dict[str, ParsedStatement], mapping: Dict[str, Dict[str, List[str]]]
) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, Any]]]:
    """存储的 LLM 映射 → 构建时实际使用的映射（确定性修复在这里统一生效：EPS 单位证据与
    会计期行取数看到的是同一份映射）。"""
    income_mapping, repairs = repair_eps_note_mapping(located.get("income"), mapping.get("income") or {})
    if not repairs:
        return mapping, {}
    return {**mapping, "income": income_mapping}, repairs


def _eps_row_ids(mapping: Dict[str, Dict[str, List[str]]]) -> List[str]:
    income = mapping.get("income") or {}
    return list(income.get("basic_eps") or income.get("diluted_eps") or [])


def eps_unit_evidence(
    located: Dict[str, ParsedStatement],
    mapping: Dict[str, Dict[str, List[str]]],
    target: Dict[str, Any],
    *,
    yahoo_eps: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """一份报告的 EPS 单位证据：{period_key, labelled, yahoo_cents, values{会计期: EPS 原文},
    implied_shares}。

    `values` 按会计期（末日|FY/H1）记原文 EPS——相邻报告的本期/比较列是同一个数（2020 年报
    比较列 16.36 = 2019 年报本期 16.36），据此把「仙」沿报告链传播；`implied_shares` = 归母净利
    （放大后）/ 原文 EPS，同一公司各期的量级相同，仙与元差 100 倍。`yahoo_cents`：某个年度列的
    原文 EPS 恰是雅虎同财年 EPS 的约 100 倍（币种一致）——01579 的「以人民幣分列示」是表内纯文本
    行，解析层（v8）没收进上下文，行名只写「基本」，雅虎是唯一的单位证据。"""
    income = located.get("income")
    mapping = effective_mapping(located, mapping)[0]
    row_ids = _eps_row_ids(mapping)
    if income is None or not row_ids:
        return None
    columns = period_columns(income, report_type=target["report_type"], end_date=target["end_date"])
    values: Dict[str, str] = {}
    implied: Optional[float] = None
    yahoo_cents = False
    low, high = EPS_CENTS_RATIO_RANGE
    for col in columns:
        eps = resolve_value(income, row_ids, col.column, scale=False)
        if eps is None or eps == 0:
            continue
        values[f"{col.end_date}|{col.fp}"] = str(eps)
        yahoo = (yahoo_eps or {}).get(col.end_date) if col.fp == "FY" else None
        if yahoo and income.currency and yahoo.get("currency") == income.currency and yahoo.get("basic_eps"):
            yahoo_cents = yahoo_cents or low <= float(eps) / float(yahoo["basic_eps"]) <= high
        if col.is_primary:
            profit = resolve_value(
                income, (mapping.get("income") or {}).get("n_income_attr_p") or [], col.column, scale=True
            )
            if profit is not None and profit != 0:
                implied = abs(float(profit) / float(eps))
    return {
        "period_key": target["period_key"],
        "labelled": per_share_divisor(income, row_ids) == 100,
        "yahoo_cents": yahoo_cents,
        "values": values,
        "implied_shares": implied,
    }


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
        row.period_key: {"currency": (row.payload or {}).get("currency"),
                         "basic_eps": (row.payload or {}).get("basic_eps")}
        for row in rows
        if (row.payload or {}).get("basic_eps")
    }


def _same_decimal(lhs: str, rhs: str) -> bool:
    try:
        return Decimal(lhs) == Decimal(rhs)
    except (InvalidOperation, TypeError):
        return False


def _period_date(period_key: str) -> int:
    try:
        return int(str(period_key)[:8])
    except ValueError:
        return 0


def propagate_eps_units(evidence: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """同一标的各报告的 EPS 单位 → {period_key: {"divisor": 1|100, "basis": ...}}。

    1. 行名/表头写明「仙」→ 100（basis=label）；原文 EPS 是雅虎同财年的约 100 倍 → 100
       （basis=yahoo，01579 这类单位只写在被解析层丢弃的纯文本行里的公司）；
    2. **报告链**：未标注的报告若某会计期的 EPS 原文与已判为仙的报告同一会计期的 EPS 相等（本期
       对比较列，或反之），继承仙（basis=chain）——02669 只有 2020 年起的年报写了「每股港仙」，
       2016-2019 年报靠比较列首尾相接补齐；
    3. **隐含股数**：链接不上的（02669 的中报与年报没有同一会计期）按「归母净利 / EPS」与最近
       一份仙报告的隐含股数比较，在 2 倍内即同一单位（basis=shares）。
    迭代到不再变化；没有任何仙证据的公司全部为 1（不猜）。"""
    items = [e for e in evidence if e and e.get("period_key")]
    units: Dict[str, Dict[str, Any]] = {}
    for e in items:
        if e.get("labelled"):
            units[e["period_key"]] = {"divisor": 100, "basis": "label"}
        elif e.get("yahoo_cents"):
            units[e["period_key"]] = {"divisor": 100, "basis": "yahoo"}
    ordered = sorted(items, key=lambda e: e["period_key"], reverse=True)
    changed = True
    while changed:
        changed = False
        cents = [e for e in ordered if e["period_key"] in units]
        for item in ordered:
            key = item["period_key"]
            if key in units:
                continue
            linked = any(
                period in other["values"] and _same_decimal(value, other["values"][period])
                for other in cents
                for period, value in item["values"].items()
            )
            if linked:
                units[key] = {"divisor": 100, "basis": "chain"}
                changed = True
        if changed:
            continue
        cents = [e for e in ordered if e["period_key"] in units and e.get("implied_shares")]
        for item in ordered:
            key = item["period_key"]
            if key in units or not item.get("implied_shares") or not cents:
                continue
            nearest = min(
                cents,
                key=lambda other: (
                    abs(_period_date(other["period_key"]) - _period_date(key)), other["period_key"]
                ),
            )
            ratio = item["implied_shares"] / nearest["implied_shares"]
            if 1 / EPS_SHARES_RATIO <= ratio <= EPS_SHARES_RATIO:
                units[key] = {"divisor": 100, "basis": "shares"}
                changed = True
    return {
        e["period_key"]: units.get(e["period_key"], {"divisor": 1, "basis": None}) for e in items
    }


# ---------------------------------------------------------------------------- 构建：夹层权益与资产小计

# 美国准则口径的夹层权益（09618 京东：「可轉換可贖回非控制性權益」「夾層權益」）；合计行
# （「負債、夾層權益及權益總額」）不是它
MEZZANINE_RE = re.compile(
    r"夾層|夹层|mezzanine|可贖回非控(?:股|制)|可赎回非控(?:股|制)|redeemable\s+non-?controlling",
    re.I,
)
_TOTAL_LABEL_RE = re.compile(r"總額|总额|總計|总计|合計|合计|總值|总值|\btotal\b", re.I)
# 修复采用门槛：替换后资产负债恒等式须在 0.1% 内闭合（原恒等式容差 1% 不通过才触发）
REPAIR_REL_TOL = 0.001


def mezzanine_row_ids(balance: ParsedStatement) -> List[str]:
    """夹层权益行（按行名确定性识别，不走 LLM）：第一条命中且不是合计行的行。"""
    for row in balance.rows:
        text = " ".join([row.label, *row.context])
        if MEZZANINE_RE.search(text) and not _TOTAL_LABEL_RE.search(row.label):
            return [row.row_id]
    return []


def _rel(lhs: float, rhs: float) -> float:
    denominator = max(abs(lhs), abs(rhs))
    return abs(lhs - rhs) / denominator if denominator else 0.0


def _row_value(row: Dict[str, Any], field: str) -> Optional[float]:
    value = row.get(field)
    return float(value) if isinstance(value, (int, float)) else None


def repair_balance_subtotals(
    row: Dict[str, Any],
    balance: ParsedStatement,
    column: int,
    balance_mapping: Dict[str, List[str]],
) -> Optional[Dict[str, Any]]:
    """资产负债恒等式不成立时，在同一张表的**无标签数值行**里找唯一能让它闭合的资产小计。

    映射把无标签小计行认错是这一类的共同根因：00148 2017 取了不含「分類為待售資產」的流动
    资产小计 r21，正确的是加上待售资产后的 r23；01995 2025H1 把无标签的流动资产小计 r18
    当成了总资产。候选只有两种替换：
    - `total_cur_assets` := 候选值，总资产改为 非流动 + 候选（总资产须是推导值或映射到无标签行）；
    - `total_assets` := 候选值（非流动/流动已知时须与其合计一致）。
    替换后 资产 = 负债 + 夹层权益 + 权益总额 须在 0.1% 内成立；按数值去重后候选**唯一**才采用，
    否则不修、仍判存疑。就地修改 row 并返回修复记录（`repaired_fields` 的条目），不修返回 None。"""
    liab, equity = _row_value(row, "total_liab"), _row_value(row, "total_equity")
    if liab is None or equity is None:
        return None
    target = liab + equity + (_row_value(row, "mezzanine_equity") or 0.0)
    assets = _row_value(row, "total_assets")
    if assets is not None and _rel(assets, target) <= IDENTITY_REL_TOL:
        return None
    by_id = {r.row_id: r for r in balance.rows}
    nca, cur = _row_value(row, "total_nca"), _row_value(row, "total_cur_assets")
    assets_ids = list(balance_mapping.get("total_assets") or [])
    assets_on_labelled_row = any((by_id.get(i) and by_id[i].label.strip()) for i in assets_ids)
    candidates: Dict[Tuple[str, float], str] = {}
    for candidate in balance.rows:
        if candidate.label.strip() or column >= len(candidate.values):
            continue
        raw = candidate.values[column]
        if raw is None:
            continue
        value = float(raw * balance.unit_multiplier)
        if _rel(value, target) <= REPAIR_REL_TOL and (assets is None or value != assets):
            if nca is None or cur is None or _rel(nca + cur, value) <= IDENTITY_REL_TOL:
                candidates.setdefault(("total_assets", value), candidate.row_id)
        if (
            nca is not None
            and not assets_on_labelled_row
            and (cur is None or value != cur)
            and _rel(nca + value, target) <= REPAIR_REL_TOL
        ):
            candidates.setdefault(("total_cur_assets", value), candidate.row_id)
    if len(candidates) != 1:
        return None
    (field, value), to_row = next(iter(candidates.items()))
    repaired: Dict[str, Any] = {}
    derived = row.setdefault("derived_fields", {})
    if field == "total_cur_assets":
        repaired["total_cur_assets"] = {
            "from_row": ",".join(balance_mapping.get("total_cur_assets") or []) or None,
            "to_row": to_row, "from_value": cur, "to_value": value,
        }
        row["total_cur_assets"] = value
        new_assets = nca + value
        repaired["total_assets"] = {
            "from_row": ",".join(assets_ids) or None, "to_row": None,
            "from_value": assets, "to_value": new_assets,
            "derived_from": list(DERIVED_SUM_FIELDS["total_assets"]),
        }
        row["total_assets"] = new_assets
        derived["total_assets"] = list(DERIVED_SUM_FIELDS["total_assets"])
    else:
        repaired["total_assets"] = {
            "from_row": ",".join(assets_ids) or None, "to_row": to_row,
            "from_value": assets, "to_value": value,
        }
        row["total_assets"] = value
        derived.pop("total_assets", None)
    row.setdefault("repaired_fields", {}).update(repaired)
    return repaired


def build_period_rows(
    located: Dict[str, ParsedStatement],
    mapping: Dict[str, Dict[str, List[str]]],
    target: Dict[str, Any],
    *,
    fingerprint: str,
    eps_unit: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """映射 + 解析行 → 各会计期的科目行（本期 is_comparative=False，比较期 True）。

    `eps_unit`：`propagate_eps_units` 给这份报告的 EPS 单位；缺省时只看本报告自己的标注。
    构建步骤（`STATEMENT_BUILD_VERSION` 覆盖的全部口径）：EPS 附注号守卫（映射修复）→ 取数 →
    每股指标按单位折元 → 夹层权益按行名取值 → 分项合计推导 → 资产小计修复 → FCF → 硬失败 → 校验。"""
    mapping, eps_repairs = effective_mapping(located, mapping)
    if eps_unit is None:
        income = located.get("income")
        row_ids = _eps_row_ids(mapping)
        divisor = per_share_divisor(income, row_ids) if income is not None and row_ids else 1
        eps_unit = {"divisor": divisor, "basis": "label" if divisor != 1 else None}
    eps_divisor = int(eps_unit.get("divisor") or 1)
    rows_by_period: Dict[str, Dict[str, Any]] = {}
    balance_column: Dict[str, int] = {}
    for kind, parsed in located.items():
        if not mapping.get(kind):
            continue  # 软必需科目缺失时整张表已被丢弃：不留空来源占位
        columns = period_columns(parsed, report_type=target["report_type"], end_date=target["end_date"])
        restated = header_restated(parsed.header)
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
                "build_version": STATEMENT_BUILD_VERSION,
            })
            row["currency_by_kind"][kind] = parsed.currency
            row["unit_by_kind"][kind] = parsed.unit_multiplier
            row["source_pages"][kind] = [parsed.page_start, parsed.page_end]
            row["source_by_kind"][kind] = {
                "period_key": target["period_key"], "end_date": target["end_date"],
                "report_type": target["report_type"], "ann_date": target.get("ann_date"),
            }
            if not col.is_primary and restated:
                # 这份报告的比较列是重列数：作为证据核对更早报告的本期行时按重列处理（v4）
                row.setdefault("restated_by_kind", {})[kind] = True
            for field, row_ids in mapping.get(kind, {}).items():
                value = resolve_value(
                    parsed, row_ids, col.column, scale=field not in PER_SHARE_FIELDS
                )
                if value is None:
                    continue
                if field in EXPENSE_MAGNITUDE_FIELDS:
                    value = abs(value)
                if field in PER_SHARE_FIELDS and eps_divisor != 1:
                    value = value / eps_divisor
                    row["eps_unit"] = {
                        "source_unit": "cents", "divisor": eps_divisor, "basis": eps_unit.get("basis"),
                    }
                row[field] = _decimal_to_number(value)
            if kind == "income" and eps_repairs:
                # 附注号守卫：映射修复对本表每一列都生效，各会计期行都记一笔（比较期按表合并时随损益表走）
                row.setdefault("repaired_fields", {}).update({
                    field: {**repair, "to_value": row.get(field)} for field, repair in eps_repairs.items()
                })
            if kind == "balance":
                balance_column[key] = col.column
                mezz = resolve_value(parsed, mezzanine_row_ids(parsed), col.column, scale=True)
                if mezz is not None:
                    row["mezzanine_equity"] = _decimal_to_number(mezz)
    rows: List[Dict[str, Any]] = []
    for key, row in rows_by_period.items():
        known = sorted({c for c in row["currency_by_kind"].values() if c})
        row["currency"] = known[0] if len(known) == 1 else None
        # 记下**实际**由代码推导的科目及其输入：清洗输入时派生值一并失效（评审 P1）
        row["derived_fields"] = {}
        for derived, addends in DERIVED_SUM_FIELDS.items():
            if row.get(derived) is None and all(row.get(f) is not None for f in addends):
                row[derived] = sum(row[f] for f in addends)
                row["derived_fields"][derived] = list(addends)
        if key in balance_column and "balance" in located:
            repaired = repair_balance_subtotals(
                row, located["balance"], balance_column[key], mapping.get("balance") or {}
            )
            if repaired:
                logger.info(
                    "资产小计修复 %s %s: %s", target.get("period_key"), key,
                    {f: (r.get("from_row"), r.get("to_row")) for f, r in repaired.items()},
                )
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


# 行上按表归属的构建元数据：比较期按表合并时随该表的来源一起取舍。`repaired_fields` 的条目
# 按科目所属报表逐条取舍（资产小计修复属资产负债表、EPS 附注号守卫属损益表）
_KIND_META = {"eps_unit": "income"}


def _merge_repaired_fields(
    merged: Dict[str, Any], incoming: Dict[str, Any], kind: str
) -> None:
    repaired = {
        field: item for field, item in (merged.get("repaired_fields") or {}).items()
        if FIELD_KIND.get(field) != kind
    }
    repaired.update({
        field: item for field, item in (incoming.get("repaired_fields") or {}).items()
        if FIELD_KIND.get(field) == kind
    })
    if repaired:
        merged["repaired_fields"] = repaired
    else:
        merged.pop("repaired_fields", None)


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
    merged_restated = dict(existing.get("restated_by_kind") or {})
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
            # 按表的构建元数据随该表的来源走：重列标记、EPS 单位（损益表）、小计修复（资产负债表）
            if (incoming.get("restated_by_kind") or {}).get(kind):
                merged_restated[kind] = True
            else:
                merged_restated.pop(kind, None)
            for meta, meta_kind in _KIND_META.items():
                if meta_kind != kind:
                    continue
                if incoming.get(meta):
                    merged[meta] = incoming[meta]
                else:
                    merged.pop(meta, None)
            _merge_repaired_fields(merged, incoming, kind)
    merged["source_by_kind"] = merged_sources
    merged["source_pages"] = merged_pages
    if merged_restated:
        merged["restated_by_kind"] = merged_restated
    else:
        merged.pop("restated_by_kind", None)
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
    merged["build_version"] = STATEMENT_BUILD_VERSION
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
    # 每股盈利也存下（v5）：雅虎 EPS 大差异要靠更晚报告的比较列判断是口径不同还是映射错误
    for field in (*CROSS_CHECK_FIELDS, *EPS_CHECK_FIELDS):
        if comparative.get(field) is not None:
            evidence[field] = comparative[field]
    restated = {kind: True for kind, flag in (comparative.get("restated_by_kind") or {}).items() if flag}
    if restated:
        # 该报告表头标注了重列：v4 据此把差异判为 info（revalidate 重放时同样可用）
        evidence["restated_by_kind"] = restated
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
        and int(payload.get("extractor_version") or 1) == STATEMENT_EXTRACTOR_VERSION
        and int(payload.get("prompt_version") or 1) == STATEMENT_PROMPT_VERSION
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
                _located_of(payload), payload["mapping"], _extract_target(row.period_key, payload),
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
        "rebuilt": 0, "failed": 0, "suspect_periods": [], "eps_cents_reports": 0,
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
            or int(payload.get("build_version") or 0) != STATEMENT_BUILD_VERSION
            or int(payload.get("eps_divisor") or 1) != divisor
        )

    for period_key in sorted((pk for pk in usable if stale(pk)), key=plan_order_key, reverse=True):
        payload, located, target = usable[period_key]
        unit = units.get(period_key) or {"divisor": 1, "basis": None}
        savepoint = db.begin_nested()
        try:
            period_rows = build_period_rows(
                located, payload["mapping"], target,
                fingerprint=str(payload.get("source_fingerprint") or ""), eps_unit=unit,
            )
            written, suspect_periods = _write_period_rows(db, symbol, market, period_rows)
            _upsert(db, symbol, market, EXTRACT_DATASET, period_key, {
                **payload,
                "build_version": STATEMENT_BUILD_VERSION,
                "eps_divisor": int(unit.get("divisor") or 1),
                "eps_unit_basis": unit.get("basis"),
                "periods_written": written,
                "suspect_periods": suspect_periods,
                "rebuilt_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮其他报告
            savepoint.rollback()
            logger.warning("报表重建失败 %s %s: %s", symbol, period_key, str(exc)[:200])
            _upsert(db, symbol, market, EXTRACT_DATASET, period_key, {
                **payload,
                "status": "failed",
                "error": f"重建失败: {exc}"[:300],
                # 已抽取成功过：给一次 LLM 重映射的机会（attempts 满 MAX_ATTEMPTS 才封顶）
                "attempts": 1,
            })
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


def _extract_row_current(payload: Dict[str, Any], fingerprint: str) -> bool:
    return (
        payload.get("source_fingerprint") == fingerprint
        and int(payload.get("extractor_version") or 1) == STATEMENT_EXTRACTOR_VERSION
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
        "total": 0, "completed": 0, "generated": 0, "attempted": 0, "failed": 0,
        "remaining": 0, "pending_periods": [], "gaps": [], "permanently_failed": 0,
        "plan_incomplete": False, "fatal": None, "suspect": 0, "suspect_periods": [],
        "rebuilt": 0, "mapping_reused": 0,
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
    else:
        # 只记完整清单：部分失败时某类计 0 份会被误读成「公司不发中报」
        _upsert(db, symbol, market, PLAN_DATASET, PLAN_PERIOD_KEY, {
            "planned_annual": sum(1 for t in targets if t["report_type"] == "annual"),
            "planned_interim": sum(1 for t in targets if t["report_type"] == "interim"),
            "period_keys": [t["period_key"] for t in targets],
            "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        db.commit()
    # 构建逻辑升版（或 EPS 单位证据变化）的存量抽取行：零下载零 LLM 重建，不占 max_new
    _merge_rebuild(result, _safe_rebuild(db, symbol, market))

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
        # 抽取器升版而报告本身没变（同一源指纹、已成功、prompt 为当前版本）：重下载重定位后若解析
        # 结果与存量**逐字节相同**，沿用已存映射不再调用 LLM——抽取器修复通常只影响少数版式，
        # 其余报告重跑 LLM 只会引入映射抖动（已被交叉核对验证过的映射被随机改写）与成本
        prior = (
            payload
            if payload
            and not reusable
            and payload.get("status") == "ok"
            and payload.get("source_fingerprint") == fingerprint
            and int(payload.get("prompt_version") or 1) == STATEMENT_PROMPT_VERSION
            and payload.get("statements")
            and isinstance(payload.get("mapping"), dict)
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
                    "usage": {"prompt_tokens": prior.get("prompt_tokens"),
                              "completion_tokens": prior.get("completion_tokens")},
                }
                result["mapping_reused"] += 1
            else:
                prior = None
                completion = chat_completion(
                    build_statement_messages(
                        symbol=symbol, market=market, report_type=target["report_type"],
                        end_date=target["end_date"], statements=_prompt_statements(located, target),
                    ),
                    # 大报表（03900 三份）推理会吃穿复盘报告的 16384 额度：报表映射单独配额
                    max_tokens=settings.statement_max_output_tokens,
                    response_format={"type": "json_object"},
                )
                mapping, unresolved = parse_statement_mapping(
                    completion["content"],
                    {kind: [r.row_id for r in parsed.rows] for kind, parsed in located.items()},
                    labels={kind: [r.label for r in parsed.rows] for kind, parsed in located.items()},
                )
            # EPS 单位：本报告的证据 + 库里其他报告的证据一起传播（链与隐含股数跨报告）
            evidence = _stored_eps_evidence(db, symbol, market, exclude=period_key)
            own = eps_unit_evidence(
                located, mapping, target, yahoo_eps=_yahoo_eps_map(db, symbol, market)
            )
            if own:
                evidence.append(own)
            eps_unit = propagate_eps_units(evidence).get(period_key) or {"divisor": 1, "basis": None}
            period_rows = build_period_rows(
                located, mapping, target, fingerprint=fingerprint, eps_unit=eps_unit
            )
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
                    int(prior.get("mapping_from_extractor") or prior.get("extractor_version") or 1)
                    if prior is not None else STATEMENT_EXTRACTOR_VERSION
                ),
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
            # 推理吃光输出额度的空/半截输出（finish_reason=length）重试也一样：确定性失败，两次封顶，
            # 不再每轮占用配额（_is_transient 现已同样判定；此处显式保留，不依赖摘要侧的实现）
            transient = _is_transient(exc) and not _output_exhausted(exc)
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
                # 定位成功但映射失败时保留结构化行（本轮新下载定位的也存——此前只存复用来的，
                # 首次 LLM 失败后重试必须重下 PDF，披露易不稳时代价大），重试不必重下载
                "statements": {kind: p.to_payload() for kind, p in (located or reusable or {}).items()},
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
    row = _load_row(db, symbol, market, PLAN_DATASET, PLAN_PERIOD_KEY)
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
    same_type = [
        key.partition("|")[0] for key in planned if key.partition("|")[2] == report_type
    ]
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


