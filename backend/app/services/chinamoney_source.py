"""中国货币网（中国外汇交易中心 CFETS，www.chinamoney.com.cn）官方数据源。

人民币汇率中间价（#200）：每个银行间工作日 9:15 发布，官方、免 token、免 Cookie，
作为 CNY 汇率的主源；frankfurter / open.er-api 降为比对与兜底。
SHIBOR（#200）：每个工作日 11:00 发布，3M 期限作为本币（CNY）无风险利率序列。

接口事实（2026-09 实测）：
- `CcprHisNew`（POST 表单）按日期区间返回历史中间价，**单次查询区间须短于一个日历年**
  （end ≥ start + 1 年即拒绝：records 为空、`flagMessage`="只提供一年历史数据查询及下载"；
  2016-01-01~2016-12-31 可以，2025-09-28~2026-09-28 不行），pageSize 可到 500；
- `ShiborHis`（GET）同样的一年限制（拒绝时 `data.message` 是同一句话），一次返回区间全部
  记录（无分页），每条是 `{"ON": "1.3640", …, "3M": "1.4300", "showDateCN": "2026-09-24"}`；
- `currency` 参数用「外币/CNY」写法（`USD/CNY,HKD/CNY,SGD/CNY`），写成 `CNY/SGD` 会被静默丢弃，
  实际生效的币对以响应 `data.searchlist` 为准——`values` 与它逐位对应；
- 节假日不发布（2026-09-25 中秋无记录），因此「今天没有新值」不是失败。

解析层是纯函数（金样 `tests/fixtures/chinamoney/`），IO 层按进程级最小间隔限速。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional

import requests

from .http_source import throttle

SOURCE_CCPR = "cfets-ccpr"
SOURCE_SHIBOR = "cfets-shibor"
SHIBOR_HISTORY_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"
SHIBOR_TERMS = ("ON", "1W", "2W", "1M", "3M", "6M", "9M", "1Y")
CCPR_HISTORY_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
# 本系统需要的外币 → 中间价币对（全部是「外币/CNY」直接报价，无需经 USD 交叉）
CCPR_PAIRS: Dict[str, str] = {"USD": "USD/CNY", "HKD": "HKD/CNY", "SGD": "SGD/CNY"}
# 窗口取 365 天（end = start + 364）：严格短于一个日历年，闰年跨度也不会触线
MAX_QUERY_DAYS = 365
_PAGE_SIZE = 500

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.chinamoney.com.cn/chinese/bkccpr/",
}
_FETCH_TIMEOUT = (10, 30)
_MIN_INTERVAL_SECONDS = 1.0


class ChinamoneyError(RuntimeError):
    """响应结构与预期不符或上游报错——不当作「没有数据」。"""


@dataclass(frozen=True)
class CcprRow:
    rate_date: date
    rates: Dict[str, Decimal]  # 外币代码 → 1 外币 = N CNY


def _to_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "---"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number > 0 else None


def parse_ccpr_history(payload: dict, currencies: Iterable[str] = CCPR_PAIRS) -> List[CcprRow]:
    """`CcprHisNew` 响应 → 按日期升序的中间价行。

    币对位置取自 `data.searchlist`（实际生效的查询币对），不假设与请求顺序一致；
    某日某币对缺值（`---`）时该币种不出现在当天的 rates 里。上游报错
    （`head.rep_code` 非 200）或缺 records 字段抛 ChinamoneyError。
    """
    if not isinstance(payload, dict):
        raise ChinamoneyError("中间价响应不是 JSON 对象")
    head = payload.get("head") or {}
    if str(head.get("rep_code", "200")) != "200":
        raise ChinamoneyError(f"中间价接口报错：{head.get('rep_code')} {head.get('rep_message')}")
    data = payload.get("data") or {}
    records = payload.get("records")
    if records is None:
        raise ChinamoneyError("中间价响应缺少 records")
    flag = str(data.get("flagMessage") or "").strip()
    if flag and not records:
        # 查询区间超过一年等：上游明确拒绝，而不是那段时间没有中间价
        raise ChinamoneyError(f"中间价接口拒绝查询：{flag}")
    searchlist = list(data.get("searchlist") or [])
    wanted = {CCPR_PAIRS[c]: c for c in currencies if c in CCPR_PAIRS}
    positions = {wanted[pair]: index for index, pair in enumerate(searchlist) if pair in wanted}
    if records and not positions:
        raise ChinamoneyError(f"中间价响应不含所需币对：searchlist={searchlist}")

    rows: List[CcprRow] = []
    for record in records:
        try:
            rate_date = date.fromisoformat(str(record.get("date", ""))[:10])
        except ValueError:
            raise ChinamoneyError(f"中间价记录日期无法解析：{record.get('date')!r}")
        values = record.get("values") or []
        rates = {}
        for currency, index in positions.items():
            if index < len(values):
                rate = _to_decimal(values[index])
                if rate is not None:
                    rates[currency] = rate
        if rates:
            rows.append(CcprRow(rate_date=rate_date, rates=rates))
    rows.sort(key=lambda row: row.rate_date)
    return rows


def query_windows(start: date, end: date, max_days: int = MAX_QUERY_DAYS) -> List[tuple]:
    """把 [start, end] 切成不超过 max_days 天的闭区间（上游单次只给一年）。"""
    if start > end:
        return []
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=max_days - 1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _throttle() -> None:
    throttle("chinamoney", _MIN_INTERVAL_SECONDS)


def _post_json(url: str, data: dict) -> dict:
    _throttle()
    response = requests.post(url, data=data, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise ChinamoneyError("中国货币网返回的不是 JSON（可能是挑战页或维护页）") from exc


def fetch_ccpr_history(
    start: date, end: date, currencies: Iterable[str] = CCPR_PAIRS
) -> List[CcprRow]:
    """区间内的人民币汇率中间价（按年切片、逐页取全）。网络/结构错误上抛。"""
    currencies = [c for c in currencies if c in CCPR_PAIRS]
    pair_param = ",".join(CCPR_PAIRS[c] for c in currencies)
    rows: Dict[date, CcprRow] = {}
    for window_start, window_end in query_windows(start, end):
        page = 1
        while True:
            payload = _post_json(
                CCPR_HISTORY_URL,
                {
                    "startDate": window_start.isoformat(),
                    "endDate": window_end.isoformat(),
                    "currency": pair_param,
                    "pageNum": page,
                    "pageSize": _PAGE_SIZE,
                },
            )
            for row in parse_ccpr_history(payload, currencies):
                rows[row.rate_date] = row
            page_total = (payload.get("data") or {}).get("pageTotal") or 1
            if page >= int(page_total):
                break
            page += 1
    return [rows[key] for key in sorted(rows)]


def parse_shibor_history(payload: dict, term: str = "3M") -> List[tuple]:
    """`ShiborHis` 响应 → [(日期, 年化利率%)]，按日期升序；该期限缺值的日期跳过。"""
    if term not in SHIBOR_TERMS:
        raise ValueError(f"未知的 SHIBOR 期限：{term}")
    if not isinstance(payload, dict):
        raise ChinamoneyError("SHIBOR 响应不是 JSON 对象")
    head = payload.get("head") or {}
    if str(head.get("rep_code", "200")) != "200":
        raise ChinamoneyError(f"SHIBOR 接口报错：{head.get('rep_code')} {head.get('rep_message')}")
    records = payload.get("records")
    if records is None:
        raise ChinamoneyError("SHIBOR 响应缺少 records")
    message = str((payload.get("data") or {}).get("message") or "").strip()
    if message and not records:
        raise ChinamoneyError(f"SHIBOR 接口拒绝查询：{message}")
    points = []
    for record in records:
        try:
            rate_date = date.fromisoformat(str(record.get("showDateCN", ""))[:10])
        except ValueError:
            raise ChinamoneyError(f"SHIBOR 记录日期无法解析：{record.get('showDateCN')!r}")
        value = _to_decimal(record.get(term))
        if value is not None:
            points.append((rate_date, value))
    points.sort(key=lambda item: item[0])
    return points


def fetch_shibor_history(start: date, end: date, term: str = "3M") -> List[tuple]:
    """区间内某期限的 SHIBOR（按年切片）。网络/结构错误上抛。"""
    points: Dict[date, Decimal] = {}
    for window_start, window_end in query_windows(start, end):
        _throttle()
        response = requests.get(
            SHIBOR_HISTORY_URL,
            params={
                "lang": "CN",
                "startDate": window_start.isoformat(),
                "endDate": window_end.isoformat(),
            },
            headers={**_HEADERS, "Referer": "https://www.chinamoney.com.cn/chinese/bkshibor/"},
            timeout=_FETCH_TIMEOUT,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChinamoneyError("中国货币网返回的不是 JSON（可能是挑战页或维护页）") from exc
        for rate_date, value in parse_shibor_history(payload, term):
            points[rate_date] = value
    return sorted(points.items())
