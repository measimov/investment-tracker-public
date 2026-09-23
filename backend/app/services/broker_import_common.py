"""三家券商导入器共享的解析与去重底座。

HASH-CRITICAL：`normalize_hash_value` / `calculate_row_hash` 的任何行为改动都会让
历史已导入流水的 row_hash 漂移，破坏跨批次去重。改动前先看
`tests/test_import_hash_stability.py` 里重构前捕获的黄金摘要。

各导入器保留自己的 HASH_FIELDS 与文本清洗函数（招商用 `strip_bom`），通过
参数传入；语义因券商而异的逻辑（账户掩码校验、行分类谓词、
get_existing_hashes、IBKR 的股息-税匹配）不在此层。`build_import_result`
的**结果骨架**（公共键 + 结算契约默认值 + 展示截断）在此层——分类逻辑
仍留在各导入器，骨架只统一"皮"。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..models.broker_fund_flow import BrokerFundFlow
from ..models.corporate_action import CorporateAction
from .holding_service import UNPERSISTED_SORT_ID

HASH_DUPLICATE_OCCURRENCE_FIELD = "duplicate_occurrence"
STRICT_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)$")
SOURCE_ROW_ERROR_PATTERN = re.compile(r"^row (\d+):")


def strip_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def normalize_hash_value(value: Any, *, strip: Callable[[Any], str] = strip_text) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return strip(value)


def calculate_row_hash(
    values: Dict[str, Any],
    fields: List[str],
    *,
    strip: Callable[[Any], str] = strip_text,
) -> str:
    if values.get(HASH_DUPLICATE_OCCURRENCE_FIELD):
        fields = fields + [HASH_DUPLICATE_OCCURRENCE_FIELD]
    payload = "|".join(normalize_hash_value(values.get(field, ""), strip=strip) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def disambiguated_row_hash(
    values: Dict[str, Any],
    occurrences: Dict[str, int],
    compute: Callable[[Dict[str, Any]], str],
) -> str:
    """同批内 hash 相同的行按「本批第几次出现」消歧（四处共用）。

    真实存在的等值成交（同价同量同日拆单）不是重复行，但天然算出同一个 hash；
    第二次及以后把出现序号并进 hash 输入重算，让它们各自唯一。

    HASH-CRITICAL：算法与调用顺序一字不能变，否则历史已导入流水的去重键漂移。
    `compute` 由各导入器传入自己的 wrapper（字段集与文本清洗规则各不相同，
    招商用 strip_bom、东财 legacy 用另一套 fields）。与原实现一致，命中重复时
    **就地**改写 values。
    """
    base = compute(values)
    occurrences[base] = occurrences.get(base, 0) + 1
    if occurrences[base] == 1:
        return base
    values[HASH_DUPLICATE_OCCURRENCE_FIELD] = occurrences[base]
    return compute(values)


def reject_unassigned_legacy_sources(db: Session, user_id: int, broker: str) -> None:
    """领养路径已退役：NULL 账户历史来源必须显式拒绝，绝不静默双记。

    账户级判重按 (user, broker_account, row_hash) 进行，看不见 NULL 桶的
    旧来源；库约束又允许同一 hash 在 NULL 桶与已分配账户各存一份——若放行，
    重新导入会给同一笔流水再建一份 canonical 记录。重建后的正常数据不存在
    这类行；从旧备份恢复的库必须先人工迁移（含旧 Excel 等异构 hash 来源，
    故按存在性整体拒绝，不做逐 hash 匹配）。
    """
    unassigned = (
        db.query(BrokerFundFlow.id)
        .filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.broker == broker,
            BrokerFundFlow.broker_account_id.is_(None),
        )
        .count()
    )
    if unassigned:
        raise ValueError(
            f"存在 {unassigned} 条未分配账户的{broker}历史来源（领养路径已退役）。"
            "请先人工迁移或清理这些 NULL 账户流水后再导入，否则会重复入账"
        )


def archived_row_count(db: Session, model, batch_id: int) -> int:
    """回滚后重数本批已归档的来源行；查询本身失败时按 0 计。

    三家导入器的异常收尾里逐字相同的那一半。计数失败要吞掉：此刻已经在处理
    另一个异常，再抛一个只会盖掉真正的失败原因，而这个数字只用于批次统计。
    """
    try:
        return db.query(model).filter(model.import_batch_id == batch_id).count()
    except Exception:
        return 0


def parse_strict_decimal(
    value: Any, *, strip: Callable[[Any], str] = strip_text
) -> Optional[Decimal]:
    text = strip(value)
    if not text or not STRICT_DECIMAL_PATTERN.fullmatch(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def source_error_rows(errors: List[str], parsed_source_rows: set[int]) -> set[int]:
    """解析报错但没有产出任何 ParsedFlow 的源行号（skipped 口径对账用）。"""
    return {
        int(match.group(1))
        for error in errors
        if (match := SOURCE_ROW_ERROR_PATTERN.match(error))
        and int(match.group(1)) not in parsed_source_rows
    }


def split_new_and_duplicate_rows(
    rows: Iterable[Any], existing_hashes: set[str]
) -> Tuple[list, list]:
    """判重分拣：库内已有或本批已现过的 row_hash 归重复，首现归新增。

    东财与 IBKR 的结果统计此前各存一份逐行相同的循环。同批内真实等值成交
    已由消歧序号分开（`disambiguated_row_hash`），`seen` 兜的是消歧之外仍然
    相同的 hash。招商现状**没有**同批判重（duplicate 只对库内 hash 判定）——
    那是行为差异，招商不要接到这里来"顺手统一"。
    """
    seen: set[str] = set()
    new_rows: list = []
    duplicate_rows: list = []
    for row in rows:
        if row.row_hash in existing_hashes or row.row_hash in seen:
            duplicate_rows.append(row)
        else:
            new_rows.append(row)
            seen.add(row.row_hash)
    return new_rows, duplicate_rows


def iso_date_range(dates: Sequence[date]) -> Tuple[Optional[str], Optional[str]]:
    """事件日期区间 → (start, end) ISO 字符串；空序列给 (None, None)。"""
    if not dates:
        return None, None
    return min(dates).isoformat(), max(dates).isoformat()


# 展示截断集中一处：样本各 10 条、告警/错误各 50 条。
RESULT_SAMPLE_LIMIT = 10
RESULT_MESSAGE_LIMIT = 50
# 疑似重复清单不是"看几条样本"而是"逐条决定"，上限单独放宽；count 字段仍给真实总数
RESULT_SUSPECTED_SAMPLE_LIMIT = 200


def base_import_result(
    *,
    broker: str,
    filename: str,
    total_rows: int,
    eligible_trade_rows: int,
    eligible_dividend_rows: int,
    eligible_tax_rows: int,
    imported_transactions: int,
    imported_corporate_actions: int,
    imported_tax_adjustments: int,
    imported_cash_events: int,
    duplicate_rows: int,
    skipped_non_trade_rows: int,
    skipped_invalid_rows: int,
    skipped_excluded_rows: int,
    excluded_unbooked_rows: int,
    affected_symbols: int,
    date_start: Optional[str],
    date_end: Optional[str],
    business_counts: Dict[str, int],
    duplicate_samples: List[dict],
    import_samples: List[dict],
    errors: List[str],
    warnings: Optional[List[str]] = None,
    eligible_cash_rows: int = 0,
    skipped_option_rows: int = 0,
    skipped_fx_rows: int = 0,
    skipped_cash_rows: int = 0,
    skipped_unsupported_rows: int = 0,
    skipped_conflict_rows: int = 0,
    expected_archived_rows: int = 0,
    suspected_duplicate_rows: int = 0,
    suspected_duplicate_samples: Optional[List[dict]] = None,
    eligible_opening_position_rows: int = 0,
) -> Dict[str, Any]:
    """三家 build_import_result 共用的结果骨架——分类逻辑留在各导入器，这里只统一"皮"。

    两个契约在此定死：

    1. **结算契约**：`complete_import_batch` 读取的每个键都显式在场（少给的
       券商落默认 0），批次结算按键直取、不再靠 `.get` 默认值调和三种方言；
       `tests/test_broker_import_contract.py` 钉住这层子集关系。
    2. **展示截断**：样本/告警/错误的截断上限只写在这一处。调用方仍应在
       构造样本前先截断行列表（`rows[:RESULT_SAMPLE_LIMIT]`），这里的兜底
       截断只保证上限不因某一家漏截而漂移。

    键集合必须是 `BrokerImportResult` schema 的子集（response_model 会静默
    丢弃多余键），同样由契约测试双向看住。券商特有键（东财账期/IBKR 来源
    覆盖率）由调用方在返回值上 update。
    """
    return {
        "broker": broker,
        "filename": filename,
        "total_rows": total_rows,
        "eligible_trade_rows": eligible_trade_rows,
        "eligible_dividend_rows": eligible_dividend_rows,
        "eligible_tax_rows": eligible_tax_rows,
        "eligible_cash_rows": eligible_cash_rows,
        "imported_transactions": imported_transactions,
        "imported_corporate_actions": imported_corporate_actions,
        "imported_tax_adjustments": imported_tax_adjustments,
        "imported_cash_events": imported_cash_events,
        "duplicate_rows": duplicate_rows,
        "skipped_non_trade_rows": skipped_non_trade_rows,
        "skipped_invalid_rows": skipped_invalid_rows,
        "skipped_option_rows": skipped_option_rows,
        "skipped_fx_rows": skipped_fx_rows,
        "skipped_cash_rows": skipped_cash_rows,
        "skipped_unsupported_rows": skipped_unsupported_rows,
        "skipped_conflict_rows": skipped_conflict_rows,
        "skipped_excluded_rows": skipped_excluded_rows,
        "excluded_unbooked_rows": excluded_unbooked_rows,
        "expected_archived_rows": expected_archived_rows,
        # 疑似重复（#190）：与已入账流水同键但 hash 不同的成交行，归档不入账待人工确认。
        # 样本上限单独给（不是展示用的 10 条，而是用户要逐条决定的清单）。
        "suspected_duplicate_rows": suspected_duplicate_rows,
        "suspected_duplicate_samples": (suspected_duplicate_samples or [])[
            :RESULT_SUSPECTED_SAMPLE_LIMIT
        ],
        # 期初建仓行（转托转入，#174）：入账为 OPENING_POSITION 公司行动
        "eligible_opening_position_rows": eligible_opening_position_rows,
        "affected_symbols": affected_symbols,
        "date_start": date_start,
        "date_end": date_end,
        "business_counts": business_counts,
        "duplicate_samples": duplicate_samples[:RESULT_SAMPLE_LIMIT],
        "import_samples": import_samples[:RESULT_SAMPLE_LIMIT],
        "warnings": (warnings or [])[:RESULT_MESSAGE_LIMIT],
        "errors": errors[:RESULT_MESSAGE_LIMIT],
        # 截断前的真实条数。列表按 RESULT_MESSAGE_LIMIT 截断、前端再截到 8 条；
        # 没有总数的话，"看到 8 条"与"一共 8 条"在界面上完全无法区分——排查会
        # 建立在错误的前提上（#167 的报障正是如此）。
        "warnings_total": len(warnings or []),
        "errors_total": len(errors),
    }


# ---------------------------------------------------------------------------
# 脱敏诊断原语（三家共用；目前只有招商接线）
#
# 报障者把对账单发过来做排查是不现实的——里面是全部持仓与金额。但只说"第 N 行
# 对不上"又完全不可定位：出错行连 ParsedFlow 都不产出，全流程零留痕。
#
# 这里的做法是让诊断报告在**构造上**就不含敏感数值：标签类字段（市场/币种/
# 业务标志）原样保留——它们正是要排查的词表，且本身不敏感；数值一律降级成
# 无量纲比值、数量级桶与字符类模式。脱敏靠"报告里根本没有这个字段"保证，
# 不靠事后过滤。
# ---------------------------------------------------------------------------

def digit_class(value: Any, *, strip: Callable[[Any], str] = strip_text) -> str:
    """把单元格文本降级成字符类模式：ASCII 数字→`d`，非 ASCII 数字→`D`，其余原样。

    `"1,234.56"→"d,ddd.dd"`、`"73.04"→"dd.dd"`、`"深Ｂ"→"深Ｂ"`、`"１２"→"DD"`。

    一个函数同时办两件事：标签列天然不含数字，于是原样透出（要看的就是词表）；
    数字列则暴露千分位、尾缀 `.0`、全角数字，以及**吃进邻词的列错位**
    （`"73.04上海"→"dd.dd上海"` 一眼可见），而不泄露任何数值本身。
    全角数字单列成 `D`：它是解析失败的直接线索，混进 `d` 就看不见了。
    """
    text = strip(value)
    return "".join(
        "d" if "0" <= char <= "9" else ("D" if char.isdigit() else char) for char in text
    )


#: 数值单元格里允许原样透出的非数字字符（都是数字格式的一部分，不承载内容）
VALUE_CLASS_PASSTHROUGH = ".,-+"


def value_class(value: Any, *, strip: Callable[[Any], str] = strip_text) -> str:
    """**不可信数值单元格**的字符类模式：数字与数字标点保留形状，其余按长度类别化。

    `"1,234.56"→"d,ddd.dd"`、`"73.04南银转债"→"dd.dd<X:4>"`、
    `"A123456789"→"<X:1>ddddddddd"`。

    与 `digit_class` 的分工：标签列（市场/币种/业务名称）是券商词表、本身要
    原样读，用 `digit_class`；而数值列一旦发生列错位，挤进来的就是证券名称或
    账号——`digit_class` 会把中文和字母原样抄进报告。类别化后仍看得见"有个
    4 字的非数字内容黏进了价格列"这一诊断信号，但内容不出来。
    """
    text = strip(value)
    out: List[str] = []
    run = 0
    for char in text:
        if "0" <= char <= "9":
            kind = "d"
        elif char.isdigit():
            kind = "D"
        elif char in VALUE_CLASS_PASSTHROUGH:
            kind = char
        else:
            run += 1
            continue
        if run:
            out.append(f"<X:{run}>")
            run = 0
        out.append(kind)
    if run:
        out.append(f"<X:{run}>")
    return "".join(out)


def mask_code(value: Any, *, keep: int = 2, strip: Callable[[Any], str] = strip_text) -> str:
    """证券代码只留前几位：`"113050"→"11****"`、`"00700"→"00***"`、`"F"→"*"`。

    前缀足以区分债券/股票/港股五位码/场外基金——排查要的正是这个粒度；
    完整代码等于报障者的持仓清单，不该出现在报告里。

    **任何非空代码至少遮掉一个字符**：美股 ticker 有 `F`/`T`/`V` 这类一两位的，
    朴素的 `text[:keep]` 会把它们原样透出，直接违背"绝不输出完整代码"。
    """
    text = strip(value)
    if not text:
        return ""
    kept = max(0, min(keep, len(text) - 1))
    return text[:kept] + "*" * (len(text) - kept)


def stable_key(
    value: Any,
    *,
    secret: str,
    label: str,
    length: int = 12,
    strip: Callable[[Any], str] = strip_text,
) -> str:
    """同一部署内跨报告稳定、且外人无法枚举的标识。

    证券代码只有六位、空间极小，截断 sha256 可被离线枚举反推，所以必须带密钥：
    这里用服务端 secret 派生 HMAC。同一部署的两份报告能把同一标的对上，
    而拿到报告的人没有 secret，反推不出代码。
    """
    text = strip(value)
    if not text:
        return ""
    message = f"{label}\x00{text}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:length]


# 已知的 PDF 生成器族。分类而非回传原文：`/Producer` 尚且规矩，`/Creator`
# 常见形如 "Microsoft Word - 张伟对账单.docx" 或带本机用户名，原样回传等于
# 在一份标着"可直接发给维护者"的报告里泄露身份。
# 排查真正需要的只是"是不是换了导出工具"，那是个分类问题。
PDF_GENERATOR_FAMILIES = (
    "libreoffice",
    "openoffice",
    # 本仓四份真实招商对账单的 /Creator 就是裸 "Calc"（LibreOffice Calc）——
    # 基线自己落进 "other" 的话，最常做的那次对照就不可读了
    "calc",
    "microsoft word",
    "microsoft excel",
    "wps",
    "adobe",
    "acrobat",
    "itext",
    "pdfbox",
    "reportlab",
    "tcpdf",
    "fpdf",
    "ghostscript",
    "skia",
    "chromium",
    "quartz",
    "wkhtmltopdf",
    "crystal reports",
    "jasper",
)


def text_fingerprint(
    value: Any, *, length: int = 12, strip: Callable[[Any], str] = strip_text
) -> str:
    """不可逆摘要：让同一来源在多份报告之间可对照，但不回传原文。"""
    text = strip(value)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def classify_generator(value: Any, *, strip: Callable[[Any], str] = strip_text) -> str:
    """PDF 生成器 → 白名单族名；不在白名单里一律 "other"，空值 "absent"。

    白名单是**输出**的唯一来源——绝不把未识别的原文当作"看起来无害"透出去。
    """
    text = strip(value).lower()
    if not text:
        return "absent"
    for family in PDF_GENERATOR_FAMILIES:
        if family in text:
            return family
    return "other"


def safe_extension(
    filename: Any, *, allowed: Sequence[str], strip: Callable[[Any], str] = strip_text
) -> str:
    """受控扩展名枚举；不在名单内一律 "other"（不回传未知后缀原文）。"""
    text = strip(filename).lower()
    _, _, extension = text.rpartition(".")
    extension = f".{extension}" if extension and extension != text else ""
    return extension if extension in allowed else "other"


def digit_run_shape(value: Any, *, strip: Callable[[Any], str] = strip_text) -> List[int]:
    """连续数字段的长度序列：`"电子对账单2502-2509.pdf"` → `[4, 4]`。

    文件名里唯一有排查价值的就是这个结构——年度单是 `[8, 8]`（起止日期），
    自定义区间单是 `[4, 4]`（年月）。非数字部分可能含真实姓名或账户备注，
    一个字都不回传。
    """
    return [len(run) for run in re.findall(r"\d+", strip(value))]


def magnitude(value: Optional[Decimal]) -> Optional[str]:
    """数量级桶：`1234 → "1e3"`、`0 → "0"`、`-0.5 → "1e-1"`。符号另行给出。"""
    if value is None:
        return None
    if value == 0:
        return "0"
    exponent = abs(value).log10().to_integral_value(rounding=ROUND_FLOOR)
    return f"1e{int(exponent)}"


def safe_ratio(
    numerator: Optional[Decimal], denominator: Optional[Decimal], *, places: int = 6
) -> Optional[Decimal]:
    """无量纲比值；分母为 0 或任一侧缺失时返回 None（而不是抛或伪造 0）。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    try:
        return (numerator / denominator).quantize(Decimal(1).scaleb(-places))
    except (InvalidOperation, ZeroDivisionError):
        return None


