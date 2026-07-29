from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
from ..database import get_db
from ..models.corporate_action import CorporateAction
from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.user import User
from ..schemas.corporate_action import (
    CorporateActionCreate,
    CorporateActionUpdate,
    CorporateActionResponse,
    CashDividendCreate,
    StockDividendCreate
)
from ..services import exchange_rate_service
from ..services.statistics_service import cash_dividend_amounts
from ..services.holding_service import lock_record, lock_security_timeline, recalculate_holdings
from ..core.deps import get_current_active_user
from ._ownership import get_owned_record

router = APIRouter()


def _validate_owned_references(db: Session, user_id: int, data: dict) -> None:
    references = {
        "broker_account_id": (BrokerAccount, "Broker account not found"),
    }
    for field, (model, detail) in references.items():
        record_id = data.get(field)
        if record_id is not None:
            get_owned_record(db, model, record_id, user_id, detail)


def _ensure_corporate_action_is_mutable(
    db: Session,
    user_id: int,
    action: CorporateAction,
) -> None:
    if action.import_batch_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Imported corporate actions cannot be modified or deleted; "
                "correct the source import instead."
            ),
        )
    broker_source = db.query(BrokerFundFlow.id).filter(
        BrokerFundFlow.user_id == user_id,
        BrokerFundFlow.corporate_action_id == action.id,
    ).first()
    ibkr_source = db.query(IbkrActivityFlow.id).filter(
        IbkrActivityFlow.user_id == user_id,
        IbkrActivityFlow.corporate_action_id == action.id,
    ).first()
    if broker_source or ibkr_source:
        raise HTTPException(
            status_code=409,
            detail=(
                "Imported corporate actions cannot be modified or deleted; "
                "correct the source import instead."
            ),
        )


def _build_corporate_action_query(
    db: Session,
    user_id: int,
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    action_type: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    broker_account_id: Optional[int] = None,
    unassigned_account: bool = False,
):
    query = db.query(CorporateAction).filter(CorporateAction.user_id == user_id)

    if symbol and symbol.strip():
        query = query.filter(CorporateAction.symbol.ilike(f"%{symbol.strip()}%"))
    if market:
        query = query.filter(CorporateAction.market == market)
    if action_type:
        query = query.filter(CorporateAction.action_type == action_type)
    if start_date:
        query = query.filter(CorporateAction.ex_date >= start_date)
    if end_date:
        query = query.filter(CorporateAction.ex_date <= end_date)
    if unassigned_account:
        query = query.filter(CorporateAction.broker_account_id.is_(None))
    elif broker_account_id is not None:
        query = query.filter(CorporateAction.broker_account_id == broker_account_id)

    return query


