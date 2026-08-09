"""
汇率管理API

汇率是全局数据（不分用户），但它是所有用户金额折算的唯一数据源：登录即可读写，
匿名一律拒绝。口径与 security_profiles 一致（全局表、逐端点挂依赖）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import date

from ..core.deps import get_current_active_user
from ..core.logging import get_app_logger
from ..database import get_db
from ..models.exchange_rate import ExchangeRate
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
    """获取最新汇率（相对于基准货币CNY）"""
    rates = exchange_rate_service.get_all_latest_rates(db, "CNY")

    # 获取最新日期
    latest_record = db.query(ExchangeRate).order_by(
        ExchangeRate.effective_date.desc()
    ).first()

    effective_date = latest_record.effective_date if latest_record else date.today()
    source = latest_record.source if latest_record else "system"

    return {
        "base_currency": "CNY",
        "rates": {k: float(v) for k, v in rates.items()},
        "effective_date": effective_date,
        "source": source
    }


@router.get("/", response_model=List[schemas.ExchangeRate])
def list_exchange_rates(
    from_currency: str = None,
    to_currency: str = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """获取汇率列表"""
    query = db.query(ExchangeRate)

    if from_currency:
        query = query.filter(ExchangeRate.from_currency == from_currency)
    if to_currency:
        query = query.filter(ExchangeRate.to_currency == to_currency)

    query = query.order_by(
        ExchangeRate.effective_date.desc(),
        ExchangeRate.from_currency
    )

    rates = query.offset(skip).limit(limit).all()
    return rates


@router.get("/{from_currency}/{to_currency}", response_model=schemas.ExchangeRate)
def get_exchange_rate(
    from_currency: str,
    to_currency: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """获取特定货币对的最新汇率"""
    rate = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == from_currency,
        ExchangeRate.to_currency == to_currency,
        ExchangeRate.is_active.is_(True),
    ).order_by(ExchangeRate.effective_date.desc()).first()

    if not rate:
        raise HTTPException(
            status_code=404,
            detail=f"Exchange rate not found for {from_currency}/{to_currency}"
        )

    return rate


@router.post("/", response_model=schemas.ExchangeRate)
def create_or_update_exchange_rate(
    rate_data: schemas.ExchangeRateCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """创建或更新汇率"""
    rate = exchange_rate_service.update_or_create_rate(
        db,
        from_currency=rate_data.from_currency,
        to_currency=rate_data.to_currency,
        rate=rate_data.rate,
        effective_date=rate_data.effective_date,
        source=rate_data.source or "manual"
    )
    return rate


@router.put("/{rate_id}", response_model=schemas.ExchangeRate)
def update_exchange_rate(
    rate_id: int,
    rate_update: schemas.ExchangeRateUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """更新汇率"""
    rate = db.query(ExchangeRate).filter(ExchangeRate.id == rate_id).first()

    if not rate:
        raise HTTPException(status_code=404, detail="Exchange rate not found")

    if rate_update.rate is not None:
        rate.rate = rate_update.rate
    if rate_update.is_active is not None:
        rate.is_active = rate_update.is_active
    if rate_update.source is not None:
        rate.source = rate_update.source

    db.commit()
    exchange_rate_service.invalidate_rate_cache(db)
    db.refresh(rate)
    return rate


@router.delete("/{rate_id}")
def delete_exchange_rate(
    rate_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """删除汇率"""
    rate = db.query(ExchangeRate).filter(ExchangeRate.id == rate_id).first()

    if not rate:
        raise HTTPException(status_code=404, detail="Exchange rate not found")

    db.delete(rate)
    db.commit()
    exchange_rate_service.invalidate_rate_cache(db)
    return {"message": "Exchange rate deleted successfully"}


@router.post("/convert", response_model=schemas.CurrencyConvertResponse)
def convert_currency(
    request: schemas.CurrencyConvertRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """转换货币"""
    try:
        converted_amount = exchange_rate_service.convert_amount(
            db,
            amount=request.amount,
            from_currency=request.from_currency,
            to_currency=request.to_currency
        )

        rate = exchange_rate_service.get_latest_rate(
            db,
            request.from_currency,
            request.to_currency
        )

        rate_info = exchange_rate_service.get_rate_info(
            db,
            request.from_currency,
            request.to_currency
        )

        return {
            "amount": request.amount,
            "from_currency": request.from_currency,
            "converted_amount": converted_amount,
            "to_currency": request.to_currency,
            "rate": rate,
            "effective_date": rate_info['effective_date'] if rate_info else date.today()
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/refresh-from-api")
def refresh_rates_from_api(
    current_user: User = Depends(get_current_active_user),
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
        "message": "Rates updated successfully",
        "updated_rates": {k: float(v) for k, v in updated_rates.items()},
        "count": len(updated_rates)
    }
