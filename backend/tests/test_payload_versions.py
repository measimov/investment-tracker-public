"""持久化产物版本字段的唯一判定（payload_versions，#281 第 2 节）。"""

import re
from pathlib import Path

from app.services.payload_versions import stored_version, versions_current


def test_missing_field_conventions():
    # 抽取器 / prompt 字段出现之前的产物就是 v1
    assert stored_version({}, "extractor_version") == 1
    assert stored_version(None, "prompt_version") == 1
    # 其余缺字段 = 0：一律过期待重建
    assert stored_version({}, "build_version") == 0
    assert stored_version({"parser_version": None}, "parser_version") == 0
    assert stored_version({"edgar_chain_version": "3"}, "edgar_chain_version") == 3
    # 认不出的值永不等于当前版本
    assert stored_version({"build_version": "x"}, "build_version") == -1


def test_versions_current_requires_every_field():
    payload = {"extractor_version": 10, "prompt_version": 5}
    assert versions_current(payload, extractor_version=10, prompt_version=5)
    assert not versions_current(payload, extractor_version=10, prompt_version=5, build_version=2)
    assert versions_current({}, extractor_version=1)
    assert not versions_current(None, parser_version=2)


def test_no_inline_version_comparisons():
    """版本判定不得再手写 `int(p.get("x_version") or N) == V` / `p.get("x_version") != V`：
    缺字段约定只在 payload_versions 定义，行内写法曾出现 or 1 / or 0 / 裸 != 三种口径。"""
    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(
        r"""\.get\(\s*["'](?:extractor|prompt|build|parser|edgar_chain)_version["']\s*\)"""
        r"""\s*(?:or\b|==|!=)"""
    )
    offenders = []
    for base in ("app", "scripts"):
        for path in (root / base).rglob("*.py"):
            if path.name == "payload_versions.py":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root)}:{lineno}")
    assert offenders == []