@router.post("", response_model=CorporateActionResponse, status_code=201)
def create_corporate_action(
    action: CorporateActionCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    创建公司行动记录

    支持的类型:
    - CASH_DIVIDEND: 现金股息
    - STOCK_DIVIDEND: 股票股息/红股
    - RIGHTS_ISSUE: 配股
    - STOCK_SPLIT: 拆股
    - REVERSE_SPLIT: 合股
    - BONUS_ISSUE: 送股
    - SPIN_OFF: 拆分
    - MERGER: 合并
    """
    action_data = action.model_dump()
    try:
        # 与交易/转仓写入口共用时间线锁；写入与重算同事务提交。
        lock_security_timeline(db, current_user.id, action.symbol, action.market)
        _validate_owned_references(db, current_user.id, action_data)
        db_action = CorporateAction(**action_data, user_id=current_user.id)
        db.add(db_action)
        db.flush()

        # 如果是会影响持仓的公司行动，重新计算持仓
        if action.action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
            recalculate_holdings(db, current_user.id, action.symbol, action.market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"该公司行动会使持仓重放失败：{exc}") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(db_action)
    return db_action


@router.post("/cash-dividend", response_model=CorporateActionResponse, status_code=201)
def create_cash_dividend(
    dividend: CashDividendCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    快捷创建现金股息记录

    自动计算税后金额
    """
    action = dividend.to_corporate_action()
    return create_corporate_action(action, current_user, db)


@router.post("/stock-dividend", response_model=CorporateActionResponse, status_code=201)
def create_stock_dividend(
    dividend: StockDividendCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    快捷创建红股/股票股息记录

    会自动重新计算持仓成本
    """
    action = dividend.to_corporate_action()
    return create_corporate_action(action, current_user, db)


@router.get("", response_model=List[CorporateActionResponse])
def list_corporate_actions(
    skip: int = Query(0, ge=0, description="跳过记录数"),
    limit: int = Query(100, ge=1, le=1000, description="返回记录数"),
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    market: Optional[str] = Query(None, description="按市场筛选"),
    action_type: Optional[str] = Query(None, description="按行动类型筛选"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    broker_account_id: Optional[int] = Query(None, description="按券商账户筛选"),
    unassigned_account: bool = Query(False, description="仅显示未分配账户的记录"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取公司行动列表

    支持多种筛选条件
    """
    query = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        action_type=action_type,
        start_date=start_date,
        end_date=end_date,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    )

    actions = query.order_by(CorporateAction.ex_date.desc()).offset(skip).limit(limit).all()
    return actions


@router.get("/count")
def get_corporate_actions_count(
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    market: Optional[str] = Query(None, description="按市场筛选"),
    action_type: Optional[str] = Query(None, description="按行动类型筛选"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    broker_account_id: Optional[int] = Query(None, description="按券商账户筛选"),
    unassigned_account: bool = Query(False, description="仅显示未分配账户的记录"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取公司行动总数。"""
    total = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        action_type=action_type,
        start_date=start_date,
        end_date=end_date,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    ).count()
    return {"total": total}


@router.get("/symbol/{symbol}", response_model=List[CorporateActionResponse])
def get_actions_by_symbol(
    symbol: str,
    market: Optional[str] = Query(None, description="市场筛选"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取特定股票的所有公司行动记录"""
    query = db.query(CorporateAction).filter(
        CorporateAction.symbol.ilike(f"%{symbol.strip()}%"),
        CorporateAction.user_id == current_user.id
    )

    if market:
        query = query.filter(CorporateAction.market == market)

    actions = query.order_by(CorporateAction.ex_date.desc()).all()
    return actions


@router.get("/statistics/summary")
def get_corporate_actions_summary(
    symbol: Optional[str] = Query(None, description="股票代码"),
    market: Optional[str] = Query(None, description="市场"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    broker_account_id: Optional[int] = Query(None, description="按券商账户筛选"),
    unassigned_account: bool = Query(False, description="仅显示未分配账户的记录"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取公司行动统计摘要

    包括总股息收入、税费等
    """
    from decimal import Decimal

    query = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        start_date=start_date,
        end_date=end_date,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    )

    # 按类型统计
    actions = query.all()

    summary = {
        "total_count": len(actions),
        "by_type": {},
        "cash_dividends": {
            "count": 0,
            "total_dividend": Decimal("0"),
            "total_tax": Decimal("0"),
            "net_dividend": Decimal("0"),
            # 股息金额跨 CNY/HKD/USD 多币种：汇总必须按最新汇率折算成 CNY
            # （与统计分析页 get_dividend_summary 同口径），原币明细单独给出。
            "base_currency": "CNY",
            "by_currency": {},
            "missing_rate_currencies": [],
        }
    }
    by_currency: dict = {}
    missing_rate_currencies: set = set()

    for action in actions:
        # 按类型统计
        if action.action_type not in summary["by_type"]:
            summary["by_type"][action.action_type] = 0
        summary["by_type"][action.action_type] += 1

        # 现金股息统计
        if action.action_type == "CASH_DIVIDEND":
            summary["cash_dividends"]["count"] += 1
            currency = action.currency or "CNY"
            # 与统计页共用金额归一 helper（显式 net=0 保留 0，NULL 走 gross−tax 兜底）
            gross, tax, net = cash_dividend_amounts(action)
            # 缺汇率时不得把外币原值混进 CNY 总额；剔除并记录币种，
            # 原币金额仍完整保留在 by_currency 明细里。
            try:
                gross_cny = exchange_rate_service.convert_to_cny(db, gross, currency)
                tax_cny = exchange_rate_service.convert_to_cny(db, tax, currency)
                net_cny = exchange_rate_service.convert_to_cny(db, net, currency)
            except ValueError:
                missing_rate_currencies.add(currency)
                gross_cny = tax_cny = net_cny = Decimal("0")
            summary["cash_dividends"]["total_dividend"] += gross_cny
            summary["cash_dividends"]["total_tax"] += tax_cny
            summary["cash_dividends"]["net_dividend"] += net_cny
            bucket = by_currency.setdefault(currency, {
                "count": 0,
                "total_dividend": Decimal("0"),
                "total_tax": Decimal("0"),
                "net_dividend": Decimal("0"),
            })
            bucket["count"] += 1
            bucket["total_dividend"] += gross
            bucket["total_tax"] += tax
            bucket["net_dividend"] += net

    # 转换Decimal为float以便JSON序列化
    summary["cash_dividends"]["total_dividend"] = float(summary["cash_dividends"]["total_dividend"])
    summary["cash_dividends"]["total_tax"] = float(summary["cash_dividends"]["total_tax"])
    summary["cash_dividends"]["net_dividend"] = float(summary["cash_dividends"]["net_dividend"])
    summary["cash_dividends"]["missing_rate_currencies"] = sorted(missing_rate_currencies)
    summary["cash_dividends"]["by_currency"] = {
        currency: {
            "count": bucket["count"],
            "total_dividend": float(bucket["total_dividend"]),
            "total_tax": float(bucket["total_tax"]),
            "net_dividend": float(bucket["net_dividend"]),
        }
        for currency, bucket in sorted(by_currency.items())
    }

    return summary


@router.get("/{action_id:int}", response_model=CorporateActionResponse)
def get_corporate_action(
    action_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取单个公司行动记录"""
    action = db.query(CorporateAction).filter(
        CorporateAction.id == action_id,
        CorporateAction.user_id == current_user.id
    ).first()
    if not action:
        raise HTTPException(status_code=404, detail="公司行动记录不存在")
    return action


@router.put("/{action_id:int}", response_model=CorporateActionResponse)
def update_corporate_action(
    action_id: int,
    action_update: CorporateActionUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """更新公司行动记录

    锁序：记录锁 → 锁内重读（等待锁期间 symbol/market 可能被并发修改）→
    时间线锁（旧+新键排序）。
    """
    update_data = action_update.model_dump(exclude_unset=True)
    try:
        lock_record(db, "corporate-action-record", action_id)

        db_action = db.query(CorporateAction).filter(
            CorporateAction.id == action_id,
            CorporateAction.user_id == current_user.id
        ).first()
        if not db_action:
            raise HTTPException(status_code=404, detail="公司行动记录不存在")
        db.refresh(db_action)

        _ensure_corporate_action_is_mutable(db, current_user.id, db_action)

        # 锁内重读后的新鲜旧键
        old_symbol = db_action.symbol
        old_market = db_action.market
        old_action_type = db_action.action_type

        new_symbol = update_data.get("symbol", old_symbol)
        new_market = update_data.get("market", old_market)
        # 锁旧、新两条时间线（排序取锁避免死锁）
        for lock_symbol, lock_market in sorted({(old_symbol, old_market), (new_symbol, new_market)}):
            lock_security_timeline(db, current_user.id, lock_symbol, lock_market)
        _validate_owned_references(db, current_user.id, update_data)
        for field, value in update_data.items():
            setattr(db_action, field, value)
        db.flush()

        # 如果是会影响持仓的公司行动，重新计算持仓
        if old_action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
            recalculate_holdings(db, current_user.id, old_symbol, old_market, commit=False)
            if db_action.symbol != old_symbol or db_action.market != old_market:
                recalculate_holdings(db, current_user.id, db_action.symbol, db_action.market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"该修改会使持仓重放失败：{exc}") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(db_action)
    return db_action


@router.delete("/{action_id:int}", status_code=204)
def delete_corporate_action(
    action_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """删除公司行动记录

    锁序：记录锁 → 锁内重读 → 时间线锁。
    """
    try:
        lock_record(db, "corporate-action-record", action_id)

        db_action = db.query(CorporateAction).filter(
            CorporateAction.id == action_id,
            CorporateAction.user_id == current_user.id
        ).first()
        if not db_action:
            raise HTTPException(status_code=404, detail="公司行动记录不存在")
        db.refresh(db_action)

        _ensure_corporate_action_is_mutable(db, current_user.id, db_action)

        symbol = db_action.symbol
        market = db_action.market
        action_type = db_action.action_type

        lock_security_timeline(db, current_user.id, symbol, market)
        db.delete(db_action)
        db.flush()

        # 如果是会影响持仓的公司行动，重新计算持仓
        if action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
            recalculate_holdings(db, current_user.id, symbol, market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"删除该公司行动会使持仓重放失败：{exc}") from exc
    except Exception:
        db.rollback()
        raise

    return None
