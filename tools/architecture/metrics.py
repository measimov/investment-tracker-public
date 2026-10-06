"""Bind function CRAP to one CI run and the current safely read source snapshot.

CLI: python -m tools.architecture.metrics --root ROOT [--ci-run RUN] [--output FILE]
Without --output, write RUN/quality.json or stdout when no run is supplied.

quality.json v1: source.{root,files,config} contains current SHA-256 maps; scope
describes production files and the line-based CRAP method; tools records actual
analyzers, implementation fingerprints and locked collectors. groups.backend /
frontend contain current|stale|partial|unknown provenance, reasons, manifest and
coverage fingerprints, run identity and original collection scope. functions use
the native adapters' fields plus group. Only a current, complete collection can
produce measured coverage/CRAP; other groups retain current CC with null coverage
and CRAP. Individual unmappable functions remain unknown. This is local evidence,
not a signature or a claim that the tests establish application correctness.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

from ops.ci.checks import ARTIFACT_PATHS, SOURCE_GLOBS, TEST_INPUT_PREFIXES, _atomic_write
from tools.architecture import python_metrics, server

GROUPS = ("backend", "frontend")
ARTIFACTS = {"backend": "python_coverage_json", "frontend": "frontend_coverage_json"}
REQUIRED_STEPS = {"backend": {"backend-tests", "coverage-json"}, "frontend": {"frontend-tests"}}
CONFIG_EXCEPTIONS = {".coveragerc", "backend/.coveragerc"}
MAX_REPORT_BYTES = 64 * 1024 * 1024
SCOPE = {
    "backend": ["backend/app/**/*.py"],
    "frontend": ["frontend/src/**/*.{ts,tsx,js,jsx,mjs,cjs,vue}"],
    "exclude": ["**/*.spec.*", "**/*.test.*", "**/*.d.ts", "**/*.generated.*"],
    "method": "crap-executable-lines-v1",
    "formula": "CC^2 * (1 - coverage)^3 + CC",
    "complexity": {"backend": "radon", "frontend": "eslint-classic-max0"},
}


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_hash(value) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _group(path: str) -> str | None:
    name = PurePosixPath(path).name
    if re.search(r"\.(spec|test|generated)\.", name) or name.endswith(".d.ts"):
        return None
    if path.startswith("backend/app/") and path.endswith(".py"):
        return "backend"
    if path.startswith("frontend/src/") and server._language(path) in {
        "typescript",
        "javascript",
        "vue",
    }:
        return "frontend"
    return None


def _config(path: str) -> bool:
    return not path.startswith(("backend/app/", "backend/tests/", "frontend/src/", "frontend/e2e/"))


def _snapshot(root: Path, snapshot: Path, ci_run: Path | None) -> tuple[dict, dict, list]:
    candidates = set(server._candidate_paths(root, ci_run))
    production = {path for path in candidates if _group(path)}
    identity = {
        path.relative_to(root).as_posix()
        for pattern in SOURCE_GLOBS
        for path in root.glob(pattern)
        if path.is_file() or path.is_symlink()
    }
    hashes, contents, errors = {}, {}, []
    for path in sorted(production | identity):
        test_input = path.startswith(TEST_INPUT_PREFIXES)
        if path not in candidates and path not in CONFIG_EXCEPTIONS and not test_input:
            errors.append({"path": path, "message": "来源文件不在允许读取范围内"})
            continue
        try:
            content = (
                server._read_file(root, path) if test_input else server._read_source(root, path)
            )
        except (OSError, ValueError):
            errors.append({"path": path, "message": "来源文件无法安全读取"})
            continue
        hashes[path] = _sha(content)
        contents[path] = content
        if path in production:
            destination = snapshot / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return hashes, contents, errors


def _read_json(root: Path, path: str) -> tuple[dict, str]:
    raw = server._read_file(root, path, MAX_REPORT_BYTES)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象")
    return value, _sha(raw)


def _invalidate(group: dict, status: str, reason: str) -> None:
    priority = {"current": 0, "unknown": 1, "partial": 2, "stale": 3}
    if priority[status] >= priority[group["status"]]:
        group["status"] = status
    if reason not in group["reasons"]:
        group["reasons"].append(reason)


def _read_group(ci_run: Path | None, name: str, hashes: dict) -> tuple[dict, dict | None]:
    group = {"status": "current", "reasons": [], "manifest": None, "coverage": None}
    if ci_run is None:
        _invalidate(group, "unknown", "未提供 CI 运行，覆盖率未知")
        return group, None
    manifest_path = f"{name}/manifest.json"
    try:
        manifest, digest = _read_json(ci_run, manifest_path)
    except (OSError, ValueError, UnicodeError):
        _invalidate(group, "unknown", "CI 清单不存在或无法安全读取")
        return group, None
    group["manifest"] = {"path": manifest_path, "sha256": digest}
    if manifest.get("schema_version") != 1 or manifest.get("group") != name:
        _invalidate(group, "unknown", "CI 清单版本或分组不匹配")
        return group, None
    source, scope = manifest.get("source"), manifest.get("scope")
    if not isinstance(source, dict) or not isinstance(scope, dict):
        _invalidate(group, "unknown", "CI 清单缺少源码身份或采集范围")
        return group, None
    group["run"] = {
        "id": manifest.get("run_id"),
        "attempt": manifest.get("run_attempt"),
        **{key: source.get(key) for key in ("commit", "head", "base", "dirty")},
    }
    group["scope"] = scope
    recorded = source.get("files")
    if (
        not isinstance(recorded, dict)
        or not recorded
        or any(
            not isinstance(key, str)
            or not server._valid_path(key)
            or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in recorded.items()
        )
    ):
        _invalidate(group, "unknown", "CI 清单缺少有效源码指纹")
        return group, None
    group["source"] = {"root": source.get("root"), "files_sha256": _json_hash(recorded)}
    differences = sorted(
        path for path in hashes.keys() | recorded.keys() if hashes.get(path) != recorded.get(path)
    )
    if differences:
        group["changed_files"] = differences
        _invalidate(group, "stale", "源码、测试或检查配置与本次 CI 指纹不一致")
    if not isinstance(source.get("root"), str) or not PurePosixPath(source["root"]).is_absolute():
        _invalidate(group, "unknown", "CI 清单缺少原采集根目录，不能猜测覆盖率路径")
    if not group["run"]["id"] or not group["run"]["attempt"] or not group["run"]["commit"]:
        _invalidate(group, "unknown", "CI 运行身份不完整")
    if (
        scope.get("source_globs") != list(SOURCE_GLOBS)
        or scope.get("coverage_requested") is not True
    ):
        _invalidate(group, "unknown", "CI 采集范围不兼容或未请求覆盖率")
    steps = manifest.get("steps")
    valid_steps = (
        isinstance(steps, list)
        and bool(steps)
        and all(isinstance(step, dict) and isinstance(step.get("id"), str) for step in steps)
    )
    if (
        manifest.get("status") != "passed"
        or not manifest.get("finished_at")
        or not valid_steps
        or not REQUIRED_STEPS[name] <= {step.get("id") for step in steps}
        or any(step.get("status") != "passed" or step.get("exit_code") != 0 for step in steps)
    ):
        _invalidate(group, "partial", "CI 或覆盖率采集未完整通过，不能产生有效评分")
    artifact = ARTIFACTS[name]
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts.get(artifact) != ARTIFACT_PATHS[artifact]:
        _invalidate(group, "unknown", "清单未声明允许的原生覆盖率 JSON 附件")
        return group, None
    artifact_path = f"{name}/{ARTIFACT_PATHS[artifact]}"
    try:
        coverage, digest = _read_json(ci_run, artifact_path)
    except (OSError, ValueError, UnicodeError):
        _invalidate(group, "unknown", "覆盖率 JSON 缺失或无法安全读取")
        return group, None
    group["coverage"] = {"path": artifact_path, "sha256": digest}
    return group, coverage


def _normalize_coverage(coverage: dict, group: dict, name: str, allowed: set[str]) -> dict:
    """Only strip the declared collection root, never infer a path from a suffix."""
    root = PurePosixPath(group["source"]["root"])
    entries = coverage.get("files") if name == "backend" else coverage
    if not isinstance(entries, dict):
        raise ValueError("原生覆盖率缺少文件对象")
    normalized = {}

    def relative(value):
        if not isinstance(value, str):
            raise ValueError("覆盖率文件路径无效")
        candidate = PurePosixPath(value)
        if candidate.is_absolute():
            try:
                value = candidate.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError("覆盖率绝对路径不属于声明的采集根目录") from exc
        if not server._valid_path(value):
            raise ValueError("覆盖率文件路径无效")
        return value

    for key, data in entries.items():
        path = relative(key)
        if path not in allowed:
            continue
        if path in normalized or not isinstance(data, dict):
            raise ValueError("覆盖率文件身份重复或数据无效")
        if name == "frontend" and data.get("path") is not None and relative(data["path"]) != path:
            raise ValueError("覆盖率文件路径与报告键不一致")
        normalized[path] = {**data, "path": path} if name == "frontend" else data
    # Native adapters decide coverage per real function. Type-only files have
    # no function to score; an omitted executable file leaves only its own
    # functions unknown instead of discarding other files' reliable evidence.
    group["missing_coverage_files"] = sorted(allowed - normalized.keys())
    return {**coverage, "files": normalized} if name == "backend" else normalized


def _frontend(snapshot: Path, files: list[str], coverage: dict | None) -> dict:
    completed = subprocess.run(
        ["node", str(Path(__file__).with_name("frontend_metrics.mjs"))],
        input=json.dumps({"root": str(snapshot), "files": files, "coverage": coverage}),
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )
    return json.loads(completed.stdout)


def build_report(root: Path, ci_run: Path | None = None) -> dict:
    root = root.resolve(strict=True)
    ci_run = ci_run.resolve(strict=True) if ci_run is not None else None
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": SCOPE,
        "groups": {},
        "functions": [],
        "errors": [],
        "tools": {"analyzers": {}, "collectors": {}, "implementation": {}},
    }
    for filename in ("metrics.py", "python_metrics.py", "frontend_metrics.mjs"):
        result["tools"]["implementation"][filename] = _sha(
            Path(__file__).with_name(filename).read_bytes()
        )
    with tempfile.TemporaryDirectory(prefix="architecture-quality-") as temporary:
        snapshot = Path(temporary)
        hashes, contents, errors = _snapshot(root, snapshot, ci_run)
        result["source"] = {
            "root": str(root),
            "files": hashes,
            "config": {p: h for p, h in hashes.items() if _config(p)},
        }
        result["errors"].extend(errors)
        evidence = {}
        for name in GROUPS:
            result["groups"][name], evidence[name] = _read_group(ci_run, name, hashes)
        origins = [
            group for group in result["groups"].values() if "run" in group and "source" in group
        ]
        if len(origins) == 2 and (
            origins[0]["run"] != origins[1]["run"]
            or origins[0]["source"]["files_sha256"] != origins[1]["source"]["files_sha256"]
        ):
            for group in origins:
                _invalidate(group, "stale", "两个分组的运行或源码身份不同，不能拼接为同一次报告")
        for name in GROUPS:
            group = result["groups"][name]
            files = sorted(path for path in hashes if _group(path) == name)
            if errors:
                _invalidate(group, "partial", "源码快照读取不完整")
            coverage = None
            if group["status"] == "current":
                try:
                    coverage = _normalize_coverage(evidence[name], group, name, set(files))
                except (TypeError, ValueError) as exc:
                    _invalidate(group, "unknown", str(exc))
            try:
                analysis = (
                    python_metrics.analyze(snapshot, files, coverage)
                    if name == "backend"
                    else _frontend(snapshot, files, coverage)
                )
            except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
                analysis = {
                    "functions": [],
                    "tools": {},
                    "errors": [
                        {"path": None, "message": f"{name} 指标分析未完成（{type(exc).__name__}）"}
                    ],
                }
            result["tools"]["analyzers"][name] = analysis["tools"]
            result["errors"].extend(analysis["errors"])
            if analysis["errors"]:
                _invalidate(group, "partial", "部分源码无法完成指标分析")
            for row in analysis["functions"]:
                if group["status"] != "current":
                    row.update(
                        coverage=None,
                        crap=None,
                        status=group["status"],
                        reason="；".join(group["reasons"]),
                    )
                result["functions"].append({**row, "group": name})
        # Collector versions are report metadata or versions in the fingerprinted
        # lockfile, never inferred from the currently installed test environment.
        python_coverage = evidence["backend"]
        if isinstance(python_coverage, dict) and isinstance(python_coverage.get("meta"), dict):
            result["tools"]["collectors"]["coverage.py-json"] = python_coverage["meta"].get(
                "version"
            )
        try:
            lock = json.loads(contents.get("frontend/package-lock.json", b"{}"))
            for package in ("vitest", "@vitest/coverage-istanbul", "@vitejs/plugin-vue"):
                result["tools"]["collectors"][package] = (
                    lock.get("packages", {}).get(f"node_modules/{package}", {}).get("version")
                )
        except (ValueError, AttributeError):
            result["errors"].append(
                {"path": "frontend/package-lock.json", "message": "无法读取采集器锁定版本"}
            )
    statuses = {group["status"] for group in result["groups"].values()}
    result["status"] = (
        "stale"
        if "stale" in statuses
        else "partial"
        if "partial" in statuses or len(statuses) > 1
        else next(iter(statuses))
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="关联一次 CI 的函数复杂度与行覆盖率")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ci-run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = build_report(args.root, args.ci_run)
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        output = args.output or (args.ci_run / "quality.json" if args.ci_run else None)
        if output is None:
            sys.stdout.write(content)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(output, content)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"质量报告未生成（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
