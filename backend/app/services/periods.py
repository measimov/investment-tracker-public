"""期间与覆盖约定（纯函数，无 DB/网络）：「哪一期」「恰好上一期」「连续几期」「覆盖了哪些期」
的唯一定义处（#337 PR-2）。

此前格雷厄姆、利润质量、signals 各写一套：年度键用 ``end_date[:4]``（52/53 周财年的年终日会
跨公历年，FY2021 止于 2022-01-01 与 FY2022 撞键，#350），「上一期」有的按列表下一行取（缺年时
拿两年前冒充），「连续」有的只数有数据的行（缺年照样算连续）。约定：

- **财年键** ``fiscal_year_key``：年度行按「期末日 − 14 天」所在年份取键。1 月上旬结账的
  52/53 周财年（FY2020 止于 2021-01-02）归到前一年；1 月底结账（零售业 FY2025 止于
  2025-01-31）、3/6/9/12 月结账的键与期末年份相同——现有数据的键逐字节不变。
- **恰好上一期** ``prior``：同类期间、期末相隔 350–380 天（× 年数，兼容 52/53 周与非日历财年）；
  找不到就是 None，绝不拿更早的一期顶替。
- **连续** ``consecutive_run``：从最新一期往前走，遇到缺期、未知或不满足条件即停，并说明停在哪里。
- **覆盖** ``coverage``：年度序列的起止、期数与中间缺失的年份，供缺口说明使用。
- 未知（None）与零是两回事：谓词返回 None 表示「不知道」，连续序列在这里停，不当作满足或不满足。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TypeVar

# 期末日往前推的天数：52/53 周财年的年终日最多比名义年末晚 6 天（取离 12/31 最近的周六/周五），
# 14 天留足余量；而任何以月末结账的财年（1 月底最早）都不会被推到上一年
FISCAL_YEAR_SHIFT_DAYS = 14
# 「恰好上一年」的期末间隔：52 周 = 364 天、53 周 = 371 天，日历年 365/366 天
PRIOR_MIN_DAYS = 350
PRIOR_MAX_DAYS = 380

T = TypeVar("T")


def parse_period_end(value: Any) -> Optional[date]:
    """``20251231`` / ``2025-12-31`` → date；无法解析 → None。"""
    text = str(value or "").replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


@dataclass(frozen=True)
class PeriodIdentity:
    """期间身份：期末日 + 期间类型（FY/H1/Q1–Q4，行上没写时为 None）。"""

    end: date
    fp: Optional[str]
    annual: bool


def period_identity(row: Dict[str, Any]) -> Optional[PeriodIdentity]:
    """报表行的期间身份。年度行 = ``fp=FY``，或（Tushare 行不带 fp）期末为 12-31。期末日解析
    不了 → None。"""
    end = parse_period_end(row.get("end_date"))
    if end is None:
        return None
    fp = str(row["fp"]) if row.get("fp") else None
    annual = fp == "FY" or str(row.get("end_date") or "").replace("-", "").endswith("1231")
    return PeriodIdentity(end=end, fp=fp, annual=annual)


def fiscal_year_key(end_date: Any, fp: Optional[str] = None) -> Optional[str]:
    """年度行的财年键（字符串年份）；不是年度行或期末日无法解析 → None。

    年度行的判定与此前各处一致：``fp=FY``，或期末为 12-31（A股 Tushare 行不带 fp）。"""
    identity = period_identity({"end_date": end_date, "fp": fp})
    if identity is None or not identity.annual:
        return None
    return str((identity.end - timedelta(days=FISCAL_YEAR_SHIFT_DAYS)).year)


def annual_year_key(row: Dict[str, Any]) -> Optional[str]:
    """报表行 → 财年键（``fiscal_year_key`` 的行版本）。"""
    return fiscal_year_key(row.get("end_date"), row.get("fp"))


def annual_by_year(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """年度行按财年键索引；同一财年取首见（调用方已按最新排序）。"""
    by_year: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        year = annual_year_key(row)
        if year and year not in by_year:
            by_year[year] = row
    return by_year


def prior(
    rows: Sequence[Dict[str, Any]], row: Dict[str, Any], years: int = 1
) -> Optional[Dict[str, Any]]:
    """同类期间（``fp`` 相同）里期末**恰好早 years 年**的那一期；没有就是 None。

    列表的下一行不一定是上一年（抽取缺年、存疑年度被过滤时可能是两年前，PR #333 评审 P2），
    所以按期末间隔判定，不按位置。"""
    end = parse_period_end(row.get("end_date"))
    if end is None:
        return None
    low, high = PRIOR_MIN_DAYS * years, PRIOR_MAX_DAYS * years
    for other in rows:
        if other is row or other.get("fp") != row.get("fp"):
            continue
        other_end = parse_period_end(other.get("end_date"))
        if other_end is not None and low <= (end - other_end).days <= high:
            return other
    return None


def is_prior_period(newer: Dict[str, Any], older: Dict[str, Any]) -> bool:
    """older 恰好是 newer 的上一期（``prior`` 的两两版本）。"""
    return prior([older], newer) is older


@dataclass(frozen=True)
class ConsecutiveRun:
    """``consecutive_run`` 的结果：连续满足条件的期数，停下的原因与停在哪一项。

    stop_reason：``gap``（与上一项不相邻）/ ``unknown``（谓词返回 None）/ ``value``（谓词返回
    False）/ None（走完了全部项）。"""

    count: int
    stop_reason: Optional[str]
    stopped_at: Any = None
    items: tuple = ()


def consecutive_run(
    items: Iterable[T],
    predicate: Callable[[T], Optional[bool]],
    *,
    adjacent: Optional[Callable[[T, T], bool]] = None,
) -> ConsecutiveRun:
    """从最新一项（items 已按新到旧排序）往前数连续满足 predicate 的项。

    predicate 返回 True = 满足、False = 不满足、None = 不知道——不知道不能算进「连续」，也不能
    当成不满足，只能停下并说明原因。adjacent(newer, older) 判定两项是否相邻，默认按报表行的
    ``is_prior_period``；缺期即停，缺期那一年的情况未知。"""
    adjacent = adjacent or is_prior_period
    taken: List[T] = []
    for item in items:
        if taken and not adjacent(taken[-1], item):
            return ConsecutiveRun(len(taken), "gap", item, tuple(taken))
        verdict = predicate(item)
        if verdict is None:
            return ConsecutiveRun(len(taken), "unknown", item, tuple(taken))
        if not verdict:
            return ConsecutiveRun(len(taken), "value", item, tuple(taken))
        taken.append(item)
    return ConsecutiveRun(len(taken), None, None, tuple(taken))


def years_adjacent(newer: str, older: str) -> bool:
    """财年键相邻（``consecutive_run`` 用于财年键序列时的 adjacent）。"""
    return int(newer) - int(older) == 1


def coverage(years: Iterable[str]) -> Dict[str, Any]:
    """财年键序列的覆盖：起止、期数与中间缺失的年份（升序）。空序列 → count=0。"""
    keys = sorted({int(year) for year in years})
    if not keys:
        return {"first": None, "last": None, "count": 0, "missing": []}
    present = set(keys)
    missing = [str(year) for year in range(keys[0], keys[-1] + 1) if year not in present]
    return {"first": str(keys[0]), "last": str(keys[-1]), "count": len(keys), "missing": missing}
