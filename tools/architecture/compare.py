"""Compare two explicit quality.json v1 reports without reading files or running tools.

Only CRAP is compared, independently for backend and frontend. A lower measured
score is an improvement; unknown, stale, partial, ambiguous or incompatible data
never is. Identity is (group, repository path, qualified name), not line number.
The returned buckets retain each side's native function fields. ``removed`` means
the identity disappeared, not that its risk was fixed; renames are not inferred.
Top-level status summarizes group compatibility, not the proportion of measured
functions. Each unknown function remains in its own bucket with no numeric delta.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
import re

from ops.ci.checks import TEST_INPUT_PREFIXES

GROUPS = ("backend", "frontend")
COLLECTORS = {
    "backend": ("coverage.py-json",),
    "frontend": ("vitest", "@vitest/coverage-istanbul", "@vitejs/plugin-vue"),
}
IMPLEMENTATIONS = {
    "backend": ("metrics.py", "python_metrics.py"),
    "frontend": ("metrics.py", "frontend_metrics.mjs"),
}
BUCKETS = ("added", "removed", "worsened", "improved", "unchanged", "unknown")


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _versions(value) -> bool:
    return bool(value) and all(isinstance(item, str) and item for item in value.values())


def _compatibility(before: dict, after: dict, group: str) -> list[str]:
    reasons = []
    signatures = []
    for label, report in (("基线", before), ("当前", after)):
        if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            reasons.append(f"{label}质量报告版本不受支持")
        source = _object(report.get("source"))
        if not isinstance(source.get("files"), dict) or not isinstance(source.get("config"), dict):
            reasons.append(f"{label}缺少源码或配置指纹")
        scope = _object(report.get("scope"))
        if (
            scope.get("method") != "crap-executable-lines-v1"
            or scope.get("formula") != "CC^2 * (1 - coverage)^3 + CC"
            or not scope.get(group)
            or not _object(scope.get("complexity")).get(group)
        ):
            reasons.append(f"{label}缺少受支持的 CRAP 计算口径或分析范围")
        provenance = _object(_object(report.get("groups")).get(group))
        if provenance.get("status") != "current":
            reasons.append(f"{label}分组不是完整有效的采集结果")
        if _object(provenance.get("scope")).get("coverage_requested") is not True:
            reasons.append(f"{label}缺少测试采集范围")
        tools = _object(report.get("tools"))
        analyzers = _object(_object(tools.get("analyzers")).get(group))
        collectors = {
            name: _object(tools.get("collectors")).get(name) for name in COLLECTORS[group]
        }
        implementation = {
            name: _object(tools.get("implementation")).get(name) for name in IMPLEMENTATIONS[group]
        }
        if not all(_versions(item) for item in (analyzers, collectors, implementation)):
            reasons.append(f"{label}缺少分析器、采集器版本或实现指纹")
        other = "frontend" if group == "backend" else "backend"
        signatures.append(
            {
                "计算口径或分析范围": {
                    **{
                        key: value
                        for key, value in scope.items()
                        if key not in (*GROUPS, "complexity")
                    },
                    "files": scope.get(group),
                    "complexity": _object(scope.get("complexity")).get(group),
                },
                "测试采集范围": provenance.get("scope"),
                "检查或依赖配置": {
                    path: digest
                    for path, digest in _object(source.get("config")).items()
                    if not isinstance(path, str) or not path.startswith(f"{other}/")
                },
                "测试输入": {
                    path: digest
                    for path, digest in _object(source.get("files")).items()
                    if isinstance(path, str)
                    and path.startswith(TEST_INPUT_PREFIXES)
                    and not path.startswith(f"{other}/")
                },
                "分析器版本": analyzers,
                "采集器版本": collectors,
                "指标实现": implementation,
            }
        )
    for field, value in signatures[0].items():
        if value != signatures[1][field]:
            reasons.append(f"两份报告的{field}不兼容")
    return list(dict.fromkeys(reasons))


def _identity(row: dict) -> tuple[str, str, str] | None:
    group, path, name = row.get("group"), row.get("path"), row.get("name")
    if (
        group not in GROUPS
        or not isinstance(path, str)
        or not path
        or not isinstance(name, str)
        or not name
    ):
        return None
    return group, path, name


def _measurement_problem(row: dict, source: dict) -> str | None:
    digest = _object(source.get("files")).get(row["path"])
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "函数缺少对应源码指纹"
    if row.get("status") != "measured":
        return "函数覆盖率或 CRAP 未知、不适用或不是有效测量"
    if (
        any(
            type(row.get(field)) not in (int, float)
            or not 0 <= row[field] <= 1e15
            or not math.isfinite(row[field])
            for field in ("cc", "coverage", "crap")
        )
        or row["cc"] < 1
        or row["coverage"] > 1
    ):
        return "函数复杂度、行覆盖率或 CRAP 数值无效"
    expected = row["cc"] ** 2 * (1 - row["coverage"]) ** 3 + row["cc"]
    if not math.isclose(row["crap"], expected, rel_tol=1e-9, abs_tol=1e-9):
        return "CRAP 与声明的复杂度、行覆盖率不一致"
    return None


def compare(before: dict, after: dict) -> dict:
    """Return per-group compatibility and CRAP change buckets, without mutation."""
    before, after = _object(before), _object(after)
    result = {"schema_version": 1, "metric": "crap", "groups": {}, **{key: [] for key in BUCKETS}}
    indexed = []
    incomplete_groups = set()
    for side, report in (("before", before), ("after", after)):
        rows = defaultdict(list)
        functions = report.get("functions")
        if not isinstance(functions, list):
            functions = []
            incomplete_groups.update(GROUPS)
            result["unknown"].append(
                {"before": None, "after": None, "delta": None, "reason": f"{side} 报告缺少函数列表"}
            )
        for raw in functions:
            row = deepcopy(raw) if isinstance(raw, dict) else None
            identity = _identity(row) if row is not None else None
            if identity is None:
                group = row.get("group") if row is not None else None
                incomplete_groups.update([group] if group in GROUPS else GROUPS)
                result["unknown"].append(
                    {
                        "before": row if side == "before" else None,
                        "after": row if side == "after" else None,
                        "delta": None,
                        "reason": "函数缺少分组、文件路径或限定名称，无法匹配",
                    }
                )
            else:
                rows[identity].append(row)
        indexed.append(rows)
    for group in GROUPS:
        reasons = _compatibility(before, after, group)
        if group in incomplete_groups:
            reasons.append("函数列表或身份不完整，不能判定新增或移除")
        result["groups"][group] = {
            "status": "incomparable" if reasons else "comparable",
            "reasons": reasons,
        }
    left, right = indexed
    for identity in sorted(left.keys() | right.keys()):
        group, path, name = identity
        old, new = left[identity], right[identity]
        item = {
            "group": group,
            "path": path,
            "name": name,
            "before": old[0] if len(old) == 1 else None,
            "after": new[0] if len(new) == 1 else None,
            "delta": None,
        }
        reasons = result["groups"][group]["reasons"].copy()
        if len(old) > 1 or len(new) > 1:
            reasons.append("同一路径存在重复限定名，不能按行号猜测函数对应关系")
            item["candidates"] = {"before": old, "after": new}
        for label, rows, report in (("基线", old, before), ("当前", new, after)):
            if len(rows) == 1:
                problem = _measurement_problem(rows[0], _object(report.get("source")))
                if problem:
                    reasons.append(f"{label}：{problem}")
        if reasons:
            bucket, reason = "unknown", "；".join(reasons)
        elif not old:
            bucket, reason = "added", "基线没有相同路径及限定名；不推断重命名"
        elif not new:
            bucket, reason = "removed", "当前没有相同路径及限定名；移除不计为分数改善"
        else:
            item["delta"] = new[0]["crap"] - old[0]["crap"]
            if math.isclose(item["delta"], 0, abs_tol=1e-9):
                bucket, reason = "unchanged", "CRAP 未变化"
            elif item["delta"] > 0:
                bucket, reason = "worsened", "CRAP 升高"
            else:
                bucket, reason = "improved", "CRAP 降低"
        result[bucket].append({**item, "reason": reason})
    comparable = sum(value["status"] == "comparable" for value in result["groups"].values())
    result["status"] = (
        "comparable" if comparable == len(GROUPS) else "partial" if comparable else "incomparable"
    )
    return result
