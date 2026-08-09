from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from types import SimpleNamespace
from decimal import Decimal
from ..database import get_db
from ..models.transaction import Transaction
from ..models.broker_account import BrokerAccount
from ..models.user import User
from ..schemas.transaction import (
    TransactionCreate,
    TransactionUpdate,
    TransactionResponse,
    TransferCreate,
)
from ..services.holding_service import (
    AccountReplayError,
    lock_record,
    lock_security_timeline,
    recalculate_holdings,
    replay_account_buckets,
    validate_no_oversell,
)
from ..core.deps import get_current_active_user
from ._ownership import ensure_record_is_mutable, get_owned_record, validate_owned_references

router = APIRouter()


IMMUTABLE_IMPORTED_TRANSACTION_DETAIL = (
    "Imported transactions cannot be modified or deleted; correct the source import instead."
)


def _ensure_transaction_is_mutable(db: Session, user_id: int, transaction: Transaction) -> None:
    ensure_record_is_mutable(
        db,
        user_id,
        transaction,
        source_link_field="transaction_id",
        detail=IMMUTABLE_IMPORTED_TRANSACTION_DETAIL,
    )


def _transaction_candidate(**data):
    return SimpleNamespace(id=None, **data)


def _validate_transaction_sequence(
    db: Session,
    user_id: int,
    candidate,
    exclude_transaction_id: int = None,
):
    query = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == candidate.symbol,
        Transaction.market == candidate.market,
    )
    if candidate.broker_account_id is None:
        query = query.filter(Transaction.broker_account_id.is_(None))
    else:
        query = query.filter(
            Transaction.broker_account_id == candidate.broker_account_id
        )
    if exclude_transaction_id is not None:
        query = query.filter(Transaction.id != exclude_transaction_id)
    transactions = query.all()
    transactions.append(candidate)
    try:
        validate_no_oversell(transactions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _build_transaction_query(
    db: Session,
    user_id: int,
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    transaction_type: Optional[str] = None,
    broker_account_id: Optional[int] = None,
    unassigned_account: bool = False,
):
    query = db.query(Transaction).filter(Transaction.user_id == user_id)

    if symbol:
        query = query.filter(Transaction.symbol == symbol)
    if market:
        query = query.filter(Transaction.market == market)
    if transaction_type:
        query = query.filter(Transaction.transaction_type == transaction_type)
    if unassigned_account:
        query = query.filter(Transaction.broker_account_id.is_(None))
    elif broker_account_id is not None:
        query = query.filter(Transaction.broker_account_id == broker_account_id)

    return query


@router.post("", response_model=TransactionResponse, status_code=201)
def create_transaction(
    transaction: TransactionCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Create a new transaction and recalculate holdings for the authenticated user.

    时间线锁 → 校验 → 写入 → 重算在同一事务内提交，与转仓/公司行动写入口
    共用同一 advisory lock 串行化，避免"校验读旧时间线、提交在别人之后"的竞态。
    """
    transaction_data = transaction.model_dump()
    try:
        lock_security_timeline(
            db, current_user.id, transaction_data["symbol"], transaction_data["market"]
        )
        validate_owned_references(db, current_user.id, transaction_data)
        candidate = _transaction_candidate(**transaction_data)
        _validate_transaction_sequence(db, current_user.id, candidate)

        db_transaction = Transaction(**transaction_data, user_id=current_user.id)
        db.add(db_transaction)
        db.flush()
        recalculate_holdings(
            db, current_user.id, db_transaction.symbol, db_transaction.market, commit=False
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(db_transaction)
    return db_transaction


@router.post("/transfer", response_model=List[TransactionResponse], status_code=201)
def create_transfer(
    transfer: TransferCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """账户间转仓：创建 TRANSFER_OUT/TRANSFER_IN 互指交易对，成本基础跟随迁移。

    不产生任何盈亏或现金流；账户为 null 表示"未指定账户"桶。返回 [转出腿, 转入腿]。

    校验策略：对完整时间线做两次严格的按账户重放（插入前基线 + 插入后）——
    这同时覆盖了历史日期转仓（transfer_date 当天转出账户必须真有足够数量）
    和转仓与后续交易的冲突（转出后未来卖出会超卖）。与所有时间线写入口共用
    事务级 advisory lock 串行化并发。交易对写入与派生持仓重算在同一事务内提交。
    """
    if transfer.from_broker_account_id == transfer.to_broker_account_id:
        raise HTTPException(status_code=422, detail="转出与转入账户不能相同")
    for account_id in (transfer.from_broker_account_id, transfer.to_broker_account_id):
        if account_id is not None:
            get_owned_record(
                db, BrokerAccount, account_id, current_user.id, "Broker account not found"
            )

    try:
        # 与全部时间线写入口共用的事务级 advisory lock，串行化并发写。
        lock_security_timeline(db, current_user.id, transfer.symbol, transfer.market)

        # 基线：现有交易的账户归属必须自洽，否则转仓建立在降级合并桶上没有意义。
        try:
            baseline = replay_account_buckets(
                db, current_user.id, transfer.symbol, transfer.market
            )
        except AccountReplayError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"现有交易的账户归属不一致，请先修正数据再转仓：{exc}",
            ) from exc

        source_state = baseline.get(transfer.from_broker_account_id)
        if source_state is None or source_state['quantity'] <= 0:
            raise HTTPException(status_code=422, detail="转出账户当前无该证券持仓")

        common = {
            "user_id": current_user.id,
            "symbol": transfer.symbol,
            "name": source_state['name'] or transfer.symbol,
            "market": transfer.market,
            "quantity": transfer.quantity,
            # 转仓不产生盈亏；price 仅作展示口径，记录转出时的平均成本。
            "price": source_state['avg_cost'],
            "fee": Decimal("0"),
            "transaction_date": transfer.transfer_date,
            "currency": source_state['currency'],
            "notes": transfer.notes,
        }
        out_leg = Transaction(
            **common,
            broker_account_id=transfer.from_broker_account_id,
            transaction_type="TRANSFER_OUT",
        )
        db.add(out_leg)
        db.flush()
        in_leg = Transaction(
            **common,
            broker_account_id=transfer.to_broker_account_id,
            transaction_type="TRANSFER_IN",
            linked_transaction_id=out_leg.id,
        )
        db.add(in_leg)
        db.flush()
        out_leg.linked_transaction_id = in_leg.id
        db.flush()

        # 插入后重放：按 transfer_date 落在真实时间线里校验，转出账户当日
        # 数量不足或与后续交易冲突都会在这里被拒绝。
        try:
            replay_account_buckets(db, current_user.id, transfer.symbol, transfer.market)
        except AccountReplayError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"转仓无法成立：{exc}",
            ) from exc

        recalculate_holdings(
            db, current_user.id, transfer.symbol, transfer.market, commit=False
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(out_leg)
    db.refresh(in_leg)
    return [out_leg, in_leg]


@router.get("", response_model=List[TransactionResponse])
def get_transactions(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    transaction_type: Optional[str] = None,
    broker_account_id: Optional[int] = None,
    unassigned_account: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get list of transactions for the authenticated user with optional filters."""
    query = _build_transaction_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        transaction_type=transaction_type,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    )

    transactions = query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).offset(skip).limit(limit).all()
    return transactions


