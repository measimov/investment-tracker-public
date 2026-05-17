from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from types import SimpleNamespace
from ..database import get_db
from ..models.transaction import Transaction
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.user import User
from ..schemas.transaction import TransactionCreate, TransactionUpdate, TransactionResponse
from ..services.holding_service import recalculate_holdings, validate_no_oversell
from ..core.deps import get_current_active_user

router = APIRouter()


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
):
    query = db.query(Transaction).filter(Transaction.user_id == user_id)

    if symbol:
        query = query.filter(Transaction.symbol == symbol)
    if market:
        query = query.filter(Transaction.market == market)
    if transaction_type:
        query = query.filter(Transaction.transaction_type == transaction_type)

    return query


@router.post("", response_model=TransactionResponse, status_code=201)
def create_transaction(
    transaction: TransactionCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Create a new transaction and recalculate holdings for the authenticated user."""
    candidate = _transaction_candidate(**transaction.model_dump())
    _validate_transaction_sequence(db, current_user.id, candidate)

    db_transaction = Transaction(**transaction.model_dump(), user_id=current_user.id)
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)

    # Recalculate holdings for this user, symbol and market
    recalculate_holdings(db, current_user.id, db_transaction.symbol, db_transaction.market)

    return db_transaction


@router.get("", response_model=List[TransactionResponse])
def get_transactions(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=10000),
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    transaction_type: Optional[str] = None,
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
    )

    transactions = query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).offset(skip).limit(limit).all()
    return transactions


@router.get("/count")
def get_transactions_count(
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    transaction_type: Optional[str] = None,
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
    """Update a transaction and recalculate holdings for the authenticated user."""
    db_transaction = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id
    ).first()
    if not db_transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Store old values for recalculation
    old_symbol = db_transaction.symbol
    old_market = db_transaction.market

    # Update fields
    update_data = transaction_update.model_dump(exclude_unset=True)
    candidate_data = {
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
    _validate_transaction_sequence(
        db,
        current_user.id,
        candidate,
        exclude_transaction_id=db_transaction.id,
    )

    for field, value in update_data.items():
        setattr(db_transaction, field, value)

    db.commit()
    db.refresh(db_transaction)

    # Recalculate holdings for old and new symbol/market if changed
    recalculate_holdings(db, current_user.id, old_symbol, old_market)
    if db_transaction.symbol != old_symbol or db_transaction.market != old_market:
        recalculate_holdings(db, current_user.id, db_transaction.symbol, db_transaction.market)

    return db_transaction


@router.delete("/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Delete a transaction and recalculate holdings for the authenticated user."""
    db_transaction = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id
    ).first()
    if not db_transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    symbol = db_transaction.symbol
    market = db_transaction.market

    db.query(BrokerFundFlow).filter(
        BrokerFundFlow.user_id == current_user.id,
        BrokerFundFlow.transaction_id == db_transaction.id
    ).update(
        {BrokerFundFlow.transaction_id: None},
        synchronize_session=False
    )
    db.delete(db_transaction)
    db.commit()

    # Recalculate holdings
    recalculate_holdings(db, current_user.id, symbol, market)

    return None
