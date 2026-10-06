"""Associate Radon function complexity with native coverage.py function reports."""

from __future__ import annotations

import ast
from collections import Counter
from importlib.metadata import version
from pathlib import Path
import tokenize

from radon.complexity import cc_visit_ast

FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


class _Functions(ast.NodeVisitor):
    """Keep qualified identities and source ranges; Radon owns complexity rules."""

    def __init__(self):
        self.names: list[str] = []
        self.functions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit_ClassDef(self, node):
        self.names.append(node.name)
        self.generic_visit(node)
        self.names.pop()

    def visit_FunctionDef(self, node):
        self.names.append(node.name)
        self.functions.append((".".join(self.names), node))
        self.generic_visit(node)
        self.names.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def _file_coverage(root: Path, path: str, report: dict | None) -> tuple[dict | None, str | None]:
    if report is None:
        return None, "未提供覆盖率报告"
    reported_files = report.get("files")
    if not isinstance(reported_files, dict):
        return None, "覆盖率报告缺少文件数据"
    matches = []
    for reported_path, data in reported_files.items():
        candidate = Path(reported_path)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(root)
            except ValueError:
                continue
        if candidate.as_posix() == path:
            matches.append(data)
    if not matches:
        return None, "覆盖率报告不包含该文件"
    if len(matches) != 1 or not isinstance(matches[0], dict):
        return None, "覆盖率文件身份不明确或数据无效"
    return matches[0], None


def _lines(region: dict, field: str) -> set[int] | None:
    values = region.get(field, [] if field == "excluded_lines" else None)
    if not isinstance(values, list) or any(type(line) is not int or line < 1 for line in values):
        return None
    return set(values)


def _function_coverage(
    data: dict,
    name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    repeated_name: bool,
) -> tuple[float | None, str, str | None]:
    functions = data.get("functions")
    if not isinstance(functions, dict):
        return None, "unknown", "覆盖率报告缺少函数级数据，不能用文件覆盖率代替"
    region = functions.get(name)
    if not isinstance(region, dict):
        return None, "unknown", "覆盖率报告不包含该函数范围"
    start = region.get("start_line")
    if start is not None and (type(start) is not int or start != node.lineno):
        return None, "unknown", "覆盖率函数定义行不匹配，不能关联同名函数"
    if start is None and repeated_name:
        return None, "unknown", "同名函数报告缺少定义行，无法区分"
    summary = region.get("summary")
    if not isinstance(summary, dict):
        return None, "unknown", "函数覆盖率汇总缺失"
    covered, statements = summary.get("covered_lines"), summary.get("num_statements")
    if type(covered) is not int or type(statements) is not int or not 0 <= covered <= statements:
        return None, "unknown", "函数可执行行计数无效"
    executed = _lines(region, "executed_lines")
    missing = _lines(region, "missing_lines")
    excluded = _lines(region, "excluded_lines")
    if executed is None or missing is None or excluded is None:
        return None, "unknown", "函数报告缺少有效的执行行范围"
    body = set(range(node.body[0].lineno, node.end_lineno + 1))
    observed = executed | missing | excluded
    if not observed <= body:
        return None, "unknown", "覆盖率行号超出该函数体，范围无法匹配"
    nested_bodies = set()
    for child in ast.walk(node):
        if child is not node and isinstance(child, FUNCTION_NODES):
            nested_bodies.update(range(child.body[0].lineno, child.end_lineno + 1))
    if observed & nested_bodies:
        return None, "unknown", "函数报告包含嵌套函数体，不能重复计算执行行"
    # Executed lines can include excluded/non-statement lines. Native summary
    # counts are authoritative; never substitute a branch-mixed percentage.
    if (
        executed & missing
        or missing & excluded
        or len(missing) != statements - covered
        or covered > len(executed - excluded)
    ):
        return None, "unknown", "函数执行行范围与可执行行计数不一致"
    if statements == 0:
        return None, "not_applicable", "函数没有可执行行，或已全部排除"
    return covered / statements, "measured", None


def analyze(root: Path, files: list[str], coverage: dict | None) -> dict:
    """Analyze allowed Python snapshots without importing or executing source code.

    The caller owns snapshot/report identity and completeness checks. ``measured``
    only means these function-level inputs can be associated and calculated.
    """
    root = root.resolve()
    result = {"functions": [], "errors": [], "tools": {"radon": version("radon")}}
    if coverage is not None and isinstance(coverage.get("meta"), dict):
        report_version = coverage["meta"].get("version")
        if isinstance(report_version, str):
            result["tools"]["coverage.py-json"] = report_version
    for path in sorted(set(files)):
        if Path(path).suffix != ".py":
            continue
        try:
            with tokenize.open(root / path) as source:
                tree = ast.parse(source.read(), filename=path)
        except (OSError, SyntaxError, UnicodeError) as error:
            result["errors"].append({"path": path, "message": f"Python 解析失败：{error}"})
            continue
        collector = _Functions()
        collector.visit(tree)
        names = Counter(name for name, _ in collector.functions)
        data, missing_reason = _file_coverage(root, path, coverage)
        for name, node in collector.functions:
            row = {
                "path": path,
                "name": name,
                "line": node.lineno,
                "end_line": node.end_lineno,
                "cc": None,
                "coverage": None,
                "crap": None,
                "status": "unknown",
                "reason": None,
            }
            try:
                # Visiting each definition delegates closure/method semantics to
                # Radon while AST supplies complete qualified names and ranges.
                row["cc"] = cc_visit_ast(node)[0].complexity
            except Exception as error:
                row["reason"] = f"Radon 无法评估该函数（{type(error).__name__}）"
                result["functions"].append(row)
                continue
            if data is None:
                row["reason"] = missing_reason
            else:
                ratio, status, reason = _function_coverage(data, name, node, names[name] > 1)
                row.update(coverage=ratio, status=status, reason=reason)
                if ratio is not None:
                    row["crap"] = row["cc"] ** 2 * (1 - ratio) ** 3 + row["cc"]
            result["functions"].append(row)
    result["functions"].sort(key=lambda row: (row["path"], row["line"], row["name"]))
    return result
