"""Read-only report routing, provenance and file-boundary regressions."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from ops.ci import checks
from tools.architecture import metrics, reports, server
from tools.architecture import mutation_reports


def write(root, path, content):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def mutation_run(root, report_root, identifier, *, killed=1, survived=1):
    target = mutation_reports.SCOPE["path"]
    original = (
        "raise RuntimeError('must never execute')\n\ndef dividend_tax_cash_flow():\n    return 1\n"
    )
    write(root, target, original)
    write(root, mutation_reports.SCOPE["tests"][0], "def test_original(): pass\n")
    requirements = write(root, "backend/requirements.txt", "pytest==9.1.1\n")
    config = '[tool.mutmut]\nsource_paths = ["app"]\n'
    value = {
        "schema_version": 1,
        "status": "completed",
        "started_at": "2026-10-04T10:00:00+00:00",
        "finished_at": "2026-10-04T10:01:00+00:00",
        "scope": copy.deepcopy(mutation_reports.SCOPE),
        "tool": {
            "mutmut": "3.8.0",
            "python": "3.12.13",
            "pytest": "9.1.1",
            "config_sha256": hashlib.sha256(config.encode()).hexdigest(),
            "implementation_sha256": "a" * 64,
        },
        "steps": {
            key: {"exit_code": 0, "duration_seconds": 1.0} for key in ("baseline", "mutation")
        },
        "counts": {"killed": killed, "survived": survived},
        "mutants": [
            {
                "name": mutation_reports.PREFIX + str(index + 1),
                "status": status,
                "native_status": status,
                "exit_code": 1 if status == "killed" else 0,
                "equivalence": "unreviewed" if status == "survived" else None,
                "diff": "../../PRIVATE-SECRET",
            }
            for index, status in enumerate(["killed"] * killed + ["survived"] * survived)
        ],
        "mutation_score": killed / (killed + survived),
        "source_files": {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [*root.rglob("*.py"), requirements]
        },
        "env": {"token": "PRIVATE-ENV"},
    }
    write(report_root, f"{identifier}/mutation/manifest.json", json.dumps(value))
    write(report_root, f"{identifier}/mutation/native/pyproject.toml", config)
    write(report_root, f"{identifier}/mutation/native/fx.original.py", original)
    write(
        report_root,
        f"{identifier}/mutation/native/baseline.xml",
        '<testsuite><testcase name="test_original"/></testsuite>',
    )
    return value


def save_mutation(report_root, identifier, manifest):
    write(report_root, f"{identifier}/mutation/manifest.json", json.dumps(manifest))


@pytest.fixture
def browser(tmp_path, monkeypatch):
    root, report_root = tmp_path / "source", tmp_path / "reports"
    write(root, "backend/app/sample.py", "def example():\n    return 1\n")
    write(root, "backend/tests/test_portfolio_purity.py", "def test_guard(): pass\n")
    report_root.mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("browsing reports must not run source analyzers or tests")

    monkeypatch.setattr(server, "_analyze_snapshot", forbidden)
    monkeypatch.setattr(metrics, "build_report", forbidden)
    return (
        root,
        report_root,
        TestClient(server.create_app(root, report_root), base_url="http://localhost"),
    )


def quality(root, coverage=1):
    hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*.py")
    }
    return {
        "schema_version": 1,
        "status": "current",
        "generated_at": "2026-10-04T10:00:00+00:00",
        "source": {"root": "/work/repo", "files": hashes, "config": {}},
        "scope": copy.deepcopy(metrics.SCOPE),
        "tools": {
            "analyzers": {"backend": {"radon": "6.0.1"}, "frontend": {"eslint": "9.39.1"}},
            "collectors": {
                "coverage.py-json": "7.16.2",
                "vitest": "2.1.9",
                "@vitest/coverage-istanbul": "2.1.9",
                "@vitejs/plugin-vue": "5.2.4",
            },
            "implementation": {
                "metrics.py": "a" * 64,
                "python_metrics.py": "b" * 64,
                "frontend_metrics.mjs": "c" * 64,
            },
        },
        "groups": {
            group: {"status": "current", "scope": {"coverage_requested": True}, "reasons": []}
            for group in ("backend", "frontend")
        },
        "functions": [
            {
                "group": "backend",
                "path": "backend/app/sample.py",
                "name": "example",
                "line": 1,
                "end_line": 2,
                "cc": 2,
                "coverage": coverage,
                "crap": 4 * (1 - coverage) ** 3 + 2,
                "status": "measured",
                "reason": None,
            }
        ],
        "errors": [],
        "credentials": {"token": "PRIVATE-QUALITY-TOKEN"},
    }


def run(root, report_root, identifier, *, with_quality=True):
    report = quality(root)
    manifest = {
        "schema_version": 1,
        "group": "backend",
        "status": "passed",
        "run_id": identifier.split("/")[0],
        "run_attempt": "1",
        "source": {
            **report["source"],
            "commit": "a" * 40,
            "head": None,
            "base": None,
            "dirty": False,
        },
        "started_at": "2026-10-04T09:00:00+00:00",
        "finished_at": "2026-10-04T10:00:00+00:00",
        "scope": {"coverage_requested": False},
        "steps": [
            {
                "id": step_id,
                "status": "passed",
                "exit_code": 0,
                "duration_seconds": 1.5,
                "log": "logs/private.log",
                "env": {"token": "PRIVATE-STEP-TOKEN"},
            }
            for step_id, _ in checks._backend_commands(Path("."), False)
        ],
        "artifacts": {
            "backend_junit": "junit/backend.xml",
            "architecture_junit": "junit/architecture.xml",
            "logs": "logs/private.log",
            "html": "playwright/index.html",
        },
        "credentials": {"token": "PRIVATE-MANIFEST-TOKEN"},
    }
    write(report_root, f"{identifier}/backend/manifest.json", json.dumps(manifest))
    write(
        report_root,
        f"{identifier}/backend/junit/backend.xml",
        """<?xml version="1.0"?>
