"""server_default 约定的真实校验（#144 复审）。

「autogenerate 出空 diff」证明不了 server_default 一致：alembic 的
compare_server_default 默认关闭，模型漏写、写错、迁移没跑，diff 都照样是空的。
这里做两层真校验：

1. **约定层**（纯元数据，不碰库）：NOT NULL 且带确定性 Python 默认的列，模型
   必须声明 server_default，**且两者的值必须一致**——只查"有没有声明"的话，
   `default="CNY"` 配 `server_default="USD"` 也能全绿；
2. **落库层**（inspector 对实际 schema）：模型声明的每个确定性 server_default，
   迁移后的数据库里必须真的存在且值一致——迁移漏列/写错值时这里直接红。

比较刻意**保留大小写**：PG 的字符串字面量区分大小写，统一 lower 会把模型的
`IBKR` 与库里的 `ibkr` 判成一致，带大写枚举的默认值漂移就此隐身。只剥掉 PG
回显的类型转换与外层引号；布尔与 JSON 另按各自语义归一。
"""

import json
import re

from sqlalchemy import inspect

from app import models  # noqa: F401  # 注册全部模型
from app.database import Base, engine

# 可确定映射到 SQL 字面量的 callable 默认。`default=dict` / `default=list`
# 不是 is_scalar，只按 is_scalar 过滤会让它们从两层校验里整个消失——
# reconciliation_snapshots 的 cash_balances/positions 就是这么漏掉的。
CALLABLE_DEFAULT_LITERALS = {dict: "{}", list: "[]"}


def _python_default_literal(column):
    """列的 Python 默认值对应的 SQL 字面量文本；不确定的返回 None。"""
    default = column.default
    if default is None:
        return None
    if getattr(default, "is_scalar", False):
        return default.arg
    if getattr(default, "is_callable", False):
        # SQLAlchemy 把无参 callable 包成 lambda ctx: fn()，原函数在 arg.__wrapped__
        fn = getattr(default.arg, "__wrapped__", default.arg)
        return CALLABLE_DEFAULT_LITERALS.get(fn)
    return None


# PG 认的布尔字面量。**必须两侧都列全**：把"不在 true 集合里"的一律当 false
# 的话，`server_default=text("not_a_boolean")` 配 Python default=False、库里
# 也是 false 时，两层守卫会把错误声明和真值判成一致，全绿。
PG_TRUE_TOKENS = frozenset({"true", "t", "y", "yes", "on", "1"})
PG_FALSE_TOKENS = frozenset({"false", "f", "n", "no", "off", "0"})


def _server_default_literal(column):
    """列声明的确定性 server_default 文本；不是字面量时返回 None。

    返回 None 有两种含义，调用方必须区分（见 _server_default_is_expression）：
    函数式默认（func.now()，本就不参与比较）与"写了表达式但解析不出字面量"
    （如 text("lower('USD')")，是个真实的比较盲区）。
    """
    default = column.server_default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if isinstance(arg, str):
        return arg
    text_value = getattr(arg, "text", None)
    if isinstance(text_value, str) and "(" not in text_value:
        return text_value
    return None


def _server_default_repr(column) -> str:
    """声明里那段 SQL 的可读文本（TextClause 的 repr 是对象地址，没法看）。"""
    arg = getattr(column.server_default, "arg", None)
    return str(getattr(arg, "text", arg))


def _server_default_is_expression(column) -> bool:
    """声明了 server_default，但它是个我们解析不出字面量的表达式。

    这类列以前被两层同时跳过：约定层因为拿不到字面量什么都不做，落库层因为
    同一个 None 直接 continue——`default="USD"` 配
    `server_default=text("lower('USD')")` 时模型、Python 默认与库里的实际效果
    可以三方漂移而全绿。既然本文件承诺三方一致，这种声明必须报出来。
    """
    return column.server_default is not None and _server_default_literal(column) is None


