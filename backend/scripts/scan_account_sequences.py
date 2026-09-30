#!/usr/bin/env python3
"""只读扫描：存量数据在新超卖校验（#270）下会不会阻塞编辑。

#270 把手工录入 / 标准 CSV 导入的超卖校验换成 validate_account_sequence（计入公司行动）。
它每次都重放**整个账户桶**的历史，所以存量里任何一个桶只要在新口径下超卖（反向拆股之后
卖出、new_shares 兜底导致的数量缩水……），该桶里此后的**无关**编辑/删除都会被拒。
公司行动的 PATCH 也改为按创建期规则校验合并后的记录：比例不可解析、配股缺认购价的
存量行会变得不可编辑。合入/部署前跑一次，先把列出的数据修好。

    cd backend && source venv/bin/activate
    python scripts/scan_account_sequences.py            # 全部用户
    python scripts/scan_account_sequences.py --user-id 2

部署机上：

    docker compose run --rm backend python scripts/scan_account_sequences.py

只读，不写任何表。发现问题时退出码 1。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def scan(user_id=None):
    from sqlalchemy import distinct

    from app.database import SessionLocal
    from app.models.corporate_action import CorporateAction
    from app.models.transaction import Transaction
    from app.schemas.corporate_action import validate_quantity_action_fields
    from app.services.holding_service import validate_account_sequence

    db = SessionLocal()
    try:
        buckets = set()
        txn_query = db.query(
            distinct(Transaction.user_id),
            Transaction.broker_account_id,
            Transaction.symbol,
            Transaction.market,
        )
        action_query = db.query(
            distinct(CorporateAction.user_id),
            CorporateAction.broker_account_id,
            CorporateAction.symbol,
            CorporateAction.market,
        ).filter(CorporateAction.action_type == "OPENING_POSITION")
        if user_id is not None:
            txn_query = txn_query.filter(Transaction.user_id == user_id)
            action_query = action_query.filter(CorporateAction.user_id == user_id)
        buckets.update(txn_query.all())
        buckets.update(action_query.all())

        oversold = []
        for owner, account_id, symbol, market in sorted(
            buckets, key=lambda row: (row[0], row[1] or 0, row[2], row[3])
        ):
            try:
                validate_account_sequence(
                    db,
                    user_id=owner,
                    broker_account_id=account_id,
                    symbol=symbol,
                    market=market,
                )
            except ValueError as exc:
                oversold.append((owner, account_id, symbol, market, str(exc)))

        invalid_actions = []
        actions = db.query(CorporateAction)
        if user_id is not None:
            actions = actions.filter(CorporateAction.user_id == user_id)
        for action in actions.order_by(CorporateAction.id):
            try:
                validate_quantity_action_fields(action)
            except ValueError as exc:
                invalid_actions.append((action, str(exc)))
        return len(buckets), oversold, invalid_actions
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--user-id", type=int, help="只扫描该用户（默认全部）")
    args = parser.parse_args()

    bucket_count, oversold, invalid_actions = scan(args.user_id)
    print(f"扫描账户桶 {bucket_count} 个")
    print(f"\n--- 新口径下超卖的账户桶：{len(oversold)} ---")
    for owner, account_id, symbol, market, message in oversold:
        account = "未指定账户" if account_id is None else f"账户#{account_id}"
        print(f"  用户#{owner} {account} {symbol}（{market}）：{message}")
    print(f"\n--- 数量字段不可用的公司行动（PATCH 将被拒）：{len(invalid_actions)} ---")
    for action, message in invalid_actions:
        print(
            f"  #{action.id} 用户#{action.user_id} {action.symbol}（{action.market}）"
            f"{action.action_type} {action.ex_date}：{message}"
        )
    if oversold or invalid_actions:
        print("\n存在问题：先修数据，再合入/部署 #270。")
        return 1
    print("\n无问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
