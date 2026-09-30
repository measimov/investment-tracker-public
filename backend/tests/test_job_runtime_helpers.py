"""job_runtime 的批量共用件（#280）：市场轮转、初始 data、失权进度闭包。"""

import pytest

from app.services import job_runtime
from app.services.background_job_store import JobOwnershipLostError
from app.services.job_runtime import initial_batch_data, make_batch_progress, rotate_by_market


def test_rotate_by_market_round_robins_and_drops_unknown_markets():
    items = [
        ("AAPL", "美股"),
        ("600519", "A股"),
        ("000001", "A股"),
        ("00700", "港股"),
        ("D05", "新加坡"),
    ]
    assert rotate_by_market(items, ["A股", "港股", "美股"]) == [
        ("000001", "A股"),
        ("00700", "港股"),
        ("AAPL", "美股"),
        ("600519", "A股"),
    ]


def test_rotate_by_market_accepts_dict_items():
    items = [
        {"symbol": "B", "market": "A股"},
        {"symbol": "A", "market": "A股"},
        {"symbol": "X", "market": "港股"},
    ]
    ordered = rotate_by_market(
        items, ["港股", "A股"], market_of=lambda i: i["market"], symbol_of=lambda i: i["symbol"]
    )
    assert [i["symbol"] for i in ordered] == ["X", "A", "B"]
    assert rotate_by_market([], ["A股"]) == []


def test_initial_batch_data_has_common_fields_and_extras():
    targets = [{"symbol": "600519", "market": "A股"}]
    data = initial_batch_data(targets, mode="fast", skipped_count=0)
    assert data["targets"] is targets and data["total"] == 1
    assert data["completed_keys"] == [] and data["results"] == []
    assert data["cancel_requested"] is False and data["abort_reason"] is None
    assert data["mode"] == "fast" and data["skipped_count"] == 0
    # 不能叫 error：_serialize 展平后会盖掉 BackgroundJob.error 列
    assert "error" not in data


def test_batch_progress_raises_when_ownership_lost(monkeypatch):
    calls = []

    def fake_set(job_id, job_type, **kwargs):
        calls.append(kwargs)
        return None if kwargs.get("completed") == 2 else {"id": job_id}

    monkeypatch.setattr(job_runtime, "set_job_progress", fake_set)
    progress = make_batch_progress("job-1", "some_type", 3)
    progress(completed=1)
    with pytest.raises(JobOwnershipLostError):
        progress(completed=2)
    assert all(call["required_attempt_count"] == 3 for call in calls)
