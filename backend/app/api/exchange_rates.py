"""
汇率管理API

汇率是全局数据（不分用户），是所有用户金额折算的唯一数据源：登录即可读，**写入仅管理员**
（#277：此前任何家庭成员误操作都会改掉所有人的人民币折算），与目录同步、采集器管理同口径。
来源由服务端决定：手工录入/改过数值的行一律 manual；删除改为停用（保留审计，停用的手工行
不再挡住官方中间价）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List
from datetime import timedelta

from ..core.deps import get_current_active_user, get_current_admin_user
from ..core.logging import get_app_logger
from ..core.timeutil import local_today
from ..database import get_db
from ..models.exchange_rate import ExchangeRate, ExchangeRateCheck
from ..models.user import User
from ..schemas import exchange_rate as schemas
from ..services import exchange_rate_service

logger = get_app_logger(__name__)

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])


@router.get("/latest", response_model=schemas.ExchangeRateLatest)
def get_latest_rates(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """获取最新汇率（相对于基准货币CNY）

    `details` 按币种给出各自的生效日期与来源；顶层 `effective_date`/`source`
    保留兼容，取各币种中最新的那条（无汇率时为今天 · system）。
    """
    details = exchange_rate_service.get_latest_rate_details(db, "CNY")
    rates = {"CNY": 1.0, **{k: float(v["rate"]) for k, v in details.items()}}

    newest = max(details.values(), key=lambda item: item["effective_date"], default=None)
    effective_date = newest["effective_date"] if newest else local_today()
    source = newest["source"] if newest else "system"

    return {
        "base_currency": "CNY",
        "rates": rates,
        "effective_date": effective_date,
        "source": source,
        "details": details,
    }


@router.get("", response_model=List[schemas.ExchangeRate])
def list_exchange_rates(
    from_currency: str = None,
    to_currency: str = None,
    include_inactive: bool = False,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """获取汇率列表（默认只列启用的行；include_inactive=true 连已停用的一起看）"""
    query = db.query(ExchangeRate)
    if not include_inactive:
        query = query.filter(ExchangeRate.is_active.is_(True))

    if from_currency:
        query = query.filter(ExchangeRate.from_currency == from_currency)
    if to_currency:
        query = query.filter(ExchangeRate.to_currency == to_currency)

    query = query.order_by(ExchangeRate.effective_date.desc(), ExchangeRate.from_currency)

    rates = query.offset(skip).limit(limit).all()
    return rates


@router.get("/source-checks", response_model=List[schemas.ExchangeRateCheck])
def list_source_checks(
    days: int = Query(30, ge=1, le=366),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """官方中间价与第三方报价的逐日比对（最近 N 天，新在前）"""
    since = local_today() - timedelta(days=days)
    return (
        db.query(ExchangeRateCheck)
        .filter(ExchangeRateCheck.check_date >= since)
        .order_by(ExchangeRateCheck.check_date.desc(), ExchangeRateCheck.from_currency)
        .all()
    )


@router.post("", response_model=schemas.ExchangeRate)
def create_or_update_exchange_rate(
    rate_data: schemas.ExchangeRateCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """手工创建或覆盖某日汇率（仅管理员；来源固定为 manual，同日行被重新启用）"""
    rate = exchange_rate_service.update_or_create_rate(
        db,
        from_currency=rate_data.from_currency,
        to_currency=rate_data.to_currency,
        rate=rate_data.rate,
        effective_date=rate_data.effective_date,
        source="manual",
    )
    return rate


@router.put("/{rate_id}", response_model=schemas.ExchangeRate)
def update_exchange_rate(
    rate_id: int,
    rate_update: schemas.ExchangeRateUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """修正汇率数值（仅管理员）：改过数值的行记为 manual——此前保留「官方中间价」标签，
    下一次自动刷新会静默覆盖用户的修正，页面上还显示成官方来源"""
    rate = db.query(ExchangeRate).filter(ExchangeRate.id == rate_id).first()

    if not rate:
        raise HTTPException(status_code=404, detail="汇率记录不存在")

    rate.rate = rate_update.rate
    rate.source = "manual"
    rate.is_active = True

    db.commit()
    exchange_rate_service.invalidate_rate_cache(db)
    db.refresh(rate)
    return rate


@router.delete("/{rate_id}", status_code=204)
def delete_exchange_rate(
    rate_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """停用汇率（仅管理员）：保留行作审计，折算不再使用它；同日重新录入即恢复启用。
    此前是硬删，官方中间价行被删掉不留任何痕迹。

    只有手工行能停用：官方中间价与第三方报价行会被下一次刷新原样重建并重新启用，停用对它们
    兑现不了「折算不再使用」（PR #300 评审）。要改用别的数值，编辑它（改过数值的行记为手工，
    刷新不再覆盖）。"""
    rate = db.query(ExchangeRate).filter(ExchangeRate.id == rate_id).first()

    if not rate:
        raise HTTPException(status_code=404, detail="汇率记录不存在")
    if (rate.source or "manual") != "manual":
        raise HTTPException(
            status_code=409,
            detail="自动来源的汇率会在下次刷新时重建，不能停用；要改用其他数值请编辑（改为手工值后刷新不会覆盖）",
        )

    rate.is_active = False
    db.commit()
    exchange_rate_service.invalidate_rate_cache(db)


@router.post("/refresh-from-api")
def refresh_rates_from_api(
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """从API刷新汇率"""
    try:
        updated_rates = exchange_rate_service.fetch_latest_rates_from_api(db)
    except Exception:
        # 原来是裸 except + detail=str(e)：既把内部错误文本（含上游 URL/异常类型）
        # 回显给客户端，又会把下面自己抛的 HTTPException 一并吞掉再包一层。
        logger.exception("刷新汇率失败")
        raise HTTPException(status_code=502, detail="汇率数据源刷新失败，请稍后重试")

    if not updated_rates:
        raise HTTPException(status_code=502, detail="汇率数据源未返回任何汇率")

    return {
        "message": "汇率已刷新",
        "updated_rates": {k: float(v) for k, v in updated_rates.items()},
        "count": len(updated_rates),
    }
