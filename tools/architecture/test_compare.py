"""Baseline comparisons must not turn missing or incompatible evidence into gains."""

from copy import deepcopy

import pytest

from tools.architecture.compare import compare


def function(name="Service.calculate", *, group="backend", coverage=0.5, cc=4, line=10):
    return {
        "group": group,
        "path": "backend/app/service.py" if group == "backend" else "frontend/src/service.ts",
        "name": name,
        "line": line,
        "end_line": line + 5,
        "cc": cc,
        "coverage": coverage,
        "crap": cc**2 * (1 - coverage) ** 3 + cc,
        "status": "measured",
        "reason": None,
    }


def report(*rows):
    return {
        "schema_version": 1,
        "generated_at": "2026-10-04T12:00:00+00:00",
        "status": "current",
        "source": {
            "root": "/work/baseline",
            "files": {row["path"]: "a" * 64 for row in rows},
            "config": {"backend/pytest.ini": "b" * 64, "frontend/vitest.config.ts": "c" * 64},
        },
        "scope": {
            "backend": ["backend/app/**/*.py"],
            "frontend": ["frontend/src/**/*.ts"],
            "exclude": ["**/*.spec.*"],
            "method": "crap-executable-lines-v1",
            "formula": "CC^2 * (1 - coverage)^3 + CC",
            "complexity": {"backend": "radon", "frontend": "eslint-classic-max0"},
        },
        "tools": {
            "analyzers": {
                "backend": {"radon": "6.0.1", "coverage.py-json": "7.16.2"},
                "frontend": {
                    "eslint": "9.39.1",
                    "@typescript-eslint/parser": "8.22.0",
                    "vue-eslint-parser": "10.2.0",
                },
            },
            "collectors": {
                "coverage.py-json": "7.16.2",
                "vitest": "2.1.9",
                "@vitest/coverage-istanbul": "2.1.9",
                "@vitejs/plugin-vue": "5.2.4",
            },
            "implementation": {
                "metrics.py": "d" * 64,
                "python_metrics.py": "e" * 64,
                "frontend_metrics.mjs": "f" * 64,
            },
        },
        "groups": {
            group: {
                "status": "current",
                "reasons": [],
                "scope": {
                    "source_globs": ["backend/app/**/*.py", "frontend/src/**/*.ts"],
                    "coverage_requested": True,
                },
                "run": {"id": "123", "attempt": "1"},
            }
            for group in ("backend", "frontend")
        },
        "functions": list(rows),
        "errors": [],
    }


def test_comparison_preserves_both_locations_and_matches_qualified_names_not_line_numbers():
    before = report(function("better"), function("worse"), function("stable"))
    after = report(
        function("better", coverage=1, line=42),
        function("worse", coverage=0),
        function("stable", line=77),
    )
    after["source"]["root"] = "/another/machine/current"
    after["source"]["files"] = {path: "b" * 64 for path in after["source"]["files"]}
    after["groups"]["backend"]["run"] = {"id": "987", "attempt": "4"}
    result = compare(before, after)
    assert result["status"] == "comparable"
    assert [row["name"] for row in result["improved"]] == ["better"]
    assert result["improved"][0]["delta"] == -2
    assert result["improved"][0]["before"]["line"] == 10
    assert result["improved"][0]["after"]["line"] == 42
    assert result["worsened"][0]["delta"] == 14
    assert result["unchanged"][0]["name"] == "stable"
    assert not result["unknown"]


def test_renamed_or_moved_functions_are_added_and_removed_not_improved():
    old = function("Original.calculate")
    renamed = function("Renamed.calculate", coverage=1)
    moved = function("Original.calculate", coverage=1)
    moved["path"] = "backend/app/moved.py"
    result = compare(report(old), report(renamed, moved))
    assert len(result["added"]) == 2
    assert len(result["removed"]) == 1
    assert not result["improved"]
    assert all(row["delta"] is None for row in result["added"] + result["removed"])


def test_nested_and_class_qualified_names_are_independent_identities():
    before = report(function("A.run"), function("B.run"), function("outer.run"))
    after = report(
        function("A.run", coverage=1), function("B.run"), function("outer.run", coverage=0)
    )
    result = compare(before, after)
    assert result["improved"][0]["name"] == "A.run"
    assert result["unchanged"][0]["name"] == "B.run"
    assert result["worsened"][0]["name"] == "outer.run"


@pytest.mark.parametrize("side", ["before", "after"])
def test_duplicate_qualified_names_remain_unknown_even_when_lines_match(side):
    before, after = report(function()), report(function(coverage=1))
    duplicate = function(line=80)
    (before if side == "before" else after)["functions"].append(duplicate)
    result = compare(before, after)
    assert not result["improved"]
    item = result["unknown"][0]
    assert "重复限定名" in item["reason"]
    assert len(item["candidates"][side]) == 2
    assert item["delta"] is None


