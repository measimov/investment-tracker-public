"""
汇率服务模块
提供汇率查询和货币转换功能
"""

from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from decimal import Decimal
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from weakref import WeakKeyDictionary

import requests
from ..config import settings
from ..models.exchange_rate import ExchangeRate, ExchangeRateCheck
from ..core.logging import get_app_logger
from ..core.timeutil import business_timezone, local_today
from . import chinamoney_source
from .job_worker import PeriodicOutcome, periodic_outcome_task


BASE_CURRENCY = "CNY"  # 基准货币
logger = get_app_logger(__name__)

# Session 级最新汇率缓存：统计路径逐笔换算会对同一批币种对发起数百次
# 相同查询（实测占摘要接口 ~10%）。以 Session 为键，请求结束缓存随会话
# 消亡；汇率写路径调用 invalidate_rate_cache 主动失效。
_session_rate_cache: "WeakKeyDictionary[Session, Dict]" = WeakKeyDictionary()


def invalidate_rate_cache(db: Session) -> None:
    _session_rate_cache.pop(db, None)


def get_latest_rate(
    db: Session, from_currency: str, to_currency: str = BASE_CURRENCY
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
    rate_record = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency,
            ExchangeRate.is_active.is_(True),
        )
        .order_by(desc(ExchangeRate.effective_date))
        .first()
    )

    if rate_record:
        return Decimal(str(rate_record.rate))

    # 尝试反向查询（如果有CNY->USD，可以计算USD->CNY）
    reverse_rate = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == to_currency,
            ExchangeRate.to_currency == from_currency,
            ExchangeRate.is_active.is_(True),
        )
        .order_by(desc(ExchangeRate.effective_date))
        .first()
    )

    if reverse_rate and Decimal(str(reverse_rate.rate)) != 0:
        return Decimal("1") / Decimal(str(reverse_rate.rate))

    return None


def get_latest_rate_details(db: Session, base_currency: str = BASE_CURRENCY) -> Dict[str, Dict]:
    """各币种对基准货币的最新有效汇率，连同**各自的**生效日期与来源。

    返回 {currency: {'rate': Decimal, 'effective_date': date, 'source': str}}，
    不含基准货币自身。汇率页逐卡显示日期/来源并标记过期——此前所有卡片共用
    全表最新一条的日期与来源，三个月没更新的币种也显示「今天 · api」（#220）。
    (from, to, effective_date) 有唯一约束，同一币种不会有同日并列。
    """
    rate_records = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.to_currency == base_currency,
            ExchangeRate.is_active.is_(True),
        )
        .order_by(ExchangeRate.effective_date.asc(), ExchangeRate.id.asc())
        .all()
    )
    details: Dict[str, Dict] = {}
    for record in rate_records:
        # 升序遍历，后来者覆盖 = 每个币种留下最新的一条
        details[record.from_currency] = {
            "rate": Decimal(str(record.rate)),
            "effective_date": record.effective_date,
            "source": record.source,
        }
    details.pop(base_currency, None)
    return details


def convert_amount(
    db: Session, amount: Decimal, from_currency: str, to_currency: str = BASE_CURRENCY
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


def update_or_create_rate(
    db: Session,
    from_currency: str,
    to_currency: str,
    rate: Decimal,
    effective_date: date = None,
    source: str = "manual",
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
        effective_date = local_today()

    invalidate_rate_cache(db)

    # 查找是否存在
    existing = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency,
            ExchangeRate.effective_date == effective_date,
        )
        .first()
    )

    if existing:
        # 更新
        existing.rate = rate
        existing.source = source
        existing.is_active = True
        existing.updated_at = datetime.now(timezone.utc)
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
            is_active=True,
        )
        db.add(new_rate)
        db.commit()
        db.refresh(new_rate)
        return new_rate


# 估值链路必需的对 CNY 汇率币种（与中间价 / 第三方抓取集一致）
REQUIRED_RATE_CURRENCIES = ("USD", "HKD", "SGD")
OFFICIAL_SOURCE = chinamoney_source.SOURCE_CCPR
THIRD_PARTY_SOURCES = ("api-ecb", "api-backup")
# 每次刷新回看的官方中间价天数：覆盖长假（国庆 7 天）后的补齐与周末残留的第三方行
OFFICIAL_LOOKBACK_DAYS = 15
# 中间价 9:15 发布，留出余量后才期待「今天」有值
OFFICIAL_PUBLISH_CUTOFF = time(9, 30)