def label_histogram(rows: Any, keys: List[str], *, strip: Callable[[Any], str] = strip_text):
    """标签列组合的计数表，按次数降序、同次数按标签排序（输出可复现）。

    未知的市场/业务写法在这里一眼可见——本次排查最关键的一块。
    """
    counter: Dict[tuple, int] = {}
    for row in rows:
        combo = tuple(strip(row.get(key)) for key in keys)
        counter[combo] = counter.get(combo, 0) + 1
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [{**dict(zip(keys, combo)), "count": count} for combo, count in ordered]


def find_dividend_for_tax(
    db: Session,
    user_id: int,
    flow: Any,
    market: str,
    broker_account_id: Optional[int] = None,
) -> Optional[CorporateAction]:
    """唯一匹配才归属：同标的/币种、除权日不晚于税项日的现金分红恰好一条时返回。"""
    query = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == flow.security_code,
        CorporateAction.market == market,
        CorporateAction.action_type == "CASH_DIVIDEND",
        CorporateAction.currency == flow.currency,
        CorporateAction.ex_date <= flow.trade_date,
        CorporateAction.broker_account_id == broker_account_id,
    )
    candidates = (
        query.order_by(CorporateAction.ex_date.desc(), CorporateAction.id.desc()).limit(2).all()
    )
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# 未归属红利税行的保留与恢复（三家共用）
#
# 找不到唯一股息的税行不能丢，也不能"归档了事"：招商/东财此前把它无链接归档
# 并把 row_hash 记入判重，于是补齐股息后重导同一对账单会被跳过——tax_withheld
# 永远缺失且无补救通道。IBKR 早有解法，这里把它下沉为三家共用。
#
# 三段式：
#   1. 首次遇到 → 归档并置 skip_reason=UNATTRIBUTED_TAX，**不**计入"已入账"；
#   2. 重导时 → load_unattributed_tax_sources 认出它们（而非当成重复行跳过）；
#   3. 找到唯一股息 → attribute_tax_source 就地转正（同一行补链接、清标记），
#      而不是插新行——两张流水表都有 row_hash 唯一约束。
# ---------------------------------------------------------------------------

