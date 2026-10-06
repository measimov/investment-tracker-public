"""Isolated static-index tests: no backend fixtures, database, or business imports."""

import sys
from pathlib import Path
from types import SimpleNamespace

from tools.architecture import python_index


def _write(root: Path, sources: dict[str, str]) -> list[str]:
    for path, source in sources.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
    return list(sources)


def test_grimp_package_edges_have_original_evidence_and_kinds(tmp_path):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "raise RuntimeError('must never execute')\n",
            "backend/app/models/__init__.py": "from .value import Value\n",
            "backend/app/models/value.py": "class Value:\n    pass\n",
            "backend/app/consumer.py": (
                "from typing import TYPE_CHECKING as TC\n"
                "from .models import Value\n"
                "from .models.value import (\n"
                "    Value,\n"
                ")\n"
                "import os, app.models.value\n"
                "if TC:\n"
                "    from .models.value import Value\n"
                "else:\n"
                "    from .models import Value\n"
                "def load():\n"
                "    from .models.value import Value\n"
            ),
        },
    )
    before = sys.path[:]
    result = python_index.analyze(tmp_path, files)
    assert sys.path == before
    assert "app" not in sys.modules
    assert result["errors"] == []
    edges = [edge for edge in result["dependencies"] if edge["source"].endswith("consumer.py")]
    assert {(edge["line"], edge["target"], edge["kind"]) for edge in edges} >= {
        (2, "backend/app/models/__init__.py", "runtime"),
        (3, "backend/app/models/value.py", "runtime"),
        (6, "backend/app/models/value.py", "runtime"),
        (6, None, "runtime"),
        (8, "backend/app/models/value.py", "type"),
        (10, "backend/app/models/__init__.py", "runtime"),
        (12, "backend/app/models/value.py", "dynamic"),
    }
    multiline = next(edge for edge in edges if edge["line"] == 3)
    assert multiline["specifier"] == ".models.value"
    assert multiline["statement"] == "from .models.value import (\n    Value,\n)"
    assert next(edge for edge in edges if edge["specifier"] == "os")["external"]
    assert result["tools"]["grimp"]


def test_dynamic_import_calls_are_explicitly_unresolved(tmp_path):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "backend/app/value.py": "",
            "backend/app/consumer.py": (
                "import importlib as imports\n"
                "from importlib import import_module as load\n"
                "imports.import_module('app.value')\n"
                "load(module_name)\n"
                "__import__('app.value')\n"
            ),
        },
    )
    result = python_index.analyze(tmp_path, files)
    dynamic = [edge for edge in result["dependencies"] if edge["kind"] == "dynamic"]
    assert [edge["line"] for edge in dynamic] == [3, 4, 5]
    assert all(edge["target"] is None for edge in dynamic)
    assert dynamic[1]["specifier"] == "load(module_name)"
    assert len(result["errors"]) == 3
    assert all("动态导入" in error["message"] for error in result["errors"])


def test_standalone_file_has_qualified_symbols_and_unresolved_imports(tmp_path):
    files = _write(
        tmp_path,
        {
            "scripts/standalone.py": (
                "import app.config\n"
                "class Runner:\n"
                "    async def run(self):\n"
                "        def same():\n"
                "            pass\n"
                "        def same():\n"
                "            pass\n"
                "def run():\n"
                "    pass\n"
            )
        },
    )
    result = python_index.analyze(tmp_path, files)
    assert [symbol["name"] for symbol in result["symbols"]] == [
        "Runner",
        "Runner.run",
        "Runner.run.same",
        "Runner.run.same",
        "run",
    ]
    assert len({symbol["id"] for symbol in result["symbols"]}) == 5
    assert result["symbols"][1]["line"] == 3
    assert result["symbols"][1]["end_line"] == 7
    assert result["dependencies"][0]["target"] is None
    assert result["dependencies"][0]["line"] == 1
    assert "独立文件" in result["errors"][0]["message"]


def test_missing_or_excluded_modules_do_not_become_parent_package_edges(tmp_path):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "backend/app/consumer.py": (
                "import app.missing\n"
                "from app.missing import value\n"
                "from .missing import value\n"
                "import app.excluded\n"
            ),
            "backend/app/excluded.py": "invalid Python; must not be scanned!\n",
        },
    )
    files.remove("backend/app/excluded.py")
    result = python_index.analyze(tmp_path, files)
    assert len(result["dependencies"]) == 4
    assert all(edge["target"] is None for edge in result["dependencies"])
    assert all(not edge["external"] for edge in result["dependencies"])
    assert len(result["errors"]) == 4
    assert all("未解析" in error["message"] for error in result["errors"])


def test_mixed_known_and_missing_import_preserves_only_verified_target(tmp_path):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "backend/app/known.py": "value = 1\n",
            "backend/app/consumer.py": "import app.known, app.missing\n",
        },
    )
    result = python_index.analyze(tmp_path, files)
    confirmed = [edge for edge in result["dependencies"] if edge["target"] is not None]
    assert [(edge["specifier"], edge["target"], edge["line"]) for edge in confirmed] == [
        ("app.known", "backend/app/known.py", 1)
    ]
    missing = next(edge for edge in result["dependencies"] if edge["specifier"] == "app.missing")
    assert missing["target"] is None
    assert missing["line"] == 1
    assert any("app.missing" in error["message"] for error in result["errors"])


