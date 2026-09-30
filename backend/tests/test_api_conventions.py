"""API 约定的静态守护（#283）。"""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
CJK = re.compile(r"[一-鿿]")

# 这些调用的参数会原样成为 HTTPException.detail：(函数名, 位置参数下标, 关键字名)
DETAIL_ARGS = (
    ("HTTPException", 1, "detail"),
    ("get_owned_record", 4, "not_found_detail"),
    ("ensure_record_is_mutable", None, "detail"),
)


def _module_constants(tree):
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def _text(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp):
        parts = [_text(node.left, constants), _text(node.right, constants)]
        return "".join(p for p in parts if p) or None
    if isinstance(node, ast.Tuple):  # (x, "文案") 形式的查找表
        return None
    return None


def test_user_facing_error_details_are_chinese():
    """前端把 detail 原样弹给用户（utils/apiErrors.ts）：不含任何中文的字面量 detail 即失败。

    覆盖 HTTPException、_ownership 的两个 detail 参数，以及模块级文案常量。
    """
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            for func, position, keyword in DETAIL_ARGS:
                if name != func:
                    continue
                values = [kw.value for kw in node.keywords if kw.arg == keyword]
                if position is not None and len(node.args) > position:
                    values.append(node.args[position])
                for value in values:
                    text = _text(value, constants)
                    if text and text.strip() and not CJK.search(text):
                        offenders.append(f"{path.relative_to(APP)}:{node.lineno}: {text!r}")
    assert offenders == []


def test_ownership_lookup_messages_are_chinese():
    """_ownership 的引用查找表把文案放在元组里，单独核对。"""
    tree = ast.parse((APP / "api" / "_ownership.py").read_text(encoding="utf-8"))
    texts = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"\b(not found|cannot)\b", node.value)
    ]
    assert texts == []
