"""持久化产物上的版本字段：读取与「是否当前版本」判定的唯一实现（#281 第 2 节）。

此前十余处行内手写 `int(payload.get("extractor_version") or 1) == X`，缺字段时而按 1、
时而按 0、时而裸 `!=`。缺字段约定只在这里定义：

- `extractor_version` / `prompt_version`：字段出现之前写出的产物就是 v1；
- 其余（`build_version`、`parser_version`、`edgar_chain_version`……）：缺字段 = 0，
  即一律视为过期待重建。

调用方传入**自己模块里**的版本常量（`versions_current(payload, extractor_version=X)`），
而不是由这里集中 import：常量名在调用时解析，测试对服务模块打桩版本号照样生效，
也不会让本模块反向依赖各条管线。
"""

from typing import Any, Mapping, Optional

# 缺字段即 v1 的字段；不在此集合的一律按 0
MISSING_AS_V1 = frozenset({"extractor_version", "prompt_version"})


def stored_version(payload: Optional[Mapping[str, Any]], field: str) -> int:
    """payload 上记录的版本号；缺字段按上面的约定补，无法解析的值按 -1（永不等于当前版本）。"""
    missing = 1 if field in MISSING_AS_V1 else 0
    if not payload:
        return missing
    value = payload.get(field)
    if value is None or value == "":
        return missing
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def versions_current(payload: Optional[Mapping[str, Any]], **expected: int) -> bool:
    """payload 的每个版本字段都等于期望值（`versions_current(p, extractor_version=10)`）。"""
    return all(stored_version(payload, field) == version for field, version in expected.items())
