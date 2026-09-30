"""app/ 里不得跨模块 import 下划线开头的私有名字（#278 第 5 条）。

私有名字的含义是「只有本模块用、改名不必通知别人」。被别的模块依赖后，改名或改签名时
没有任何保护——此前 report_statement_service 一次从 report_digest_service 导入了 7 个
私有名字。需要共用就改成公开名（或挪进公共模块）。测试可以直接访问私有名字，不在检查范围。
"""

import ast
from pathlib import Path
from typing import List

APP = Path(__file__).parent.parent / "app"


def find_private_imports(source: str, filename: str) -> List[str]:
    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.name
                if name.startswith("_") and not name.startswith("__"):
                    module = "." * node.level + (node.module or "")
                    violations.append(f"{filename}:{node.lineno}: from {module} import {name}")
    return violations


def test_no_cross_module_private_imports():
    violations = []
    for path in sorted(APP.rglob("*.py")):
        violations += find_private_imports(
            path.read_text(encoding="utf-8"), str(path.relative_to(APP.parent))
        )
    assert not violations, "改成公开名再共用：\n" + "\n".join(violations)


def test_guard_flags_private_but_not_dunder():
    source = "from .a import _x, y\nfrom ..b import __version__\nfrom c import _z\n"
    assert find_private_imports(source, "m.py") == [
        "m.py:1: from .a import _x",
        "m.py:3: from c import _z",
    ]
