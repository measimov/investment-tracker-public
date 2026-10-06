"""重抽脚本只统计完整计划内的待办；旧身份保留并单列，不制造永远无法归零的任务。"""

import runpy
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import report_statement_service as svc
from app.services.report_statement_prompts import STATEMENT_PROMPT_VERSION
from app.services.report_statements import STATEMENT_EXTRACTOR_VERSION
from app.services.profile_store import upsert_profile_row

SCRIPT = runpy.run_path(Path(__file__).resolve().parents[1] / "scripts/rerun_report_statements.py")
CURRENT = {
    "status": "ok",
    "extractor_version": STATEMENT_EXTRACTOR_VERSION,
    "prompt_version": STATEMENT_PROMPT_VERSION,
}
OLD = {"status": "failed", "extractor_version": 1, "attempts": 4}


@pytest.fixture
def db():
    session = SessionLocal()
    session.query(SecurityProfileData).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.query(SecurityProfileData).delete()
        session.commit()
        session.close()


def _write(db, key, payload, *, dataset=svc.EXTRACT_DATASET, symbol="01023"):
    upsert_profile_row(db, symbol, "港股", dataset, key, payload)
    db.commit()


def test_stale_rows_separate_replaced_interim_identities_and_keep_planned_older_year(db):
    _write(
        db,
        "current",
        {
            "period_keys": [
                "20251231|interim",
                "20241231|interim",
                "20250630|annual",
                "20150630|annual",
            ]
        },
        dataset=svc.PLAN_DATASET,
    )
    for key, payload in (
        ("20251231|interim", CURRENT),
        ("20250630|annual", OLD),
        ("20150630|annual", OLD),
        ("20250630|interim", OLD),
        ("20260630|interim", OLD),
        ("20100630|annual", OLD),
    ):
        _write(db, key, payload)
    before = {r.period_key: r.payload for r in db.query(SecurityProfileData).all()}
    stale, unplanned = SCRIPT["_stale_rows"](db, None)
    assert {r.period_key for r in stale} == {"20250630|annual", "20150630|annual"}
    assert {r.period_key for r in unplanned} == {"20250630|interim", "20260630|interim"}
    assert {r.period_key: r.payload for r in db.query(SecurityProfileData).all()} == before


@pytest.mark.parametrize("plan", [None, {}, {"period_keys": []}])
def test_unknown_plan_keeps_conservative_stale_count_and_symbol_filter(db, plan):
    if plan is not None:
        _write(db, "current", plan, dataset=svc.PLAN_DATASET)
    _write(db, "20260630|interim", OLD)
    _write(db, "20251231|annual", OLD, symbol="00700")
    stale, unplanned = SCRIPT["_stale_rows"](db, "01023")
    assert [(r.symbol, r.period_key) for r in stale] == [("01023", "20260630|interim")]
    assert unplanned == []


def test_cli_converges_with_legacy_failures_without_deleting_or_running_them(
    db, monkeypatch, capsys
):
    _write(
        db,
        "current",
        {"period_keys": ["20251231|interim", "20241231|interim"]},
        dataset=svc.PLAN_DATASET,
    )
    _write(db, "20251231|interim", CURRENT)
    _write(db, "20241231|interim", CURRENT)
    _write(db, "20250630|interim", OLD)
    _write(db, "20260630|interim", OLD)
    before = {r.id: r.payload for r in db.query(SecurityProfileData).all()}

    def unexpected_ensure(*args, **kwargs):
        pytest.fail("计划外遗留不能触发下载或模型调用")

    monkeypatch.setattr(svc, "ensure_report_statements", unexpected_ensure)
    for args in (["rerun", "--all", "--dry-run"], ["rerun", "--all"]):
        monkeypatch.setattr("sys.argv", args)
        assert SCRIPT["main"]() == 0
        output = capsys.readouterr().out
        assert "过期抽取 0 份" in output and "计划外遗留 2 份" in output
        assert "20250630|interim" in output and "20260630|interim" in output
    assert {r.id: r.payload for r in db.query(SecurityProfileData).all()} == before
