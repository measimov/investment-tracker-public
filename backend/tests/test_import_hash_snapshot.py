"""hash 快照脚本的守卫回归（#135 复审）。

脚本最要命的失效模式不是算错 hash，而是**没跑到却照常输出**——空快照与空
快照 diff 出来正是"零差异"，什么都没验证却看着像验证过了。真实对账单是各人
本地的未入库文件（`data/` 在 git 里只有 `.gitkeep`），所以这里全部用合成路径
与打桩 parser，不依赖任何真实文件；固件里的账户号也一律是合成值。
"""

import json
import pathlib

import pytest

from scripts import import_hash_snapshot as snap


class _Row:
    def __init__(self, row_hash, legacy=None):
        self.row_hash = row_hash
        self.legacy_row_hash = legacy or f"legacy-{row_hash}"


def _args(**kwargs):
    argv = []
    for key, paths in kwargs.items():
        for path in paths:
            argv += [f"--{key}", str(path)]
    return snap.build_parser().parse_args(argv)


@pytest.fixture
def files(tmp_path):
    made = {}
    for key, name in (
        ("cmb", "招商_2026年对账单.pdf"),              # 刻意不带 cmb_ 前缀
        ("eastmoney", "东财明细_2026.pdf"),             # 刻意不带 eastmoney_ 前缀
        ("ibkr-xlsx", "交易历史_2026.xlsx"),
        ("ibkr-activity", "U12345678.TRANSACTIONS.csv"),  # 合成账户号
    ):
        path = tmp_path / name
        path.write_bytes(b"stub")
        made[key] = [path]
    return made


def _stub(rows, total_rows, errors=()):
    return lambda content, name: (rows, {}, total_rows, list(errors))


def _patch_all(monkeypatch, stub):
    """把每个输入类别的 parse 换成打桩，保留其余元组项。"""
    for key in snap.PARSERS:
        help_text, _parse, extract, suffixes = snap.PARSERS[key]
        monkeypatch.setitem(snap.PARSERS, key, (help_text, stub, extract, suffixes))


def test_arbitrary_filenames_are_accepted(files, monkeypatch):
    """显式绑定的意义：文件叫什么都行，不依赖任何前缀契约。

    早先版本按 `cmb_*.pdf` 之类前缀路由，只在作者机器上匹配得到——别人跑同
    一条命令会得到完全不同（甚至空）的覆盖面，parity 证据没法复现。
    """
    _patch_all(monkeypatch, _stub([_Row("h1"), _Row("h2")], 2))
    out = snap.snapshot(snap.collect_sources(_args(**files)))

    assert len(out) == 4
    assert all(payload["total_rows"] == 2 for payload in out.values())
    # 东财两套 hash 都要进快照
    eastmoney = next(v for k, v in out.items() if k.startswith("eastmoney::"))
    assert eastmoney["hashes"] == [["h1", "legacy-h1"], ["h2", "legacy-h2"]]


@pytest.mark.parametrize("dropped", sorted(snap.PARSERS))
def test_missing_any_input_category_fails_loudly(files, dropped):
    """少了任意一个输入类别都要中止。

    IBKR 的 xlsx 与 activity csv 是**两个独立必填类别**：两者由 parse_rows
    分派到不同 reader（xlsx 把 Trade ID 并进 description 参与 hash）。合成
    一个类别的话只给其中一种也能过守卫——上一版就是这么退化的。
    """
    kwargs = {k: v for k, v in files.items() if k != dropped}
    with pytest.raises(SystemExit) as excinfo:
        snap.collect_sources(_args(**kwargs))
    assert "一个样本都没给" in str(excinfo.value)
    assert dropped in str(excinfo.value)


def test_nonexistent_path_fails_loudly(files, tmp_path):
    files = {**files, "cmb": [tmp_path / "不存在.pdf"]}
    with pytest.raises(SystemExit) as excinfo:
        snap.collect_sources(_args(**files))
    assert "不存在或不是文件" in str(excinfo.value)


def test_row_level_errors_are_snapshotted_not_swallowed(files, monkeypatch):
    """parser 返回的行级 errors 必须进快照参与比对，而不是被丢掉。

    三家 parse_rows 的行级失败只会追加进 errors 并继续、**不抛异常**；只取
    rows 的话，错误内容变了也看不出来。真实招商对账单本就带十几条"红利税
    缺证券代码"的告警，所以不能一律硬失败——但必须可比对。
    """
    _patch_all(monkeypatch, _stub([_Row("h1")], 2, errors=["row 7: 坏行"]))
    out = snap.snapshot(snap.collect_sources(_args(**files)))
    assert all(payload["errors"] == ["row 7: 坏行"] for payload in out.values())