@router.get("/count")
def get_transactions_count(
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    transaction_type: Optional[str] = None,
    broker_account_id: Optional[int] = None,
    unassigned_account: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get transaction count for the authenticated user with optional filters."""
    total = _build_transaction_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        transaction_type=transaction_type,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    ).count()
    return {"total": total}


@router.get("/{transaction_id}", response_model=TransactionResponse)
def get_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get a specific transaction by ID for the authenticated user."""
    transaction = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id
    ).first()
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return transaction


@router.put("/{transaction_id}", response_model=TransactionResponse)
def update_transaction(
    transaction_id: int,
    transaction_update: TransactionUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Update a transaction and recalculate holdings for the authenticated user.

    锁序（防死锁纪律）：记录锁 → 锁内重读该行取得新鲜的旧键 → 时间线锁
    （旧+新键排序）。等待记录锁期间该行可能已被并发修改（如 symbol 被改），
    锁前读取的任何字段都不可信。
    """
    update_data = transaction_update.model_dump(exclude_unset=True)

    try:
        lock_record(db, "transaction-record", transaction_id)

        # 锁内重读：等待锁期间行可能被并发 update/delete。
        db_transaction = db.query(Transaction).filter(
            Transaction.id == transaction_id,
            Transaction.user_id == current_user.id
        ).first()
        if not db_transaction:
            raise HTTPException(status_code=404, detail="Transaction not found")
        db.refresh(db_transaction)

        _ensure_transaction_is_mutable(db, current_user.id, db_transaction)
        if db_transaction.transaction_type in ("TRANSFER_OUT", "TRANSFER_IN"):
            raise HTTPException(
                status_code=409,
                detail="转仓交易不支持编辑；请删除转仓对后重新创建。",
            )

        # 新鲜的旧键
        old_symbol = db_transaction.symbol
        old_market = db_transaction.market

        validate_owned_references(db, current_user.id, update_data)
        candidate_data = {
            "broker_account_id": db_transaction.broker_account_id,
            "import_batch_id": db_transaction.import_batch_id,
            "symbol": db_transaction.symbol,
            "name": db_transaction.name,
            "market": db_transaction.market,
            "transaction_type": db_transaction.transaction_type,
            "quantity": db_transaction.quantity,
            "price": db_transaction.price,
            "fee": db_transaction.fee,
            "transaction_date": db_transaction.transaction_date,
            "currency": db_transaction.currency,
            "notes": db_transaction.notes,
        }
        candidate_data.update(update_data)
        candidate = _transaction_candidate(**candidate_data)

        # 时间线锁：旧+新两键排序取锁
        lock_keys = sorted({
            (old_symbol, old_market),
            (candidate.symbol, candidate.market),
        })
        for lock_symbol, lock_market in lock_keys:
            lock_security_timeline(db, current_user.id, lock_symbol, lock_market)

        _validate_transaction_sequence(
            db,
            current_user.id,
            candidate,
            exclude_transaction_id=db_transaction.id,
        )

        for field, value in update_data.items():
            setattr(db_transaction, field, value)
        db.flush()

        recalculate_holdings(db, current_user.id, old_symbol, old_market, commit=False)
        if db_transaction.symbol != old_symbol or db_transaction.market != old_market:
            recalculate_holdings(
                db, current_user.id, db_transaction.symbol, db_transaction.market,
                commit=False,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(db_transaction)
    return db_transaction


@router.delete("/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Delete a transaction and recalculate holdings for the authenticated user.

    锁序：记录锁（转仓对锁两腿，按 id 升序）→ 锁内重读 → 时间线锁。
    先做一次无锁窥视只为确定要锁哪些记录 id；一切业务判断基于锁内重读。
    """
    # 无锁窥视：确定记录锁集合（转仓对需要锁两条腿）。
    peek = db.query(
        Transaction.id, Transaction.linked_transaction_id
    ).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id
    ).first()
    if not peek:
        raise HTTPException(status_code=404, detail="Transaction not found")
    record_ids = sorted({transaction_id} | (
        {peek.linked_transaction_id} if peek.linked_transaction_id is not None else set()
    ))
    for record_id in record_ids:
        lock_record(db, "transaction-record", record_id)

    # 锁内重读：等待锁期间行可能被并发修改或删除。
    db_transaction = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id
    ).first()
    if not db_transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    db.refresh(db_transaction)

    _ensure_transaction_is_mutable(db, current_user.id, db_transaction)

    symbol = db_transaction.symbol
    market = db_transaction.market

    if db_transaction.linked_transaction_id is not None:
        # 转仓对成对删除；删除后先做严格按账户重放——若目标账户已有依赖该
        # 转仓的卖出，删除会让时间线不再自洽，必须 409 拒绝而不是让重算
        # 静默降级为合并桶。全程与重算同事务提交。
        try:
            lock_security_timeline(db, current_user.id, symbol, market)
            linked = db.query(Transaction).filter(
                Transaction.id == db_transaction.linked_transaction_id,
                Transaction.user_id == current_user.id,
            ).first()
            if linked is not None:
                db.delete(linked)
            db.delete(db_transaction)
            db.flush()
            try:
                replay_account_buckets(db, current_user.id, symbol, market)
            except AccountReplayError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"存在依赖该转仓的后续交易，不能删除：{exc}",
                ) from exc
            recalculate_holdings(db, current_user.id, symbol, market, commit=False)
            db.commit()
        except ValueError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"删除转仓对会使持仓重放失败：{exc}",
            ) from exc
        except Exception:
            db.rollback()
            raise
        return None

    try:
        lock_security_timeline(db, current_user.id, symbol, market)
        db.delete(db_transaction)
        db.flush()
        recalculate_holdings(db, current_user.id, symbol, market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"删除该交易会使持仓重放失败：{exc}",
        ) from exc
    except Exception:
        db.rollback()
        raise

    return None
