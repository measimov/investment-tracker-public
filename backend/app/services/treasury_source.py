"""美国财政部每日国库券利率（home.treasury.gov，官方、免 key）。

#200：13 周（3M）国库券的息票等价收益率 `ROUND_B1_YIELD_13WK_2`（年化 %）作为美元
无风险利率的参考序列——只采集与展示，不参与本币（CNY）风险指标。

接口：`…/interest-rates/pages/xml?data=daily_treasury_bill_rates&field_tdr_date_value=YYYY`
按年返回 Atom XML，每个 `<entry>` 的 `<m:properties>` 里有 `INDEX_DATE` 与各期限字段；
休市日没有条目。解析层是纯函数（金样 `tests/fixtures/treasury/`）。
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple

import requests

SOURCE = "us-treasury"
BILL_RATES_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
FIELD_13WK = "ROUND_B1_YIELD_13WK_2"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
_FETCH_TIMEOUT = (10, 60)
_MIN_INTERVAL_SECONDS = 1.0


class TreasuryError(RuntimeError):
    """响应不是预期的 Atom XML。"""


def parse_bill_rates(xml_text: str, field: str = FIELD_13WK) -> List[Tuple[date, Decimal]]:
    """Atom XML → [(日期, 年化收益率%)]，按日期升序；该字段为空的条目跳过。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise TreasuryError(f"国库券利率响应不是合法 XML：{exc}") from exc
    if root.tag != f"{{{_NS['atom']}}}feed":
        raise TreasuryError(f"国库券利率响应根节点不是 Atom feed：{root.tag}")
    points = []
    for props in root.iter(f"{{{_NS['m']}}}properties"):
        raw_date = props.findtext("d:INDEX_DATE", default="", namespaces=_NS)
        raw_value = props.findtext(f"d:{field}", default="", namespaces=_NS)
        if not raw_date:
            continue
        try:
            rate_date = date.fromisoformat(raw_date[:10])
        except ValueError as exc:
            raise TreasuryError(f"国库券利率日期无法解析：{raw_date!r}") from exc
        try:
            value = Decimal(raw_value.strip()) if raw_value and raw_value.strip() else None
        except InvalidOperation:
            value = None
        if value is not None:
            points.append((rate_date, value))
    points.sort(key=lambda item: item[0])
    return points


_throttle_lock = threading.Lock()
_last_fetch_at = 0.0


def _throttle() -> None:
    global _last_fetch_at
    with _throttle_lock:
        elapsed = time.monotonic() - _last_fetch_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_fetch_at = time.monotonic()


def fetch_bill_rates(start: date, end: date, field: str = FIELD_13WK) -> List[Tuple[date, Decimal]]:
    """[start, end] 内的国库券利率（按年请求）。网络/结构错误上抛。"""
    points: Dict[date, Decimal] = {}
    for year in range(start.year, end.year + 1):
        _throttle()
        response = requests.get(
            BILL_RATES_URL,
            params={"data": "daily_treasury_bill_rates", "field_tdr_date_value": str(year)},
            headers=_HEADERS,
            timeout=_FETCH_TIMEOUT,
        )
        response.raise_for_status()
        for rate_date, value in parse_bill_rates(response.text, field):
            if start <= rate_date <= end:
                points[rate_date] = value
    return sorted(points.items())
