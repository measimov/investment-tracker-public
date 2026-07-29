"""portfolio 内核纯度守卫：禁止一切应用层/DB 依赖与隐式"当前时间"。

内核纯函数化是反事实模拟与策略回测的前提；任何人往里加应用层导入
（app.* 任意模块，含 app.database / app.services）、sqlalchemy 或
date.today() 都应在这里立刻失败。包内一级相对导入（from .semantics ...）
是唯一允许的内部引用方式。
"""

import ast
from pathlib import Path
from typing import List

PORTFOLIO_DIR = Path(__file__).parent.parent / "app" / "services" / "portfolio"

# 绝对导入黑名单：应用层任意模块与 ORM。内核只允许标准库 + 包内一级相对导入。
FORBIDDEN_ABSOLUTE_PREFIXES = ("app", "sqlalchemy")


def _is_forbidden_absolute(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in FORBIDDEN_ABSOLUTE_PREFIXES
    )


def find_import_violations(source: str, filename: str) -> List[str]:
    """返回违反内核纯度的导入清单；供守卫测试与自证用例共用。"""
    violations = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_absolute(alias.name):
                    violations.append(f"{filename}: 禁止导入 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level >= 2:
                # ..models / ...database 等越出 portfolio 包的相对导入全部违规。
                violations.append(
                    f"{filename}: 相对导入越出内核包 (from {'.' * node.level}{module} ...)"
                )
            elif node.level == 0 and _is_forbidden_absolute(module):
                violations.append(f"{filename}: 禁止导入 {module}")
    return violations


def _iter_modules():
    return sorted(PORTFOLIO_DIR.glob("*.py"))


def test_portfolio_kernel_has_no_app_or_db_imports():
    violations = []
    for path in _iter_modules():
        violations.extend(find_import_violations(path.read_text(), path.name))
    assert not violations, "portfolio 内核出现应用层/DB 依赖:\n" + "\n".join(violations)


def test_portfolio_kernel_has_no_implicit_today():
    violations = []
    for path in _iter_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "today"
            ):
                violations.append(f"{path.name}:{node.lineno}: 内核不得调用 .today()，请由调用方传入")
    assert not violations, "\n".join(violations)


def test_guard_rejects_known_forbidden_imports():
    """守卫自证：这些写法必须被拦截，防止黑名单悄悄失效（review #53）。"""
    forbidden_snippets = [
        "from app.database import SessionLocal",
        "import app.database",
        "from app.models.transaction import Transaction",
        "from app.services.exchange_rate_service import convert_to_cny",
        "import sqlalchemy",
        "from sqlalchemy.orm import Session",
        "from ..models.transaction import Transaction",
        "from ...database import get_db",
    ]
    for snippet in forbidden_snippets:
        assert find_import_violations(snippet, "fake.py"), f"守卫漏拦: {snippet}"


def test_guard_allows_stdlib_and_intra_package_imports():
    allowed_snippets = [
        "from decimal import Decimal",
        "import logging",
        "from .semantics import bonus_share_factor",
        "from .fx import ExchangeRateLookup",
        "from typing import Dict, Optional",
    ]
    for snippet in allowed_snippets:
        assert not find_import_violations(snippet, "fake.py"), f"守卫误拦: {snippet}"