def expected_official_date(now: Optional[datetime] = None) -> date:
    """此刻应当已经发布的最近一期中间价日期（只排除周末；法定节假日不可知）。

    节假日当天按此判定会「缺今天」而每个周期 tick 多打一次中国货币网——只是一次
    请求，且在 fx_official_max_stale_days 之内不会降级写第三方。
    """
    now = now or datetime.now(business_timezone())
    candidate = now.date()
    if now.time() < OFFICIAL_PUBLISH_CUTOFF:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


@periodic_outcome_task
def periodic_refresh_rates() -> PeriodicOutcome:
    """任一必需币种缺最近一期汇率即刷新（周期任务，periodic_registry 以名字
    refresh_rates_if_stale 注册——名字即告警键，保持不变；幂等）。

    「最近一期」按中间价发布节奏（工作日 9:15）判定，而不是「今天有一行」——周末与
    节假日没有官方新值，旧口径会让每个 tick 都去抓、再拿第三方当天值顶上。逐币种检查：
    只查 USD 会掩盖 HKD/SGD 缺失（手工只更新了 USD、上游部分返回、写入中断）。

    结果契约：已是最新 → succeeded（目标状态已核实）；官方中间价接口报错、或两路都
    拿不到任何汇率 → failed（fetch_latest_rates_from_api 自吞这些错误，只经 errors 交代）；
    节假日官方无新值但接口正常 → succeeded。
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        expected = expected_official_date()
        fresh_currencies = {
            row[0]
            for row in db.query(ExchangeRate.from_currency).filter(
                ExchangeRate.from_currency.in_(REQUIRED_RATE_CURRENCIES),
                ExchangeRate.to_currency == "CNY",
                ExchangeRate.effective_date >= expected,
                ExchangeRate.is_active.is_(True),
            )
        }
        if fresh_currencies >= set(REQUIRED_RATE_CURRENCIES):
            return PeriodicOutcome.succeeded(0, "已是最新")
        errors: List[str] = []
        rates = fetch_latest_rates_from_api(db, errors=errors)
        logger.info(
            "汇率自动刷新完成: %s 个币种（此前缺 %s 起的汇率: %s）",
            len(rates),
            expected.isoformat(),
            sorted(set(REQUIRED_RATE_CURRENCIES) - fresh_currencies),
        )
        official_errors = [item for item in errors if item.startswith("official")]
        if official_errors or not rates:
            return PeriodicOutcome.failed(
                "；".join(errors) or "官方与第三方汇率源均未返回数据", count=len(rates)
            )
        return PeriodicOutcome.succeeded(len(rates))
    finally:
        db.close()


def _fetch_third_party_quotes() -> Tuple[str, Dict[str, Decimal]]:
    """第三方聚合报价 → (来源标签, {外币: 1 外币 = N CNY})；两家都失败抛 RuntimeError。

    frankfurter（欧洲央行参考价）优先、open.er-api 备用，均以 USD 为基准交叉出 HKD/SGD。
    只取值不写库：官方源可用时它们只用于逐日比对。
    """
    errors = []
    for source, url in (
        ("api-ecb", "https://api.frankfurter.app/latest?from=USD&to=CNY,HKD,SGD"),
        ("api-backup", "https://open.er-api.com/v6/latest/USD"),
    ):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            rates = (response.json() or {}).get("rates") or {}
            if "CNY" not in rates:
                raise ValueError("响应缺少 CNY")
            usd_to_cny = Decimal(str(rates["CNY"]))
            quotes = {"USD": usd_to_cny}
            for currency in ("HKD", "SGD"):
                if rates.get(currency):
                    # 外币 -> CNY = (USD -> CNY) / (USD -> 外币)
                    quotes[currency] = usd_to_cny / Decimal(str(rates[currency]))
            return source, quotes
        except Exception as exc:  # noqa: BLE001 - 逐源降级，错误汇总后上抛
            errors.append(f"{source}: {exc}")
            logger.warning("第三方汇率 %s 获取失败: %s", source, exc)
    raise RuntimeError("第三方汇率源全部失败：" + "；".join(errors))


def _upsert_unless_manual(
    db: Session, currency: str, rate: Decimal, effective_date: date, source: str
) -> bool:
    """写入 外币→CNY 汇率；同日已有**启用中**的手工行不覆盖（用户的刻意输入优先）。

    停用的手工行不再保护（#277）：「删除」改为停用后，否则一条被删的手工汇率会永久挡住
    该日的官方中间价。"""
    existing = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == currency,
            ExchangeRate.to_currency == BASE_CURRENCY,
            ExchangeRate.effective_date == effective_date,
        )
        .first()
    )
    if existing is not None and existing.is_active and (existing.source or "manual") == "manual":
        return False
    if (
        existing is not None
        and existing.source == source
        and existing.is_active
        and Decimal(str(existing.rate)) == rate
    ):
        return False
    update_or_create_rate(
        db, currency, BASE_CURRENCY, rate, effective_date=effective_date, source=source
    )
    return True


def apply_official_rows(db: Session, rows, window_start: date, window_end: date) -> Dict[str, int]:
    """把一段区间的官方中间价写库，并停用同区间内官方没有覆盖的第三方行。

    「官方有覆盖的区间以官方为准」：周末/节假日里第三方写下的行（旧口径每天都写）会让
    按日查找在那几天用第三方值、而最新汇率也停在第三方——区间内这些日期没有中间价，
    正确口径是沿用前一个发布日，所以停用（is_active=False，保留审计）而不是删除。
    最近一期之后的第三方行只在官方仍新鲜时停用（降级期写下的兜底行保留）。
    返回 {"written": 新写/改写行数, "deactivated": 停用的第三方行数}。
    """
    written = 0
    official_dates: Dict[str, set] = {}
    for row in rows:
        for currency, rate in row.rates.items():
            official_dates.setdefault(currency, set()).add(row.rate_date)
            if _upsert_unless_manual(db, currency, rate, row.rate_date, OFFICIAL_SOURCE):
                written += 1

    deactivated = 0
    for currency, dates in official_dates.items():
        latest_official = max(dates)
        # 官方仍新鲜时，最近一期之后（节假日/周末）的第三方行同样作废——官方没有新值
        # 就该沿用最近一期；官方已陈旧（降级期）时，之后的第三方行是合法兜底，保留
        fresh = latest_official >= window_end - timedelta(days=settings.fx_official_max_stale_days)
        cutoff = window_end if fresh else min(window_end, latest_official)
        stale_rows = (
            db.query(ExchangeRate)
            .filter(
                ExchangeRate.from_currency == currency,
                ExchangeRate.to_currency == BASE_CURRENCY,
                ExchangeRate.source.in_(THIRD_PARTY_SOURCES),
                ExchangeRate.is_active.is_(True),
                ExchangeRate.effective_date >= window_start,
                ExchangeRate.effective_date <= cutoff,
            )
            .all()
        )
        for stale in stale_rows:
            if stale.effective_date not in dates:
                stale.is_active = False
                deactivated += 1
    if deactivated:
        invalidate_rate_cache(db)
        db.commit()
    return {"written": written, "deactivated": deactivated}


def _latest_official(db: Session) -> Dict[str, Tuple[date, Decimal]]:
    latest: Dict[str, Tuple[date, Decimal]] = {}
    for currency in REQUIRED_RATE_CURRENCIES:
        row = (
            db.query(ExchangeRate)
            .filter(
                ExchangeRate.from_currency == currency,
                ExchangeRate.to_currency == BASE_CURRENCY,
                ExchangeRate.source == OFFICIAL_SOURCE,
                ExchangeRate.is_active.is_(True),
            )
            .order_by(desc(ExchangeRate.effective_date))
            .first()
        )
        if row is not None:
            latest[currency] = (row.effective_date, Decimal(str(row.rate)))
    return latest


def record_rate_checks(
    db: Session,
    official: Dict[str, Tuple[date, Decimal]],
    reference_source: str,
    reference_rates: Dict[str, Decimal],
    check_date: date,
) -> List[ExchangeRateCheck]:
    """逐币种记一行「第三方 vs 最近一期中间价」差异；超阈值打 warning。"""
    checks = []
    for currency, (official_date, official_rate) in official.items():
        reference_rate = reference_rates.get(currency)
        if reference_rate is None or not official_rate:
            continue
        diff_pct = ((reference_rate / official_rate) - 1) * 100
        diff_pct = diff_pct.quantize(Decimal("0.0001"))
        check = (
            db.query(ExchangeRateCheck)
            .filter(
                ExchangeRateCheck.from_currency == currency,
                ExchangeRateCheck.to_currency == BASE_CURRENCY,
                ExchangeRateCheck.check_date == check_date,
            )
            .first()
        )
        if check is None:
            check = ExchangeRateCheck(
                from_currency=currency, to_currency=BASE_CURRENCY, check_date=check_date
            )
            db.add(check)
        check.official_date = official_date
        check.official_source = OFFICIAL_SOURCE
        check.official_rate = official_rate
        check.reference_source = reference_source
        check.reference_rate = reference_rate.quantize(Decimal("0.00000001"))
        check.diff_pct = diff_pct
        checks.append(check)
        if abs(diff_pct) > Decimal(str(settings.fx_check_warn_pct)):
            logger.warning(
                "汇率比对差异超阈值: %s/CNY 第三方(%s)=%s 官方中间价(%s)=%s 差 %s%%",
                currency,
                reference_source,
                reference_rate,
                official_date,
                official_rate,
                diff_pct,
            )
    db.commit()
    return checks


def fetch_latest_rates_from_api(
    db: Session, *, errors: Optional[List[str]] = None
) -> Dict[str, Decimal]:
    """刷新 外币→CNY 汇率，返回各币种当前生效的汇率（可能不是本次新写的）。

    1. 官方：中国货币网人民币汇率中间价，回看 OFFICIAL_LOOKBACK_DAYS 天全部写库
       （节假日补齐），并停用区间内官方未覆盖日期上的第三方行；
    2. 第三方（frankfurter → open.er-api）只取值，与最近一期中间价逐日比对记差异；
    3. 降级：某币种最近一期中间价已超过 fx_official_max_stale_days 天（官方源持续失败），
       才把第三方报价以今天日期写入，来源标签如实写第三方。
    两路都失败返回空字典（调用方按失败处理）。errors 给出时追加各源错误
    （`official: …` / `third_party: …`），供周期任务判定失败——本函数自己不上抛。
    """
    today = local_today()
    try:
        rows = chinamoney_source.fetch_ccpr_history(
            today - timedelta(days=OFFICIAL_LOOKBACK_DAYS), today, REQUIRED_RATE_CURRENCIES
        )
        stats = apply_official_rows(db, rows, today - timedelta(days=OFFICIAL_LOOKBACK_DAYS), today)
        logger.info(
            "人民币汇率中间价已同步: %s 行（最近一期 %s；停用第三方残留 %s 行）",
            stats["written"],
            rows[-1].rate_date.isoformat() if rows else "无",
            stats["deactivated"],
        )
    except Exception as exc:  # noqa: BLE001 - 官方源失败走比对/降级，不中断
        logger.warning("人民币汇率中间价获取失败: %s", exc)
        if errors is not None:
            errors.append(f"official: 人民币汇率中间价获取失败：{str(exc)[:200]}")

    reference_source, reference_rates = None, {}
    try:
        reference_source, reference_rates = _fetch_third_party_quotes()
    except RuntimeError as exc:
        logger.warning("%s", exc)
        if errors is not None:
            errors.append(f"third_party: {str(exc)[:200]}")

    official = _latest_official(db)
    if official and reference_rates:
        record_rate_checks(db, official, reference_source, reference_rates, today)

    current: Dict[str, Decimal] = {}
    stale_before = today - timedelta(days=settings.fx_official_max_stale_days)
    for currency in REQUIRED_RATE_CURRENCIES:
        official_entry = official.get(currency)
        if official_entry is not None and official_entry[0] >= stale_before:
            current[currency] = official_entry[1]
            continue
        if currency in reference_rates:
            logger.warning(
                "%s 官方中间价缺失或超过 %s 天未更新（最近 %s），降级写入第三方 %s 报价",
                currency,
                settings.fx_official_max_stale_days,
                official_entry[0].isoformat() if official_entry else "无",
                reference_source,
            )
            _upsert_unless_manual(db, currency, reference_rates[currency], today, reference_source)
            current[currency] = reference_rates[currency]
        elif official_entry is not None:
            current[currency] = official_entry[1]
    return current


def fx_source_warnings(db: Session) -> List[str]:
    """数据质量告警：当前生效汇率不是官方中间价，或最近一次比对差异超阈值。"""
    warnings: List[str] = []
    for currency in REQUIRED_RATE_CURRENCIES:
        info = get_rate_info(db, currency)
        if info and info["source"] in THIRD_PARTY_SOURCES:
            warnings.append(
                f"{currency}/CNY 当前使用第三方报价（{info['source']}，"
                f"{info['effective_date'].isoformat()}），官方中间价暂不可用"
            )
    latest_check_date = db.query(func.max(ExchangeRateCheck.check_date)).scalar()
    if latest_check_date is not None:
        threshold = Decimal(str(settings.fx_check_warn_pct))
        for check in (
            db.query(ExchangeRateCheck)
            .filter(ExchangeRateCheck.check_date == latest_check_date)
            .order_by(ExchangeRateCheck.from_currency)
        ):
            if abs(Decimal(str(check.diff_pct))) > threshold:
                warnings.append(
                    f"{check.from_currency}/CNY 第三方报价（{check.reference_source}）与官方中间价"
                    f"（{check.official_date.isoformat()}）相差 {Decimal(str(check.diff_pct)):+.2f}%"
                )
    return warnings


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
    rate_record = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency,
            ExchangeRate.is_active.is_(True),
        )
        .order_by(desc(ExchangeRate.effective_date))
        .first()
    )

    if rate_record:
        return {
            "rate": Decimal(str(rate_record.rate)),
            "effective_date": rate_record.effective_date,
            "source": rate_record.source,
        }

    return None
