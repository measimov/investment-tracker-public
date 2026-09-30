"""账本市场枚举的唯一定义（#278）。

账本身份键是 (symbol, market)。此前 `VALID_MARKETS` 在两个 schema 里各抄一份，手工交易与
公司行动的 market 只有 max_length——「HK」这样的任意字符串能入库成新的身份键，
`normalize_manual_symbol` 也认不出它。前端 `utils/securities.ts:MARKETS` 与这里对齐。
"""

from typing import Optional

# 手工录入（交易/公司行动/自选/特例规则/标准 CSV 导入）可选的市场，顺序即界面顺序
MANUAL_MARKETS = ("A股", "B股", "港股", "美股", "新加坡股", "加密货币")
MANUAL_MARKET_SET = frozenset(MANUAL_MARKETS)

# 只由券商导入器产生、只进来源归档的市场（招商场外基金、资金流水），手工入口不接受
IMPORTER_ONLY_MARKETS = ("场外开基", "资金")


def require_manual_market(value: Optional[str]) -> Optional[str]:
    """schema validator 用：None 原样放行（部分更新），其余必须是手工市场之一。"""
    if value is None:
        return None
    value = value.strip()
    if value not in MANUAL_MARKET_SET:
        raise ValueError(f"market 必须是 {' / '.join(MANUAL_MARKETS)} 之一，收到「{value}」")
    return value
