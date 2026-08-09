"""把真实对账单解析出的全部 row_hash 落成快照，供重构前后逐字节比对。

HASH-CRITICAL 改动（`broker_import_common` 的 hash 相关函数、任一导入器的
`HASH_FIELDS` 或文本清洗、同批消歧逻辑）必须双跑本脚本：
`tests/test_import_hash_stability.py` 的金样只钉住几个构造值，钉不住真实账本上
数千条流水的实际形态（BOM、尾缀 `.0`、同批等值成交的消歧序号……）。

**文件与 parser 的绑定必须由调用方显式给出**，脚本不猜文件名：`data/` 在 git
里只有 `.gitkeep`，真实对账单是各人本地的未入库文件，名字千差万别。早先按
`cmb_*.pdf` 这类前缀路由的版本，只在作者机器上匹配得到——别人跑同一条命令
会得到完全不同（甚至空）的覆盖面，那份 parity 证据就没法复现。

**双跑请一律走 `scripts/import_hash_parity.sh`，不要手敲命令序列**：

    cd backend
    scripts/import_hash_parity.sh --baseline main -- \\
        --cmb "$PWD/../data/招商对账单.pdf" \\
        --eastmoney "$PWD/../data/东财对账单.pdf" \\
        --ibkr-xlsx "$PWD/../data/ibkr导出.xlsx" \\
        --ibkr-activity "$PWD/../data/activity.csv"

它负责 fail-fast 与"两次确实跑在不同代码上"，本脚本只管产出一份快照。手敲的
命令序列全程 fail-fast 不了：交互 shell 不因上一条失败而停止，而 `> out.json`
在命令失败时仍会留下空文件——任一次跑挂了就是空快照 vs 空快照，`diff` 退出 0
且零输出；`git checkout main` 挂了则两次跑在同一份代码上，两份非空快照天然
相同。两种都是"零差异"的假绿，而且看不出区别（这个坑本文档自己踩过两次）。

单独跑本脚本时路径用绝对路径。每个输入类别至少要给一个文件（同一 flag 可重复
传多份），少任何一个直接中止。

IBKR 的 **xlsx 与 activity csv 是两个独立必填类别**：两者由 `parse_rows` 分派
到不同 reader（xlsx 会把 Trade ID 并进 description 参与 hash，csv 走另一套
账户与流水输入路径）。合成一个类别的话，只给其中一种也能通过覆盖守卫，而
那等于漏测一整条 hash 输入路径却仍报告 parity 成功。

快照里同时收 `row_hash`、`total_rows` 与解析器返回的 `errors`：
- 三家 `parse_rows` 的行级失败只会追加进 `errors` 并继续，不抛异常。只取
  `rows` 的话，整份文件解析失败（`rows=[]`）也会输出一个空数组并正常退出；
- errors 本身进快照参与比对：错误内容变了同样是回归信号。真实招商对账单
  本就带十几条"红利税缺证券代码"的行级告警，一律硬失败会让工具不可用；
- 但**有源数据却零成功行**是硬错误，直接中止。
"""

import argparse
import json
import pathlib
import sys
from typing import Callable, Dict, List, Tuple

from app.services import cmb_fund_flow_importer as cmb
from app.services import eastmoney_statement_importer as em
from app.services import ibkr_activity_importer as ibkr


def _single_hash(rows) -> list:
    return [row.row_hash for row in rows]


def _eastmoney_hashes(rows) -> list:
    # 东财是唯一有两套 hash 的导入器，两套都要进快照
    return [[row.row_hash, row.legacy_row_hash] for row in rows]