def test_total_parse_failure_without_exception_fails_loudly(files, monkeypatch):
    """[回归锁] 整份文件解析失败但 parser 不抛异常时，必须中止而非输出空数组。

    这是本脚本此前最隐蔽的假绿：rows=[] 却照常写进快照并以 0 退出，
    before/after 都是空数组 → diff 干净 → 看着像验证过了。
    """
    _patch_all(monkeypatch, _stub([_Row("ok")], 1))
    help_text, _parse, extract, suffixes = snap.PARSERS["cmb"]
    monkeypatch.setitem(
        snap.PARSERS,
        "cmb",
        (help_text, _stub([], 120, errors=[f"row {i}: 全崩" for i in range(120)]),
         extract, suffixes),
    )

    with pytest.raises(SystemExit) as excinfo:
        snap.snapshot(snap.collect_sources(_args(**files)))
    assert "零条成功解析" in str(excinfo.value)


def test_empty_source_file_fails_loudly(files, monkeypatch):
    _patch_all(monkeypatch, _stub([_Row("ok")], 1))
    help_text, _parse, extract, suffixes = snap.PARSERS["ibkr-xlsx"]
    monkeypatch.setitem(
        snap.PARSERS, "ibkr-xlsx", (help_text, _stub([], 0), extract, suffixes)
    )

    with pytest.raises(SystemExit) as excinfo:
        snap.snapshot(snap.collect_sources(_args(**files)))
    assert "一行都没读到" in str(excinfo.value)


def test_parser_exception_fails_loudly(files, monkeypatch):
    def boom(content, name):
        raise ValueError("坏 PDF")

    _patch_all(monkeypatch, _stub([_Row("ok")], 1))
    help_text, _parse, extract, suffixes = snap.PARSERS["cmb"]
    monkeypatch.setitem(snap.PARSERS, "cmb", (help_text, boom, extract, suffixes))

    with pytest.raises(SystemExit) as excinfo:
        snap.snapshot(snap.collect_sources(_args(**files)))
    assert "坏 PDF" in str(excinfo.value)


def test_documented_command_shape_runs_from_outside_the_repo(files, monkeypatch, capsys):
    """文档命令必须真能跑：脚本被复制到仓库外、只靠显式路径工作。

    上一版文档的第一条命令按原样跑不起来（脚本复制到 /tmp 后靠自身位置推导
    数据目录，macOS 上解析成 /data 直接退出）。现在没有任何位置推导。
    """
    _patch_all(monkeypatch, _stub([_Row("h")], 1))

    argv = []
    for key, paths in files.items():
        argv += [f"--{key}", str(pathlib.Path(paths[0]).resolve())]
    snap.main(argv)

    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 4


def test_same_basename_in_different_dirs_fails_loudly(tmp_path, monkeypatch, files):
    """同名不同目录的两份对账单不得静默互相覆盖。

    快照按 `类别::文件名` 索引：后一份会盖掉前一份，最终 JSON 少一条、退出码
    仍是 0——被盖掉那份的 hash 回归 before/after 都看不见。
    """
    _patch_all(monkeypatch, _stub([_Row("h")], 1))
    other = tmp_path / "另一个目录"
    other.mkdir()
    duplicate = other / pathlib.Path(files["cmb"][0]).name
    duplicate.write_bytes(b"stub")

    sources = snap.collect_sources(_args(**{**files, "cmb": files["cmb"] + [duplicate]}))
    with pytest.raises(SystemExit) as excinfo:
        snap.snapshot(sources)
    assert "重复的文件名" in str(excinfo.value)


def test_wrong_suffix_for_a_category_fails_loudly(files, tmp_path):
    """后缀校验挡住"把同一个 csv 喂给两个 IBKR flag"这种伪覆盖。"""
    csv_path = files["ibkr-activity"][0]
    with pytest.raises(SystemExit) as excinfo:
        snap.collect_sources(_args(**{**files, "ibkr-xlsx": [csv_path]}))
    assert "后缀应为 .xlsx" in str(excinfo.value)


def test_script_runs_as_a_real_program(tmp_path):
    """真起子进程跑一次，证明脚本作为**程序**可执行、守卫也确实生效。

    进程内调用 main(argv) 测不到 shebang/入口/argparse 的端到端可用性——
    文档里那条命令曾经因为把整串 argv 塞进标量变量而在 zsh 下 exit 127，
    而当时的进程内用例照样绿。
    """
    import os
    import subprocess
    import sys

    backend = pathlib.Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(backend)}
    result = subprocess.run(
        [sys.executable, str(backend / "scripts" / "import_hash_snapshot.py")],
        cwd=backend, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0, "一个样本都不给却成功退出 = 假绿"
    assert "一个样本都没给" in result.stderr

    # --help 必须列出全部四个输入类别（IBKR 两种格式各自独立）
    helped = subprocess.run(
        [sys.executable, str(backend / "scripts" / "import_hash_snapshot.py"), "--help"],
        cwd=backend, env=env, capture_output=True, text=True,
    )
    assert helped.returncode == 0
    for flag in ("--cmb", "--eastmoney", "--ibkr-xlsx", "--ibkr-activity"):
        assert flag in helped.stdout, flag