def _strip_pg_noise(value: str) -> str:
    """去掉 PG 回显的类型转换与外层引号：`'CNY'::character varying` → `CNY`。

    **不做 lower()**：字符串字面量大小写敏感。
    """
    value = value.strip()
    value = re.sub(r"::[a-zA-Z_ ]+(\[\])?(\([^)]*\))?$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _comparable(value, column) -> str:
    """把 Python 默认 / 模型 server_default / 库内默认归一到可比字符串。

    按类型分派而非一刀切：布尔的 true/True/1 是同一个值，JSON 的 `{}` 与
    `{ }` 也是；文本则原样保留大小写。

    **认不出来的值不做任何归一**，原样带上"无法解析"前缀返回：任何比较都会
    因此判为不一致。归一到某个默认值（尤其布尔的 false）会把错误声明伪装成
    正确值。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = _strip_pg_noise(str(value))
    python_type = None
    try:
        python_type = getattr(column.type, "python_type", None)
    except NotImplementedError:  # 某些类型未实现 python_type
        pass
    if python_type is bool:
        lowered = text.lower()
        if lowered in PG_TRUE_TOKENS:
            return "true"
        if lowered in PG_FALSE_TOKENS:
            return "false"
        return f"<不是布尔字面量:{text}>"
    if column.type.__class__.__name__ in {"JSON", "JSONB"}:
        try:
            return json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
        except (ValueError, TypeError):
            return f"<不是 JSON 字面量:{text}>"
    return text


def test_not_null_defaults_declare_a_matching_server_default():
    """约定：NOT NULL + 确定性 Python 默认 ⇒ 必须声明**值一致的** server_default。

    这是 CLAUDE.md 那条约定的可执行形态。遍历而非清单：#144 按 issue 清单
    改完后，这条测试先后揪出五个漏网（broker_fund_flows.broker、
    ibkr_activity_flows 的 broker/base_currency，以及 callable 默认的
    reconciliation_snapshots.cash_balances/positions）。
    """
    problems = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.nullable:
                continue
            python_default = _python_default_literal(column)
            if python_default is None:
                continue
            declared = _server_default_literal(column)
            if column.server_default is None:
                problems.append(
                    f"{table.name}.{column.name}: 有 Python 默认 {python_default!r} "
                    "但没有 server_default（绕过 ORM 的写入会报错或落 NULL）"
                )
            elif _server_default_is_expression(column):
                problems.append(
                    f"{table.name}.{column.name}: server_default 是解析不出字面量的表达式"
                    f"（{_server_default_repr(column)!r}），无法与 Python 默认 "
                    f"{python_default!r} 比对——请改成字面量，或在本文件显式支持它"
                )
            elif declared is not None and _comparable(declared, column) != _comparable(
                python_default, column
            ):
                problems.append(
                    f"{table.name}.{column.name}: Python 默认={python_default!r} "
                    f"与 server_default={declared!r} 不一致"
                )
    assert not problems, "server_default 约定被破坏：\n" + "\n".join(problems)


def test_declared_server_defaults_match_the_database():
    """落库：模型声明的确定性 server_default 必须与实际 schema 一致。

    autogenerate 不比对 server_default，模型和迁移在这上面可以无声漂移——
    这条用 inspector 读实际 schema 逐列核对，迁移漏列/写错值都会红。
    """
    inspector = inspect(engine)
    mismatches = []
    for table in Base.metadata.sorted_tables:
        db_columns = {col["name"]: col for col in inspector.get_columns(table.name)}
        for column in table.columns:
            declared = _server_default_literal(column)
            if declared is None:
                continue
            actual = (db_columns.get(column.name) or {}).get("default")
            if actual is None:
                mismatches.append(f"{table.name}.{column.name}: 模型声明了默认值，库里没有")
            elif _comparable(actual, column) != _comparable(declared, column):
                mismatches.append(
                    f"{table.name}.{column.name}: 模型={declared!r} 库里={actual!r}"
                )
    assert not mismatches, "server_default 模型与库不一致：\n" + "\n".join(mismatches)
