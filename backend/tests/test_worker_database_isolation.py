"""xdist 必须在 Settings/engine 首次导入前隔离数据库，controller 不写原库。"""

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

import conftest


@pytest.mark.parametrize("worker_id", [None, "gw0", "gw1"])
def test_first_app_import_and_scratch_databases_use_selected_worker(worker_id):
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        "postgresql+psycopg2://tester:p%40ss@127.0.0.1:5432/isolation_test"
        "?gssencmode=disable&application_name=isolation"
    )
    env.pop("PYTEST_XDIST_WORKER", None)
    if worker_id:
        env["PYTEST_XDIST_WORKER"] = worker_id
    # 独立解释器才能发现 Settings/engine 已被过早导入的错误；此处只构造 engine，不连库。
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import os
import runpy
import conftest
from app.config import settings
from app.database import engine
from sqlalchemy.engine import make_url

selected = make_url(os.environ['DATABASE_URL'])
rules = runpy.run_path('tests/test_security_rules_migration.py')
symbols = runpy.run_path('tests/test_rule_symbol_migration.py')
assert make_url(settings.database_url) == selected == engine.url
assert selected.username == 'tester' and selected.password == 'p@ss'
assert selected.query == {'gssencmode': 'disable', 'application_name': 'isolation'}
for migration in (rules, symbols):
    assert make_url(migration['SCRATCH_URL']).query == selected.query
print(json.dumps([selected.database, rules['SCRATCH_DB'], symbols['SCRATCH_DB']]))
""",
        ],
        cwd=conftest.BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    name = "isolation_test" + (f"_{worker_id}" if worker_id else "")
    assert json.loads(result.stdout) == [name, name + "_rm", name + "_sm"]


@pytest.mark.parametrize("worker_id", ["", "gw2", "master", "../gw0"])
def test_unexpected_worker_is_rejected(worker_id):
    with pytest.raises(RuntimeError, match="gw0 and gw1"):
        conftest._worker_database_url("postgresql://localhost/isolation_test", worker_id)


@pytest.mark.parametrize(
    "database_url",
    ["sqlite:///investment_test", "postgresql://localhost/investment"],
)
@pytest.mark.parametrize("worker_id", [None, "gw0"])
def test_unsafe_source_database_is_rejected(database_url, worker_id):
    with pytest.raises(RuntimeError, match="PostgreSQL|non-test"):
        conftest._worker_database_url(database_url, worker_id)


@pytest.mark.parametrize("suffix", ["", "_gw0", "_rm", "_sm"])
@pytest.mark.parametrize("multibyte", [False, True])
def test_database_names_cannot_be_silently_truncated(suffix, multibyte):
    # PostgreSQL 的标识符限制按 UTF-8 字节计，不是 Python 字符数。
    prefix = "test库" if multibyte else "test"
    name = prefix + "x" * (63 - len(prefix.encode()) - len(suffix))
    url = make_url("postgresql://localhost/test").set(database=name)
    derived = conftest._database_url_with_suffix(url.render_as_string(), suffix)
    assert len(make_url(derived).database.encode()) == 63
    with pytest.raises(RuntimeError, match="63 bytes"):
        conftest._database_url_with_suffix(url.set(database=name + "x").render_as_string(), suffix)


@pytest.mark.parametrize("mode", ["serial", "controller", "worker"])
def test_only_test_processes_migrate_and_seed(monkeypatch, mode):
    database_url = "postgresql://localhost/isolation_test"
    monkeypatch.setenv("DATABASE_URL", database_url)
    upgrade = Mock()
    seed = Mock()
    monkeypatch.setattr(conftest.command, "upgrade", upgrade)
    monkeypatch.setattr(conftest, "_seed_test_users", seed)
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=0 if mode == "serial" else 2))
    if mode == "worker":
        config.workerinput = {"workerid": "gw0"}
    conftest.pytest_sessionstart(SimpleNamespace(config=config))
    if mode == "controller":
        upgrade.assert_not_called()
        seed.assert_not_called()
    else:
        assert upgrade.call_count == 1
        assert upgrade.call_args.args[1] == "head"
        seed.assert_called_once_with(database_url)


def test_controller_still_rejects_unsafe_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/investment")
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=2))
    with pytest.raises(RuntimeError, match="non-test"):
        conftest.pytest_sessionstart(SimpleNamespace(config=config))


def test_worker_bootstrap_imports_are_covered(tmp_path):
    pytest.importorskip("pytest_cov")
    pytest.importorskip("xdist")
    (tmp_path / "coverage_probe.py").write_text(
        "class Model:\n    value = 42\n\ndef answer():\n    return Model.value\n"
    )
    # 导出真实 hook（保留其 hookimpl 元数据），只把数据库副作用替换成首次模块导入。
    # 若恢复到 pytest_configure，模块的 class/赋值/def 行会在 worker 开启 coverage 前执行。
    (tmp_path / "conftest.py").write_text(
        f"""
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location('backend_bootstrap', {str(conftest.__file__)!r})
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

def import_probe(*args):
    import coverage_probe
    worker = os.environ.get('PYTEST_XDIST_WORKER', 'controller')
    Path(worker + '.bootstrap').write_text(os.environ['DATABASE_URL'])

bootstrap.command.upgrade = import_probe
bootstrap._seed_test_users = import_probe
for hook in ('pytest_configure', 'pytest_sessionstart'):
    if hasattr(bootstrap, hook):
        globals()[hook] = getattr(bootstrap, hook)
"""
    )
    for index in range(2):
        (tmp_path / f"test_probe_{index}.py").write_text(
            "from coverage_probe import answer\ndef test_answer():\n    assert answer() == 42\n"
        )
    env = os.environ.copy()
    for name in list(env):
        if name.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_XDIST_")):
            env.pop(name)
    env.update(
        DATABASE_URL="postgresql://localhost/bootstrap_test",
        PYTEST_ADDOPTS="",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        COVERAGE_FILE=str(tmp_path / ".coverage"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_cov.plugin",
            "-p",
            "xdist.plugin",
            "-n2",
            "--dist=loadfile",
            "--max-worker-restart=0",
            f"--confcutdir={tmp_path}",
            "--cov=coverage_probe",
            "--cov-report=json:coverage.json",
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for worker in ("gw0", "gw1"):
        assert (tmp_path / f"{worker}.bootstrap").read_text().endswith(f"bootstrap_test_{worker}")
    assert not (tmp_path / "controller.bootstrap").exists()
    report = json.loads((tmp_path / "coverage.json").read_text())
    probe = report["files"]["coverage_probe.py"]
    assert probe["missing_lines"] == []
    assert {1, 2, 4, 5}.issubset(probe["executed_lines"])