UNATTRIBUTED_TAX = "unattributed_tax"
# 存量归档的「转托转入/转存管转入」（#174）：升级后回填此标记，重导时在原行上转正为
# OPENING_POSITION 公司行动，与税行同一套三段式
UNBOOKED_OPENING_POSITION = "unbooked_opening_position"
# 「托管转出」（深圳，真实减仓）本版未建模：归档并告警持仓可能高估，如实 PARTIAL
UNBOOKED_CUSTODY_OUT = "unbooked_custody_out"


def mark_unbooked(source, skip_reason: str, note: str):
    """把新建的归档行标记为"已归档未入账"（调用方负责 db.add）。"""
    source.skip_reason = skip_reason
    source.notes = f"{source.notes or ''}; {note}".strip("; ")
    return source


def attribute_source(source, *, corporate_action_id: int, note: str):
    """就地转正为公司行动：补链接、清标记、留痕。绝不插新行（row_hash 唯一约束）。"""
    source.corporate_action_id = corporate_action_id
    source.skip_reason = None
    source.notes = f"{source.notes or ''}; {note}".strip("; ")
    return source


# ---------------------------------------------------------------------------
# 预览用的"待入账交易"替身（两家共用）
#
# 整批一票否决的持仓校验此前只在 commit 通道跑，预览无对应物：用户拿到干净
# 预览、正式导入却被整批拒绝，"先看 /preview" 的契约在这类失败上失效（#132）。
# 校验本身是纯内存重放，缺的只是"这批还没落库的交易"——用这个替身补上即可，
# 不必让预览变成读写端点。
#
# 字段覆盖三处重放读到的全部属性，因此**一个类型**同时喂得动
# replay_account_quantities（symbol/market/transaction_type/quantity）与内核的
# _replay_events（另加 price/fee/currency/name/broker_account_id/id/
# linked_transaction_id）。分成两种替身就等于又造一次"同一问题不同答案"。
#
# id 恒为 None：_txn_replay_order 会把它换成 UNPERSISTED_SORT_ID 排到同日同
# 类型的最后，正是"本批最新、还没落库"该有的位置。调用方自己拼排序元组时
# （两个导入器的预检）也必须用同一个哨兵，用 0 会把替身排到既有交易**之前**，
# 而正式导入 flush 后拿到的真 id 排在之后——同日多笔卖出时，预览与导入会
# 指向不同的首笔超卖、报出不同余量。
#
# 刻意不用未落库的 Transaction ORM 实例：它一旦被关系级联捎带进 flush 就是
# 真写库，而预览必须是只读的。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProspectiveTransaction:
    symbol: str
    market: str
    transaction_type: str
    quantity: Decimal
    transaction_date: date
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    currency: str = "CNY"
    name: Optional[str] = None
    broker_account_id: Optional[int] = None
    # 招商的同日 tie-break 用流水号/合同号/行号
    serial_number: Optional[str] = None
    contract_number: Optional[str] = None
    source_row_number: int = 0
    id: Optional[int] = None
    linked_transaction_id: Optional[int] = None