<testsuites><testsuite>
<testcase classname="backend.tests.test_portfolio_purity" name="test_guard" file="backend/tests/test_portfolio_purity.py" line="0" />
<testcase classname="backend.tests.test_private_imports" name="test_private_imports"><failure message="app/example.py:4 私有导入; token=PRIVATE-CREDENTIAL">PRIVATE-TRACEBACK</failure></testcase>
<testcase classname="backend.tests.test_no_implicit_clock" name="test_clock"><skipped message="native skip" /></testcase>
<testcase classname="backend.tests.test_holdings" name="test_other"><failure message="PRIVATE-BUSINESS-FAILURE" /></testcase>
<system-out>PRIVATE-STDOUT</system-out><system-err>PRIVATE-STDERR</system-err>
</testsuite></testsuites>""",
    )
    write(report_root, f"{identifier}/backend/logs/private.log", "PRIVATE-LOG")
    write(report_root, f"{identifier}/backend/junit/architecture.xml", "<testsuites/>")
    write(report_root, f"{identifier}/playwright/index.html", "PRIVATE-HTML")
    if with_quality:
        write(report_root, f"{identifier}/quality.json", json.dumps(report))
    return report, manifest


def test_list_and_detail_support_run_and_attempt_without_exposing_unapproved_artifacts(browser):
    root, report_root, client = browser
    run(root, report_root, "run-one")
    run(root, report_root, "run-two/2")
    listing = client.get("/api/reports").json()
    assert {item["id"] for item in listing["reports"]} == {"run-one", "run-two/2"}
    assert all(item["ci_status"]["backend"] == "failed" for item in listing["reports"])
    assert any("矛盾" in item["message"] for item in listing["errors"])
    assert not listing["truncated"]
    response = client.get("/api/report", params={"id": "run-two/2"})
    assert response.status_code == 200
    report = response.json()
    assert report["source"] == {"status": "current", "can_navigate": True, "changed_files": []}
    assert report["quality"]["functions"][0]["crap"] == 2
    assert report["ci"]["backend"]["status"] == "failed"
    assert next(
        step for step in report["ci"]["backend"]["steps"] if step["id"] == "backend-tests"
    ) == {"id": "backend-tests", "status": "failed", "exit_code": 0, "duration_seconds": 1.5}
    cases = {row["rule"]: row for row in report["guards"]}
    assert cases["portfolio_purity"]["status"] == "passed"
    assert cases["portfolio_purity"]["line"] == 1
    assert cases["private_imports"]["status"] == "failed"
    assert "私有导入" in cases["private_imports"]["message"]
    assert cases["no_implicit_clock"]["status"] == "skipped"
    assert cases["deploy_config_sync"]["status"] == "unknown"
    assert "PRIVATE-" not in response.text
    assert (
        client.get("/api/report", params={"id": "run-one/backend/logs/private.log"}).status_code
        == 422
    )
    assert client.get("/reports/run-one/playwright/index.html").status_code == 404


def test_list_keeps_newest_reports_when_real_json_total_exceeds_shared_budget(browser):
    root, report_root, client = browser
    report = quality(root)
    row = report["functions"][0]
    report["functions"] = [{**row, "name": f"function_{index}"} for index in range(8300)]
    serialized = json.dumps(report)
    assert len(serialized.encode()) < reports.MAX_JSON_BYTES
    assert len(serialized.encode()) * 25 > reports.MAX_TOTAL_BYTES
    for index in range(25):
        path = write(report_root, f"run-{index:02}/quality.json", serialized)
        os.utime(path, (1700000000 + index, 1700000000 + index))

    listing = client.get("/api/reports").json()
    identifiers = {item["id"] for item in listing["reports"]}
    assert {"run-22", "run-23", "run-24"} <= identifiers
    assert listing["truncated"]
    assert any("预算" in error["message"] for error in listing["errors"])
    assert client.get("/api/report", params={"id": "run-24"}).json()["quality"] is not None


def test_list_selects_latest_run_and_attempt_metadata_before_candidate_limit(browser, monkeypatch):
    root, report_root, client = browser
    monkeypatch.setattr(reports, "MAX_RUNS", 2)
    for index, identifier in enumerate(("z-old", "y-old/1", "a-new", "b-new/2")):
        path = write(report_root, f"{identifier}/quality.json", json.dumps(quality(root)))
        os.utime(path, (1700000000 + index, 1700000000 + index))
    listing = client.get("/api/reports").json()
    assert {item["id"] for item in listing["reports"]} == {"a-new", "b-new/2"}
    assert listing["truncated"]


def test_missing_quality_keeps_existing_ci_and_marks_absent_guards_unknown(browser):
    root, report_root, client = browser
    _, manifest = run(root, report_root, "ci-only", with_quality=False)
    manifest["artifacts"] = {}
    write(report_root, "ci-only/backend/manifest.json", json.dumps(manifest))
    report = client.get("/api/report", params={"id": "ci-only"}).json()
    assert report["quality"] is None
    assert report["ci"]["backend"]["status"] == "incomplete"
    assert all(row["status"] == "unknown" for row in report["guards"])
    assert report["errors"][0]["path"] == "ci-only/quality.json"


def test_source_is_rechecked_on_each_read_and_history_never_jumps_to_new_lines(browser):
    root, report_root, client = browser
    run(root, report_root, "history")
    assert client.get("/api/report", params={"id": "history"}).json()["source"]["can_navigate"]
    write(root, "backend/app/sample.py", "def example():\n    return 2\n")
    report = client.get("/api/report", params={"id": "history"}).json()
    assert report["source"] == {
        "status": "stale",
        "can_navigate": False,
        "changed_files": ["backend/app/sample.py"],
    }
    assert report["quality"]["functions"][0]["crap"] == 2


def test_new_production_file_and_missing_fingerprints_disallow_navigation(browser):
    root, report_root, client = browser
    report, manifest = run(root, report_root, "history")
    write(root, "backend/app/new.py", "def added(): pass\n")
    assert client.get("/api/report", params={"id": "history"}).json()["source"]["status"] == "stale"
    report["source"].pop("files")
    write(report_root, "history/quality.json", json.dumps(report))
    manifest["source"].pop("files")
    write(report_root, "history/backend/manifest.json", json.dumps(manifest))
    response = client.get("/api/report", params={"id": "history"}).json()
    assert response["quality"] is None
    assert response["source"]["status"] == "unknown"
    assert not response["source"]["can_navigate"]


@pytest.mark.parametrize(
    "identifier",
    ["../run", "run/../outside", "/absolute", "a/b/c", "run\\outside", ".", "run/%2e%2e"],
)
def test_report_and_compare_reject_path_traversal(browser, identifier):
    _, _, client = browser
    assert client.get("/api/report", params={"id": identifier}).status_code == 422
    assert (
        client.get("/api/compare", params={"before": identifier, "after": "run"}).status_code == 422
    )


def test_symlink_run_attempt_and_artifact_cannot_escape_root(browser, tmp_path):
    root, report_root, client = browser
    outside = tmp_path / "outside"
    run(root, outside, "outside")
    (report_root / "linked").symlink_to(outside / "outside", target_is_directory=True)
    (report_root / "nested").mkdir()
    (report_root / "nested/1").symlink_to(outside / "outside", target_is_directory=True)
    assert client.get("/api/report", params={"id": "linked"}).status_code == 404
    assert client.get("/api/report", params={"id": "nested/1"}).status_code == 404
    assert client.get("/api/reports").json()["reports"] == []
    run(root, report_root, "safe")
    (report_root / "safe/quality.json").unlink()
    (report_root / "safe/quality.json").symlink_to(outside / "outside/quality.json")
    response = client.get("/api/report", params={"id": "safe"}).json()
    assert response["quality"] is None
    assert any("安全读取" in error["message"] for error in response["errors"])


def test_report_root_symlink_is_not_followed(browser, tmp_path):
    root, report_root, _ = browser
    run(root, report_root, "safe")
    link = tmp_path / "linked-root"
    link.symlink_to(report_root, target_is_directory=True)
    client = TestClient(server.create_app(root, link), base_url="http://localhost")
    assert client.get("/api/reports").json()["reports"] == []
    assert client.get("/api/report", params={"id": "safe"}).status_code == 404


def test_oversized_or_malformed_quality_keeps_native_ci_visible(browser, monkeypatch):
    root, report_root, client = browser
    run(root, report_root, "large")
    monkeypatch.setattr(reports, "MAX_JSON_BYTES", 100)
    response = client.get("/api/report", params={"id": "large"}).json()
    assert response["quality"] is None
    assert response["ci"]["backend"] is not None
    assert any("大小限制" in error["message"] for error in response["errors"])
    write(report_root, "large/quality.json", "{invalid")
    response = client.get("/api/report", params={"id": "large"}).json()
    assert response["quality"] is None
    assert any("JSON 格式无效" in error["message"] for error in response["errors"])


def test_junit_entity_declarations_and_unapproved_attachment_paths_are_rejected(browser, tmp_path):
    root, report_root, client = browser
    _, manifest = run(root, report_root, "unsafe")
    write(
        report_root,
        "unsafe/backend/junit/backend.xml",
        '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><testsuite>&secret;</testsuite>',
    )
    response = client.get("/api/report", params={"id": "unsafe"}).json()
    assert all(row["status"] == "unknown" for row in response["guards"])
    assert any("实体声明" in error["message"] for error in response["errors"])
    manifest["artifacts"]["backend_junit"] = "../../outside.xml"
    write(report_root, "unsafe/backend/manifest.json", json.dumps(manifest))
    assert all(
        row["status"] == "unknown"
        for row in client.get("/api/report", params={"id": "unsafe"}).json()["guards"]
    )


def test_compare_uses_native_module_and_keeps_each_sides_navigation_qualification(browser):
    root, report_root, client = browser
    before, _ = run(root, report_root, "before")
    before["functions"][0].update(coverage=0.5, crap=2.5)
    write(report_root, "before/quality.json", json.dumps(before))
    write(root, "backend/app/sample.py", "def example():\n    return 2\n")
    run(root, report_root, "after")
    response = client.get("/api/compare", params={"before": "before", "after": "after"})
    assert response.status_code == 200
    result = response.json()
    assert not result["before"]["source"]["can_navigate"]
    assert result["after"]["source"]["can_navigate"]
    assert len(result["comparison"]["improved"]) == 1
    assert result["comparison"]["improved"][0]["delta"] == -0.5


def test_unconfigured_or_absent_reports_are_explicit(browser):
    root, _, client = browser
    no_root = TestClient(server.create_app(root), base_url="http://localhost")
    response = no_root.get("/api/reports").json()
    assert response["reports"] == []
    assert response["errors"]
    assert no_root.get("/api/report", params={"id": "missing"}).status_code == 404
    assert client.get("/api/report", params={"id": "missing"}).status_code == 404


def test_malformed_manifest_fingerprints_do_not_break_native_guard_display(browser):
    root, report_root, client = browser
    _, manifest = run(root, report_root, "partial")
    manifest["source"]["files"] = None
    write(report_root, "partial/backend/manifest.json", json.dumps(manifest))
    response = client.get("/api/report", params={"id": "partial"})
    assert response.status_code == 200
    assert response.json()["source"]["status"] == "unknown"
    assert response.json()["guards"][0]["path"] is None


@pytest.mark.parametrize("group", ["backend", "frontend"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_step",
        "missing_end",
        "invalid_end",
        "missing_artifact",
        "duplicate_step",
        "bad_exit",
    ],
)
def test_list_and_detail_reuse_publisher_validation_instead_of_trusting_passed(
    browser, group, fault
):
    root, report_root, client = browser
    _, manifest = run(root, report_root, "incomplete")
    manifest["group"] = group
    factory = checks._backend_commands if group == "backend" else checks._frontend_commands
    manifest["steps"] = [
        {"id": step_id, "status": "passed", "exit_code": 0, "duration_seconds": 1}
        for step_id, _ in factory(Path("."), False)
    ]
    manifest["artifacts"] = dict(checks.ARTIFACT_PATHS)
    if fault == "missing_step":
        manifest["steps"].pop()
    elif fault == "missing_end":
        manifest["finished_at"] = None
    elif fault == "invalid_end":
        manifest["finished_at"] = "2026-10-03T00:00:00+00:00"
    elif fault == "missing_artifact":
        manifest["artifacts"] = {}
    elif fault == "duplicate_step":
        manifest["steps"].append(manifest["steps"][0])
    elif fault == "bad_exit":
        manifest["steps"][0]["exit_code"] = 1
    write(report_root, f"incomplete/{group}/manifest.json", json.dumps(manifest))
    detail = client.get("/api/report", params={"id": "incomplete"}).json()
    listing = client.get("/api/reports").json()
    assert detail["ci"][group]["status"] == "incomplete"
    assert listing["reports"][0]["ci_status"][group] == "incomplete"
    assert detail["errors"] and listing["errors"]
    if fault == "missing_step":
        assert detail["ci"][group]["steps"][-1]["status"] == "incomplete"
        assert detail["ci"][group]["steps"][-1]["exit_code"] is None


def test_complete_historical_ci_stays_readable_and_passed_without_guard_contradiction(browser):
    root, report_root, client = browser
    run(root, report_root, "complete")
    write(
        report_root,
        "complete/backend/junit/backend.xml",
        '<testsuite><testcase classname="test_portfolio_purity" name="test_guard"/></testsuite>',
    )
    write(root, "backend/app/sample.py", "def example(): return 2\n")
    detail = client.get("/api/report", params={"id": "complete"}).json()
    assert detail["source"]["status"] == "stale"
    assert detail["ci"]["backend"]["status"] == "passed"
    assert client.get("/api/reports").json()["reports"][0]["ci_status"]["backend"] == "passed"
    assert detail["guards"][0]["status"] == "passed"


@pytest.mark.parametrize("outcome", ["failure", "error"])
@pytest.mark.parametrize(
    "artifact,step_id", [("backend", "backend-tests"), ("architecture", "architecture-tests")]
)
def test_native_failure_or_error_cannot_coexist_with_green_step_or_group(
    browser, outcome, artifact, step_id
):
    root, report_root, client = browser
    run(root, report_root, "contradiction")
    write(report_root, "contradiction/backend/junit/backend.xml", "<testsuites/>")
    write(
        report_root,
        f"contradiction/backend/junit/{artifact}.xml",
        f'<testsuite><testcase classname="test_portfolio_purity" name="test_guard"><{outcome} message="guard failed"/></testcase></testsuite>',
    )
    detail = client.get("/api/report", params={"id": "contradiction"}).json()
    assert detail["ci"]["backend"]["status"] == "failed"
    step = next(row for row in detail["ci"]["backend"]["steps"] if row["id"] == step_id)
    assert step["status"] == "failed" and step["exit_code"] == 0
    assert detail["guards"][0]["status"] == ("failed" if outcome == "failure" else "error")
    assert any("矛盾" in error["message"] for error in detail["errors"])
    assert client.get("/api/reports").json()["reports"][0]["ci_status"]["backend"] == "failed"


def test_failure_beyond_guard_display_limit_is_still_reflected_in_ci(browser):
    root, report_root, client = browser
    run(root, report_root, "many")
    cases = '<testcase classname="test_portfolio_purity" name="passing"/>' * 500
    cases += '<testcase classname="test_portfolio_purity" name="last_failure"><failure/></testcase>'
    write(report_root, "many/backend/junit/backend.xml", f"<testsuite>{cases}</testsuite>")
    detail = client.get("/api/report", params={"id": "many"}).json()
    assert detail["ci"]["backend"]["status"] == "failed"
    assert len([row for row in detail["guards"] if row["test"]]) == 500
    assert any("展示上限" in error["message"] for error in detail["errors"])


def test_actual_failing_pytest_junit_corrects_a_claimed_passing_manifest(browser, tmp_path):
    root, report_root, client = browser
    run(root, report_root, "native-failure")
    test_file = write(
        tmp_path, "native/test_portfolio_purity.py", "def test_guard():\n    assert 1 == 2\n"
    )
    native = report_root / "native-failure/backend/junit/backend.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), f"--junitxml={native}", "-q"],
        cwd=test_file.parent,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    detail = client.get("/api/report", params={"id": "native-failure"}).json()
    assert detail["guards"][0]["status"] == "failed"
    assert detail["ci"]["backend"]["status"] == "failed"
    assert client.get("/api/reports").json()["reports"][0]["ci_status"]["backend"] == "failed"


def guard_run(root, report_root, identifier, cases):
    for rule in reports.GUARDS:
        path = root / f"backend/tests/test_{rule}.py"
        if not path.exists():
            write(root, path.relative_to(root), "def test_guard(): pass\n")
    _, manifest = run(root, report_root, identifier, with_quality=False)
    manifest["tools"] = {"python": "3.12.13", "pytest": "9.1.1"}
    write(report_root, f"{identifier}/backend/manifest.json", json.dumps(manifest))
    native = []
    for rule, test, status in cases:
        outcome = "" if status == "passed" else f"<{status}/>"
        native.append(f'<testcase classname="test_{rule}" name="{test}">{outcome}</testcase>')
    write(
        report_root,
        f"{identifier}/backend/junit/backend.xml",
        f"<testsuite>{''.join(native)}</testsuite>",
    )
    return manifest


@pytest.mark.parametrize(
    "before_status,after_status,bucket",
    [
        ("passed", "failure", "worsened"),
        ("failure", "passed", "improved"),
        ("passed", "passed", "unchanged"),
        ("failure", "failure", "unchanged"),
        ("failure", "skipped", "unknown"),
        ("skipped", "passed", "unknown"),
        ("error", "passed", "unknown"),
        ("passed", "error", "unknown"),
    ],
)
def test_guard_comparison_classifies_native_cases_independently_of_missing_crap(
    browser, before_status, after_status, bucket
):
    root, report_root, client = browser
    guard_run(root, report_root, "before", [("portfolio_purity", "test_guard", before_status)])
    guard_run(root, report_root, "after", [("portfolio_purity", "test_guard", after_status)])
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()
    guards = result["guard_comparison"]
    assert guards["status"] == "comparable"
    assert any(row["test"] == "test_guard" for row in guards[bucket])
    assert result["comparison"]["status"] == "incomparable"
    if bucket != "improved":
        assert not guards["improved"]


def test_guard_comparison_matches_rule_and_name_and_never_counts_disappearance_as_improvement(
    browser,
):
    root, report_root, client = browser
    guard_run(
        root,
        report_root,
        "before",
        [
            ("portfolio_purity", "same_name", "failure"),
            ("private_imports", "same_name", "passed"),
            ("env_contract", "disappeared", "failure"),
        ],
    )
    guard_run(
        root,
        report_root,
        "after",
        [
            ("portfolio_purity", "same_name", "passed"),
            ("private_imports", "same_name", "failure"),
            ("no_implicit_clock", "new_case", "passed"),
        ],
    )
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()[
        "guard_comparison"
    ]
    assert [row["rule"] for row in result["improved"]] == ["portfolio_purity"]
    assert [row["rule"] for row in result["worsened"]] == ["private_imports"]
    assert [row["test"] for row in result["added"]] == ["new_case"]
    assert [row["test"] for row in result["removed"]] == ["disappeared"]
    assert "不表示" in result["removed"][0]["reason"]


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("tests", "测试源码"),
        ("config", "检查配置"),
        ("scope", "采集口径"),
        ("version", "工具版本"),
        ("missing_version", "缺少 Python"),
        ("missing_end", "未完整结束"),
        ("missing_junit", "原生守卫结果缺失"),
        ("invalid_junit", "原生守卫结果缺失"),
    ],
)
def test_guard_baseline_rejects_incompatible_or_incomplete_evidence(browser, fault, reason):
    root, report_root, client = browser
    guard_run(root, report_root, "before", [("portfolio_purity", "test_guard", "failure")])
    manifest = guard_run(root, report_root, "after", [("portfolio_purity", "test_guard", "passed")])
    if fault == "tests":
        manifest["source"]["files"]["backend/tests/test_portfolio_purity.py"] = "f" * 64
    elif fault == "config":
        manifest["source"]["files"]["backend/pytest.ini"] = "f" * 64
    elif fault == "scope":
        manifest["scope"]["selected_tests"] = "subset"
    elif fault == "version":
        manifest["tools"]["pytest"] = "10.0.0"
    elif fault == "missing_version":
        del manifest["tools"]
    elif fault == "missing_end":
        manifest["finished_at"] = None
    elif fault == "missing_junit":
        (report_root / "after/backend/junit/backend.xml").unlink()
    elif fault == "invalid_junit":
        write(report_root, "after/backend/junit/backend.xml", "<invalid>")
    write(report_root, "after/backend/manifest.json", json.dumps(manifest))
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()[
        "guard_comparison"
    ]
    assert result["status"] == "incomparable"
    assert not result["improved"]
    assert any(reason in item for item in result["reasons"])


def test_changed_product_source_allows_guard_comparison_but_duplicate_case_does_not(browser):
    root, report_root, client = browser
    guard_run(root, report_root, "before", [("portfolio_purity", "test_guard", "failure")])
    write(root, "backend/app/sample.py", "def example(): return 2\n")
    guard_run(root, report_root, "after", [("portfolio_purity", "test_guard", "passed")])
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()
    assert result["guard_comparison"]["improved"][0]["test"] == "test_guard"
    assert not result["before"]["source"]["can_navigate"]
    guard_run(
        root,
        report_root,
        "after",
        [
            ("portfolio_purity", "test_guard", "passed"),
            ("portfolio_purity", "test_guard", "passed"),
        ],
    )
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()[
        "guard_comparison"
    ]
    assert not result["improved"]
    assert any("名称重复" in row["reason"] for row in result["unknown"])


def test_mutation_only_run_lists_native_results_and_safe_function_entry(browser):
    root, report_root, client = browser
    mutation_run(root, report_root, "mutation-only/2")
    listing = client.get("/api/reports").json()["reports"]
    assert [(row["id"], row["mutation_status"]) for row in listing] == [
        ("mutation-only/2", "completed")
    ]
    response = client.get("/api/report", params={"id": "mutation-only/2"})
    assert response.status_code == 200
    detail = response.json()
    value = detail["mutation"]
    assert detail["quality"] is None and all(item is None for item in detail["ci"].values())
    assert not any(error["path"].endswith("quality.json") for error in detail["errors"])
    assert value["status"] == "completed" and value["mutation_score"] == 0.5
    assert value["steps"]["baseline"]["status"] == "passed"
    assert value["counts"] == dict.fromkeys(mutation_reports.STATES, 0) | {
        "killed": 1,
        "survived": 1,
    }
    assert value["mutants"][1]["equivalence"] == "unreviewed"
    assert value["source"] == {"status": "current", "can_navigate": True, "changed_files": []}
    assert value["line"] == 3
    assert "PRIVATE-" not in response.text
    assert all("diff" not in row and "line" not in row for row in value["mutants"])


@pytest.mark.parametrize(
    "state", ["timeout", "tool_error", "no_coverage", "not_checked", "unknown"]
)
def test_unfinished_native_mutant_never_produces_score(browser, state):
    root, report_root, client = browser
    value = mutation_run(root, report_root, "partial")
    value["mutants"][0].update(status=state, native_status=state, exit_code=None)
    value["counts"] = {state: 1, "survived": 1}
    save_mutation(report_root, "partial", value)
    response = client.get("/api/report", params={"id": "partial"}).json()["mutation"]
    assert response["status"] == "incomplete" and response["mutation_score"] is None
    assert response["counts"][state] == 1
    assert response["mutants"][0]["native_status"] == state
    assert client.get("/api/reports").json()["reports"][0]["mutation_status"] == "incomplete"


@pytest.mark.parametrize(
    "problem",
    [
        "baseline",
        "mutation",
        "finished",
        "score",
        "counts",
        "duplicate",
        "native",
        "fingerprints",
        "config",
    ],
)
def test_mutation_completed_claim_needs_consistent_evidence(browser, problem):
    root, report_root, client = browser
    value = mutation_run(root, report_root, "bad")
    if problem in ("baseline", "mutation"):
        value["steps"][problem]["exit_code"] = 1
    elif problem == "finished":
        value.pop("finished_at")
    elif problem == "score":
        value["mutation_score"] = 1.0
    elif problem == "counts":
        value["counts"]["killed"] = 5
    elif problem == "duplicate":
        value["mutants"].append(value["mutants"][0])
    elif problem == "native":
        value["mutants"][0]["exit_code"] = 0
    elif problem == "fingerprints":
        value["source_files"] = {"../../secret": "a" * 64}
    elif problem == "config":
        value["tool"]["config_sha256"] = "b" * 64
    save_mutation(report_root, "bad", value)
    result = client.get("/api/report", params={"id": "bad"}).json()["mutation"]
    assert result["status"] == "incomplete" and result["mutation_score"] is None
    assert result["reason"]


@pytest.mark.parametrize(
    "baseline",
    [
        None,
        "<testsuite/>",
        "<testsuite><testcase><failure/></testcase></testsuite>",
        "<testsuite><testcase><skipped/></testcase></testsuite>",
        '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><testsuite/>',
    ],
)
def test_mutation_baseline_requires_original_junit_success(browser, baseline):
    root, report_root, client = browser
    mutation_run(root, report_root, "baseline")
    path = "baseline/mutation/native/baseline.xml"
    if baseline is None:
        (report_root / path).unlink()
    else:
        write(report_root, path, baseline)
    result = client.get("/api/report", params={"id": "baseline"}).json()["mutation"]
    assert result["steps"]["baseline"]["status"] != "passed"
    assert result["mutation_score"] is None


def test_mutation_history_retains_score_but_source_drift_disables_navigation(browser):
    root, report_root, client = browser
    mutation_run(root, report_root, "history")
    target = mutation_reports.SCOPE["path"]
    write(root, target, "def dividend_tax_cash_flow():\n    return 2\n")
    result = client.get("/api/report", params={"id": "history"}).json()["mutation"]
    assert result["mutation_score"] == 0.5
    assert result["source"] == {"status": "stale", "can_navigate": False, "changed_files": [target]}
    assert result["line"] == 3  # Archived original, not today's definition at line 1.


def test_mutation_symlinks_and_forged_original_never_supply_source_location(browser, tmp_path):
    root, report_root, client = browser
    mutation_run(root, report_root, "safe")
    original = report_root / "safe/mutation/native/fx.original.py"
    original.write_text("def dividend_tax_cash_flow(): pass\n")
    assert not client.get("/api/report", params={"id": "safe"}).json()["mutation"]["source"][
        "can_navigate"
    ]
    original.unlink()
    original.symlink_to(root / mutation_reports.SCOPE["path"])
    result = client.get("/api/report", params={"id": "safe"}).json()
    assert any("安全读取" in row["message"] for row in result["errors"])
    target = root / mutation_reports.SCOPE["path"]
    outside = write(tmp_path, "outside.py", target.read_text())
    target.unlink()
    target.symlink_to(outside)
    result = client.get("/api/report", params={"id": "safe"}).json()["mutation"]
    assert not result["source"]["can_navigate"]
    (report_root / "safe/mutation/manifest.json").unlink()
    (report_root / "safe/mutation/manifest.json").symlink_to(outside)
    assert client.get("/api/report", params={"id": "safe"}).json()["mutation"] is None


def test_mutation_comparison_is_independent_and_allows_selected_test_content_changes(browser):
    root, report_root, client = browser
    mutation_run(root, report_root, "before")
    right = mutation_run(root, report_root, "after", killed=2, survived=0)
    test = mutation_reports.SCOPE["tests"][0]
    right["source_files"][test] = "f" * 64
    save_mutation(report_root, "after", right)
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()
    comparison = result["mutation_comparison"]
    assert comparison["status"] == "comparable"
    assert comparison["before_score"] == 0.5 and comparison["after_score"] == 1.0
    assert comparison["delta"] == 0.5 and comparison["change"] == "improved"
    assert comparison["changed_test_files"] == [test]
    assert any("不推断" in reason for reason in comparison["reasons"])
    reverse = client.get("/api/compare", params={"before": "after", "after": "before"}).json()
    assert reverse["mutation_comparison"]["change"] == "worsened"


@pytest.mark.parametrize(
    "problem", [*mutation_reports.TOOLS, "target", "dependency", "population", "unknown", "missing"]
)
def test_incompatible_mutation_comparison_cannot_claim_improvement(browser, problem):
    root, report_root, client = browser
    mutation_run(root, report_root, "before")
    right = mutation_run(root, report_root, "after", killed=2, survived=0)
    if problem in mutation_reports.TOOLS:
        right["tool"].pop(problem)
    elif problem == "target":
        right["source_files"][mutation_reports.SCOPE["path"]] = "f" * 64
    elif problem == "dependency":
        right["source_files"]["backend/app/sample.py"] = "f" * 64
    elif problem == "population":
        right["mutants"][0]["name"] = mutation_reports.PREFIX + "333"
    elif problem == "unknown":
        right["status"] = "incomplete"
    elif problem == "missing":
        run(root, report_root, "after")
        (report_root / "after/mutation/manifest.json").unlink()
    if problem != "missing":
        save_mutation(report_root, "after", right)
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()[
        "mutation_comparison"
    ]
    assert result["status"] == "incomparable" and result["change"] == "unknown"
    assert result["delta"] is None and result["reasons"]


def test_legacy_mutation_keeps_historical_score_but_missing_tool_identity_is_incomparable(browser):
    root, report_root, client = browser
    legacy = mutation_run(root, report_root, "legacy")
    legacy["tool"] = {"mutmut": "3.8.0"}
    save_mutation(report_root, "legacy", legacy)
    assert (
        client.get("/api/report", params={"id": "legacy"}).json()["mutation"]["mutation_score"]
        == 0.5
    )
    result = client.get("/api/compare", params={"before": "legacy", "after": "legacy"}).json()[
        "mutation_comparison"
    ]
    assert result["status"] == "incomparable" and result["change"] == "unknown"


def test_mutation_source_hashes_native_binary_fixtures_without_serving_them(browser):
    root, report_root, client = browser
    value = mutation_run(root, report_root, "fixtures")
    path = "backend/tests/fixtures/reports/native.pages.txt.gz"
    fixture = root / path
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"\x00\x01PRIVATE-FIXTURE")
    value["source_files"][path] = hashlib.sha256(fixture.read_bytes()).hexdigest()
    save_mutation(report_root, "fixtures", value)
    response = client.get("/api/report", params={"id": "fixtures"})
    mutation = response.json()["mutation"]
    assert mutation["status"] == "completed" and mutation["source"]["can_navigate"]
    assert "PRIVATE-FIXTURE" not in response.text
    fixture.write_bytes(b"changed")
    mutation = client.get("/api/report", params={"id": "fixtures"}).json()["mutation"]
    assert mutation["source"]["changed_files"] == [path]
    assert not mutation["source"]["can_navigate"]


@pytest.mark.parametrize("tool", mutation_reports.TOOLS)
def test_mutation_comparison_requires_identical_tool_and_collection_identity(browser, tool):
    root, report_root, client = browser
    mutation_run(root, report_root, "before")
    right = mutation_run(root, report_root, "after", killed=2, survived=0)
    right["tool"][tool] = "b" * 64 if tool.endswith("sha256") else "other-version"
    if tool == "config_sha256":
        config = "# changed native configuration\n"
        write(report_root, "after/mutation/native/pyproject.toml", config)
        right["tool"][tool] = hashlib.sha256(config.encode()).hexdigest()
    save_mutation(report_root, "after", right)
    result = client.get("/api/compare", params={"before": "before", "after": "after"}).json()[
        "mutation_comparison"
    ]
    assert result["before_score"] == 0.5 and result["after_score"] == 1
    assert result["status"] == "incomparable" and result["change"] == "unknown"
    assert any(tool in reason for reason in result["reasons"])


def test_mutation_rejects_scope_expansion_and_sensitive_fingerprint_paths(browser):
    root, report_root, client = browser
    value = mutation_run(root, report_root, "unsafe")
    value["source_files"]["backend/tests/secrets/credential.json"] = "b" * 64
    save_mutation(report_root, "unsafe", value)
    result = client.get("/api/report", params={"id": "unsafe"}).json()["mutation"]
    assert result["source_files"] == {} and result["mutation_score"] is None
    value["scope"]["function"] = "some_other_function"
    save_mutation(report_root, "unsafe", value)
    result = client.get("/api/report", params={"id": "unsafe"}).json()
    assert result["mutation"] is None
    assert any("固定试点范围" in error["message"] for error in result["errors"])


def test_mutation_requirements_drift_preserves_history_but_prevents_comparison(browser):
    root, report_root, client = browser
    mutation_run(root, report_root, "before")
    right = mutation_run(root, report_root, "after", killed=2, survived=0)
    requirements = write(root, "backend/requirements.txt", "pytest==9.2.0\n")
    right["source_files"]["backend/requirements.txt"] = hashlib.sha256(
        requirements.read_bytes()
    ).hexdigest()
    save_mutation(report_root, "after", right)
    before = client.get("/api/report", params={"id": "before"}).json()["mutation"]
    after = client.get("/api/report", params={"id": "after"}).json()["mutation"]
    assert before["status"] == after["status"] == "completed"
    assert before["mutation_score"] == 0.5 and after["mutation_score"] == 1
    assert before["source"]["changed_files"] == ["backend/requirements.txt"]
    assert not before["source"]["can_navigate"] and after["source"]["can_navigate"]
    comparison = client.get("/api/compare", params={"before": "before", "after": "after"}).json()[
        "mutation_comparison"
    ]
    assert comparison["status"] == "incomparable" and comparison["change"] == "unknown"
    assert comparison["delta"] is None
    assert any("执行依赖" in reason for reason in comparison["reasons"])


def test_mutation_legacy_missing_requirements_retains_history_without_comparable_claim(browser):
    root, report_root, client = browser
    legacy = mutation_run(root, report_root, "legacy")
    legacy["source_files"].pop("backend/requirements.txt")
    save_mutation(report_root, "legacy", legacy)
    report = client.get("/api/report", params={"id": "legacy"}).json()["mutation"]
    assert report["status"] == "completed" and report["mutation_score"] == 0.5
    comparison = client.get("/api/compare", params={"before": "legacy", "after": "legacy"}).json()[
        "mutation_comparison"
    ]
    assert comparison["status"] == "incomparable" and comparison["change"] == "unknown"
    assert any("requirements.txt" in reason for reason in comparison["reasons"])


@pytest.mark.parametrize("extension", ["json", "csv", "txt.gz"])
def test_fingerprinting_test_data_does_not_allow_source_browsing(browser, monkeypatch, extension):
    root, report_root, client = browser
    path = f"backend/tests/fixtures/reports/sample.{extension}"
    write(root, path, "PRIVATE-FIXTURE")
    monkeypatch.setattr(server, "_analyze_snapshot", lambda *_: {"errors": []})
    assert path in checks._source_files(root)
    assert path not in server._candidate_paths(root, report_root)
    response = client.get(
        "/api/source", params={"path": path, "sha256": checks._source_files(root)[path]}
    )
    assert response.status_code == 404
    assert "PRIVATE-FIXTURE" not in response.text
