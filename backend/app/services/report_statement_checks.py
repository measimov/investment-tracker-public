"""港股三张报表抽取行的**确定性校验层**（纯函数，无 DB）。

抽取管线把「定位→映射→取数」做完后，这里回答两个问题：

1. 这一行的数字**自洽**吗——会计恒等式（毛利=收入−成本、总资产=非流动+流动、总负债=流动+
   非流动、权益总额=归母+少数、资产=负债+夹层权益+权益总额、FCF=CFO−|capex|）与合理性
   （总资产>0、流动资产≤总资产、幅度守卫）；
2. 与**外部证据**对得上吗——Yahoo 同一财年的行、下一年报告的比较列（由服务层传入）。

结论落在行上的 `validation` 块：`status` 只有 ok/suspect 两档；`suspect` 当且仅当有
severity=error 的检查不通过。存疑科目列在 `suspect_fields`，读取侧（利润质量/格雷厄姆/
分析输入）用 `scrub_suspect_fields` 置空后由 Yahoo 补缺；UI 仍拿到原值并打标。

**来源优先级与重列（v4）**：比较列来自更晚的报告、且该表表头标注「經重列/重列/重新表述/
Restated」时，差异是公司自己的重列而不是抽取错误——记 info（`reason=comparative_restated`），
**保留原值**（分析用首次披露数，只标注「已重列」）。雅虎差异若能被更晚报告的比较列解释（比较列
与我们一致 → 雅虎口径不同 `yahoo_definition_diff`；比较列带重列标记且与雅虎一致 → 雅虎取的是
重列数 `yahoo_restated`）同样记 info。没有标记的比较列大差异仍判存疑。

**硬失败**（`hard_failures`）是另一层：总资产≤0、流动资产>总资产、总资产小于归母权益一半、
三张表币种冲突——这些不是"某个科目可疑"而是"这行根本不是那份报表"（09926 2020 的
`854,84 3` 拆数字行就是这样被抓住的）。主行硬失败 → 该报告判确定性失败；比较期行硬失败
→ 直接丢弃（相邻年份的报告会再写一次）。

改任何阈值/检查项 bump `STATEMENT_VALIDATION_VERSION`：它与抽取器/prompt 版本**解耦**，
校验规则变了只需 `revalidate_report_statements` 零下载零 LLM 重算，不必重抽 300 份报告。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

# v5：EPS 与雅虎相差 >20% 且更晚报告比较列解释不了 → error（基本与摊薄各一条）
STATEMENT_VALIDATION_VERSION = 5

IDENTITY_REL_TOL = 0.01
CROSS_CHECK_REL_TOL = 0.01
# 与下一年报告比较列的偏差：1-5% 记 info（可能是重述），>5% 才 suspect
COMPARATIVE_INFO_TOL = 0.01
COMPARATIVE_SUSPECT_TOL = 0.05
# 幅度守卫：总资产不可能小于归母权益的一半（拆数字行的总资产会小几个量级）
HARD_MAGNITUDE_RATIO = 0.5
# 「与更晚报告的比较列一致」的容差（雅虎口径差异 / 雅虎取了重列数的判据）
DEFINITION_MATCH_TOL = 0.005
# EPS 与雅虎的比值落在这个区间 = 单位差 100 倍（仙当元），判 error
EPS_CENTS_RATIO_RANGE = (90.0, 110.0)
# EPS 与雅虎相差超过这个比例且更晚报告的比较列解释不了 → error（v5）。20% 容得下雅虎对
# 摊薄/加权股数口径的小差异，容不下附注号（13.0 vs 1.471）这类映射错误
EPS_MISMATCH_TOL = 0.20
EPS_CHECK_FIELDS = ("basic_eps", "diluted_eps")

# 表头的重列标记（生产表头实测的全部变体）：「（重新表述）」02669、「（經重述）」00148/00799/00883、
# 「（經重列）」「经重列」、「（重列）」00728、「（已重述）」02333、「(Restated)」00148；另收「重新呈列」
RESTATED_RE = re.compile(r"重新表述|重述|重列|重新呈列|restated|re-presented", re.I)
_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{5,}")

# 交叉核对科目所属报表（重列标记按表记录）；与 report_statement_prompts.FIELD_KIND 一致
_CHECK_FIELD_KIND = {
    "total_revenue": "income",
    "n_income_attr_p": "income",
    "basic_eps": "income",
    "diluted_eps": "income",
    "total_assets": "balance",
    "n_cashflow_act": "cashflow",
}

# 交叉核对的科目及严重级：归母净利 Yahoo 对部分公司是含少数股东口径，先记 info
CROSS_CHECK_FIELDS: Dict[str, str] = {
    "total_revenue": "error",
    "total_assets": "error",
    "n_cashflow_act": "error",
    "n_income_attr_p": "info",
}

# 派生科目 → 输入科目。行上的 `derived_fields` 记录**实际**由代码推导出来的科目（总资产可能
# 直接抽自报表，那就是独立证据不在此列）；没有元数据的旧行只认 FCF（它始终由代码算出）。
# 输入被清洗后派生值必须一并清空——它的恒等式通过只是因为它由那个错误输入算出来，不是
# 独立证据（PR #207 评审 P1）；可信补缺完成后由 `rederive_fields` 重新推导
DEFAULT_DERIVED_FIELDS: Dict[str, List[str]] = {"free_cashflow": ["n_cashflow_act", "capex"]}
SUM_DERIVED_FIELDS: Dict[str, List[str]] = {
    "total_assets": ["total_nca", "total_cur_assets"],
    "total_liab": ["total_cur_liab", "total_ncl"],
    "total_equity": ["total_hldr_eqy_exc_min_int", "minority_int"],
}

# 参与"清洗"的数值科目（元数据与 validation 自身不在其列）
NUMERIC_FIELDS = frozenset({
    "total_revenue", "cost_of_revenue", "gross_profit", "operating_income", "n_income_attr_p",
    "total_profit", "income_tax", "ebitda", "sga_exp", "int_exp", "basic_eps", "diluted_eps",
    "total_assets", "total_nca", "total_cur_assets", "total_cur_liab", "total_ncl",
    "accounts_receiv", "inventories", "fix_assets", "money_cap", "total_liab",
    "total_hldr_eqy_exc_min_int", "total_equity", "minority_int", "total_debt",
    "n_cashflow_act", "capex", "depr_fa_coga_dpba", "free_cashflow", "mezzanine_equity",
    "lt_borr", "st_borr", "div_paid_owners",
})


def header_restated(header: Sequence[str]) -> bool:
    """表头是否标注比较列已重列。只看纯文本表头行——带金额的数据行（表头截取到第一条金额行
    之后还会带几行数据）里的「重列」是科目名，不是列标注。"""
    return any(
        RESTATED_RE.search(line or "") and not _AMOUNT_RE.search(line or "") for line in header
    )


def _num(row: Dict[str, Any], field: str) -> Optional[float]:
    value = row.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rel_diff(lhs: float, rhs: float) -> float:
    denominator = max(abs(lhs), abs(rhs))
    return abs(lhs - rhs) / denominator if denominator else 0.0


def _identity(
    check_id: str,
    row: Dict[str, Any],
    *,
    lhs_field: str,
    addends: Sequence[str],
    signs: Optional[Sequence[int]] = None,
    severity: str = "error",
    tol: float = IDENTITY_REL_TOL,
    skip_reason: Optional[str] = None,
    allow_lhs_excess: bool = False,
) -> Dict[str, Any]:
    """lhs == Σ sign·addend；任一项缺失 → skipped（缺失不是错误）。

    `allow_lhs_excess`：合计**大于**分项之和不算错——权益总额里除了归母与少数股东还可能有
    永久资本证券/其他权益工具（03900 绿城三期差 16-24%、01133 差 5% 都是这一类，生产实测
    全是假阳性），这种恒等式只能单边校验：合计小于分项之和才是映射错误。"""
    fields = [lhs_field, *addends]
    lhs = _num(row, lhs_field)
    values = [_num(row, field) for field in addends]
    if lhs is None or any(value is None for value in values):
        return {
            "id": check_id, "severity": severity, "status": "skipped", "fields": fields,
            "detail": skip_reason or "科目缺失，未校验",
        }
    signs = list(signs or [1] * len(addends))
    rhs = sum(sign * value for sign, value in zip(signs, values))
    rel = _rel_diff(lhs, rhs)
    status = "ok" if rel <= tol else "suspect"
    detail = "" if status == "ok" else f"{lhs_field}={lhs:.0f} 与分项合计 {rhs:.0f} 相差 {rel:.1%}"
    if status == "suspect" and allow_lhs_excess and lhs > rhs:
        status = "ok"
        detail = f"{lhs_field} 比分项合计多 {rel:.1%}（可能含永久资本证券等其他权益工具，单边校验通过）"
    return {
        "id": check_id, "severity": severity, "status": status, "fields": fields,
        "lhs": lhs, "rhs": rhs, "rel_diff": rel, "tol": tol,
        "detail": detail,
    }


def balance_identity_addends(row: Dict[str, Any]) -> tuple:
    """资产负债恒等式右端：有夹层权益时三项，否则两项（夹层权益缺失 ≠ 科目缺失）。"""
    if _num(row, "mezzanine_equity") is not None:
        return ("total_liab", "mezzanine_equity", "total_equity")
    return ("total_liab", "total_equity")


def identity_checks(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = [
        _identity("gross_profit_identity", row, lhs_field="gross_profit",
                  addends=("total_revenue", "cost_of_revenue"), signs=(1, -1)),
        _identity("total_assets_identity", row, lhs_field="total_assets",
                  addends=("total_nca", "total_cur_assets")),
        _identity("total_liab_identity", row, lhs_field="total_liab",
                  addends=("total_cur_liab", "total_ncl")),
        _identity("total_equity_identity", row, lhs_field="total_equity",
                  addends=("total_hldr_eqy_exc_min_int", "minority_int"), allow_lhs_excess=True),
        # 资产 = 负债 + 夹层权益 + 权益总额（含少数股东）。没抽到权益总额时**不能**拿归母权益
        # 冒充——少数股东权益为负的公司（09926）会假阳性。夹层权益（美国准则口径的可赎回非控制
        # 性权益，09618 2019/2020）既不是负债也不是权益，缺了它恒等式差的正好是这一行
        _identity("balance_sheet_identity", row, lhs_field="total_assets",
                  addends=balance_identity_addends(row),
                  skip_reason="权益总额未知（含少数股东权益），资产恒等式未校验"),
    ]
    cfo, capex, fcf = _num(row, "n_cashflow_act"), _num(row, "capex"), _num(row, "free_cashflow")
    if cfo is None or capex is None or fcf is None:
        checks.append({
            "id": "free_cashflow_identity", "severity": "error", "status": "skipped",
            "fields": ["free_cashflow", "n_cashflow_act", "capex"], "detail": "科目缺失，未校验",
        })
    else:
        expected = cfo - abs(capex)
        rel = _rel_diff(fcf, expected)
        checks.append({
            "id": "free_cashflow_identity", "severity": "error",
            "status": "ok" if rel <= IDENTITY_REL_TOL else "suspect",
            "fields": ["free_cashflow", "n_cashflow_act", "capex"],
            "lhs": fcf, "rhs": expected, "rel_diff": rel, "tol": IDENTITY_REL_TOL,
            "detail": "" if rel <= IDENTITY_REL_TOL else f"自由现金流 {fcf:.0f} ≠ CFO−|capex| {expected:.0f}",
        })
    if capex is not None and capex > 0:
        checks.append({
            "id": "capex_sign", "severity": "info", "status": "suspect", "fields": ["capex"],
            "detail": "capex 为正（报表符号通常为负），已按绝对值计入自由现金流",
        })
    return checks


def sanity_checks(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """合理性：`hard=True` 的项由 `hard_failures` 升级为整行失败。"""
    checks: List[Dict[str, Any]] = []
    total_assets = _num(row, "total_assets")
    equity = _num(row, "total_hldr_eqy_exc_min_int")
    current_assets = _num(row, "total_cur_assets")
    revenue = _num(row, "total_revenue")
    if total_assets is not None:
        checks.append({
            "id": "total_assets_positive", "severity": "error", "hard": True,
            "status": "ok" if total_assets > 0 else "suspect", "fields": ["total_assets"],
            "detail": "" if total_assets > 0 else f"总资产 {total_assets:.0f} ≤ 0",
        })
    if total_assets is not None and current_assets is not None:
        ok = current_assets <= total_assets * (1 + IDENTITY_REL_TOL)
        checks.append({
            "id": "current_assets_within_total", "severity": "error", "hard": True,
            "status": "ok" if ok else "suspect", "fields": ["total_cur_assets", "total_assets"],
            "detail": "" if ok else f"流动资产 {current_assets:.0f} 大于总资产 {total_assets:.0f}",
        })
    if total_assets is not None and equity is not None and equity != 0:
        ok = total_assets >= HARD_MAGNITUDE_RATIO * abs(equity)
        checks.append({
            "id": "assets_magnitude_guard", "severity": "error", "hard": True,
            "status": "ok" if ok else "suspect",
            "fields": ["total_assets", "total_hldr_eqy_exc_min_int"],
            "detail": "" if ok else (
                f"总资产 {total_assets:.0f} 不足归母权益 {equity:.0f} 的一半——像是数字被拆开或错列"
            ),
        })
    if revenue is not None:
        checks.append({
            "id": "revenue_non_negative", "severity": "error",
            "status": "ok" if revenue >= 0 else "suspect", "fields": ["total_revenue"],
            "detail": "" if revenue >= 0 else f"收入 {revenue:.0f} 为负",
        })
    return checks


def currency_check(row: Dict[str, Any]) -> Dict[str, Any]:
    by_kind = row.get("currency_by_kind") or {}
    known = sorted({str(value) for value in by_kind.values() if value})
    if len(known) > 1:
        return {
            "id": "currency_consistency", "severity": "error", "hard": True, "status": "suspect",
            "fields": [], "detail": f"三张表币种不一致: {by_kind}",
        }
    return {
        "id": "currency_consistency", "severity": "error", "status": "ok" if known else "skipped",
        "fields": [], "detail": "" if known else "表头未识别出币种",
    }


def _evidence_later(row: Dict[str, Any], evidence: Dict[str, Any]) -> bool:
    """比较列证据是否来自**更晚**的报告（报告期晚于本行来源报告）。"""
    theirs = str(evidence.get("source_end_date") or "")
    ours = str(row.get("source_end_date") or row.get("end_date") or "")
    return bool(theirs) and theirs > ours


def evidence_restated(row: Dict[str, Any], evidence: Optional[Dict[str, Any]], field: str) -> bool:
    """该科目所在报表的比较列是否被**更晚**的报告在表头标注为重列。"""
    if not evidence:
        return False
    kind = _CHECK_FIELD_KIND.get(field)
    restated = evidence.get("restated_by_kind") or {}
    return bool(kind and restated.get(kind)) and _evidence_later(row, evidence)


def _same_currency(row: Dict[str, Any], other: Optional[Dict[str, Any]]) -> bool:
    if not other:
        return False
    ours, theirs = row.get("currency"), other.get("currency")
    return bool(ours) and bool(theirs) and ours == theirs


def _explain_yahoo_diff(
    row: Dict[str, Any],
    comparative: Optional[Dict[str, Any]],
    field: str,
    ours: float,
    theirs: float,
) -> Optional[Dict[str, str]]:
    """雅虎差异能否被更晚报告的比较列解释 → {reason, detail}；解释不了返回 None。

    - 更晚报告的比较列与我们一致（≤0.5%）：两份官方报告互相印证，是雅虎口径不同（00883 收入
      含/不含某些项目）→ `yahoo_definition_diff`；
    - 更晚报告标注了重列、且它的重列数与雅虎一致：雅虎取的是重列后的数（02669 2024 CFO）
      → `yahoo_restated`，保留首次披露值。"""
    if not comparative or not _same_currency(row, comparative) or not _evidence_later(row, comparative):
        return None
    later = _num(comparative, field)
    if later is None:
        return None
    source = comparative.get("source_period_key")
    rel = _rel_diff(ours, theirs)
    if _rel_diff(ours, later) <= DEFINITION_MATCH_TOL:
        return {
            "reason": "yahoo_definition_diff",
            "detail": f"{field} 与雅虎相差 {rel:.1%}，但与 {source} 比较列一致（雅虎口径不同）",
        }
    if evidence_restated(row, comparative, field) and _rel_diff(later, theirs) <= DEFINITION_MATCH_TOL:
        return {
            "reason": "yahoo_restated",
            "detail": (
                f"{field} 与雅虎相差 {rel:.1%}：{source} 已重列该期（重列数与雅虎一致），"
                "保留首次披露值"
            ),
        }
    return None


def _eps_check(
    row: Dict[str, Any],
    yahoo_row: Dict[str, Any],
    field: str,
    comparative: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """每股盈利与雅虎同财年同科目比对（v5 起基本与摊薄各一条）：

    - 比值≈100：「仙当元」的单位错误（error `eps_unit_100x`，#223）；
    - 差 ≤1%：ok；1%–20%：info（雅虎 EPS 口径与报表不总一致，只记不判）；
    - 差 >20%：error `eps_mismatch`，**除非**更晚报告的比较列能解释（与我们一致 → 雅虎口径不同；
      标注重列且与雅虎一致 → 雅虎取了重列数），此时仍是 info。v4 之前大差异只记 info，附注号
      被当成 EPS（00148 2024「13.0」vs 雅虎 1.471）也照样当官方数送进格雷厄姆。"""
    ours, theirs = _num(row, field), _num(yahoo_row, field)
    if ours is None or theirs is None or not _same_currency(row, yahoo_row):
        return None
    base: Dict[str, Any] = {
        "id": f"yahoo_{field}", "fields": [field], "source": "yahoo_fundamentals",
        "ours": ours, "theirs": theirs,
    }
    if theirs != 0:
        ratio = ours / theirs
        low, high = EPS_CENTS_RATIO_RANGE
        if low <= ratio <= high:
            return {
                **base, "severity": "error", "status": "suspect", "reason": "eps_unit_100x",
                "detail": f"{field} {ours:g} 是雅虎 {theirs:g} 的 {ratio:.0f} 倍，疑似以仙列示未折元",
            }
    rel = _rel_diff(ours, theirs)
    if rel <= CROSS_CHECK_REL_TOL:
        return {**base, "severity": "info", "status": "ok", "rel_diff": rel, "detail": ""}
    if rel <= EPS_MISMATCH_TOL:
        return {
            **base, "severity": "info", "status": "suspect", "rel_diff": rel,
            "detail": f"{field} 与雅虎相差 {rel:.1%}",
        }
    explained = _explain_yahoo_diff(row, comparative, field, ours, theirs)
    if explained:
        return {**base, "severity": "info", "status": "suspect", "rel_diff": rel, **explained}
    return {
        **base, "severity": "error", "status": "suspect", "rel_diff": rel, "reason": "eps_mismatch",
        "detail": f"{field} {ours:g} 与雅虎 {theirs:g} 相差 {rel:.1%}，疑似映射到非每股盈利行",
    }


def cross_check_row(
    row: Dict[str, Any],
    *,
    yahoo_row: Optional[Dict[str, Any]] = None,
    comparative_row: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """与 Yahoo 同期行 / 另一份报告的同期比较列逐科目核对。币种未知或不同 → skipped 并写原因。

    v4：比较列差异且证据报告标注了重列 → info `comparative_restated`；雅虎差异能被更晚报告的
    比较列解释 → info `yahoo_definition_diff` / `yahoo_restated`（见模块 docstring）。"""
    checks: List[Dict[str, Any]] = []
    for source_name, other, (info_tol, suspect_tol) in (
        ("yahoo_fundamentals", yahoo_row, (CROSS_CHECK_REL_TOL, CROSS_CHECK_REL_TOL)),
        ("comparative", comparative_row, (COMPARATIVE_INFO_TOL, COMPARATIVE_SUSPECT_TOL)),
    ):
        if not other:
            continue
        is_yahoo = source_name == "yahoo_fundamentals"
        source = source_name if is_yahoo else other.get("source_period_key")
        ours_currency, theirs_currency = row.get("currency"), other.get("currency")
        for field, severity in CROSS_CHECK_FIELDS.items():
            check_id = f"{'yahoo' if is_yahoo else 'comparative'}_{field}"
            ours, theirs = _num(row, field), _num(other, field)
            if ours is None or theirs is None:
                checks.append({
                    "id": check_id, "severity": severity, "status": "skipped", "fields": [field],
                    "source": source, "detail": "对方或本行缺该科目",
                })
                continue
            if not ours_currency or not theirs_currency or ours_currency != theirs_currency:
                checks.append({
                    "id": check_id, "severity": severity, "status": "skipped", "fields": [field],
                    "source": source,
                    "detail": f"币种未知或不同（{ours_currency} vs {theirs_currency}），不比较",
                })
                continue
            rel = _rel_diff(ours, theirs)
            label = "雅虎" if is_yahoo else f"{source} 比较列"
            check: Dict[str, Any] = {
                "id": check_id, "fields": [field], "source": source,
                "ours": ours, "theirs": theirs, "rel_diff": rel,
            }
            explained = (
                _explain_yahoo_diff(row, comparative_row, field, ours, theirs)
                if is_yahoo and rel > info_tol
                else None
            )
            if rel <= info_tol:
                check.update(status="ok", severity=severity, tol=info_tol, detail="")
            elif not is_yahoo and evidence_restated(row, other, field):
                # 更晚的报告自己标注了重列：差异是公司重列，不是抽取错误——保留原值只标注
                check.update(
                    status="suspect", severity="info", tol=suspect_tol, reason="comparative_restated",
                    detail=f"{field} 与{label}相差 {rel:.1%}（该报告表头标注重列，保留首次披露值）",
                )
            elif explained:
                check.update(status="suspect", severity="info", tol=info_tol, **explained)
            elif rel <= suspect_tol:
                # 比较列 1-5%、没有重列标记：可能是未标注的重述，只记不判
                check.update(
                    status="suspect", severity="info", tol=suspect_tol,
                    detail=f"{field} 与{label}相差 {rel:.1%}",
                )
            else:
                check.update(
                    status="suspect", severity=severity,
                    tol=info_tol if severity == "error" else suspect_tol,
                    detail=f"{field} 与{label}相差 {rel:.1%}",
                )
            checks.append(check)
        if is_yahoo:
            for eps_field in EPS_CHECK_FIELDS:
                eps = _eps_check(row, other, eps_field, comparative_row)
                if eps:
                    checks.append(eps)
    return checks


def hard_failures(row: Dict[str, Any]) -> List[str]:
    """整行不可信的原因列表（空 = 通过）。"""
    reasons = [
        check["detail"]
        for check in [*sanity_checks(row), currency_check(row)]
        if check.get("hard") and check["status"] == "suspect"
    ]
    return reasons


def _suspect_fields(checks: Iterable[Dict[str, Any]]) -> List[str]:
    """存疑科目 = 外部交叉核对不通过的科目 ∪ 恒等式不通过且**没有**正面外部证据的科目。

    正面外部证据（同一科目通过了雅虎/比较列核对，且没有另一份外部证据说它错）只豁免
    **恒等式**的连带指控——恒等式另一端可疑时不清空有独立证据的那一端；外部证据彼此冲突
    （雅虎说对、比较列说错）的科目仍然存疑，且只影响它自己，不扩散到无关科目。"""
    checks = list(checks)
    external_failed: List[str] = []
    for check in checks:
        if check["severity"] != "error" or check["status"] != "suspect" or not check.get("source"):
            continue
        for field in check.get("fields") or []:
            if field not in external_failed:
                external_failed.append(field)
    passed_cross: set = set()
    for check in checks:
        if check["severity"] == "error" and check["status"] == "ok" and check.get("source"):
            passed_cross.update(f for f in (check.get("fields") or []) if f not in external_failed)
    failed = list(external_failed)
    for check in checks:
        if check["severity"] != "error" or check["status"] != "suspect" or check.get("source"):
            continue
        for field in check.get("fields") or []:
            if field in passed_cross or field in failed:
                continue
            failed.append(field)
    return failed


def _row_level_failure(checks: Iterable[Dict[str, Any]]) -> bool:
    """整行不可信（币种冲突、幅度守卫等 hard 检查不通过）——与"某些科目存疑"是两回事。"""
    return any(check.get("hard") and check["status"] == "suspect" for check in checks)


def validate_period_row(
    row: Dict[str, Any], *, extra_checks: Optional[Sequence[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """行 → validation 块（不修改 row）。extra_checks = 服务层做的交叉核对。"""
    checks = [*identity_checks(row), *sanity_checks(row), currency_check(row), *(extra_checks or [])]
    suspect_fields = _suspect_fields(checks)
    row_level = _row_level_failure(checks)
    # status=suspect 当且仅当有科目要清洗或整行不可信；一条 error 检查不通过但责任科目全部
    # 有正面外部证据时，不留下"存疑却无字段"的空状态
    return {
        "version": STATEMENT_VALIDATION_VERSION,
        "status": "suspect" if (suspect_fields or row_level) else "ok",
        "row_level": row_level,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "suspect_fields": suspect_fields,
        "checks": checks,
    }


def finalize_validation(
    row: Dict[str, Any], *, extra_checks: Optional[Sequence[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """就地写入 validation 块并返回 row。"""
    row["validation"] = validate_period_row(row, extra_checks=extra_checks)
    return row


def validation_current(payload: Dict[str, Any]) -> bool:
    validation = payload.get("validation") or {}
    return int(validation.get("version") or 0) == STATEMENT_VALIDATION_VERSION


def statement_row_usable(payload: Dict[str, Any]) -> bool:
    """当前版本且未存疑（供只想要"可信行"的读者使用；分析路径按科目清洗，不整行剔除）。"""
    from .report_statement_prompts import statement_row_current

    return statement_row_current(payload) and (payload.get("validation") or {}).get("status") != "suspect"


def derived_field_inputs(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    recorded = payload.get("derived_fields")
    if isinstance(recorded, dict) and recorded:
        return {field: list(inputs) for field, inputs in recorded.items()}
    return {field: list(inputs) for field, inputs in DEFAULT_DERIVED_FIELDS.items()}


def scrub_suspect_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """返回副本：存疑科目置 None（由 Yahoo 补缺），**由它们推导出的科目一并置空**。
    整行不可信（`row_level`：币种冲突等 hard 失败）→ 全部数值科目置空；旧版 validation 没有
    row_level 但 `suspect_fields` 为空且存疑 → 同样按整行处理（那是它唯一可能的含义）。"""
    validation = payload.get("validation") or {}
    if validation.get("status") != "suspect":
        return dict(payload)
    fields = list(validation.get("suspect_fields") or [])
    row_level = validation.get("row_level")
    if row_level is None:
        row_level = not fields
    scrubbed = dict(payload)
    if row_level:
        for field in NUMERIC_FIELDS:
            if field in scrubbed:
                scrubbed[field] = None
        return scrubbed
    cleared = set(fields)
    derived = derived_field_inputs(payload)
    changed = True
    while changed:  # 派生链可能多级（总资产 ← 分项；FCF ← CFO），跑到不动点
        changed = False
        for field, inputs in derived.items():
            if field not in cleared and any(inp in cleared for inp in inputs):
                cleared.add(field)
                changed = True
    for field in cleared:
        if field in scrubbed:
            scrubbed[field] = None
    return scrubbed


def rederive_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """就地重新推导被清空的派生科目（输入已由可信来源补齐时）：FCF = CFO − |capex|，
    分项合计按 `derived_fields`/SUM_DERIVED_FIELDS。只填 None 的派生科目，不覆盖已有值。"""
    derived = derived_field_inputs(row)
    for field, inputs in derived.items():
        if row.get(field) is not None:
            continue
        values = [_num(row, inp) for inp in inputs]
        if any(value is None for value in values):
            continue
        if field == "free_cashflow":
            row[field] = values[0] - abs(values[1])
        elif field in SUM_DERIVED_FIELDS:
            row[field] = sum(values)
    return row


RESTATED_REASONS = frozenset({"comparative_restated", "yahoo_restated"})


def restated_fields(payload: Dict[str, Any]) -> List[str]:
    """被更晚报告重列过的科目（保留首次披露值，只标注「已重列」）：UI 标注与分析输入共用。"""
    fields: List[str] = []
    for check in (payload.get("validation") or {}).get("checks") or []:
        if check.get("reason") in RESTATED_REASONS:
            fields.extend(f for f in check.get("fields") or [] if f not in fields)
    return fields


def validation_summary(payload: Dict[str, Any]) -> str:
    """一句话说明存疑原因（进 gaps / 分析输入 data_gaps）。"""
    validation = payload.get("validation") or {}
    reasons = [
        check.get("detail")
        for check in validation.get("checks") or []
        if check.get("severity") == "error" and check.get("status") == "suspect" and check.get("detail")
    ]
    return "；".join(reasons[:3])
