"""周期任务的持久化运行状态（`services/scheduled_state.py`）。

`job_worker.register_periodic_task` 的间隔只记在进程内存里，重启后首个 tick 就会再跑一遍
——对「每周一次」「每个交易日一次」的任务意味着每次部署都重跑。这类任务改为短 tick +
本表判定是否到期：一行 = 一个任务名，记录最近一次运行/成功的时间与任务自定的明细
（如已处理到的交易日）。全局表，不属于任何用户。
"""

from sqlalchemy import Column, DateTime, String
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB

from ..database import Base


class ScheduledTaskState(Base):
    __tablename__ = "scheduled_task_state"

    name = Column(String(100), primary_key=True, comment="任务名（与周期任务注册名一致）")
    last_run_at = Column(DateTime(timezone=True), comment="最近一次运行（含失败）的时间")
    last_success_at = Column(DateTime(timezone=True), comment="最近一次成功的时间")
    detail = Column(
        JSONB,
        nullable=False,
        server_default=sa_text("'{}'::jsonb"),
        comment="任务自定的明细，如已处理到的交易日",
    )
