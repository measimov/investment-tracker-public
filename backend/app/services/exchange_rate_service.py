"""
汇率服务模块
提供汇率查询和货币转换功能
"""
from sqlalchemy.orm import Session
from sqlalchemy import desc
from decimal import Decimal
from datetime import date, datetime
from typing import Dict, Optional
from weakref import WeakKeyDictionary

import requests
from ..models.exchange_rate import ExchangeRate
from ..core.logging import get_app_logger


BASE_CURRENCY = "CNY"  # 基准货币
logger = get_app_logger(__name__)

# Session 级最新汇率缓存：统计路径逐笔换算会对同一批币种对发起数百次
# 相同查询（实测占摘要接口 ~10%）。以 Session 为键，请求结束缓存随会话
# 消亡；汇率写路径调用 invalidate_rate_cache 主动失效。
_session_rate_cache: "WeakKeyDictionary[Session, Dict]" = WeakKeyDictionary()


def invalidate_rate_cache(db: Session) -> None:
    _session_rate_cache.pop(db, None)


def get_latest_rate(
    db: Session,
    from_currency: str,
    to_currency: str = BASE_CURRENCY
) -> Optional[Decimal]:
    """
    获取最新汇率

    Args:
        db: 数据库会话
        from_currency: 源币种
        to_currency: 目标币种（默认为CNY）

    Returns:
        汇率值，如果未找到返回None
    """
    # 如果是相同货币，返回1
    if from_currency == to_currency:
        return Decimal("1.0")

    try:
        cache = _session_rate_cache.setdefault(db, {})
    except TypeError:
        cache = None  # 不可弱引用的会话实现（测试替身等）：直接跳过缓存
    key = (from_currency, to_currency)
    if cache is not None and key in cache:
        return cache[key]

    rate = _query_latest_rate(db, from_currency, to_currency)
    if cache is not None:
        cache[key] = rate
    return rate


def _query_latest_rate(
    db: Session,
    from_currency: str,
    to_currency: str,
) -> Optional[Decimal]:
    # 查询最新的有效汇率
    rate_record = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == from_currency,
        ExchangeRate.to_currency == to_currency,
        ExchangeRate.is_active.is_(True),
    ).order_by(desc(ExchangeRate.effective_date)).first()

    if rate_record:
        return Decimal(str(rate_record.rate))

    # 尝试反向查询（如果有CNY->USD，可以计算USD->CNY）
    reverse_rate = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == to_currency,
        ExchangeRate.to_currency == from_currency,
        ExchangeRate.is_active.is_(True),
    ).order_by(desc(ExchangeRate.effective_date)).first()

    if reverse_rate and Decimal(str(reverse_rate.rate)) != 0:
        return Decimal("1") / Decimal(str(reverse_rate.rate))

    return None


def get_all_latest_rates(db: Session, base_currency: str = BASE_CURRENCY) -> Dict[str, Decimal]:
    """
    获取所有币种对基准货币的最新汇率

    Args:
        db: 数据库会话
        base_currency: 基准货币（默认CNY）

    Returns:
        {currency: rate} 字典
    """
    rates = {}
    rates[base_currency] = Decimal("1.0")

    # 查询所有以base_currency为目标货币的汇率
    rate_records = db.query(ExchangeRate).filter(
        ExchangeRate.to_currency == base_currency,
        ExchangeRate.is_active.is_(True),
    ).all()

    # 按币种分组，取最新的
    currency_rates = {}
    for record in rate_records:
        currency = record.from_currency
        if currency not in currency_rates or record.effective_date > currency_rates[currency]['date']:
            currency_rates[currency] = {
                'rate': Decimal(str(record.rate)),
                'date': record.effective_date
            }

    # 提取汇率
    for currency, data in currency_rates.items():
        rates[currency] = data['rate']

    return rates


def convert_amount(
    db: Session,
    amount: Decimal,
    from_currency: str,
    to_currency: str = BASE_CURRENCY
) -> Decimal:
    """
    转换金额

    Args:
        db: 数据库会话
        amount: 金额
        from_currency: 源币种
        to_currency: 目标币种（默认为CNY）

    Returns:
        转换后的金额
    """
    if from_currency == to_currency:
        return amount

    rate = get_latest_rate(db, from_currency, to_currency)

    if rate is None:
        # 如果找不到汇率，抛出异常
        raise ValueError(f"Exchange rate not found for {from_currency} -> {to_currency}")

    return amount * rate


def convert_to_cny(db: Session, amount: Decimal, from_currency: str) -> Decimal:
    """转换为人民币（CNY）"""
    return convert_amount(db, amount, from_currency, "CNY")


def convert_to_usd(db: Session, amount_cny: Decimal) -> Decimal:
    """
    将CNY金额转换为USD

    Args:
        db: 数据库会话
        amount_cny: CNY金额

    Returns:
        USD金额
    """
    # 获取USD对CNY的汇率
    usd_to_cny_rate = get_latest_rate(db, "USD", "CNY")

    if usd_to_cny_rate is None or usd_to_cny_rate == 0:
        raise ValueError("USD to CNY exchange rate not found")

    # CNY转USD = CNY金额 / (USD对CNY的汇率)
    return amount_cny / usd_to_cny_rate


def convert_amount_to_both_currencies(
    db: Session,
    amount: Decimal,
    from_currency: str
) -> Dict[str, Decimal]:
    """
    将金额转换为CNY和USD两种货币

    Args:
        db: 数据库会话
        amount: 金额
        from_currency: 源币种

    Returns:
        {"cny": Decimal, "usd": Decimal}
    """
    # 先转换为CNY
    amount_cny = convert_to_cny(db, amount, from_currency)

    # 再转换为USD
    amount_usd = convert_to_usd(db, amount_cny)

    return {
        "cny": amount_cny,
        "usd": amount_usd
    }


