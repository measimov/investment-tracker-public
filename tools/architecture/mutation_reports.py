"""Bounded, read-only projection of the fixed mutmut pilot's native report."""

from collections import Counter
import ast
import hashlib
import math
import re
import xml.etree.ElementTree as ET

from ops.ci.mutation import FUNCTION, PREFIX, TARGET, TEST
from tools.architecture import server

STATES = ("killed", "survived", "timeout", "tool_error", "no_coverage", "not_checked", "unknown")
TOOLS = ("mutmut", "python", "pytest", "config_sha256", "implementation_sha256")
SCOPE = {"path": f"backend/{TARGET}", "function": FUNCTION, "tests": [f"backend/{TEST}"]}
HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_BYTES = 2 * 1024 * 1024


def _fingerprints(value):
    if not isinstance(value, dict) or not value or len(value) > 10000:
        return {}
    for path, digest in value.items():
        if (
            not isinstance(path, str)
            or not server._valid_path(path)
            or not (
                path in ("backend/conftest.py", "backend/alembic.ini", "backend/requirements.txt")
                or path.startswith(("backend/app/", "backend/tests/", "backend/alembic/"))
            )
            or any(
                not server._allowed_directory(part)
                and not (index == 3 and path.startswith("backend/tests/fixtures/reports/"))
                for index, part in enumerate(path.split("/")[:-1])
            )
            or path.split("/")[-1].startswith(".")
            or (server.SENSITIVE_NAME.search(path.split("/")[-1]) and not path.endswith(".py"))
            or not isinstance(digest, str)
            or not HASH.fullmatch(digest)
        ):
            return {}
    return value if all(path in value for path in [SCOPE["path"], *SCOPE["tests"]]) else {}


def _step(value):
    value = value if isinstance(value, dict) else {}
    code = value.get("exit_code")
    code = code if type(code) is int else None
    duration = value.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        duration = None
    return {
        "status": "unknown" if code is None else "passed" if code == 0 else "failed",
        "exit_code": code,
        "duration_seconds": duration,
    }


def _baseline(reader, prefix, step):
    raw = reader.read(f"{prefix}/native/baseline.xml", MAX_BYTES)
    if raw is None:
        step["status"] = "unknown" if step["status"] == "passed" else step["status"]
        return False
    try:
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper() or b"\x00" in raw:
            raise ValueError
        cases = list(ET.fromstring(raw).iter("testcase"))
        if any(
            case.find("failure") is not None or case.find("error") is not None for case in cases
        ):
            step["status"] = "failed"
        elif not cases or any(case.find("skipped") is not None for case in cases):
            step["status"] = "unknown" if step["status"] == "passed" else step["status"]
        return step["status"] == "passed"
    except (ET.ParseError, ValueError):
        step["status"] = "unknown" if step["status"] == "passed" else step["status"]
        return False


def _source(reader, prefix, root, files, enabled):
    result = {"status": "unknown", "can_navigate": False, "changed_files": []}
    if not files or not enabled:
        return result, None
    # Only the pilot's recorded backend snapshot is hashed, never arbitrary paths.
    remaining, target = 32 * 1024 * 1024, None
    for path, expected in files.items():
        try:
            raw = server._read_file(root, path, min(MAX_BYTES, remaining))
            remaining -= len(raw)
            if hashlib.sha256(raw).hexdigest() != expected:
                result["changed_files"].append(path)
            elif path == SCOPE["path"]:
                target = raw
        except (OSError, ValueError):
            result["changed_files"].append(path)
    result["changed_files"].sort()
    result["status"] = "stale" if result["changed_files"] else "current"
    original = reader.read(f"{prefix}/native/fx.original.py", MAX_BYTES)
    if original is not None:
        target = original if hashlib.sha256(original).hexdigest() == files[SCOPE["path"]] else None
    line = None
    if target is not None:
        try:
            definitions = [
                node
                for node in ast.parse(target).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == FUNCTION
            ]
            if len(definitions) == 1:
                line = definitions[0].lineno
        except (SyntaxError, ValueError):
            pass
    result["can_navigate"] = result["status"] == "current" and line is not None
    return result, line


