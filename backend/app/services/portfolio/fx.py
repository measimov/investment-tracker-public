"""日期感知的汇率查找与换算（纯内核，无 DB 依赖）。

记录以鸭子类型传入：任何带 from_currency / to_currency / rate /
effective_date 属性的对象均可（ORM 行或简单命名元组）。
"""

from bisect import bisect_right
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple


class ExchangeRateLookup:
    """In-memory date-aware exchange rate lookup for analytics loops."""

    def __init__(self, records):
        self._rates: Dict[Tuple[str, str], List[Tuple[date, Decimal]]] = defaultdict(list)
        self._dates: Dict[Tuple[str, str], List[date]] = {}

        for record in records:
            key = (record.from_currency, record.to_currency)
            self._rates[key].append((record.effective_date, Decimal(str(record.rate))))

        for key, values in self._rates.items():
            values.sort(key=lambda item: item[0])
            self._dates[key] = [effective_date for effective_date, _ in values]

    def _rate_on_or_before(self, key: Tuple[str, str], effective_date: date) -> Optional[Decimal]:
        values = self._rates.get(key)
        if not values:
            return None

        index = bisect_right(self._dates[key], effective_date) - 1
        if index < 0:
            return None
        return values[index][1]

    def _latest_rate(self, key: Tuple[str, str]) -> Optional[Decimal]:
        values = self._rates.get(key)
        if not values:
            return None
        return values[-1][1]

    def get_rate_on_or_before(
        self,
        from_currency: Optional[str],
        to_currency: str,
        effective_date: date,
    ) -> Optional[Decimal]:
        source = from_currency or to_currency
        if source == to_currency:
            return Decimal("1")

        direct_key = (source, to_currency)
        direct_rate = self._rate_on_or_before(direct_key, effective_date)
        if direct_rate is not None:
            return direct_rate

        reverse_key = (to_currency, source)
        reverse_rate = self._rate_on_or_before(reverse_key, effective_date)
        if reverse_rate and reverse_rate != 0:
            return Decimal("1") / reverse_rate

        latest_direct = self._latest_rate(direct_key)
        if latest_direct is not None:
            return latest_direct

        latest_reverse = self._latest_rate(reverse_key)
        if latest_reverse and latest_reverse != 0:
            return Decimal("1") / latest_reverse

        return None


def convert_on_date(
    amount: Decimal,
    currency: Optional[str],
    effective_date: date,
    rate_lookup: ExchangeRateLookup,
    base_currency: str = "CNY",
) -> Decimal:
    """按指定日期汇率换算到本位币；查不到汇率时原样返回（历史兼容口径）。"""
    rate = rate_lookup.get_rate_on_or_before(currency or base_currency, base_currency, effective_date)
    if rate is None:
        return amount
    return amount * rate