@dataclass(frozen=True)
class ProspectiveCorporateAction:
    """本批会建的公司行动替身（预览专用）。

    看着"现金红利不改数量、注入它没意义"，其实必须注入：对账比对的
    `relevant_keys` 是按"本账户拥有任意 CorporateAction"激活证券时间线的，
    而一条被激活的证券若在别的账户上存在分账户重放矛盾，就会落进
    `replay_inconsistent` 把整体判 MISMATCHED。preview 看不到本批红利 →
    忽略该证券 → 判 MATCHED；正式导入先落红利 → 激活 → MISMATCHED 整批回滚。

    id 用与交易替身同一个哨兵：_event_sort_key 直接读 .id，None 会在与真实
    id 比较时抛 TypeError。

    比例/配股字段一律留 None：两个导入器目前只建 CASH_DIVIDEND，但留着字段
    面，将来注入数量类行动时 semantics 的因子函数能原样工作，不会静默算错。
    """

    symbol: str
    market: str
    action_type: str
    ex_date: date
    broker_account_id: Optional[int] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    id: int = UNPERSISTED_SORT_ID
    distribution_ratio: Optional[str] = None
    shares_received: Optional[Decimal] = None
    split_ratio: Optional[str] = None
    new_shares: Optional[Decimal] = None
    subscription_quantity: Optional[Decimal] = None
    subscription_price: Optional[Decimal] = None
    # OPENING_POSITION（#174）：数量 / 单位成本(可选) / 总成本(可选)
    adjusted_quantity: Optional[Decimal] = None
    adjusted_cost_per_share: Optional[Decimal] = None
    cost_basis_adjustment: Optional[Decimal] = None


