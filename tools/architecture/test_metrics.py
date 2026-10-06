"""Quality provenance tests using real adapters and tiny source/coverage fixtures."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ops.ci import checks
from tools.architecture import metrics, reports


def write(root, path, content):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    write(root, "backend/app/sample.py", "def example():\n    return 1\n")
    write(root, "frontend/src/sample.ts", "export function example() {\n  return 1\n}\n")
    write(root, "frontend/src/sample.spec.ts", "throw new Error('never load test')\n")
    write(root, "frontend/src/types.d.ts", "declare function declared(): void\n")
    write(root, "frontend/src/types/api.generated.ts", "export function generated() { return 1 }\n")
    write(root, "backend/tests/test_sample.py", "raise RuntimeError('never load test')\n")
    write(
        root,
        "frontend/package-lock.json",
        json.dumps(
            {
                "packages": {
                    "node_modules/vitest": {"version": "2.1.9"},
                    "node_modules/@vitest/coverage-istanbul": {"version": "2.1.9"},
                    "node_modules/@vitejs/plugin-vue": {"version": "5.2.4"},
                }
            }
        ),
    )
    write(root, "frontend/vitest.coverage.config.ts", "throw new Error('never load config')\n")
    write(root, "backend/.coveragerc", "[run]\nbranch = True\n")
    return root


def manifests(root, run):
    initial = metrics.build_report(root)
    coverage = {
        "backend": {
            "meta": {"version": "7.16.2"},
            "files": {
                "/work/this-run/repo/backend/app/sample.py": {
                    "functions": {
                        "example": {
                            "start_line": 1,
                            "executed_lines": [2],
                            "missing_lines": [],
                            "excluded_lines": [],
                            "summary": {"covered_lines": 1, "num_statements": 1},
                        }
                    }
                }
            },
        },
        "frontend": {
            "/work/this-run/repo/frontend/src/sample.ts": {
                "path": "/work/this-run/repo/frontend/src/sample.ts",
                "statementMap": {
                    "0": {"start": {"line": 2, "column": 2}, "end": {"line": 2, "column": 10}}
                },
                "s": {"0": 0},
                "fnMap": {},
                "f": {},
            }
        },
    }
    result = {}
    for name in metrics.GROUPS:
        artifact = metrics.ARTIFACTS[name]
        manifest = {
            "schema_version": 1,
            "group": name,
            "run_id": "example-run",
            "run_attempt": "1",
            "status": "passed",
            "finished_at": "2026-10-04T10:00:00+00:00",
            "source": {
                "root": "/work/this-run/repo",
                "files": initial["source"]["files"],
                "commit": "a" * 40,
                "head": None,
                "base": None,
                "dirty": True,
            },
            "scope": {"source_globs": list(metrics.SOURCE_GLOBS), "coverage_requested": True},
            "steps": [
                {"id": step, "status": "passed", "exit_code": 0}
                for step in sorted(metrics.REQUIRED_STEPS[name])
            ],
            "artifacts": {artifact: metrics.ARTIFACT_PATHS[artifact]},
        }
        write(run, f"{name}/manifest.json", json.dumps(manifest))
        write(run, f"{name}/{metrics.ARTIFACT_PATHS[artifact]}", json.dumps(coverage[name]))
        result[name] = copy.deepcopy(manifest)
    return result


def save_manifest(run, name, manifest):
    write(run, f"{name}/manifest.json", json.dumps(manifest))


def assert_no_scores(report, group=None):
    rows = [row for row in report["functions"] if group is None or row["group"] == group]
    assert rows
    assert all(row["coverage"] is None and row["crap"] is None for row in rows)
    assert all(row["cc"] == 1 for row in rows)


def test_no_report_returns_current_cc_without_executing_tests_configs_or_generated_files(project):
    report = metrics.build_report(project)
    assert report["status"] == "unknown"
    assert report["errors"] == []
    assert {row["path"] for row in report["functions"]} == {
        "backend/app/sample.py",
        "frontend/src/sample.ts",
    }
    assert_no_scores(report)
    assert "backend/.coveragerc" in report["source"]["config"]
    assert "backend/tests/test_sample.py" in report["source"]["files"]
    assert report["tools"]["analyzers"]["backend"]["radon"] == "6.0.1"
    assert report["tools"]["analyzers"]["frontend"]["eslint"] == "9.39.1"
    assert report["tools"]["implementation"]["frontend_metrics.mjs"]


def test_matching_complete_run_is_bound_to_hashes_tools_scope_and_original_root(project, tmp_path):
    run = tmp_path / "run"
    manifests(project, run)
    report = metrics.build_report(project, run)
    assert report["status"] == "current"
    assert report["errors"] == []
    assert [row["coverage"] for row in report["functions"]] == [1, 0]
    assert [row["crap"] for row in report["functions"]] == [1, 2]
    assert all(row["status"] == "measured" for row in report["functions"])
    assert (
        report["source"]["files"]["backend/app/sample.py"]
        == hashlib.sha256((project / "backend/app/sample.py").read_bytes()).hexdigest()
    )
    assert report["groups"]["frontend"]["source"]["root"] == "/work/this-run/repo"
    assert report["groups"]["frontend"]["coverage"]["sha256"]
    assert report["tools"]["collectors"]["vitest"] == "2.1.9"
    assert report["tools"]["collectors"]["coverage.py-json"] == "7.16.2"


@pytest.mark.parametrize(
    "path,content",
    [
        ("backend/app/sample.py", "def example():\n    return 2\n"),
        ("backend/tests/test_sample.py", "changed = True\n"),
        ("frontend/vitest.coverage.config.ts", "changed = true\n"),
        ("backend/.coveragerc", "[run]\nbranch = False\n"),
    ],
)
def test_changed_source_tests_or_configuration_make_scores_stale(project, tmp_path, path, content):
    run = tmp_path / "run"
    manifests(project, run)
    write(project, path, content)
    report = metrics.build_report(project, run)
    assert report["status"] == "stale"
    assert_no_scores(report)
    assert all(path in group["changed_files"] for group in report["groups"].values())


@pytest.mark.parametrize(
    "path,content",
    [
        ("backend/tests/fixtures/reports/sample.json", b'{"value": 1}'),
        ("backend/tests/fixtures/reports/sample.csv", b"value\n1\n"),
        ("backend/tests/fixtures/reports/sample.txt.gz", gzip.compress(b"value = 1")),
        ("backend/tests/snapshots/statistics_baseline.json", b'{"value": 1}'),
    ],
)
@pytest.mark.parametrize("change", ["add", "modify", "delete"])
def test_test_data_changes_invalidate_metrics_and_report_browser(
    project, tmp_path, path, content, change
):
    fixture = project / path
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if change != "add":
        fixture.write_bytes(content)
    run = tmp_path / "run"
    data = manifests(project, run)
    quality = metrics.build_report(project, run)
    assert quality["status"] == "current"
    assert quality["errors"] == []
    assert quality["source"]["files"] == checks._source_files(project)
    assert reports._source_status(project, run, quality, data)["status"] == "current"

    if change == "delete":
        fixture.unlink()
    else:
        fixture.write_bytes(content if change == "add" else content + b"changed")

    changed = metrics.build_report(project, run)
    assert changed["status"] == "stale"
    assert changed["errors"] == []
    assert_no_scores(changed)
    assert all(path in group["changed_files"] for group in changed["groups"].values())
    assert changed["source"]["files"] == checks._source_files(project)
    assert reports._source_status(project, run, quality, data) == {
        "status": "stale",
        "can_navigate": False,
        "changed_files": [path],
    }


def test_new_and_removed_production_files_invalidate_collection_identity(project, tmp_path):
    run = tmp_path / "run"
    manifests(project, run)
    (project / "backend/app/sample.py").unlink()
    write(project, "backend/app/new.py", "def added():\n    return 1\n")
    report = metrics.build_report(project, run)
    assert report["status"] == "stale"
    assert_no_scores(report)
    assert {"backend/app/sample.py", "backend/app/new.py"} <= set(
        report["groups"]["backend"]["changed_files"]
    )


@pytest.mark.parametrize("change", ["failed", "running", "missing-step", "skipped-step"])
def test_incomplete_collection_preserves_cc_but_never_a_valid_score(project, tmp_path, change):
    run = tmp_path / "run"
    data = manifests(project, run)
    backend = data["backend"]
    if change in {"failed", "running"}:
        backend["status"] = change
        backend["finished_at"] = None if change == "running" else backend["finished_at"]
    elif change == "missing-step":
        backend["steps"] = backend["steps"][:1]
    else:
        backend["steps"][0]["status"] = "skipped"
    save_manifest(run, "backend", backend)
    report = metrics.build_report(project, run)
    assert report["groups"]["backend"]["status"] == "partial"
    assert_no_scores(report, "backend")
    assert report["groups"]["frontend"]["status"] == "current"


@pytest.mark.parametrize("change", ["run", "commit", "fingerprint"])
def test_different_job_origins_cannot_be_combined(project, tmp_path, change):
    run = tmp_path / "run"
    data = manifests(project, run)
    if change == "run":
        data["frontend"]["run_id"] = "another-run"
    elif change == "commit":
        data["frontend"]["source"]["commit"] = "b" * 40
    else:
        data["frontend"]["source"]["files"]["backend/app/sample.py"] = "0" * 64
    save_manifest(run, "frontend", data["frontend"])
    report = metrics.build_report(project, run)
    assert all(group["status"] == "stale" for group in report["groups"].values())
    assert_no_scores(report)


@pytest.mark.parametrize(
    "change", ["root", "scope", "no-coverage", "artifact-path", "outside-path"]
)
def test_unsupported_or_ambiguous_provenance_stays_unknown(project, tmp_path, change):
    run = tmp_path / "run"
    data = manifests(project, run)
    frontend = data["frontend"]
    if change == "root":
        frontend["source"].pop("root")
    elif change == "scope":
        frontend["scope"]["source_globs"] = ["frontend/src/**/*.ts"]
    elif change == "no-coverage":
        frontend["scope"]["coverage_requested"] = False
    elif change == "artifact-path":
        frontend["artifacts"]["frontend_coverage_json"] = "../../outside.json"
    else:
        coverage_path = run / "frontend/coverage/frontend/coverage-final.json"
        coverage = json.loads(coverage_path.read_text())
        value = next(iter(coverage.values()))
        coverage_path.write_text(json.dumps({"/other/repo/frontend/src/sample.ts": value}))
    save_manifest(run, "frontend", frontend)
    report = metrics.build_report(project, run)
    assert report["groups"]["frontend"]["status"] == "unknown"
    assert_no_scores(report, "frontend")


def test_symlink_source_and_report_cannot_escape_safety_boundary(project, tmp_path):
    run = tmp_path / "run"
    manifests(project, run)
    outside = write(tmp_path, "outside.py", "raise RuntimeError('must not execute')\n")
    source = project / "backend/app/sample.py"
    source.unlink()
    source.symlink_to(outside)
    artifact = run / "frontend/coverage/frontend/coverage-final.json"
    artifact.unlink()
    artifact.symlink_to(outside)
    report = metrics.build_report(project, run)
    assert any(error["path"] == "backend/app/sample.py" for error in report["errors"])
    assert all(row["path"] != "backend/app/sample.py" for row in report["functions"])
    assert_no_scores(report)
    assert report["groups"]["frontend"]["coverage"] is None


def test_missing_production_coverage_leaves_functions_unknown_without_inventing_zero(
    project, tmp_path
):
    run = tmp_path / "run"
    manifests(project, run)
    write(run, "frontend/coverage/frontend/coverage-final.json", "{}")
    report = metrics.build_report(project, run)
    assert report["groups"]["frontend"]["status"] == "current"
    assert report["groups"]["frontend"]["missing_coverage_files"] == ["frontend/src/sample.ts"]
    assert_no_scores(report, "frontend")
    rows = [row for row in report["functions"] if row["group"] == "frontend"]
    assert all(row["status"] == "unknown" for row in rows)
    assert all("不包含该文件" in row["reason"] for row in rows)


def test_real_type_only_files_need_no_coverage_entry(project, tmp_path):
    repository = Path(__file__).resolve().parents[2]
    paths = [
        "frontend/src/types/index.ts",
        *(
            f"frontend/src/views/{view}/types.ts"
            for view in ("holdings", "opinions", "security-detail", "statistics")
        ),
    ]
    for path in paths:
        write(project, path, (repository / path).read_text(encoding="utf-8"))
    run = tmp_path / "run"
    manifests(project, run)
    report = metrics.build_report(project, run)
    assert report["errors"] == []
    assert report["groups"]["frontend"]["status"] == "current"
    assert report["groups"]["frontend"]["missing_coverage_files"] == sorted(paths)
    assert set(paths) <= report["source"]["files"].keys()
    rows = [row for row in report["functions"] if row["group"] == "frontend"]
    assert len(rows) == 1
    assert rows[0]["path"] == "frontend/src/sample.ts"
    assert (rows[0]["status"], rows[0]["coverage"], rows[0]["crap"]) == ("measured", 0, 2)


@pytest.mark.parametrize("group", ["backend", "frontend"])
@pytest.mark.parametrize("same_file", [False, True])
def test_missing_function_evidence_preserves_other_measured_functions(
    project, tmp_path, group, same_file
):
    prefix, extension, body, expected_coverage, expected_crap = (
        ("backend/app", "py", "def unreported():\n    return 2\n", 1, 1)
        if group == "backend"
        else ("frontend/src", "ts", "export function unreported() {\n  return 2\n}\n", 0, 2)
    )
    path = f"{prefix}/{'sample' if same_file else 'unreported'}.{extension}"
    existing = (project / path).read_text() if same_file else ""
    write(project, path, existing + "\n" + body)
    run = tmp_path / "run"
    manifests(project, run)
    report = metrics.build_report(project, run)
    assert report["errors"] == []
    assert report["groups"][group]["status"] == "current"
    assert report["groups"][group]["missing_coverage_files"] == ([] if same_file else [path])
    rows = {row["name"]: row for row in report["functions"] if row["group"] == group}
    measured, unknown = rows["example"], rows["unreported"]
    assert (measured["status"], measured["coverage"], measured["crap"]) == (
        "measured",
        expected_coverage,
        expected_crap,
    )
    assert unknown["cc"] == 1
    assert unknown["status"] == "unknown"
    assert unknown["coverage"] is None
    assert unknown["crap"] is None
    assert unknown["reason"]


def test_current_source_parse_failure_marks_the_group_partial(project, tmp_path):
    write(project, "backend/app/sample.py", "def invalid(:\n")
    run = tmp_path / "run"
    manifests(project, run)
    report = metrics.build_report(project, run)
    assert report["groups"]["backend"]["status"] == "partial"
    assert report["errors"][0]["path"] == "backend/app/sample.py"
    assert report["groups"]["frontend"]["status"] == "current"


def test_cli_writes_default_quality_json_without_changing_native_artifacts(project, tmp_path):
    run = tmp_path / "run"
    manifests(project, run)
    native = {str(p): p.read_bytes() for p in run.rglob("*.json")}
    execution = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.architecture.metrics",
            "--root",
            str(project),
            "--ci-run",
            str(run),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert execution.returncode == 0, execution.stderr
    assert execution.stdout == ""
    assert json.loads((run / "quality.json").read_text())["status"] == "current"
    assert all(Path(p).read_bytes() == content for p, content in native.items())


@pytest.mark.parametrize("symlink_parent", [False, True])
def test_test_input_fingerprints_do_not_follow_symlinks(project, tmp_path, symlink_parent):
    path = "backend/tests/fixtures/reports/sample.txt.gz"
    fixture = project / path
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(gzip.compress(b"original"))
    run = tmp_path / "run"
    data = manifests(project, run)
    quality = metrics.build_report(project, run)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / fixture.name).write_bytes(fixture.read_bytes())
    fixture.unlink()
    if symlink_parent:
        fixture.parent.rmdir()
        fixture.parent.symlink_to(outside, target_is_directory=True)
    else:
        fixture.symlink_to(outside / fixture.name)
    changed = metrics.build_report(project, run)
    assert path not in changed["source"]["files"]
    assert changed["status"] == "stale"
    assert_no_scores(changed)
    assert reports._source_status(project, run, quality, data)["status"] == "stale"