def test_parse_failure_does_not_hide_other_package_edges(tmp_path):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "backend/app/broken.py": "def invalid(:\n",
            "backend/app/consumer.py": "from .value import Value\n",
            "backend/app/value.py": "class Value: pass\n",
        },
    )
    result = python_index.analyze(tmp_path, files)
    assert result["dependencies"][0]["target"] == "backend/app/value.py"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == "backend/app/broken.py"
    assert "解析失败" in result["errors"][0]["message"]


def test_repeated_analysis_reads_current_snapshot(tmp_path):
    sources = {
        "backend/app/__init__.py": "",
        "backend/app/consumer.py": "from .first import Value\n",
        "backend/app/first.py": "class Value: pass\n",
        "backend/app/second.py": "class Value: pass\n",
    }
    files = _write(tmp_path, sources)
    first = python_index.analyze(tmp_path, files)
    sources["backend/app/consumer.py"] = "from .second import Value\n"
    _write(tmp_path, sources)
    second = python_index.analyze(tmp_path, files)
    assert first["dependencies"][0]["target"] == "backend/app/first.py"
    assert second["dependencies"][0]["target"] == "backend/app/second.py"


def test_absent_grimp_evidence_has_no_invented_line(tmp_path, monkeypatch):
    files = _write(
        tmp_path,
        {"backend/app/__init__.py": "", "backend/app/value.py": "class Value: pass\n"},
    )
    graph = SimpleNamespace(
        modules={"app"},
        find_modules_directly_imported_by=lambda module: {"app.value"},
        get_import_details=lambda **kwargs: [],
    )
    monkeypatch.setattr(python_index, "_build_graph", lambda sources: graph)
    result = python_index.analyze(tmp_path, files)
    assert result["dependencies"][0]["line"] is None
    assert "无法定位" in result["errors"][0]["message"]


def test_loaded_app_cannot_redirect_grimp_outside_snapshot(tmp_path, monkeypatch):
    files = _write(tmp_path, {"backend/app/__init__.py": "import app.hidden\n"})
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace())
    result = python_index.analyze(tmp_path, files)
    assert any("当前进程已加载 app" in error["message"] for error in result["errors"])
    assert result["dependencies"][0]["target"] is None


def test_actual_backend_package_resolves_portfolio_relative_import():
    root = Path(__file__).resolve().parents[2]
    files = sorted(str(path.relative_to(root)) for path in (root / "backend/app").rglob("*.py"))
    result = python_index.analyze(root, files)
    matching = [
        edge
        for edge in result["dependencies"]
        if edge["source"] == "backend/app/services/portfolio/fifo.py"
        and edge["specifier"] == ".semantics"
    ]
    assert len(matching) == 1
    assert matching[0]["target"] == "backend/app/services/portfolio/semantics.py"
    assert matching[0]["line"] > 0
    lines = (root / matching[0]["source"]).read_text().splitlines()
    assert "from .semantics import" in lines[matching[0]["line"] - 1]
    assert not result["errors"]
    assert "app" not in sys.modules


def test_native_cycle_witnesses_separate_runtime_type_and_mixed_edges(tmp_path):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "import app.child\n",
            "backend/app/child.py": "import app\n",
            "backend/app/runtime_a.py": "import app.runtime_b\nimport app.runtime_b\n",
            "backend/app/runtime_b.py": "def later():\n    import app.runtime_a\n",
            "backend/app/type_a.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import app.type_b\n",
            "backend/app/type_b.py": "from typing import TYPE_CHECKING as TC\nif TC:\n    import app.type_a\n",
            "backend/app/mixed_a.py": "import app.mixed_b\n",
            "backend/app/mixed_b.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import app.mixed_a\n",
        },
    )
    result = python_index.analyze(tmp_path, files)
    assert result["errors"] == []
    assert result["cycles_truncated"] is False
    assert len(result["cycles"]) == 4
    assert {row["kind"] for row in result["cycles"]} == {"runtime", "type-only", "mixed"}
    assert any(
        row["paths"]
        == ["backend/app/__init__.py", "backend/app/child.py", "backend/app/__init__.py"]
        for row in result["cycles"]
    )
    for cycle in result["cycles"]:
        assert cycle["tool"] == "grimp"
        assert cycle["paths"][0] == cycle["paths"][-1]
        assert cycle["paths"][0] == min(cycle["paths"])
        assert len(cycle["edges"]) == len(cycle["paths"]) - 1
        for edge in cycle["edges"]:
            assert edge["line"] > 0
            assert any(
                all(dependency[key] == value for key, value in edge.items())
                for dependency in result["dependencies"]
            )
    assert any(edge["kind"] == "dynamic" for row in result["cycles"] for edge in row["edges"])
    assert python_index.analyze(tmp_path, files)["cycles"] == result["cycles"]


def test_cycles_ignore_unresolved_parent_fallback_and_report_native_query_limit(
    tmp_path, monkeypatch
):
    files = _write(
        tmp_path,
        {
            "backend/app/__init__.py": "import app.a\n",
            "backend/app/a.py": "import app.missing\n",
        },
    )
    result = python_index.analyze(tmp_path, files)
    assert result["cycles"] == []
    assert any("未解析" in issue["message"] for issue in result["errors"])
    _write(tmp_path, {"backend/app/a.py": "import app\n"})
    monkeypatch.setattr(python_index, "_MAX_CYCLE_QUERIES", 0)
    result = python_index.analyze(tmp_path, files)
    assert result["cycles"] == []
    assert result["cycles_truncated"] is True
    assert result["errors"] == []