@pytest.mark.parametrize("status", ["unknown", "not_applicable", "partial", "stale"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_unknown_never_appears_as_improvement(status, side):
    before, after = report(function(coverage=0)), report(function(coverage=1))
    row = (before if side == "before" else after)["functions"][0]
    row.update(status=status, coverage=None, crap=None, reason="函数范围无法映射")
    result = compare(before, after)
    assert len(result["unknown"]) == 1
    assert not result["improved"]
    assert result["unknown"][0][side]["reason"] == "函数范围无法映射"


@pytest.mark.parametrize("status", ["unknown", "partial", "stale"])
def test_incomplete_group_cannot_hide_missing_functions_as_removals(status):
    before, after = report(function()), report()
    after["groups"]["backend"]["status"] = status
    result = compare(before, after)
    assert result["status"] == "partial"
    assert not result["removed"]
    assert result["unknown"][0]["name"] == "Service.calculate"


@pytest.mark.parametrize(
    "path,value,reason",
    [
        (("scope", "method"), "instruction-coverage-v2", "口径"),
        (("scope", "backend"), ["backend/app/subset/*.py"], "范围"),
        (("scope", "exclude"), [], "范围"),
        (("groups", "backend", "scope", "coverage_requested"), False, "采集范围"),
        (("tools", "analyzers", "backend", "radon"), "7.0.0", "分析器版本"),
        (("tools", "collectors", "coverage.py-json"), "8.0.0", "采集器版本"),
        (("tools", "implementation", "python_metrics.py"), "9" * 64, "指标实现"),
        (("source", "config", "backend/pytest.ini"), "9" * 64, "配置"),
    ],
)
def test_incompatible_scope_tools_or_config_prevent_score_comparison(path, value, reason):
    before, after = report(function()), report(function(coverage=1))
    target = after
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = compare(before, after)
    assert result["groups"]["backend"]["status"] == "incomparable"
    assert any(reason in item for item in result["groups"]["backend"]["reasons"])
    assert not result["improved"]
    assert result["unknown"][0]["delta"] is None


def test_frontend_tool_or_config_change_does_not_invalidate_python_comparison():
    before = report(function(), function(group="frontend"))
    after = report(function(coverage=1), function(group="frontend", coverage=1))
    after["tools"]["analyzers"]["frontend"]["eslint"] = "10.0.0"
    after["source"]["config"]["frontend/vitest.config.ts"] = "9" * 64
    result = compare(before, after)
    assert result["status"] == "partial"
    assert result["improved"][0]["group"] == "backend"
    assert result["unknown"][0]["group"] == "frontend"


def test_equal_but_unknown_versions_cannot_establish_compatibility():
    before, after = report(function()), report(function(coverage=1))
    for item in (before, after):
        item["tools"]["collectors"]["coverage.py-json"] = None
    result = compare(before, after)
    assert not result["improved"]
    assert "缺少分析器" in result["unknown"][0]["reason"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("crap", None),
        ("crap", float("nan")),
        ("crap", -1),
        ("crap", 0),
        ("coverage", 2),
        ("coverage", True),
        ("cc", 0),
        ("cc", 10**400),
    ],
)
def test_invalid_measured_numbers_are_unknown(field, value):
    before, after = report(function()), report(function(coverage=1))
    after["functions"][0][field] = value
    result = compare(before, after)
    assert not result["improved"]
    assert result["unknown"][0]["delta"] is None


def test_missing_function_list_and_source_fingerprint_are_unknown():
    before, after = report(function()), report(function(coverage=1))
    del after["functions"]
    result = compare(before, after)
    assert result["status"] == "incomparable"
    assert not result["removed"]
    after = report(function(coverage=1))
    after["source"]["files"].clear()
    assert "源码指纹" in compare(before, after)["unknown"][0]["reason"]


def test_malformed_function_identity_cannot_hide_a_removed_function():
    before, after = report(function()), report(function())
    after["functions"][0]["name"] = None
    result = compare(before, after)
    assert result["groups"]["backend"]["status"] == "incomparable"
    assert len(result["unknown"]) == 2
    assert not result["removed"]


def test_empty_compatible_reports_have_no_invented_scores():
    result = compare(report(), report())
    assert result["status"] == "comparable"
    assert all(
        not result[bucket]
        for bucket in ("added", "removed", "worsened", "improved", "unchanged", "unknown")
    )
    assert "score" not in result


def test_comparison_is_deterministic_and_does_not_mutate_or_alias_inputs():
    before, after = report(function()), report(function(coverage=1))
    originals = deepcopy((before, after))
    result = compare(before, after)
    assert result == compare(before, after)
    assert (before, after) == originals
    result["improved"][0]["after"]["line"] = 12345
    assert (before, after) == originals
    assert "rules" not in result and "mutation" not in result


@pytest.mark.parametrize(
    "path",
    [
        "backend/tests/fixtures/reports/sample.json",
        "backend/tests/fixtures/reports/sample.csv",
        "backend/tests/fixtures/reports/sample.txt.gz",
        "backend/tests/snapshots/statistics_baseline.json",
    ],
)
@pytest.mark.parametrize("change", ["add", "modify", "delete"])
def test_changed_test_inputs_prevent_comparing_two_current_backend_reports(path, change):
    before = report(function(), function(group="frontend"))
    after = report(function(coverage=1), function(group="frontend", coverage=1))
    if change != "add":
        before["source"]["files"][path] = "a" * 64
    if change != "delete":
        after["source"]["files"][path] = "b" * 64
    result = compare(before, after)
    assert result["groups"]["backend"]["status"] == "incomparable"
    assert "两份报告的测试输入不兼容" in result["groups"]["backend"]["reasons"]
    assert result["unknown"][0]["group"] == "backend"
    assert result["unknown"][0]["delta"] is None
    assert result["groups"]["frontend"]["status"] == "comparable"
    assert [row["group"] for row in result["improved"]] == ["frontend"]