# 输入类别 → (CLI flag 说明, parse_rows, hash 提取, 期望后缀)
#
# IBKR 刻意拆成两个**独立必填**类别：两种格式由 parse_rows 分派到不同 reader
# （xlsx 把 Trade ID 并进 description 参与 hash，csv 走另一套账户与流水输入
# 路径）。合成一个类别的话只给其中一种也能过守卫，等于漏测一整条 hash 路径
# 却仍报告 parity 成功。后缀校验让"把 csv 同时喂给两个 flag"也过不了关。
PARSERS: Dict[str, Tuple[str, Callable, Callable, Tuple[str, ...]]] = {
    "cmb": ("招商证券对账单 PDF", cmb.parse_rows, _single_hash, (".pdf",)),
    "eastmoney": ("东方财富对账单 PDF", em.parse_rows, _eastmoney_hashes, (".pdf",)),
    "ibkr-xlsx": ("IBKR 交易历史 xlsx", ibkr.parse_rows, _single_hash, (".xlsx",)),
    "ibkr-activity": ("IBKR Activity CSV", ibkr.parse_rows, _single_hash, (".csv",)),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for key, (help_text, _parse, _extract, suffixes) in PARSERS.items():
        parser.add_argument(
            f"--{key}",
            action="append",
            metavar="PATH",
            help=f"{help_text}（{'/'.join(suffixes)}；可重复，至少一个）",
        )
    return parser


def _dest(key: str) -> str:
    """argparse 把 `--ibkr-xlsx` 存成 `ibkr_xlsx`。"""
    return key.replace("-", "_")


def collect_sources(args) -> List[Tuple[str, pathlib.Path]]:
    """把 CLI 参数拍平成 [(parser_key, path)]；覆盖不全或文件不存在时中止。"""
    missing = [key for key in PARSERS if not getattr(args, _dest(key))]
    if missing:
        raise SystemExit(
            f"这些输入类别一个样本都没给：{sorted(missing)}\n"
            "空快照与空快照 diff 出来是'零差异'，那是假绿——每个类别至少给一份"
            "真实对账单。确实不再需要某一类时，请显式改 PARSERS（可见的改动）。"
        )
    sources: List[Tuple[str, pathlib.Path]] = []
    problems: List[str] = []
    for key in PARSERS:
        suffixes = PARSERS[key][3]
        for raw in getattr(args, _dest(key)):
            path = pathlib.Path(raw)
            if not path.is_file():
                problems.append(f"--{key} {raw}：路径不存在或不是文件")
                continue
            if path.suffix.lower() not in suffixes:
                problems.append(
                    f"--{key} {raw}：后缀应为 {'/'.join(suffixes)}"
                    "（拆成独立类别就是为了两种格式各测一遍，喂错等于漏测一条路径）"
                )
                continue
            sources.append((key, path))
    if problems:
        raise SystemExit("输入有问题：\n  " + "\n  ".join(problems))
    return sources


def snapshot(sources: List[Tuple[str, pathlib.Path]]) -> Dict[str, dict]:
    """逐份解析并收集 hash / total_rows / errors；解析失败或零成功行即中止。"""
    out: Dict[str, dict] = {}
    failures: List[str] = []

    for key, path in sources:
        _help, parse, extract, _suffixes = PARSERS[key]
        label = f"{key}::{path.name}"
        if label in out:
            # 两份不同目录、同名的对账单会静默互相覆盖：最终 JSON 里只剩一份、
            # 退出码仍是 0，被盖掉那份的 hash 回归 before/after 都看不见。
            failures.append(
                f"{label}：同一类别里有重复的文件名（另一份来自别的目录）。"
                "快照按 类别::文件名 索引，重名会互相覆盖并静默漏测——"
                "请改名或分批跑。"
            )
            continue
        try:
            rows, _counts, total_rows, errors = parse(path.read_bytes(), path.name)
        except Exception as exc:  # 显式指定了却解析不了，本身就是回归
            failures.append(f"{path.name} [{key}]: {type(exc).__name__}: {exc}")
            continue
        if total_rows and not rows:
            failures.append(
                f"{path.name} [{key}]: 源文件有 {total_rows} 行但零条成功解析"
                f"（errors={len(errors)}）——空快照会伪装成零差异"
            )
            continue
        if not total_rows:
            failures.append(f"{path.name} [{key}]: 源文件一行都没读到")
            continue
        out[label] = {
            "total_rows": total_rows,
            "hashes": extract(rows),
            # errors 进快照参与比对：内容变了同样是回归信号（真实招商对账单
            # 本就带十几条行级告警，一律硬失败会让工具不可用）
            "errors": list(errors),
        }

    if failures:
        raise SystemExit("解析失败：\n  " + "\n  ".join(failures))
    return out


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    out = snapshot(collect_sources(args))
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    per_parser: Dict[str, List[int]] = {}
    for label, payload in out.items():
        key = label.split("::", 1)[0]
        bucket = per_parser.setdefault(key, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += len(payload["hashes"])
        bucket[2] += len(payload["errors"])
    for key, (files, hashes, errors) in sorted(per_parser.items()):
        print(f"# {key}: {files} 份文件 / {hashes} 条 hash / {errors} 条行级告警", file=sys.stderr)


if __name__ == "__main__":
    main()
