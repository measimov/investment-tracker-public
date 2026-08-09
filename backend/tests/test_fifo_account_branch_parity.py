"""单账户 / 多账户两条 FIFO 分支必须对同一份数据得出相同数字。

同一证券会在两条分支间来回切换：没有转仓、账户归属一致时走
calculate_fifo_pnl（合并重放）；一旦出现转仓或跨账户持仓，就改走
replay_fifo_multi_account + merge_account_fifo_results。两条路径若在精度上
不一致，用户会看到"加了一笔转仓，已实现盈亏就变了"。

历史缺陷：账户级结果在 merge **之前**就转成了 float，精度在聚合前已丢，
之后再 Decimal(str(...)) 也补不回来。用 NUMERIC(18,8) 量级的金额实测：
8931992295.31575055 + 1641890924.22944734 两条分支相差 1.9073486328125e-06。
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.portfolio.fifo import (
    calculate_fifo_pnl,
    merge_account_fifo_results,
    replay_fifo_multi_account,
)

SYMBOL, MARKET = "600000", "A股"

# 复审给出的、能暴露 float 精度丢失的金额（NUMERIC(18,8) 允许的量级）
LOT_A = Decimal("8931992295.31575055")
LOT_B = Decimal("1641890924.22944734")


def _txn(txn_id, txn_type, quantity, price, day, account_id, fee="0"):
    return SimpleNamespace(
        id=txn_id,
        transaction_type=txn_type,
        transaction_date=date(2026, 1, day),
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        fee=Decimal(fee),
        broker_account_id=account_id,
        symbol=SYMBOL,
        market=MARKET,
        linked_transaction_id=None,
    )


def _scenario(account_a, account_b):
    """两笔买入 + 两笔全额卖出；金额刻意选在 float 精度边界上。"""
    return [
        _txn(1, "BUY", 1, LOT_A, 2, account_a),
        _txn(2, "BUY", 1, LOT_B, 3, account_b),
        _txn(3, "SELL", 1, LOT_A, 10, account_a),
        _txn(4, "SELL", 1, LOT_B, 11, account_b),
    ]


def test_single_and_multi_account_branches_agree_on_sold_cost():
    """两个账户 → 多账户分支；同样的交易挂同一账户 → 单账户分支。"""
    multi = merge_account_fifo_results(
        SYMBOL, MARKET,
        replay_fifo_multi_account(SYMBOL, MARKET, _scenario(1, 2), []),
    )
    single = calculate_fifo_pnl(SYMBOL, MARKET, _scenario(1, 1), [])

    # 修复前：10573883219.545198 vs 10573883219.545197
    assert multi["sold_cost"] == single["sold_cost"], (
        f"两条分支的 sold_cost 不一致：多账户 {multi['sold_cost']!r} "
        f"vs 单账户 {single['sold_cost']!r}"
    )
    assert multi["realized_pnl"] == single["realized_pnl"]
    assert multi["current_holdings_cost"] == single["current_holdings_cost"]


def test_merged_sold_cost_matches_exact_decimal_sum():
    """聚合结果必须等于 Decimal 精确和，而不是逐账户 float 相加的结果。"""
    multi = merge_account_fifo_results(
        SYMBOL, MARKET,
        replay_fifo_multi_account(SYMBOL, MARKET, _scenario(1, 2), []),
    )

    assert multi["sold_cost"] == float(LOT_A + LOT_B)
    # 逐账户先 float 再相加会得到另一个值——这正是修复前的结果
    assert multi["sold_cost"] != float(LOT_A) + float(LOT_B)


def test_merged_output_is_float_at_the_user_level_exit():
    """内部保持 Decimal，但用户级出口仍必须是 float（既有消费方的形状）。"""
    multi = merge_account_fifo_results(
        SYMBOL, MARKET,
        replay_fifo_multi_account(SYMBOL, MARKET, _scenario(1, 2), []),
    )

    for field in ("realized_pnl", "sold_cost", "current_holdings_cost"):
        assert isinstance(multi[field], float), f"{field} 应在出口转 float"
    for trade in multi["closed_trades"]:
        for field in ("quantity", "proceeds", "matched_cost", "realized_pnl"):
            assert isinstance(trade[field], float), f"closed_trades.{field} 应为 float"


def test_open_lots_also_agree_between_branches():
    """未平仓批次（buy_queue）同样不得在两条分支间漂移。"""
    open_only = [
        _txn(1, "BUY", 1, LOT_A, 2, 1),
        _txn(2, "BUY", 1, LOT_B, 3, 2),
    ]
    multi = merge_account_fifo_results(
        SYMBOL, MARKET, replay_fifo_multi_account(SYMBOL, MARKET, open_only, []),
    )
    single = calculate_fifo_pnl(
        SYMBOL, MARKET,
        [_txn(1, "BUY", 1, LOT_A, 2, 1), _txn(2, "BUY", 1, LOT_B, 3, 1)],
        [],
    )

    assert multi["current_holdings_cost"] == single["current_holdings_cost"]
    assert multi["current_holdings_cost"] == float(LOT_A + LOT_B)
    for lot in multi["buy_queue"]:
        assert isinstance(lot["total_cost"], float)


def test_ordinary_amounts_are_unaffected():
    """常规金额下两条分支本就一致——守住修复没有改变正常口径。"""
    plain = [
        _txn(1, "BUY", 100, "10", 2, 1),
        _txn(2, "BUY", 50, "12", 3, 2),
        _txn(3, "SELL", 100, "15", 10, 1),
    ]
    multi = merge_account_fifo_results(
        SYMBOL, MARKET, replay_fifo_multi_account(SYMBOL, MARKET, plain, []),
    )

    assert multi["sold_cost"] == pytest.approx(1000.0)
    assert multi["realized_pnl"] == pytest.approx(500.0)
