"""导入产物的备注只写人能读懂的一句（#286）：新导入走 import_note，存量由迁移 0040 清理。"""

from app.services.broker_import_common import append_note, import_note
from tests.helpers import load_migration

clean_note = load_migration("20260930_0040").clean_note


def test_import_note_and_append_once():
    assert import_note("东方财富证券", "证券买入", None) == "东方财富证券对账单导入 · 证券买入"
    note = append_note(import_note("招商证券", "红利入账"), "招商证券红利税补缴")
    assert append_note(note, "招商证券红利税补缴") == note  # 同一笔股息多行税只记一次
    assert append_note(None, "IBKR 预扣税") == "IBKR 预扣税"


def test_migration_strips_machine_fields():
    assert (
        clean_note(
            "东方财富证券对账单; scope=stock; row=1; 业务=证券买入; source_cny_price=1.0240; "
            "source_cny_amount=1024.00; settlement_rate="
        )
        == "东方财富证券对账单 · 证券买入"
    )
    assert (
        clean_note(
            "东方财富证券对账单; scope=hk_connect; row=3; 业务=红利入账; row_hash="
            + "a" * 64
            + "; 东方财富证券红利税 row=9; row_hash="
            + "b" * 64
            + "; 东方财富证券红利税 row=10; row_hash="
            + "c" * 64
        )
        == "东方财富证券对账单 · 红利入账 · 东方财富证券红利税"
    )
    assert (
        clean_note(
            "IBKR Activity Statement; account=U1; row=5; type=Buy; raw_symbol=PDD; gross=-1; net=-1"
        )
        == "IBKR对账单导入"
    )
    assert (
        clean_note("招商证券对账单; 流水号=123; 合同编号=; 业务=证券买入")
        == "招商证券对账单 · 证券买入"
    )


def test_migration_leaves_user_notes_and_relisting_marker_alone():
    assert clean_note("看好长期，分批买入") == "看好长期，分批买入"
    assert clean_note("row 1 of my plan") == "row 1 of my plan"
    relisting = (
        "IBKR Activity Statement; synthetic_relisting_transfer; A->B; transfer_date=2025-01-01"
    )
    assert clean_note(relisting) == relisting
    assert clean_note(None) is None