def update_or_create_rate(
    db: Session,
    from_currency: str,
    to_currency: str,
    rate: Decimal,
    effective_date: date = None,
    source: str = "manual"
) -> ExchangeRate:
    """
    更新或创建汇率

    Args:
        db: 数据库会话
        from_currency: 源币种
        to_currency: 目标币种
        rate: 汇率
        effective_date: 生效日期（默认今天）
        source: 来源

    Returns:
        ExchangeRate记录
    """
    if effective_date is None:
        effective_date = date.today()

    invalidate_rate_cache(db)

    # 查找是否存在
    existing = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == from_currency,
        ExchangeRate.to_currency == to_currency,
        ExchangeRate.effective_date == effective_date
    ).first()

    if existing:
        # 更新
        existing.rate = rate
        existing.source = source
        existing.is_active = True
        existing.updated_at = datetime.now()
        db.commit()
        db.refresh(existing)
        return existing
    else:
        # 创建
        new_rate = ExchangeRate(
            from_currency=from_currency,
            to_currency=to_currency,
            rate=rate,
            effective_date=effective_date,
            source=source,
            is_active=True
        )
        db.add(new_rate)
        db.commit()
        db.refresh(new_rate)
        return new_rate


def fetch_latest_rates_from_api(db: Session) -> Dict[str, Decimal]:
    """
    从API获取最新汇率并保存到数据库
    优先使用 frankfurter.app（欧洲央行数据）
    失败时使用 open.er-api.com 作为备用

    Returns:
        更新的汇率字典 {currency: rate}
    """
    today = date.today()
    updated_rates = {}

    # 方案1: 尝试 frankfurter.app（欧洲央行，最权威）
    try:
        response = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=CNY,HKD,SGD",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        if data and 'rates' in data:
            rates = data['rates']

            # USD -> CNY 直接使用
            if 'CNY' in rates:
                usd_to_cny = Decimal(str(rates['CNY']))
                update_or_create_rate(
                    db,
                    from_currency='USD',
                    to_currency='CNY',
                    rate=usd_to_cny,
                    effective_date=today,
                    source='api-ecb'
                )
                updated_rates['USD'] = usd_to_cny

                # 转换其他货币为 外币 -> CNY 的汇率
                for currency in ['HKD', 'SGD']:
                    if currency in rates:
                        # USD -> 外币 和 USD -> CNY
                        usd_to_foreign = Decimal(str(rates[currency]))

                        # 外币 -> CNY = (USD -> CNY) / (USD -> 外币)
                        # 例如: HKD -> CNY = 7.0 / 7.8 = 0.897
                        foreign_to_cny = usd_to_cny / usd_to_foreign

                        update_or_create_rate(
                            db,
                            from_currency=currency,
                            to_currency='CNY',
                            rate=foreign_to_cny,
                            effective_date=today,
                            source='api-ecb'
                        )
                        updated_rates[currency] = foreign_to_cny

                logger.info(
                    "Fetched exchange rates from frankfurter.app: USD->CNY=%s, HKD->CNY=%s, SGD->CNY=%s",
                    f"{updated_rates.get('USD', 0):.4f}",
                    f"{updated_rates.get('HKD', 0):.4f}",
                    f"{updated_rates.get('SGD', 0):.4f}",
                )
                return updated_rates

    except Exception as e:
        logger.warning("frankfurter.app exchange rate fetch failed; trying backup API: %s", e)

    # 方案2: 备用 - open.er-api.com
    try:
        response = requests.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        if data and 'rates' in data:
            rates = data['rates']

            # USD -> CNY
            if 'CNY' in rates:
                usd_to_cny = Decimal(str(rates['CNY']))
                update_or_create_rate(
                    db,
                    from_currency='USD',
                    to_currency='CNY',
                    rate=usd_to_cny,
                    effective_date=today,
                    source='api-backup'
                )
                updated_rates['USD'] = usd_to_cny

                # 转换其他货币
                for currency in ['HKD', 'SGD']:
                    if currency in rates:
                        usd_to_foreign = Decimal(str(rates[currency]))
                        foreign_to_cny = usd_to_cny / usd_to_foreign

                        update_or_create_rate(
                            db,
                            from_currency=currency,
                            to_currency='CNY',
                            rate=foreign_to_cny,
                            effective_date=today,
                            source='api-backup'
                        )
                        updated_rates[currency] = foreign_to_cny

                logger.info(
                    "Fetched exchange rates from open.er-api.com: USD->CNY=%s, HKD->CNY=%s, SGD->CNY=%s",
                    f"{updated_rates.get('USD', 0):.4f}",
                    f"{updated_rates.get('HKD', 0):.4f}",
                    f"{updated_rates.get('SGD', 0):.4f}",
                )
                return updated_rates

    except Exception as e:
        logger.error("Backup exchange rate API failed: %s", e)

    return {}


def get_rate_info(db: Session, from_currency: str, to_currency: str = BASE_CURRENCY) -> Dict:
    """
    获取汇率详细信息

    Returns:
        {
            'rate': Decimal,
            'effective_date': date,
            'source': str
        }
    """
    rate_record = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency == from_currency,
        ExchangeRate.to_currency == to_currency,
        ExchangeRate.is_active.is_(True),
    ).order_by(desc(ExchangeRate.effective_date)).first()

    if rate_record:
        return {
            'rate': Decimal(str(rate_record.rate)),
            'effective_date': rate_record.effective_date,
            'source': rate_record.source
        }

    return None
