"""后台任务的内联快路径底座（issue #134）。

八个 job 模块此前各自抄了一份逐字相同的 `run_*_job`——只有日志里的名词不同。
那不只是重复：其中的 `required_attempt_count` 是一道**容易漏、漏了很难发现**
的护栏（#127 就是靠它兜住的），八份拷贝意味着新增 job 家族时有八分之一的机会
照着漏掉那一行的版本抄。收敛成一处后，新 job 只能拿到带守卫的那一版。

用法（模块里保留同名 `run_*_job`，签名不变——API 层按名 import 给
`background_tasks.add_task`，测试也按名参数化）：

    def run_price_refresh_job(job_id: str) -> None:
        run_job_inline(job_id, JOB_TYPE, execute_price_refresh_job,
                       label="Price refresh", logger=logger)

`execute_*` 写成模块全局名而不是在 import 时传进工厂：名字在**调用时**才从
模块 globals 解析，monkeypatch 模块属性照旧生效（测试正是这么打桩的）。
"""

from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Sequence

from ..database import SessionLocal
from .background_job_store import (
    JobOwnershipLostError,
    claim_job,
    get_job,
    handle_job_failure,
    job_heartbeat,
    set_job_progress,
    update_job,
)


def run_job_inline(
    job_id: str,
    job_type: str,
    execute: Callable[[Dict[str, Any]], None],
    *,
    label: str,
    logger,
) -> None:
    """按 id 认领并执行；意外错误走重试/退避路径。

    `required_attempt_count` 不是可选项：长任务（分析家族单轮 2-4h）的租约只有
    300s，内联线程几乎必然已被 worker 接管——不带这个守卫，僵尸线程最终抛出的
    异常会把**接管者正在跑的那一次**重新排队或直接标失败。
    """
    claimed = claim_job(job_id, job_type)
    if not claimed:
        logger.info("%s job %s was already claimed or no longer queued", label, job_id)
        return
    try:
        execute(claimed)
    except Exception as exc:
        logger.exception("%s job %s failed", label, job_id)
        handle_job_failure(
            job_id,
            job_type,
            str(exc),
            required_attempt_count=claimed.get("attempt_count"),
        )


# ---------------------------------------------------------------------------
# 批量 job 的可靠性管道（issue #134 下半）
#
# 只抽**确定逐字相同**的那部分：进度回写的失权哨兵、取消标志的置/查、
# 「db 会话 + 心跳 + 失权安静退出」这个三件套的嵌套顺序。
#
# 循环体刻意留在各自模块——两个批量 job 的 execute_* 实测只有 42% 逐行相同
# （242/218 行中 97 行），单条目要做什么、失败怎么分类、跳过条件都不一样。
# 更要紧的是 security_analysis_batch 的 progress() 在 analyze_one 的 try
# **内部**被调用，哨兵异常必须显式 re-raise 才不被 except Exception 吞掉；
# report_digest_batch 的调用点在 try **外**。把循环也统一掉就是行为变更，
# 而那正是 #127 修过的坑。
# ---------------------------------------------------------------------------


def rotate_by_market(
    items: Iterable[Any],
    markets: Sequence[str],
    *,
    market_of: Callable[[Any], str] = lambda item: item[1],
    symbol_of: Callable[[Any], str] = lambda item: item[0],
) -> List[Any]:
    """按市场轮转排序（A股→B股→港股→美股→A股…），组内按代码；不在 markets 里的条目丢弃。

    批量任务用轮转而不是按市场分组：同一 Tushare 接口的相邻两次调用被其他市场的标的自然
    拉开，等于零成本的接口级降频。条目可以是 (symbol, market) 元组或带键的 dict（传访问器）。
    此前批量分析、批量观点与雪球按标的采集各写一份（#280）。
    """
    by_market: Dict[str, List[Any]] = {}
    for item in items:
        by_market.setdefault(market_of(item), []).append(item)
    for bucket in by_market.values():
        bucket.sort(key=symbol_of)
    order = [market for market in markets if market in by_market]
    ordered: List[Any] = []
    index = 0
    while any(by_market[market] for market in order):
        bucket = by_market[order[index % len(order)]]
        if bucket:
            ordered.append(bucket.pop(0))
        index += 1
    return ordered


def initial_batch_data(targets: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    """批量 job 的初始 data：固化目标 + 续跑键 + 进度计数 + 取消/中止标志。

    三个批量家族（分析/观点/财报回填）此前各抄一份（#280）；家族特有字段走 extra。
    """
    return {
        # 目标固化：批量要跑几十分钟，期间用户可能导入新交易；不固化会让
        # 重试/接管时的范围漂移，进度条倒退
        "targets": targets,
        # 已完成键：接管/重试时精确续跑，不重复烧 LLM
        "completed_keys": [],
        "total": len(targets),
        "completed": 0,
        "progress_percent": 0,
        "success_count": 0,
        "failed_count": 0,
        "current_symbol": None,
        "current_market": None,
        "results": [],
        "cancel_requested": False,
        "cancelled": False,
        # 不能叫 error：_serialize 展平后会盖掉 BackgroundJob.error 列
        "abort_reason": None,
        **extra,
    }


def make_batch_progress(job_id: str, job_type: str, attempt) -> Callable[..., None]:
    """批量循环的进度回写闭包：一旦失权就抛哨兵，让调用方立刻停手。

    set_job_progress 返回 None 有三种情形（行不存在／已非 running／attempt 变了
    =被接管），对批量循环而言处置相同：停手。不停的话僵尸线程会接着对剩余条目
    重复调用外部 API 并烧 token，而产物按键 upsert——接管者与僵尸各写一遍，
    成本护栏也按两份计。
    """

    def progress(**updates: Any) -> None:
        if set_job_progress(job_id, job_type, required_attempt_count=attempt, **updates) is None:
            raise JobOwnershipLostError(job_id)

    return progress


def request_job_cancel(job_id: str, job_type: str, user_id: int):
    """置中止标志；执行循环在**每个条目开始前**检查，当前条目跑完即收尾。

    不做线程级强杀：正在进行的外部调用已经花掉了配额/token，中途丢弃只是浪费。
    """
    job = get_job(job_id, job_type, user_id)
    if not job:
        return None
    if job.get("status") not in ("queued", "running"):
        return job
    return update_job(job_id, job_type, data_updates={"cancel_requested": True}) or job


def is_cancel_requested(job_id: str, job_type: str, user_id: int) -> bool:
    job = get_job(job_id, job_type, user_id)
    return bool(job and job.get("cancel_requested"))


@contextmanager
def batch_execution(job_id: str, job_type: str, *, attempt, max_seconds: float, logger, label: str):
    """批量执行的外壳：db 会话 + 心跳续租 + 失权安静退出。

    三者的**嵌套顺序**是有讲究的，也正是值得收敛的原因：失权的 except 必须包在
    心跳上下文**外面**（心跳线程要先随上下文退出），而 db.close() 又要在最外层
    的 finally。顺序写反了要么漏关会话，要么让哨兵逃到 run_*/worker——那里只会
    打出误导性的「任务失败」堆栈（虽然 attempt 守卫本身仍是安全的）。

    失权不是失败：接管者正在跑同一个 job，安静退出即可。
    """
    db = SessionLocal()
    try:
        with job_heartbeat(job_id, job_type, attempt_count=attempt, max_seconds=max_seconds):
            yield db
    except JobOwnershipLostError:
        logger.warning(
            "%s job %s 已被接管或进入终态，本次执行停止（剩余条目交由接管者）",
            label,
            job_id,
        )
    finally:
        db.close()
