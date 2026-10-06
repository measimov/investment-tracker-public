"""财报摘要的数字核对（纯函数，#289）：摘要里的营业收入、归母净利与同期报表行比较。

摘要是 LLM 对章节原文的转述，评测里出现过分部金额单位错 10 倍、收入与报表不符。这里不判摘要对错、不改缓存摘要，给出
**可复核的标记**并隔离矛盾摘要的分析输入：分析输入据此注明「该期摘要数字与报表不符，该期摘要隔离、数字以报表为准」。

- 摘要侧取模型按约定填的「核心财务」（`营业收入` / `归母净利润`，形如「68.66 亿元」）；
- 报表侧由调用方传入同期的数值与币种（A股 Tushare 利润表、港股 PDF 报表行、美股 EDGAR 透视）；
- 币种写明且与报表不同 → 不比（本层没有汇率）；读不出数值 → 不比。

核对在**读取时**按当前报表行现算（report_digest_service.load_report_digests），不落库：报表常在
摘要之后才抽到或被修正（分析 job、每周刷新都是先摘要后报表），存下来的结论会过时。

容差同时看两样：摘要按两位小数的亿元写（v3 prompt），**舍入本身**就有 ±0.005 亿元的误差——
60 万元写成「0.01 亿元」是对的；另有 `DIGEST_QA_REL_TOL` 的相对容差吸收口径细差。
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple

DIGEST_QA_VERSION = 3
# 摘要数字与报表的相对差超过它记 mismatch（口径差、四舍五入之外的偏差）
DIGEST_QA_REL_TOL = 0.02
# 比值落在 10^k 的 ±15% 以内（k ≥ 1）记 magnitude：单位换算错
MAGNITUDE_BAND = 0.15
QA_FIELDS = {"营业收入": "total_revenue", "归母净利润": "n_income_attr_p"}

_UNIT_MULTIPLIERS: Tuple[Tuple[str, float], ...] = (
    ("万亿", 1e12),
    ("亿", 1e8),
    ("百万", 1e6),
    ("千万", 1e7),
    ("万", 1e4),
    ("千", 1e3),
    ("billion", 1e9),
    ("million", 1e6),
    ("thousand", 1e3),
)
_CURRENCY_WORDS: Tuple[Tuple[str, str], ...] = (
    ("港元", "HKD"),
    ("港币", "HKD"),
    ("港幣", "HKD"),
    ("HK$", "HKD"),
    ("HKD", "HKD"),
    ("美元", "USD"),
    ("US$", "USD"),
    ("USD", "USD"),
    ("人民币", "CNY"),
    ("人民幣", "CNY"),
    ("RMB", "CNY"),
    ("CNY", "CNY"),
)
_NUMBER_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")


def parse_amount(text: Any) -> Optional[Tuple[float, Optional[str], float]]:
    """只取带金额单位的数值，跳过年份/百分比；多个金额必须是同一数值的单位换写。

    未披露后的说明数字不是该科目金额；无法确定口径时返回 None，绝不当作核对通过。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    compact = re.sub(r"\s+", "", text).translate(str.maketrans({"億": "亿", "萬": "万"}))
    if any(word in compact for word in ("原文未提及", "未明确", "未明確", "未标明", "未標明")):
        return None
    amounts = []
    for found in _NUMBER_RE.finditer(compact):
        if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", found.group(1)):
            return None
        if found.start() and compact[found.start() - 1] in ".,":
            return None  # 不把坏小数的尾段当成另一个金额
        digits = found.group(1).replace(",", "")
        tail = compact[found.end() :].lstrip(")）").lower()
        multiplier = None
        for word, factor in _UNIT_MULTIPLIERS:
            if tail.startswith(word.lower()):
                multiplier = factor
                break
        if multiplier is None and re.match(r"(?:元|港元|美元|港币|港幣)", tail):
            multiplier = 1.0
        if multiplier is None:
            continue
        value = float(digits)
        if not math.isfinite(value):
            return None
        prefix = compact[: found.start()]
        accounting_parens = prefix.endswith(("(", "（")) and (
            compact[found.end() :].startswith((")", "）"))
            or (prefix in ("(", "（") and compact.endswith((")", "）")))
        )
        if accounting_parens or re.search(r"(?:亏损|虧損)[^；;。]*$", prefix):
            value = -abs(value)
        decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
        amounts.append((value * multiplier, 0.5 * 10.0 ** (-decimals) * multiplier))
    if not amounts:
        return None
    value, rounding = amounts[0]
    if any(abs(other - value) > max(rounding, tolerance) for other, tolerance in amounts[1:]):
        return None  # 当期/上期、分部合计等多金额，不能猜哪个是该科目
    currencies = {code for word, code in _CURRENCY_WORDS if word.lower() in compact.lower()}
    if len(currencies) > 1:
        return None
    return value, next(iter(currencies), None), rounding


