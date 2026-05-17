"""
汇率管理API
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import date

from ..database import get_db
from ..models.exchange_rate import ExchangeRate
from ..schemas import exchange_rate as schemas
from ..services import exchange_rate_service

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])


@router.get("/latest", response_model=schemas.ExchangeRateLatest)
def get_latest_rates(db: Session = Depends(get_db)):
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
    db: Session = Depends(get_db)
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
    db: Session = Depends(get_db)
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
    db: Session = Depends(get_db)
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
    db: Session = Depends(get_db)
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
    db.refresh(rate)
    return rate


@router.delete("/{rate_id}")
def delete_exchange_rate(rate_id: int, db: Session = Depends(get_db)):
    """删除汇率"""
    rate = db.query(ExchangeRate).filter(ExchangeRate.id == rate_id).first()

    if not rate:
        raise HTTPException(status_code=404, detail="Exchange rate not found")

    db.delete(rate)
    db.commit()
    return {"message": "Exchange rate deleted successfully"}


@router.post("/convert", response_model=schemas.CurrencyConvertResponse)
def convert_currency(
    request: schemas.CurrencyConvertRequest,
    db: Session = Depends(get_db)
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
def refresh_rates_from_api(db: Session = Depends(get_db)):
    """从API刷新汇率"""
    try:
        updated_rates = exchange_rate_service.fetch_latest_rates_from_api(db)

        if not updated_rates:
            raise HTTPException(
                status_code=500,
                detail="Failed to fetch rates from API"
            )

        return {
            "message": "Rates updated successfully",
            "updated_rates": {k: float(v) for k, v in updated_rates.items()},
            "count": len(updated_rates)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