def load_held_sources(
    db: Session,
    model,
    *,
    user_id: int,
    skip_reason: str,
    unlinked_column,
    hashes=None,
    hash_aliases: Optional[Dict[str, str]] = None,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    """本批"已归档但未入账"（`skip_reason` 指定原因、`unlinked_column` 仍为空）的源行，
    键一律是**当前 row_hash**。未归属税行与疑似重复行共用这一个加载器。

    调用方要把这些 hash 从"重复行"里排除（或按各自规则处理），否则它们永远等不到
    补齐的股息 / 人工确认。

    hash_aliases: {历史/别名 hash → 当前 hash}。东财的判重口径是
    `row_hash OR legacy_row_hash`（老版本算法存档的行仍在库里），只按当前
    hash 找的话会「判重放行 → loader 找不到原行 → 新建一条当前 hash 来源」，
    旧孤儿永久保留、经济来源变成两条。传入别名后，无论归档行存的是哪一种
    hash，返回的键都归一到当前 hash——调用方的循环仍用 flow.row_hash 取值。
    """
    lookup: Dict[str, str] = {}
    for row_hash in hashes or ():
        lookup[row_hash] = row_hash
    for alias, current in (hash_aliases or {}).items():
        lookup.setdefault(alias, current)
    if not lookup:
        return {}
    query = db.query(model).filter(
        model.user_id == user_id,
        model.row_hash.in_(list(lookup)),
        model.skip_reason == skip_reason,
        unlinked_column.is_(None),
    )
    if broker_account_id is not None:
        query = query.filter(model.broker_account_id == broker_account_id)
    return {lookup[row.row_hash]: row for row in query.all()}


def load_unattributed_tax_sources(
    db: Session,
    model,
    *,
    user_id: int,
    hashes=None,
    hash_aliases: Optional[Dict[str, str]] = None,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    """本批"已归档但未归属"的税行（`load_held_sources` 的税行特化）。"""
    return load_held_sources(
        db,
        model,
        user_id=user_id,
        skip_reason=UNATTRIBUTED_TAX,
        unlinked_column=model.corporate_action_id,
        hashes=hashes,
        hash_aliases=hash_aliases,
        broker_account_id=broker_account_id,
    )


def mark_unattributed_tax(source, note: str):
    """把新建的归档行标记为"未归属税行"（调用方负责 db.add）。"""
    source.skip_reason = UNATTRIBUTED_TAX
    source.notes = f"{source.notes or ''}; {note}".strip("; ")
    return source


def attribute_tax_source(source, corporate_action_id: int):
    """就地转正：补链接、清标记、留痕。绝不插新行（row_hash 唯一约束）。"""
    source.corporate_action_id = corporate_action_id
    source.skip_reason = None
    source.notes = (
        f"{source.notes or ''}; attributed during account-scoped re-import"
    ).strip("; ")
    return source


# ---------------------------------------------------------------------------
# 疑似重复守卫（#190）——row_hash 之外的第二层，**不改任何 hash 输入**
#
# 同一笔真实成交在券商新旧导出里成交价精度不同（1.30 vs 1.2980），trade_price 在
# HASH_FIELDS 里，hash 判重失效 → 双份入账。改 HASH_FIELDS 是 HASH-CRITICAL 变更
# （全部历史行换键、必须双跑 parity），代价极大；这里改为：hash 仍是一级键，
# 另按「代码 / 日期 / 方向 / |数量| / 发生金额 / 币种」找已入账的归档行——发生金额
# 是结算精确值，不受展示精度影响，所以同键不同 hash 的差异面恰好就是精度漂移。
#
# 三条纪律：
#   1. **计数而非存在**：同日同价的真实多笔成交是常态（同批判重靠
#      duplicate_occurrence 区分），每键只把"多出已入账行数"的那部分判疑似；
#   2. 已入账一侧只认 `transaction_id IS NOT NULL` 的归档行（"这笔经济成交入过账吗"），
#      归档行有精确 amount 而 Transaction 没有金额列；
#   3. 疑似行归档为 skip_reason=SUSPECTED_DUPLICATE、不入账，沿用未归属税行的
#      三段式（标记 → 重导时加载 → 人工确认后原地转正），绝不插第二条同 hash 行。
# ---------------------------------------------------------------------------

SUSPECTED_DUPLICATE = "suspected_duplicate"

# (证券代码, 成交日期, 方向, |数量|, 发生金额, 币种) —— Decimal 直接参与比较
# （1.30 == 1.3），不要字符串化
BookedTradeKey = Tuple[str, date, str, Decimal, Decimal, str]


@dataclass
class SuspectedDuplicateResolution:
    """`classify_suspected_duplicates` 的结论，预览与导入共用同一份。"""

    held_hashes: set = field(default_factory=set)  # 本批判疑似、要归档不入账的 row_hash
    matches: Dict[str, Any] = field(default_factory=dict)  # row_hash → 配对的已入账归档行
    previously_held: Dict[str, Any] = field(default_factory=dict)  # 上次已归档为疑似的行
    confirmed_hashes: frozenset = frozenset()


def mark_suspected_duplicate(source, note: str):
    """把新建的归档行标记为"疑似重复"（调用方负责 db.add）。"""
    source.skip_reason = SUSPECTED_DUPLICATE
    source.notes = f"{source.notes or ''}; {note}".strip("; ")
    return source


def book_suspected_source(source, transaction_id: int):
    """人工确认为真实成交后就地转正：补交易链接、清标记、留痕。绝不插新行。"""
    source.transaction_id = transaction_id
    source.skip_reason = None
    source.notes = (
        f"{source.notes or ''}; confirmed as a distinct trade during re-import"
    ).strip("; ")
    return source


def classify_suspected_duplicates(
    db: Session,
    model,
    *,
    user_id: int,
    broker_account_id: Optional[int],
    candidates: Sequence[Any],
    key_of_flow: Callable[[Any], BookedTradeKey],
    key_of_source: Callable[[Any], Optional[BookedTradeKey]],
    batch_hashes: set,
    previously_held: Optional[Dict[str, Any]] = None,
    confirmed_hashes: frozenset = frozenset(),
) -> SuspectedDuplicateResolution:
    """按二级键把本批候选行分成"疑似重复（扣住）"与"照常入账"。

    candidates: 会成为交易且 hash 不在库里的解析行（调用方已剔除 hash 命中行）；
    key_of_flow / key_of_source: 解析行 / 归档行 → BookedTradeKey（归档行返回 None 表示
    它不是成交行，不参与配对）；batch_hashes: 本批全部 row_hash——hash 已命中的
    已入账行由本批的重复行"解释"，不再占用额度。
    """
    previously_held = previously_held or {}
    resolution = SuspectedDuplicateResolution(
        previously_held=dict(previously_held), confirmed_hashes=confirmed_hashes
    )
    pool = sorted(
        (
            flow
            for flow in candidates
            if flow.row_hash not in confirmed_hashes and flow.row_hash not in previously_held
        ),
        key=lambda flow: (flow.source_row_number or 0, flow.row_hash),
    )
    if not pool:
        return resolution
    keys = {key_of_flow(flow) for flow in pool}
    query = db.query(model).filter(
        model.user_id == user_id,
        model.transaction_id.isnot(None),
        model.trade_date.in_(sorted({key[1] for key in keys})),
        model.security_code.in_(sorted({key[0] for key in keys})),
    )
    if broker_account_id is not None:
        query = query.filter(model.broker_account_id == broker_account_id)
    else:
        query = query.filter(model.broker_account_id.is_(None))
    existing_by_key: Dict[BookedTradeKey, List[Any]] = {}
    for row in query.order_by(model.trade_date, model.security_code, model.id).all():
        if row.row_hash in batch_hashes:
            continue  # 本批已有同 hash 行与之配对，不再占用额度
        key = key_of_source(row)
        if key is not None and key in keys:
            existing_by_key.setdefault(key, []).append(row)
    consumed: Dict[BookedTradeKey, int] = {}
    for flow in pool:
        key = key_of_flow(flow)
        budget = existing_by_key.get(key, [])
        used = consumed.get(key, 0)
        if used < len(budget):
            resolution.held_hashes.add(flow.row_hash)
            resolution.matches[flow.row_hash] = budget[used]
            consumed[key] = used + 1
    return resolution