def comparable_core_amount(label: str, text: Any):
    """核心科目只与同口径金额比较；调整利润和分部收入仍可留在正文中但不冒充合并科目。"""
    if isinstance(text, str):
        if label == "归母净利润" and re.search(
            r"经调整|經調整|扣非|基本[纯純]利|扣除.*[减減]值|不包括非经常|不包括非經常|非[归歸]母",
            text,
        ):
            return None
        if label == "营业收入" and re.search(r"油[气氣][销銷]售收入|分部[收收益]", text):
            return None
    return parse_amount(text)


def digest_for_llm(entry: Dict[str, Any]) -> Dict[str, Any]:
    """摘要和商业画像共用的输入门禁。矛盾摘要整期隔离，避免正文重复的错数继续传播。

    不修改缓存或接口返回的源摘要，不用报表数字擅自改写摘要。没有可比科目也不表示通过。
    """
    qa = entry.get("qa") or {}
    checked = list(qa.get("checked") or [])
    flags = qa.get("flags") or []
    digest = {} if flags else dict(entry.get("digest") or {})
    # 旧摘要中的“未设风险章节”也是 AI 推断，不能当成报告结构的事实继续复制（#386）。
    # 只改送模型的覆盖描述，保留相邻的具体风险、金额和原始缓存。
    risk = digest.get("风险要点")
    if isinstance(risk, str):
        clauses = re.split(r"([。；;，,:：\n])", risk)
        for index, clause in enumerate(clauses):
            if re.search(
                r"(?:未|没有|沒有|无|無)[^。；;，,:：\n]{0,30}(?:风险|風險)"
                r"[^。；;，,:：\n]{0,15}(?:章节|章節)",
                clause,
            ):
                clauses[index] = "所提供节选的风险章节覆盖未核验，不能据此判断年报是否设有风险章节"
            elif re.fullmatch(r"未披露(?:主要|具体|具體)?(?:风险|風險)(?:因素|要点|要點)", clause):
                clauses[index] = "所提供节选未列出具体风险因素"
        digest["风险要点"] = "".join(clauses)
    result = {
        "digest": digest,
        "numeric_qa": {
            "checked": checked,
            "unverified": [label for label in QA_FIELDS if label not in checked],
            "quarantined": bool(flags),
        },
    }
    note = qa_note(qa)
    if note:
        result["数字核对"] = note + "；该期摘要已隔离，不能引用其中的正文或数字"
    return result


def _magnitude_off(ratio: float) -> Optional[int]:
    """比值是否接近 10 的整数次幂（|k| ≥ 1）：返回 k，否则 None。"""
    if ratio <= 0:
        return None
    k = round(math.log10(ratio))
    if k == 0:
        return None
    return k if abs(ratio / (10**k) - 1) <= MAGNITUDE_BAND else None


def check_digest_numbers(
    digest: Dict[str, Any],
    statement: Optional[Dict[str, Any]],
    *,
    statement_currency: Optional[str],
) -> Dict[str, Any]:
    """摘要「核心财务」对报表行 → {version, checked:[...], flags:[...]}。

    flags 每条 {field, digest, statement, reason: magnitude|mismatch, detail}；checked 记下实际
    比较了哪些科目（没比的不进 checked，便于区分「核对通过」与「没法核对」）。"""
    result: Dict[str, Any] = {"version": DIGEST_QA_VERSION, "checked": [], "flags": []}
    core = digest.get("核心财务") if isinstance(digest, dict) else None
    if not isinstance(core, dict) or not statement:
        return result
    for label, field in QA_FIELDS.items():
        parsed = comparable_core_amount(label, core.get(label))
        reference = statement.get(field)
        if parsed is None or reference is None:
            continue
        value, currency, rounding = parsed
        if currency and statement_currency and currency != statement_currency:
            continue
        try:
            reference = float(reference)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(reference):
            continue
        result["checked"].append(label)
        tolerance = max(rounding, abs(reference) * DIGEST_QA_REL_TOL)
        if abs(value - reference) <= tolerance:
            continue  # 舍入或口径细差之内（含「0.00 亿元」对应一个很小的非零数）
        magnitude = None
        if value and reference and (value > 0) == (reference > 0):
            magnitude = _magnitude_off(value / reference)
        if magnitude is not None:
            result["flags"].append(
                {
                    "field": label,
                    "digest": core.get(label),
                    "statement": reference,
                    "reason": "magnitude",
                    "detail": f"摘要的{label}是报表的 10^{magnitude} 倍，疑似单位换算错误",
                }
            )
            continue
        if value and reference and (value > 0) != (reference > 0):
            diff = "符号相反"
        else:
            diff = f"相差 {abs(value / reference - 1):.1%}" if reference else "不一致"
        result["flags"].append(
            {
                "field": label,
                "digest": core.get(label),
                "statement": reference,
                "reason": "mismatch",
                "detail": f"摘要的{label}与报表{diff}",
            }
        )
    return result


def qa_note(qa: Optional[Dict[str, Any]]) -> Optional[str]:
    """分析输入里的一句话提示；没有标记返回 None。"""
    flags: List[Dict[str, Any]] = (qa or {}).get("flags") or []
    if not flags:
        return None
    return "；".join(flag["detail"] for flag in flags) + "——该期数字以报表为准"
