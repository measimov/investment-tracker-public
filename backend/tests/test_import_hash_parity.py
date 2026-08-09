"""`scripts/import_hash_parity.sh` 的 **shell 控制流**回归（#135 复审第四轮）。

双跑比对最要命的失效模式不是算错 hash，而是**整条流水线没跑到却报零差异**：

- 交互 shell 默认不因上一条命令失败而停止，而 `> before.json` 的重定向在命令
  失败时仍会先留下一个空文件 → 空快照 vs 空快照 → `diff` 退出 0、零输出；
- 旧文档流程里 `git checkout main` 一旦失败，两次快照就都跑在同一份代码上 →
  两份非空快照天然完全相同 → 同样是零差异。

这两条都只在**真实 shell 执行**时才暴露：进程内调用 `snapshot.main(argv)` 的
用例（`test_import_hash_snapshot.py`）永远绿。所以这里全部起子进程跑脚本，
并且用一个临时 git 仓库 + 打桩工具，不碰真实对账单、不依赖网络。

打桩工具按 `PYTHONPATH` 指向的那一侧读 `marker.txt` 再吐 JSON——于是"基线跑
的是基线代码、工作树跑的是工作树代码"这件事本身也被钉住了。
"""

import os
import pathlib
import shutil
import subprocess

import pytest

PARITY_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "import_hash_parity.sh"

# 按 PYTHONPATH 那一侧的 marker.txt 产出快照：两侧代码不同 → 快照不同。
# 行为由环境变量控制，好让每个失效场景各自打桩。
STUB_TOOL = '''\
import json, os, pathlib, sys

mode = os.environ.get("STUB_MODE", "ok")
side = pathlib.Path(os.environ["PYTHONPATH"], "marker.txt").read_text().strip()

if mode == "boom" or (mode == "boom_after" and side != "baseline") or (
    mode == "boom_before" and side == "baseline"
):
    print("打桩：解析失败", file=sys.stderr)
    sys.exit(1)
if mode == "empty":
    sys.exit(0)
if mode == "no_hashes":
    print("{}")
    sys.exit(0)

payload = {"cmb::x.pdf": {"total_rows": 1, "hashes": [side], "errors": []}}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
'''


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """临时仓库：基线提交 marker=baseline，当前分支 marker=worktree。"""
    root = tmp_path / "repo"
    (root / "backend" / "scripts").mkdir(parents=True)
    _git(root, "init", "--quiet")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")  # 不依赖 git 的默认分支名
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    shutil.copy(PARITY_SCRIPT, root / "backend" / "scripts" / PARITY_SCRIPT.name)
    (root / "backend" / "marker.txt").write_text("baseline\n")
    (root / "backend" / "其他.txt").write_text("v1")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "baseline")

    # 工作树侧改成另一份代码——否则脚本会以"与基线一致"为由拒绝跑（见对应用例）
    (root / "backend" / "marker.txt").write_text("worktree\n")

    tool = tmp_path / "stub_snapshot.py"
    tool.write_text(STUB_TOOL)
    return root, tool


def _run(repo_root, tool, *, mode="ok", extra=(), args=("--cmb", "/tmp/x.pdf")):
    import sys

    env = {
        **os.environ,
        "PARITY_PYTHON": sys.executable,
        "PARITY_SNAPSHOT_TOOL": str(tool),
        "STUB_MODE": mode,
    }
    env.pop("PARITY_KEEP", None)
    return subprocess.run(
        ["bash", str(repo_root / "backend" / "scripts" / PARITY_SCRIPT.name), *extra, "--", *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_identical_hashes_report_zero_diff(repo):
    """两侧 hash 相同 → 退出 0 并明确报零差异。"""
    root, tool = repo
    # 两侧 marker 一致，但**另一个已跟踪文件**有改动——这才是真实场景：
    # 代码确实变了（所以 vacuity 守卫放行），而 hash 没变。
    (root / "backend" / "marker.txt").write_text("baseline\n")
    (root / "backend" / "其他.txt").write_text("v2")

    result = _run(root, tool)

    assert result.returncode == 0, result.stderr
    assert "零差异" in result.stdout


def test_hash_difference_fails(repo):
    """两侧 hash 不同 → 非零退出并打出 diff。"""
    root, tool = repo
    result = _run(root, tool)

    assert result.returncode != 0
    assert "有差异" in result.stderr
    assert "零差异" not in result.stdout
    assert "baseline" in result.stderr and "worktree" in result.stderr


@pytest.mark.parametrize("mode", ["boom_after", "boom_before"])
def test_a_failed_snapshot_run_never_reaches_the_diff(repo, mode):
    """[回归锁] 任一侧快照生成失败 → 立即中止，**绝不**走到 diff。

    这正是旧文档流程的假绿：python 挂了，重定向仍留下空文件，交互 shell 接着
    往下走，`diff` 两个空文件退出 0、零输出，看着像验证过了。
    """
    root, tool = repo
    result = _run(root, tool, mode=mode)

    assert result.returncode != 0
    assert "零差异" not in result.stdout
    assert "快照生成失败" in result.stderr


def test_empty_snapshot_never_reaches_the_diff(repo):
    """工具退出 0 却什么都没输出（空快照）也必须中止。"""
    root, tool = repo
    result = _run(root, tool, mode="empty")

    assert result.returncode != 0
    assert "零差异" not in result.stdout
    assert "空文件" in result.stderr


def test_snapshot_without_any_hash_never_reaches_the_diff(repo):
    """输出合法 JSON 但一条 hash 都没有 → 同样是"什么都没验证"。"""
    root, tool = repo
    result = _run(root, tool, mode="no_hashes")

    assert result.returncode != 0
    assert "一条 hash 都没有" in result.stderr


def test_worktree_identical_to_baseline_is_refused(repo):
    """[回归锁] 工作树与基线一模一样时拒绝跑。

    旧流程里 `git checkout main` 失败会让两次快照跑在同一份代码上，得到两份
    非空且必然相同的快照。现在不再切分支（基线跑在 git worktree 里），这条
    守卫再兜一道：零差异必须来自"代码变了但 hash 没变"。
    """
    root, tool = repo
    _git(root, "checkout", "--quiet", "--", "backend/marker.txt")  # 还原到与基线一致

    result = _run(root, tool)

    assert result.returncode != 0
    assert "完全一致" in result.stderr
    assert "零差异" not in result.stdout


def test_unknown_baseline_ref_fails(repo):
    root, tool = repo
    result = _run(root, tool, extra=("--baseline", "没有这个分支"))

    assert result.returncode != 0
    assert "基线 ref 解析失败" in result.stderr


def test_missing_snapshot_args_fails(repo):
    """`--` 之后不给任何快照参数 → 非零退出（不能跑出一份空快照）。"""
    root, tool = repo
    result = _run(root, tool, args=())

    assert result.returncode != 0
    assert "没有任何快照参数" in result.stderr


def test_baseline_worktree_is_cleaned_up(repo):
    """临时 worktree 用完即拆，不给仓库留垃圾（失败路径也一样）。"""
    root, tool = repo
    _run(root, tool, mode="boom")

    listed = subprocess.run(
        ["git", "-C", str(root), "worktree", "list"],
        capture_output=True, text=True, check=True,
    )
    assert len(listed.stdout.strip().splitlines()) == 1, listed.stdout