def load(reader, identifier, root, clean_text, *, with_source=True):
    prefix = f"{identifier}/mutation"
    path = f"{prefix}/manifest.json"
    raw = reader.json(path, MAX_BYTES)
    if raw is None:
        return None
    if raw.get("schema_version") != 1 or raw.get("scope") != SCOPE:
        reader.errors.append({"path": path, "message": "变异报告版本或固定试点范围不受支持"})
        return None
    reasons = []
    files = _fingerprints(raw.get("source_files"))
    if not files:
        reasons.append("变异源码指纹缺失或无效")
    steps = raw.get("steps") if isinstance(raw.get("steps"), dict) else {}
    steps = {name: _step(steps.get(name)) for name in ("baseline", "mutation")}
    if not _baseline(reader, prefix, steps["baseline"]):
        reasons.append("原测试未通过，或原生 JUnit 证据缺失、不完整")
    if steps["mutation"]["status"] != "passed":
        reasons.append("变异执行未完整通过")
    rows, names = [], set()
    originals = raw.get("mutants")
    if not isinstance(originals, list) or not originals or len(originals) > 10000:
        originals = []
        reasons.append("变异结果缺失或超过读取上限")
    for item in originals:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            reasons.append("变异记录无效")
            continue
        name = item["name"]
        if not re.fullmatch(re.escape(PREFIX) + r"\d+", name) or name in names:
            reasons.append("变异标识重复或不属于固定函数")
            continue
        names.add(name)
        state = item.get("status") if item.get("status") in STATES else "unknown"
        code = item.get("exit_code") if type(item.get("exit_code")) is int else None
        native = clean_text(item.get("native_status"), 100)
        if state in ("killed", "survived") and (
            native != state or code != (1 if state == "killed" else 0)
        ):
            state = "unknown"
            reasons.append("变异结论与原生状态或退出码矛盾")
        rows.append(
            {
                "name": name,
                "status": state,
                "native_status": native,
                "exit_code": code,
                "equivalence": "unreviewed" if state == "survived" else None,
            }
        )
    counts = {state: 0 for state in STATES}
    counts.update(Counter(item["status"] for item in rows))
    claimed = raw.get("counts")
    if (
        not isinstance(claimed, dict)
        or any(
            type(value) is not int or value < 0 or key not in STATES
            for key, value in claimed.items()
        )
        or any(claimed.get(state, 0) != count for state, count in counts.items())
    ):
        reasons.append("变异计数与逐项原生结果不一致")
    if any(counts[state] for state in STATES[2:]):
        reasons.append("存在超时、工具错误、无覆盖、未检查或未知变异；分数未知")
    if raw.get("status") != "completed" or any(
        not isinstance(raw.get(key), str) or not raw[key].strip()
        for key in ("started_at", "finished_at")
    ):
        reasons.append("变异运行缺少完整结束记录")
    score = raw.get("mutation_score")
    expected = counts["killed"] / len(rows) if rows else None
    if type(score) not in (int, float) or not math.isfinite(score) or score != expected:
        reasons.append("有效变异分数缺失或与逐项结果不一致")
    tools = raw.get("tool") if isinstance(raw.get("tool"), dict) else {}
    tools = {key: clean_text(tools.get(key), 128) for key in TOOLS}
    if tools["config_sha256"]:
        config = reader.read(f"{prefix}/native/pyproject.toml", MAX_BYTES)
        if config is None or hashlib.sha256(config).hexdigest() != tools["config_sha256"]:
            tools["config_sha256"] = None
            reasons.append("原生变异配置缺失或指纹不匹配")
    status = raw.get("status")
    status = status if status in ("completed", "running", "failed", "incomplete") else "unknown"
    if status == "completed" and reasons:
        status = "incomplete"
    source, line = _source(reader, prefix, root, files, with_source)
    original_reason = clean_text(raw.get("reason"))
    return {
        "status": status,
        "reason": "；".join(dict.fromkeys(([original_reason] if original_reason else []) + reasons))
        or None,
        "tool": tools,
        "scope": SCOPE,
        "steps": steps,
        "counts": counts,
        "mutants": rows,
        "mutation_score": score if not reasons else None,
        "source_files": files,
        "source": source,
        "line": line,
        "started_at": clean_text(raw.get("started_at"), 100),
        "finished_at": clean_text(raw.get("finished_at"), 100),
    }


def compare(before, after):
    result = {
        "status": "incomparable",
        "reasons": [],
        "before_score": None,
        "after_score": None,
        "delta": None,
        "change": "unknown",
        "changed_test_files": [],
    }
    reasons = result["reasons"]
    if not before or not after:
        reasons.append("至少一侧缺少变异报告")
        return result
    result.update(before_score=before["mutation_score"], after_score=after["mutation_score"])
    if any(
        value["status"] != "completed" or value["mutation_score"] is None
        for value in (before, after)
    ):
        reasons.append("至少一侧未取得完整有效分数；未知不能算作改善")
    for key in TOOLS:
        left, right = before["tool"].get(key), after["tool"].get(key)
        if (
            not left
            or not right
            or (key.endswith("sha256") and (not HASH.fullmatch(left) or not HASH.fullmatch(right)))
        ):
            reasons.append(f"变异工具身份缺失或无效：{key}")
        elif left != right:
            reasons.append(f"变异工具或采集口径不同：{key}")
    if before["scope"] != after["scope"]:
        reasons.append("固定函数或原测试选择范围不同")
    left, right = before["source_files"], after["source_files"]
    if "backend/requirements.txt" not in left or "backend/requirements.txt" not in right:
        reasons.append("缺少 backend/requirements.txt 依赖指纹，不能确认相同运行依赖")
    selected = set(before["scope"]["tests"]) & set(after["scope"]["tests"])
    changes = {path for path in left.keys() | right.keys() if left.get(path) != right.get(path)}
    result["changed_test_files"] = sorted(changes & selected)
    if not left or not right:
        reasons.append("缺少源码指纹，无法确认同一变异总体")
    elif changes - selected:
        reasons.append("目标源码或测试之外的执行依赖、配置已变化，不能确认同一变异总体")
    if {row["name"] for row in before["mutants"]} != {row["name"] for row in after["mutants"]}:
        reasons.append("变异集合不同，不能按同一总体比较")
    if not reasons:
        delta = after["mutation_score"] - before["mutation_score"]
        result.update(
            status="comparable",
            delta=delta,
            change="improved" if delta > 0 else "worsened" if delta < 0 else "unchanged",
        )
        if result["changed_test_files"]:
            reasons.append("所选原测试内容已变化；这里只比较结果，不推断变化原因")
    return result
