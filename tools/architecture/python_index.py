"""Static Python evidence; Grimp owns package resolution, AST owns source locations."""

from __future__ import annotations

import ast
import io
import platform
import sys
import tempfile
import threading
import tokenize
from importlib.metadata import version
from importlib.util import resolve_name
from pathlib import Path

import grimp


_GRAPH_LOCK = threading.Lock()
_APP_ROOT = Path("backend/app")
_MAX_CYCLE_HINTS = 200
_MAX_CYCLE_QUERIES = 10000


class _Evidence(ast.NodeVisitor):
    def __init__(self, path: str, source: str, tree: ast.AST):
        self.path = path
        self.source = source
        self.symbols: list[dict] = []
        self.imports: list[dict] = []
        self.names: list[str] = []
        self.function_depth = 0
        self.type_checking = False
        self.type_names = {"TYPE_CHECKING"}
        self.importlib_names = {"importlib"}
        self.import_module_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "importlib":
                        self.importlib_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if node.module == "typing" and alias.name == "TYPE_CHECKING":
                        self.type_names.add(alias.asname or alias.name)
                    if node.module == "importlib" and alias.name == "import_module":
                        self.import_module_names.add(alias.asname or alias.name)

    def _definition(self, node: ast.AST, kind: str) -> None:
        self.names.append(node.name)
        name = ".".join(self.names)
        self.symbols.append(
            {
                "id": f"{self.path}:{name}:{node.lineno}",
                "path": self.path,
                "name": name,
                "kind": kind,
                "line": node.lineno,
                "end_line": node.end_lineno,
            }
        )
        self.function_depth += kind == "function"
        self.generic_visit(node)
        self.function_depth -= kind == "function"
        self.names.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node, "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        guard = node.test
        is_type_guard = (isinstance(guard, ast.Name) and guard.id in self.type_names) or (
            isinstance(guard, ast.Attribute) and guard.attr == "TYPE_CHECKING"
        )
        self.visit(node.test)
        previous = self.type_checking
        self.type_checking = previous or is_type_guard
        for child in node.body:
            self.visit(child)
        self.type_checking = previous
        for child in node.orelse:
            self.visit(child)

    def _import(self, node: ast.AST, specifier: str, *, call: bool = False) -> None:
        kind = "type" if self.type_checking else "runtime"
        if kind != "type" and (self.function_depth or call):
            kind = "dynamic"
        self.imports.append(
            {
                "specifier": specifier,
                "line": node.lineno,
                "end_line": node.end_lineno,
                "kind": kind,
                "statement": ast.get_source_segment(self.source, node) or ast.unparse(node),
                "call": call,
                "from": isinstance(node, ast.ImportFrom),
            }
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._import(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._import(node, "." * node.level + (node.module or ""))

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        dynamic = isinstance(function, ast.Name) and (
            function.id == "__import__" or function.id in self.import_module_names
        )
        dynamic = dynamic or (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id in self.importlib_names
            and function.attr == "import_module"
        )
        if dynamic:
            argument = node.args[0] if node.args else None
            specifier = (
                argument.value
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                else ast.unparse(node)
            )
            self._import(node, specifier, call=True)
        self.generic_visit(node)


def _app_module(path: str) -> str | None:
    relative = Path(path)
    if not relative.is_relative_to(_APP_ROOT):
        return None
    parts = list(relative.relative_to(_APP_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _build_graph(sources: dict[str, str]):
    """Grimp sees only the allowlisted snapshot, never the live business package."""
    with tempfile.TemporaryDirectory(prefix="architecture-python-") as directory:
        staging = Path(directory)
        for path, source in sources.items():
            if _app_module(path) is None:
                continue
            destination = staging / Path(path).relative_to(_APP_ROOT.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8")
        if not (staging / "app/__init__.py").is_file():
            raise ValueError("允许文件中缺少 backend/app/__init__.py，无法分析 app 包")
        # Grimp uses top-level package discovery without importing its code. Its
        # public API needs a search path; serialize and restore this brief change.
        with _GRAPH_LOCK:
            if "app" in sys.modules:
                raise RuntimeError("当前进程已加载 app，无法保证 Grimp 只读取允许文件快照")
            previous_path = sys.path[:]
            try:
                sys.path.insert(0, str(staging))
                return grimp.build_graph("app", include_external_packages=True, cache_dir=None)
            finally:
                sys.path[:] = previous_path


def _matching_evidence(imports: list[dict], detail: dict, imported: str) -> dict | None:
    line = detail.get("line_number")
    if not isinstance(line, int):
        return None
    candidates = [
        item for item in imports if not item["call"] and item["line"] <= line <= item["end_line"]
    ]
    if len(candidates) > 1:
        statement = detail.get("line_contents", "").strip()
        exact = [item for item in candidates if item["statement"].strip() == statement]
        if exact:
            candidates = exact
    if len(candidates) > 1:
        candidates = [
            item
            for item in candidates
            if not item["from"]
            and (item["specifier"] == imported or item["specifier"].startswith(imported + "."))
        ]
    return candidates[0] if len(candidates) == 1 else None


def _dependency(path: str, target: str | None, evidence: dict, external: bool) -> dict:
    return {
        "source": path,
        "target": target,
        "specifier": evidence["specifier"],
        "line": evidence["line"],
        "kind": evidence["kind"],
        "external": external,
        "statement": evidence["statement"],
    }


def _matches_internal_target(path: str, module: str, imported: str, item: dict) -> bool:
    """Reject Grimp's nearest-parent fallback for a missing explicit module.

    This only validates its edge; resolve_name expands a relative spelling and
    does not discover or import modules or create replacement edges.
    """
    expected = item["specifier"]
    if expected.startswith("."):
        package = module if Path(path).name == "__init__.py" else module.rpartition(".")[0]
        try:
            expected = resolve_name(expected, package)
        except (ImportError, ValueError):
            return False
    return imported == expected or (item["from"] and imported.startswith(expected + "."))


def _cycle_hints(dependencies: list[dict], module_paths: dict[str, str]) -> tuple[list, bool]:
    """Return bounded native witnesses, not an enumeration of every simple cycle.

    cycles: [{tool, kind: runtime|type-only|mixed, paths: closed relative path
    sequence, edges: [{source,target,kind,line}]}]. Runtime includes dynamic
    imports, whose edge kind stays dynamic. Only verified Grimp edges are used.
    """
    path_modules = {path: module for module, path in module_paths.items()}
    verified = [
        edge
        for edge in dependencies
        if edge["source"] in path_modules and edge["target"] in path_modules
    ]
    cycles, seen, queries = [], set(), 0
    for scope in ("runtime", "type-only", "mixed"):
        pairs: dict[tuple[str, str], list[dict]] = {}
        graph = grimp.ImportGraph()
        for edge in verified:
            if scope == "runtime" and edge["kind"] == "type":
                continue
            if scope == "type-only" and edge["kind"] != "type":
                continue
            pair = edge["source"], edge["target"]
            pairs.setdefault(pair, []).append(edge)
            graph.add_import(importer=path_modules[pair[0]], imported=path_modules[pair[1]])
        for source, target in sorted(pairs):
            if queries >= _MAX_CYCLE_QUERIES:
                return cycles, True
            queries += 1
            # Grimp's public path API performs the graph traversal. Its package
            # cycle-breaker API intentionally omits parent/child imports, which
            # is why a verified edge plus its native return chain is used here.
            if source == target:
                paths = [source, target]  # A native direct self-import is already closed.
            else:
                chain = graph.find_shortest_chain(
                    importer=path_modules[target], imported=path_modules[source], as_packages=False
                )
                if chain is None:
                    continue
                paths = [source, *(module_paths[module] for module in chain)]
            body = paths[:-1]
            start = body.index(min(body))
            body = body[start:] + body[:start]
            paths = [*body, body[0]]
            edges = [
                min(pairs[pair], key=lambda edge: (edge["kind"] == "type", edge["line"] or 0))
                for pair in zip(paths, paths[1:])
            ]
            kinds = {"type" if edge["kind"] == "type" else "runtime" for edge in edges}
            kind = "mixed" if len(kinds) > 1 else "type-only" if "type" in kinds else "runtime"
            key = kind, tuple(paths)
            if kind != scope or key in seen:
                continue
            if len(cycles) >= _MAX_CYCLE_HINTS:
                return cycles, True
            seen.add(key)
            cycles.append(
                {
                    "tool": "grimp",
                    "kind": kind,
                    "paths": paths,
                    "edges": [
                        {field: edge[field] for field in ("source", "target", "kind", "line")}
                        for edge in edges
                    ],
                }
            )
    return sorted(cycles, key=lambda row: (row["kind"], row["paths"])), False


def analyze(root: Path, files: list[str]) -> dict:
    """Analyze allowed paths; unknown imports stay unknown instead of being guessed."""
    result = {
        "symbols": [],
        "dependencies": [],
        "cycles": [],
        "cycles_truncated": False,
        "errors": [],
        "tools": {"grimp": version("grimp"), "ast": platform.python_version()},
    }
    sources: dict[str, str] = {}
    evidence_by_path: dict[str, _Evidence] = {}
    for path in sorted(set(files)):
        if Path(path).suffix != ".py":
            continue
        try:
            data = (root / path).read_bytes()
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            source = data.decode(encoding)
            tree = ast.parse(source, filename=path)
        except (OSError, SyntaxError, UnicodeError) as error:
            result["errors"].append({"path": path, "message": f"Python 解析失败：{error}"})
            # Preserve the module identity but omit invalid contents so one bad
            # file cannot suppress evidence from every other package module.
            sources[path] = ""
            continue
        sources[path] = source
        evidence = _Evidence(path, source, tree)
        evidence.visit(tree)
        evidence_by_path[path] = evidence
        result["symbols"].extend(evidence.symbols)

    module_paths = {module: path for path in sources if (module := _app_module(path))}
    graph = None
    if module_paths:
        try:
            graph = _build_graph(sources)
        except Exception as error:
            result["errors"].append(
                {"path": "backend/app", "message": f"Grimp 包分析失败：{error}"}
            )

    for path, evidence in evidence_by_path.items():
        used: set[int] = set()
        module = _app_module(path)
        if graph is not None and module in graph.modules:
            for imported in sorted(graph.find_modules_directly_imported_by(module)):
                external = not (imported == "app" or imported.startswith("app."))
                details = graph.get_import_details(importer=module, imported=imported)
                for detail in details or [{}]:
                    target = module_paths.get(imported)
                    item = _matching_evidence(evidence.imports, detail, imported)
                    if item is None:
                        # Without source evidence Grimp's nearest-parent fallback
                        # cannot be distinguished from a verified package import.
                        target = None
                        item = {
                            "specifier": imported,
                            "line": None,
                            "kind": "runtime",
                            "statement": detail.get("line_contents", ""),
                        }
                        result["errors"].append(
                            {"path": path, "message": f"无法定位 Grimp 依赖 {imported} 的导入证据"}
                        )
                    else:
                        used.add(id(item))
                        if not external and not _matches_internal_target(
                            path, module, imported, item
                        ):
                            target = None
                    result["dependencies"].append(_dependency(path, target, item, external))
                    if target is None and not external:
                        result["errors"].append(
                            {"path": path, "message": f"未解析目标：{item['specifier']}"}
                        )
        for item in evidence.imports:
            if id(item) in used:
                continue
            specifier = item["specifier"]
            external = not item["call"] and not (
                specifier.startswith(".") or specifier == "app" or specifier.startswith("app.")
            )
            result["dependencies"].append(_dependency(path, None, item, external))
            reason = "动态导入无法由 Grimp 静态确定" if item["call"] else "Grimp 未解析该导入"
            if module is None and not item["call"]:
                reason = "独立文件不在 Grimp 包分析范围，目标未解析"
            result["errors"].append(
                {"path": path, "message": f"第 {item['line']} 行 {specifier}：{reason}"}
            )
    result["symbols"].sort(key=lambda item: (item["path"], item["line"], item["name"]))
    result["dependencies"].sort(
        key=lambda item: (
            item["source"],
            item["line"] or 0,
            item["specifier"],
            item["target"] or "",
        )
    )
    try:
        result["cycles"], result["cycles_truncated"] = _cycle_hints(
            result["dependencies"], module_paths
        )
    except Exception as error:
        result["cycles_truncated"] = True
        result["errors"].append(
            {"path": "backend/app", "message": f"Grimp 循环提示未完成：{error}"}
        )
    return result
