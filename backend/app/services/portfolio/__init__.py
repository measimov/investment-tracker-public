"""纯组合计算内核（portfolio engine）。

本包是三大产品目的（历史复盘/反事实/回测、看板、LLM 报告）共享的计算核心：
给定一条事件流（交易 + 公司行动）、价格序列和汇率查找，重放出持仓、FIFO
批次、TTWR 曲线和风险指标。

铁律：本包内不允许出现任何 ORM/DB 依赖 —— 不 import app.models、不接受
Session、不调用 date.today()。所有数据以参数传入，所有"当前时间"由调用方
显式提供。这保证同一套逻辑既能算真实历史（statistics_service 从 DB 取数
喂入），也能算反事实与回测（simulation 构造虚拟事件流喂入）。

模块划分：
- semantics.py  公司行动数量语义（送股/拆股因子，字段优先级的唯一出处）
- fifo.py       FIFO 队列重放与已实现盈亏
- fx.py         日期感知汇率查找与换算
- curve.py      持仓重放与 TTWR 收益曲线
- metrics.py    风险指标、交易能力指标、XIRR
"""
