"""禁止 app/ 与 manage.py 里出现依赖容器时区的「当前时间」（#278 第 3 条）。

生产容器是 UTC：`date.today()`、`datetime.now()`（不带时区）、`datetime.today()` 在北京时间
0–8 点都差一天；`datetime.utcnow()` 返回 naive 值，与 timestamptz 比较时语义含糊（且已弃用）。
业务日期一律 `app.core.timeutil.local_today()`，时间戳一律 `datetime.now(timezone.utc)` 或
`datetime.now(business_timezone())`。此前只靠评审，#271/#272/#278 各抓到一批残留。
"""

import ast
from pathlib import Path
from typing import List

BACKEND = Path(__file__).parent.parent
SCANNED = [*sorted((BACKEND / "app").rglob("*.py")), BACKEND / "manage.py"]

CLOCK_OWNERS = {"date", "datetime"}


def _owner_name(node: ast.expr) -> str:
    """`date` / `datetime` / `datetime.date` / `_dt.datetime` → 末段名字。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def find_clock_violations(source: str, filename: str) -> List[str]:
    violations = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = _owner_name(node.func.value)
        if owner not in CLOCK_OWNERS:
            continue
        attr = node.func.attr
        naive_now = attr == "now" and not node.args and not node.keywords
        if attr in ("today", "utcnow") or naive_now:
            violations.append(f"{filename}:{node.lineno}: {owner}.{attr}()")
    return violations


def test_app_has_no_implicit_clock():
    violations = []
    for path in SCANNED:
        violations += find_clock_violations(
            path.read_text(encoding="utf-8"), str(path.relative_to(BACKEND))
        )
    assert not violations, "改用 local_today() / datetime.now(timezone.utc)：\n" + "\n".join(
        violations
    )


def test_guard_catches_known_forms():
    source = """
import datetime as _dt
from datetime import date, datetime
date.today()
datetime.today()
datetime.now()
datetime.utcnow()
_dt.date.today()
_dt.datetime.now()
datetime.now(timezone.utc)
datetime.now(tz=business_timezone())
"""
    found = find_clock_violations(source, "x.py")
    assert [v.split(": ")[1] for v in found] == [
        "date.today()",
        "datetime.today()",
        "datetime.now()",
        "datetime.utcnow()",
        "date.today()",
        "datetime.now()",
    ]


def test_tests_use_business_date():
    """测试里的「今天」也必须是业务日：业务代码按 local_today() 判「今天/昨天」，测试若用 UTC 的
    date.today() 构造数据，CI（UTC）每天 16–24 点这 8 小时必然对不上（2026-09-30 的 CI 就栽在这里）。
    只查 `date.today()` / `datetime.today()`；naive now 在测试里另有合法用途（构造本地时间戳）。"""
    violations = []
    for path in sorted((BACKEND / "tests").rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for violation in find_clock_violations(
            path.read_text(encoding="utf-8"), str(path.relative_to(BACKEND))
        ):
            if violation.endswith((".today()",)):
                violations.append(violation)
    assert not violations, "测试改用 local_today()：\n" + "\n".join(violations)
