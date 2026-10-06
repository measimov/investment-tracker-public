"""Function coverage evidence and CRAP regressions using Radon and native reports."""

import json
from pathlib import Path

from coverage import Coverage
import pytest

from tools.architecture import python_metrics


RISK_SOURCE = (
    "def risk(value):\n"
    + "".join(f"    if value == {i}: return {i}\n" for i in range(9))
    + "    return value\n"
)


def write(root: Path, source: str) -> list[str]:
    (root / "sample.py").write_text(source, encoding="utf-8")
    return ["sample.py"]


def function_report(name, start, executed, missing, excluded=None, **summary_overrides):
    excluded = excluded or []
    covered = len(set(executed) - set(excluded))
    return {
        name: {
            "start_line": start,
            "executed_lines": executed,
            "missing_lines": missing,
            "excluded_lines": excluded,
            "summary": {
                "covered_lines": covered,
                "num_statements": covered + len(missing),
                **summary_overrides,
            },
        }
    }


def report(functions):
    return {"meta": {"version": "7.16.2"}, "files": {"sample.py": {"functions": functions}}}


def native_report(root: Path, source: str, exercise: str) -> dict:
    filename = root / write(root, source)[0]
    collector = Coverage(
        data_file=str(root / ".coverage"), source=[str(root)], branch=True, config_file=False
    )
    collector.start()
    try:
        namespace = {}
        exec(compile(source, str(filename), "exec"), namespace)
        exec(exercise, namespace)
    finally:
        collector.stop()
    destination = root / "coverage.json"
    collector.json_report(outfile=str(destination))
    return json.loads(destination.read_text())


@pytest.mark.parametrize("covered,expected", [(0, 110), (5, 22.5), (10, 10)])
def test_crap_formula_uses_executable_line_ratio_not_branch_percent(tmp_path, covered, expected):
    files = write(tmp_path, RISK_SOURCE)
    body = list(range(2, 12))
    evidence = report(
        function_report("risk", 1, body[:covered], body[covered:], percent_covered=17)
    )
    result = python_metrics.analyze(tmp_path, files, evidence)
    row = result["functions"][0]
    assert row["cc"] == 10
    assert row["coverage"] == covered / 10
    assert row["crap"] == expected
    assert row["status"] == "measured"
    assert row["reason"] is None
    assert result["tools"]["radon"]
    assert result["tools"]["coverage.py-json"] == "7.16.2"


@pytest.mark.parametrize(
    "evidence,reason",
    [
        (None, "未提供"),
        ({"files": {}}, "不包含该文件"),
        ({"files": {"sample.py": {"summary": {"percent_covered": 100}}}}, "缺少函数级数据"),
        (report({}), "不包含该函数范围"),
    ],
)
def test_missing_coverage_stays_unknown_not_zero(tmp_path, evidence, reason):
    row = python_metrics.analyze(tmp_path, write(tmp_path, RISK_SOURCE), evidence)["functions"][0]
    assert row["cc"] == 10
    assert row["coverage"] is None and row["crap"] is None
    assert row["status"] == "unknown"
    assert reason in row["reason"]


def test_native_nested_function_lines_are_not_counted_in_outer_function(tmp_path):
    source = (
        "def outer():\n"
        "    def inner(value):\n"
        "        if value:\n"
        "            return 1\n"
        "        return 0\n"
        "    return 2\n"
    )
    evidence = native_report(tmp_path, source, "outer()")
    rows = {
        row["name"]: row
        for row in python_metrics.analyze(tmp_path, ["sample.py"], evidence)["functions"]
    }
    assert rows["outer"]["cc"] == 1
    assert rows["outer"]["coverage"] == 1
    assert rows["outer"]["crap"] == 1
    assert rows["outer.inner"]["cc"] == 2
    assert rows["outer.inner"]["coverage"] == 0
    assert rows["outer.inner"]["crap"] == 6
    assert rows["outer.inner"]["line"] == 2
    assert rows["outer.inner"]["end_line"] == 5


def test_overlapping_nested_ranges_are_rejected_instead_of_counted_twice(tmp_path):
    source = "def outer():\n    def inner():\n        return 1\n    return 2\n"
    functions = {
        **function_report("outer", 1, [2, 4], [3]),
        **function_report("outer.inner", 2, [], [3]),
    }
    rows = python_metrics.analyze(tmp_path, write(tmp_path, source), report(functions))["functions"]
    assert rows[0]["status"] == "unknown"
    assert "嵌套" in rows[0]["reason"]
    assert rows[0]["coverage"] is None
    assert rows[1]["coverage"] == 0


