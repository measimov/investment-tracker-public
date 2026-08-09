"""统计编排层：查 DB → 喂 portfolio 纯内核 → 组响应。

所有重放/FIFO/曲线/指标计算都在 services/portfolio/ 内核中（无 DB 依赖）；
本包负责数据装载（transactions/corporate actions/prices/rates）、汇率换算到
本位币和响应字段组装。原 1.7k 行的 statistics_service 按职责拆分（issue #136）：

- `fx` — 汇率装载（DbExchangeRateLookup，一请求构造一次）与折算口径
- `fifo_results` — 按 (symbol, market) 重放 FIFO 的编排入口
- `aggregates` — 概览/分市场/分时段/成本分布、持仓表现、已实现盈亏、股息、收益卡片
- `analytics` — 收益分析（区间钳制/行情同步/曲线/指标/基准对比，按阶段拆步）
- `pricing` — 服务端估值定价与新鲜度
- `snapshot` — 看板与 LLM 报告共用的组合快照

对外入口统一从本包导入；改数值口径前后必须双跑
`scripts/metrics_parity_report.py`（见 CLAUDE.md）。
"""

from .aggregates import (
    calculate_current_holdings_performance,
    calculate_performance_summary,
    calculate_realized_pnl_fifo,
    get_dividend_summary,
    get_holdings_cost_breakdown,
    get_statistics_by_market,
    get_statistics_by_time,
    get_summary_statistics,
)
from .analytics import calculate_performance_analytics
from .pricing import PRICE_STALE_DAYS, resolve_server_prices
from .snapshot import build_portfolio_snapshot

__all__ = [
    "PRICE_STALE_DAYS",
    "build_portfolio_snapshot",
    "calculate_current_holdings_performance",
    "calculate_performance_analytics",
    "calculate_performance_summary",
    "calculate_realized_pnl_fifo",
    "get_dividend_summary",
    "get_holdings_cost_breakdown",
    "get_statistics_by_market",
    "get_statistics_by_time",
    "get_summary_statistics",
    "resolve_server_prices",
]
