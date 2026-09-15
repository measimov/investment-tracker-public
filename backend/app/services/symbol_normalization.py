"""手工录入标的代码的归一化（与导入器口径对齐）。

账本身份键是 (symbol, market)。三家导入器早已各自归一（IBKR 港股 zfill(5)、
大写等），但手工入口（交易/自选/公司行动）过去原样入库，产生过同一证券两个
键的分裂（3900 vs 03900，存量已于 2026-09-02 修复）。本模块是**手工入口归一
化的唯一实现**——新增手工入口必须过这里，别再各自 strip/upper。

规则刻意保守，只做无歧义的归一：
- 去首尾空白；字母一律大写（PostgreSQL 唯一约束区分大小写，aapl/AAPL 会并存，
  小写条目还查不到既有档案/行情）
- 港股纯数字代码补零到 5 位（交易所官方形态，与 IBKR 导入器、行情源一致）

其余市场不动：A股/B股 6 位无歧义，美股 ticker 原样（BRK.B 等本就含点）。
"""

from typing import Optional

HK_MARKET = "港股"


def normalize_manual_symbol(symbol: str, market: Optional[str]) -> str:
    text = (symbol or "").strip().upper()
    if market == HK_MARKET and text.isdigit() and len(text) <= 5:
        return text.zfill(5)
    return text