def test_native_decorators_async_methods_and_same_simple_names(tmp_path):
    source = (
        "def decorate(function):\n"
        "    return function\n"
        "@decorate\n"
        "def run(value):\n"
        "    if value:\n"
        "        return 1\n"
        "    return 0\n"
        "class Service:\n"
        "    @staticmethod\n"
        "    @decorate\n"
        "    async def run(value):\n"
        "        if value:\n"
        "            return 2\n"
        "        return 3\n"
    )
    evidence = native_report(
        tmp_path, source, "import asyncio\nrun(True)\nasyncio.run(Service.run(False))"
    )
    rows = {
        row["name"]: row
        for row in python_metrics.analyze(tmp_path, ["sample.py"], evidence)["functions"]
    }
    file_data = next(iter(evidence["files"].values()))
    assert rows["run"]["line"] == 4 and rows["run"]["end_line"] == 7
    assert rows["Service.run"]["line"] == 11 and rows["Service.run"]["end_line"] == 14
    for name in ["run", "Service.run"]:
        summary = file_data["functions"][name]["summary"]
        assert rows[name]["cc"] == 2
        assert rows[name]["coverage"] == summary["covered_lines"] / summary["num_statements"]
        assert rows[name]["status"] == "measured"


def test_same_qualified_name_is_disambiguated_by_definition_line(tmp_path):
    source = "def same():\n    return 1\nfirst = same\ndef same():\n    return 2\n"
    evidence = native_report(tmp_path, source, "first()\nsame()")
    rows = python_metrics.analyze(tmp_path, ["sample.py"], evidence)["functions"]
    assert [row["line"] for row in rows] == [1, 4]
    assert rows[0]["status"] == "unknown"
    assert "定义行" in rows[0]["reason"]
    assert rows[1]["status"] == "measured" and rows[1]["coverage"] == 1
    native_function = next(iter(evidence["files"].values()))["functions"]["same"]
    native_function.pop("start_line")
    rows = python_metrics.analyze(tmp_path, ["sample.py"], evidence)["functions"]
    assert all(row["status"] == "unknown" and row["crap"] is None for row in rows)


def test_excluded_function_is_not_reported_as_zero_coverage(tmp_path):
    source = "def ignored():  # pragma: no cover\n    return 1\n"
    evidence = native_report(tmp_path, source, "ignored()")
    row = python_metrics.analyze(tmp_path, ["sample.py"], evidence)["functions"][0]
    assert row["status"] == "not_applicable"
    assert row["coverage"] is None and row["crap"] is None


@pytest.mark.parametrize(
    "functions,reason",
    [
        (function_report("risk", 99, [], list(range(2, 12))), "定义行"),
        (function_report("risk", 1, [12], [], num_statements=1), "超出"),
        ({"risk": {"summary": {"num_statements": 10, "covered_lines": 10}}}, "执行行范围"),
        (function_report("risk", 1, [2], [], covered_lines=20), "计数无效"),
        (function_report("risk", 1, [2], [], num_statements=10), "不一致"),
    ],
)
def test_bad_or_unmatched_function_ranges_are_unknown(tmp_path, functions, reason):
    row = python_metrics.analyze(tmp_path, write(tmp_path, RISK_SOURCE), report(functions))[
        "functions"
    ][0]
    assert row["status"] == "unknown"
    assert reason in row["reason"]
    assert row["coverage"] is None and row["crap"] is None


def test_source_is_never_executed_and_parse_failures_are_visible(tmp_path):
    source = "raise RuntimeError('must not execute')\nasync def safe():\n    return 1\n"
    write(tmp_path, source)
    (tmp_path / "broken.py").write_text("def broken(:\n")
    result = python_metrics.analyze(tmp_path, ["sample.py", "broken.py"], None)
    assert [row["name"] for row in result["functions"]] == ["safe"]
    assert result["errors"][0]["path"] == "broken.py"
    assert "解析失败" in result["errors"][0]["message"]


def test_nested_class_method_keeps_qualified_identity(tmp_path):
    source = "def factory():\n    class Item:\n        def read(self):\n            return 1\n    return Item\n"
    evidence = native_report(tmp_path, source, "factory()().read()")
    rows = python_metrics.analyze(tmp_path, ["sample.py"], evidence)["functions"]
    assert [row["line"] for row in rows] == [1, 3]
    assert [row["name"] for row in rows] == ["factory", "factory.Item.read"]
    assert all(row["status"] == "measured" for row in rows)
