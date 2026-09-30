"""披露易 PDF → 逐页文本（港股报表抽取用；#281 由 report_statement_service 迁出）。

两处修复只在这里：`baseline_text` 按基线聚行（02313 的 CJK 字体字形框落在基线之下，默认按 top
聚行会把科目名与数字拆成两行），`_drop_overdrawn` 按画家模型丢掉被后画文字盖住的串（01023
报表页在标题位置先画模板页眉再画标题）。财报摘要与股息表格仍用默认 `extract_text()`，
统一前须先跑 `scripts/report_extraction_audit.py --fixtures` 评估（#281 第 4 节）。
改这里的渲染逻辑必须 bump `report_statements.STATEMENT_EXTRACTOR_VERSION`。
"""

import io
from typing import Any, Dict, List, Tuple

import pdfplumber


def _drop_overdrawn(chars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """被后画的文字覆盖的字符串整段丢掉（画家模型：后画的盖住先画的）。

    時代集團 01023 的报表页在标题位置先画了模板页眉「綜合財務報表附註」再画「綜合損益表」，
    渲染出来只见后者，但两串字符位置逐字重合，pdfplumber 按 x 排序后拼成
    「綜綜合合財損務益報表表附註」，标题永远匹配不上。按内容流把字符切成「同一行连续绘制」
    的串；一串里过半字符槽位被后面的串覆盖，就整串视为被盖住（露出的尾巴「表附註」渲染时
    也不可见——它和被盖住的部分属于同一次绘制）。同文重画（加粗效果）也顺带去重。"""
    if not chars:
        return chars
    slot = [
        (
            round(float(ch["x0"]) * 2),
            round(float(ch["top"]) * 2),
            round(float(ch.get("size") or 0) * 2),
        )
        for ch in chars
    ]
    last_index: Dict[Tuple[int, int, int], int] = {}
    for index, key in enumerate(slot):
        last_index[key] = index
    if len(last_index) == len(chars):
        return chars
    # 同一行（top/size 相同）、内容流里连续且 x 单调向右的字符 = 一次绘制的串；x 回退
    # 说明另一串从左边重新开始画（同一位置重画的加粗、或盖在上面的新标题）
    runs: List[List[int]] = [[0]]
    for index in range(1, len(chars)):
        if slot[index][1:] == slot[index - 1][1:] and slot[index][0] > slot[index - 1][0]:
            runs[-1].append(index)
        else:
            runs.append([index])
    dropped: set = set()
    for run in runs:
        covered = sum(1 for index in run if last_index[slot[index]] != index)
        if covered >= 2 and covered * 2 >= len(run):
            dropped.update(run)
        else:
            dropped.update(index for index in run if last_index[slot[index]] != index)
    return [ch for index, ch in enumerate(chars) if index not in dropped]


def baseline_text(chars: List[Dict[str, Any]], *, page_height: float) -> str:
    """按**基线**而不是字形框顶边聚行后抽文本。

    pdfplumber 默认按 char 的 top 聚行，而 top 来自字体的 ascent/descent 度量：申洲國際
    02313 的年报正文是 Source Han Sans（字形框整体落在基线之下）配 Helvetica 数字（正常
    度量），同一行的科目名与数字 top 相差 7.5pt（半行），被拆成两行——58 行现金流量表全部
    「无标签」，上一行的科目名进了下一行的上下文，模型推理 2.3 万 token 后仍把页脚「2025 43」
    映射成经营现金流。两者的文本矩阵 f 分量（基线 y）完全相同，所以把每个字符的 top/bottom
    改写成「基线 − 0.8×字号 / 基线 + 0.2×字号」再交给 pdfplumber 自己的聚行与排版，正常字体
    的页面输出与 `page.extract_text()` 逐行一致。缺 matrix 或非直立文字的页面退回默认抽取。"""
    adjusted: List[Dict[str, Any]] = []
    for ch in _drop_overdrawn(chars):
        matrix = ch.get("matrix")
        if not matrix or len(matrix) < 6 or not ch.get("upright", True):
            return pdfplumber.utils.extract_text(chars)
        size = float(ch.get("size") or 0) or float(ch["bottom"] - ch["top"])
        top = page_height - float(matrix[5]) - size * 0.8
        shift = top - float(ch["top"])
        copy = dict(ch)
        copy["top"] = top
        copy["bottom"] = top + size
        copy["doctop"] = float(ch["doctop"]) + shift
        copy["y1"] = page_height - top
        copy["y0"] = page_height - top - size
        adjusted.append(copy)
    return pdfplumber.utils.extract_text(adjusted)


def page_text(page) -> str:
    return baseline_text(page.chars, page_height=float(page.height))


def extract_pages(pdf_bytes: bytes) -> List[str]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [page_text(page) for page in pdf.pages]
