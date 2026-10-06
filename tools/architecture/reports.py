"""Read-only HTTP report contract; no tests, logs, HTML or credentials are served.

GET /api/reports -> {reports:[{id,quality_status,ci_status:{backend,frontend},
mutation_status,finished_at}],errors:[{path,message}],truncated}. IDs are run or run/attempt.
GET /api/report?id=ID -> {id,quality:null|quality-v1,ci:{backend,frontend},
guards:[{rule,test,status,path,line,message}],source:{status,can_navigate,
changed_files},errors}. CI entries expose only identity, timestamps and step
outcomes. Guards are native JUnit cases from five existing guard modules; missing
cases remain unknown. Their optional file/line come from JUnit, never a guessed
violation location. Source navigation requires all recorded fingerprints to match.
CI conclusions reuse the publisher's completion checks; native JUnit failures
also prevent a contradictory passed conclusion, including in the report list.
GET /api/compare?before=ID&after=ID -> {before:{id,source},after:{id,source},
comparison:<compare.compare result>,guard_comparison:{status,reasons,added,removed,
worsened,improved,unknown,unchanged},errors}. Guard items retain rule/test and both
native outcomes. Test fingerprints and collection settings must be compatible;
missing, skipped and error results never count as improvements. Historical scores stay historical;
each side independently declares whether its source locations can be followed.
Detail also includes mutation:null|{status,reason,tool,scope,steps,counts,mutants,
mutation_score,source_files,source,line,started_at,finished_at}. This is the bounded
pilot's summary, never a test invocation or a mutant diff. Its source identity is
independent of CI/CRAP. Comparison includes mutation_comparison:{status,reasons,
before_score,after_score,delta,change,changed_test_files}; missing tool identity,
unknown results or a changed mutation population cannot become improvements.

An unconfigured report root lists no reports. Invalid IDs return 422, absent or
symlinked runs return 404. Missing/invalid optional artifacts are reported without
discarding available CI results. Reads are fixed-name, size-limited and no-follow.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import heapq
from itertools import chain
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import xml.etree.ElementTree as ET

from fastapi import HTTPException

from ops.ci.checks import ARTIFACT_PATHS, SOURCE_GLOBS, TEST_INPUT_PREFIXES
from ops.ci.publish import _assess_group
from tools.architecture import compare, mutation_reports, server
from tools.architecture.metrics import _group

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_JUNIT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RUNS = 200
PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
GUARDS = (
    "portfolio_purity",
    "private_imports",
    "no_implicit_clock",
    "deploy_config_sync",
    "env_contract",
)
SECRET = re.compile(r"(?i)(\b(?:password|secret|token|authorization|cookie)\b\s*[:=]\s*)([^\s,;]+)")


def _text(value, limit=2000):
    if not isinstance(value, str):
        return None
    value = "".join(character for character in value if ord(character) >= 32 or character in "\n\t")
    return SECRET.sub(r"\1[已隐藏]", value)[:limit]


def _pick(value, fields):
    return {key: value[key] for key in fields if key in value} if isinstance(value, dict) else {}


def _quality(value):
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("functions"), list)
    ):
        return None
    if (
        any(
            not isinstance(value.get(field), dict)
            for field in ("source", "scope", "tools", "groups")
        )
        or any(not isinstance(value["source"].get(field), dict) for field in ("files", "config"))
        or any(
            not isinstance(value["groups"].get(group), dict) for group in ("backend", "frontend")
        )
    ):
        return None
    if any(
        value["groups"][group].get("status") not in ("current", "stale", "partial", "unknown")
        for group in ("backend", "frontend")
    ):
        return None
    result = _pick(value, ("schema_version", "generated_at", "status"))
    result["scope"] = _pick(
        value.get("scope"), ("backend", "frontend", "exclude", "method", "formula", "complexity")
    )
    tools = value.get("tools") if isinstance(value.get("tools"), dict) else {}
    analyzers = tools.get("analyzers") if isinstance(tools.get("analyzers"), dict) else {}
    result["tools"] = {
        "analyzers": {
            "backend": _pick(analyzers.get("backend"), ("radon", "coverage.py-json")),
            "frontend": _pick(
                analyzers.get("frontend"),
                ("eslint", "@typescript-eslint/parser", "vue-eslint-parser"),
            ),
        },
        "collectors": _pick(
            tools.get("collectors"),
            ("coverage.py-json", "vitest", "@vitest/coverage-istanbul", "@vitejs/plugin-vue"),
        ),
        "implementation": _pick(
            tools.get("implementation"), ("metrics.py", "python_metrics.py", "frontend_metrics.mjs")
        ),
    }
    result["source"] = _pick(value.get("source"), ("root", "files", "config"))
    result["groups"] = {}
    for name in ("backend", "frontend"):
        original = (
            value.get("groups", {}).get(name, {}) if isinstance(value.get("groups"), dict) else {}
        )
        if not isinstance(original, dict):
            original = {}
        group = _pick(original, ("status", "reasons", "scope", "changed_files"))
        for field, keys in {
            "run": ("id", "attempt", "commit", "head", "base", "dirty"),
            "source": ("root", "files_sha256"),
            "manifest": ("path", "sha256"),
            "coverage": ("path", "sha256"),
        }.items():
            group[field] = _pick(original.get(field), keys)
        result["groups"][name] = group
    result["functions"] = [
        _pick(
            row,
            (
                "group",
                "path",
                "name",
                "line",
                "end_line",
                "column",
                "end_column",
                "cc",
                "coverage",
                "crap",
                "status",
                "reason",
            ),
        )
        for row in value["functions"]
        if isinstance(row, dict)
    ]
    result["errors"] = [
        {"path": error.get("path"), "message": _text(error.get("message"))}
        for error in (value.get("errors") if isinstance(value.get("errors"), list) else [])
        if isinstance(error, dict)
    ]
    return result


def _id(value: str) -> list[str]:
    parts = value.split("/")
    if len(parts) not in (1, 2) or any(not PART.fullmatch(part) for part in parts):
        raise HTTPException(422, "报告标识必须是 run 或 run/attempt，不能包含越界路径")
    return parts


def _directory(root: Path, parts=()) -> int:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_info(root: Path, path: str):
    if not server._valid_path(path):
        raise ValueError("报告路径无效")
    parts = path.split("/")
    descriptor = _directory(root, parts[:-1])
    try:
        return os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
    finally:
        os.close(descriptor)


class _Reader:
    def __init__(self, root):
        self.root, self.remaining, self.errors = root, MAX_TOTAL_BYTES, []
        self.budget_exhausted = False

    def read(self, path, maximum):
        try:
            raw = server._read_file(self.root, path, min(maximum, self.remaining))
            self.remaining -= len(raw)
            return raw
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            message = "报告无法安全读取或超过大小限制"
            if self.remaining < maximum:
                try:
                    info = _file_info(self.root, path)
                    if stat.S_ISREG(info.st_mode) and self.remaining < info.st_size <= maximum:
                        self.budget_exhausted = True
                        message = "报告读取预算不足，结果已截断"
                except (OSError, ValueError):
                    pass
            self.errors.append({"path": path, "message": message})
            return None

    def json(self, path, maximum=None):
        maximum = MAX_JSON_BYTES if maximum is None else maximum
        raw = self.read(path, maximum)
        if raw is None:
            return None
        try:

            def invalid_constant(_value):
                raise ValueError

            data = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            self.errors.append({"path": path, "message": "报告 JSON 格式无效"})
            return None


def _ci(manifest, name, errors, path):
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("group") != name
    ):
        return None
    assessed = _assess_group(name, manifest, None)
    errors.extend({"path": path, "message": problem} for problem in assessed["problems"])
    result = _pick(manifest, ("run_id", "run_attempt", "started_at", "finished_at"))
    result["status"] = assessed["status"]
    result["source"] = _pick(manifest.get("source"), ("commit", "head", "base", "dirty"))
    result["steps"] = [
        {
            "id": step["id"],
            "status": step["status"],
            "exit_code": step["code"],
            "duration_seconds": step["duration"],
        }
        for step in assessed["steps"]
    ]
    return result


def _documents(reader, identifier):
    quality_path = f"{identifier}/quality.json"
    raw = reader.json(quality_path)
    quality = _quality(raw)
    if raw is not None and quality is None:
        reader.errors.append({"path": quality_path, "message": "质量报告版本或必要字段无效"})
    manifests = {
        name: reader.json(f"{identifier}/{name}/manifest.json", MAX_MANIFEST_BYTES)
        for name in ("backend", "frontend")
    }
    return quality, manifests


def _source_status(root, report_root, quality, manifests):
    unknown = {"status": "unknown", "can_navigate": False, "changed_files": []}
    sources = ([quality.get("source")] if quality else []) + [
        value.get("source") for value in manifests.values() if isinstance(value, dict)
    ]
    fingerprints = []
    for source in sources:
        files = source.get("files") if isinstance(source, dict) else None
        if (
            not isinstance(files, dict)
            or not files
            or len(files) > 10000
            or any(
                not isinstance(path, str)
                or not server._valid_path(path)
                or (
                    server._language(path) is None
                    and path not in {".coveragerc", "backend/.coveragerc"}
                    and not path.startswith(TEST_INPUT_PREFIXES)
                )
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for path, digest in files.items()
            )
        ):
            return unknown
        fingerprints.append(files)
    if not fingerprints:
        return unknown
    expected = fingerprints[0]
    if any(value != expected for value in fingerprints[1:]):
        return {**unknown, "status": "stale"}
    try:
        allowed = set(server._candidate_paths(root, report_root))
        paths = {path for path in allowed if _group(path)} | {
            path.relative_to(root).as_posix()
            for pattern in SOURCE_GLOBS
            for path in root.glob(pattern)
            if path.is_file() or path.is_symlink()
        }
        actual = {}
        for path in paths | expected.keys():
            test_input = path.startswith(TEST_INPUT_PREFIXES)
            if (
                path not in allowed
                and path not in {".coveragerc", "backend/.coveragerc"}
                and not test_input
            ):
                continue
            try:
                content = (
                    server._read_file(root, path) if test_input else server._read_source(root, path)
                )
                actual[path] = hashlib.sha256(content).hexdigest()
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        return unknown
    changed = sorted(
        path for path in actual.keys() | expected.keys() if actual.get(path) != expected.get(path)
    )
    return {
        "status": "stale" if changed else "current",
        "can_navigate": not changed,
        "changed_files": changed,
    }


def _guards(reader, identifier, manifests, failed_steps):
    rows = []
    backend = manifests.get("backend")
    artifacts = backend.get("artifacts", {}) if isinstance(backend, dict) else {}
    for name in ("backend_junit", "architecture_junit"):
        if not isinstance(artifacts, dict) or artifacts.get(name) != ARTIFACT_PATHS[name]:
            continue
        path = f"{identifier}/backend/{ARTIFACT_PATHS[name]}"
        raw = reader.read(path, MAX_JUNIT_BYTES)
        if raw is None:
            reader.errors.append({"path": path, "message": "清单声明的 JUnit 报告缺失或不可读取"})
            continue
        try:
            text = raw.decode("utf-8")
            if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
                raise ValueError
            document = ET.fromstring(text)
        except (ValueError, UnicodeError, ET.ParseError):
            reader.errors.append({"path": path, "message": "JUnit XML 无效或包含不允许的实体声明"})
            continue
        truncated = False
        for case in document.iter("testcase"):
            outcome = next(
                (
                    case.find(tag)
                    for tag in ("error", "failure", "skipped")
                    if case.find(tag) is not None
                ),
                None,
            )
            if outcome is not None and outcome.tag in {"failure", "error"}:
                failed_steps.add(
                    "backend-tests" if name == "backend_junit" else "architecture-tests"
                )
            classname = case.get("classname", "")
            rule = next((rule for rule in GUARDS if f"test_{rule}" in classname.split(".")), None)
            if rule is None:
                continue
            if len(rows) >= 500:
                if not truncated:
                    reader.errors.append(
                        {"path": path, "message": "守卫用例超过展示上限，结果未完整展示"}
                    )
                    truncated = True
                continue
            file = case.get("file")
            if file is not None:
                source = backend.get("source") if isinstance(backend.get("source"), dict) else {}
                original_root = source.get("root")
                if PurePosixPath(file).is_absolute() and isinstance(original_root, str):
                    try:
                        file = PurePosixPath(file).relative_to(original_root).as_posix()
                    except ValueError:
                        file = None
                if file is not None and (
                    not server._valid_path(file) or server._language(file) is None
                ):
                    file = None
                fingerprints = source.get("files")
                if file is not None and (
                    not isinstance(fingerprints, dict) or file not in fingerprints
                ):
                    file = None
            line = case.get("line", "")
            rows.append(
                {
                    "rule": rule,
                    "test": _text(case.get("name"), 512),
                    "status": "passed"
                    if outcome is None
                    else "failed"
                    if outcome.tag == "failure"
                    else outcome.tag,
                    "path": file,
                    "line": int(line) + 1 if file and len(line) <= 9 and line.isdigit() else None,
                    "message": _text(outcome.get("message")) if outcome is not None else None,
                }
            )
    for rule in GUARDS:
        if not any(row["rule"] == rule for row in rows):
            rows.append(
                {
                    "rule": rule,
                    "test": None,
                    "status": "unknown",
                    "path": None,
                    "line": None,
                    "message": "未找到该守卫的原生 JUnit 结果",
                }
            )
    return rows


def _ci_and_guards(reader, identifier, manifests):
    ci = {
        name: _ci(value, name, reader.errors, f"{identifier}/{name}/manifest.json")
        for name, value in manifests.items()
    }
    failed_steps = set()
    guards = _guards(reader, identifier, manifests, failed_steps)
    backend = ci.get("backend")
    if backend is not None and failed_steps:
        contradicted = backend["status"] == "passed"
        for step in backend["steps"]:
            if step["id"] in failed_steps and step["status"] == "passed":
                step["status"] = "failed"
                contradicted = True
        if backend["status"] == "passed":
            backend["status"] = "failed"
        if contradicted:
            reader.errors.append(
                {
                    "path": f"{identifier}/backend/manifest.json",
                    "message": "原生 JUnit 存在失败或错误，与清单的通过结论矛盾；已保留失败结果",
                }
            )
    return ci, guards


def _compare_guards(before, after, before_manifest, after_manifest):
    """Compare existing JUnit outcomes, never infer or reimplement rule findings."""
    buckets = ("added", "removed", "worsened", "improved", "unknown", "unchanged")
    result = {"status": "comparable", "reasons": [], **{key: [] for key in buckets}}
    signatures, fingerprints = [], []
    for label, report, manifest in (
        ("基线", before, before_manifest),
        ("对照", after, after_manifest),
    ):
        manifest = manifest if isinstance(manifest, dict) else {}
        backend = report["ci"].get("backend")
        if backend is None or backend["status"] not in {"passed", "failed"}:
            result["reasons"].append(f"{label}后端检查未完整结束，守卫结果不可直接比较")
        source = manifest.get("source")
        files = source.get("files") if isinstance(source, dict) else None
        if (
            not isinstance(files, dict)
            or not files
            or any(
                not isinstance(path, str)
                or not server._valid_path(path)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for path, digest in files.items()
            )
        ):
            result["reasons"].append(f"{label}缺少有效的测试源码指纹")
            files = {}
        fingerprints.append(files)
        scope = manifest.get("scope")
        if not isinstance(scope, dict) or not scope:
            result["reasons"].append(f"{label}缺少检查采集口径")
        tools = manifest.get("tools")
        versions = {
            name: tools.get(name) if isinstance(tools, dict) else None
            for name in ("python", "pytest")
        }
        if any(not isinstance(value, str) or not value for value in versions.values()):
            result["reasons"].append(f"{label}缺少 Python 或 pytest 版本")
        if any(str(error.get("path", "")).endswith(".xml") for error in report["errors"]):
            result["reasons"].append(f"{label}原生守卫结果缺失、读取失败或展示不完整")
        signatures.append(
            {
                "测试源码或检查配置": {
                    path: digest
                    for path, digest in files.items()
                    if not path.startswith(("backend/app/", "frontend/src/"))
                },
                "检查采集口径": scope,
                "测试工具版本": versions,
            }
        )
    for field, value in signatures[0].items():
        if value != signatures[1][field]:
            result["reasons"].append(f"两份报告的{field}不兼容")
    if result["reasons"]:
        result["status"] = "incomparable"
    indexed = []
    for report in (before, after):
        rows = defaultdict(list)
        for row in report["guards"]:
            rows[(row["rule"], row["test"])].append(row)
        indexed.append(rows)
    left, right = indexed
    for rule, test in sorted(left.keys() | right.keys(), key=lambda key: (key[0], key[1] or "")):
        old, new = left[(rule, test)], right[(rule, test)]
        item = {
            "rule": rule,
            "test": test,
            "before": old[0] if len(old) == 1 else None,
            "after": new[0] if len(new) == 1 else None,
        }
        reasons = result["reasons"].copy()
        if len(old) > 1 or len(new) > 1:
            reasons.append("规则与用例名称重复，无法确定对应关系")
            item["candidates"] = {"before": old, "after": new}
        if not test:
            reasons.append("缺少该守卫的原生用例结果")
        elif not all(f"backend/tests/test_{rule}.py" in files for files in fingerprints):
            reasons.append("缺少该守卫测试源码的指纹，不能确认规则未变")
        if any(row["status"] not in {"passed", "failed"} for row in old + new):
            reasons.append("跳过、执行错误或未知结果不能认定为改善")
        if reasons:
            bucket, reason = "unknown", "；".join(dict.fromkeys(reasons))
        elif not old:
            bucket, reason = "added", "基线未记录此用例；新增结果不算改善"
        elif not new:
            bucket, reason = "removed", "对照报告未记录此用例；消失不表示规则风险改善"
        elif old[0]["status"] == new[0]["status"]:
            bucket, reason = "unchanged", "原生用例结果未变化"
        elif new[0]["status"] == "failed":
            bucket, reason = "worsened", "同一守卫用例由通过变为失败"
        else:
            bucket, reason = "improved", "同一守卫用例由失败变为通过"
        result[bucket].append({**item, "reason": reason})
    return result


class ReportStore:
    def __init__(self, source_root: Path, report_root: Path | None):
        self.source_root, self.report_root = source_root, report_root

    def _require_run(self, identifier):
        parts = _id(identifier)
        try:
            if self.report_root is None:
                raise FileNotFoundError
            os.close(_directory(self.report_root, parts))
        except OSError as exc:
            raise HTTPException(404, "报告不存在或不可安全读取") from exc

    def list(self):
        result = {"reports": [], "errors": [], "truncated": False}
        if self.report_root is None:
            result["errors"].append({"path": None, "message": "未配置报告目录"})
            return result
        try:
            os.close(_directory(self.report_root))
        except OSError:
            result["errors"].append({"path": None, "message": "报告根目录不存在或不可安全读取"})
            return result
        reader = _Reader(self.report_root)

        def directories(parts=()):
            try:
                descriptor = _directory(self.report_root, parts)
                try:
                    with os.scandir(descriptor) as entries:
                        for entry in entries:
                            if PART.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                                yield entry.name
                finally:
                    os.close(descriptor)
            except OSError:
                return

        def candidates():
            for run in directories():
                identifiers = chain(
                    (run,),
                    (
                        f"{run}/{attempt}"
                        for attempt in directories((run,))
                        if attempt not in ("backend", "frontend", "mutation")
                    ),
                )
                for identifier in identifiers:
                    modified = []
                    for artifact in (
                        "quality.json",
                        "backend/manifest.json",
                        "frontend/manifest.json",
                        "mutation/manifest.json",
                    ):
                        try:
                            info = _file_info(self.report_root, f"{identifier}/{artifact}")
                            if stat.S_ISREG(info.st_mode):
                                modified.append(info.st_mtime_ns)
                        except (OSError, ValueError):
                            continue
                    if modified:
                        yield max(modified), identifier

        # Inspect only metadata before selecting a bounded set. Old JSON must not
        # spend the shared byte budget before the most recently written reports.
        selected = heapq.nlargest(MAX_RUNS + 1, candidates())
        result["truncated"] = len(selected) > MAX_RUNS
        for _, identifier in selected[:MAX_RUNS]:
            if reader.budget_exhausted or reader.remaining <= 0:
                result["truncated"] = True
                break
            quality, manifests = _documents(reader, identifier)
            mutation = mutation_reports.load(
                reader, identifier, self.source_root, _text, with_source=False
            )
            if quality is not None or any(manifests.values()) or mutation is not None:
                ci, _ = _ci_and_guards(reader, identifier, manifests)
                dates = [
                    value.get("finished_at")
                    for value in ci.values()
                    if value and isinstance(value.get("finished_at"), str)
                ]
                if mutation and mutation["finished_at"]:
                    dates.append(mutation["finished_at"])
                result["reports"].append(
                    {
                        "id": identifier,
                        "quality_status": quality.get("status") if quality else None,
                        "mutation_status": mutation["status"] if mutation else None,
                        "ci_status": {
                            name: value.get("status") if value else None
                            for name, value in ci.items()
                        },
                        "finished_at": max(dates) if dates else None,
                    }
                )
            result["truncated"] |= reader.budget_exhausted
        result["reports"].sort(
            key=lambda item: (item["finished_at"] or "", item["id"]), reverse=True
        )
        result["errors"] = reader.errors
        return result

    def _report(self, identifier):
        self._require_run(identifier)
        reader = _Reader(self.report_root)
        quality, manifests = _documents(reader, identifier)
        mutation = mutation_reports.load(reader, identifier, self.source_root, _text)
        if (
            quality is None
            and not any(manifests.values())
            and mutation is None
            and not reader.errors
        ):
            raise HTTPException(404, "该运行没有可读取的报告")
        if quality is None and (mutation is None or any(manifests.values())):
            reader.errors.append(
                {
                    "path": f"{identifier}/quality.json",
                    "message": "质量报告缺失或不可读取，未计算覆盖率评分",
                }
            )
        ci, guards = _ci_and_guards(reader, identifier, manifests)
        return {
            "id": identifier,
            "quality": quality,
            "mutation": mutation,
            "ci": ci,
            "guards": guards,
            "source": _source_status(self.source_root, self.report_root, quality, manifests),
            "errors": reader.errors,
        }, manifests

    def report(self, identifier):
        return self._report(identifier)[0]

    def compare(self, before, after):
        left, left_manifests = self._report(before)
        right, right_manifests = self._report(after)
        return {
            "before": {"id": before, "source": left["source"]},
            "after": {"id": after, "source": right["source"]},
            "comparison": compare.compare(left["quality"] or {}, right["quality"] or {}),
            "guard_comparison": _compare_guards(
                left, right, left_manifests.get("backend"), right_manifests.get("backend")
            ),
            "mutation_comparison": mutation_reports.compare(left["mutation"], right["mutation"]),
            "errors": left["errors"] + right["errors"],
        }
