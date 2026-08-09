#!/usr/bin/env bash
#
# HASH-CRITICAL 改动的双跑比对：用**同一份**快照工具，分别喂当前工作树与基线
# 提交的 app/ 代码，逐字节 diff 两份 row_hash 快照。
#
#     backend/scripts/import_hash_parity.sh \
#         --baseline main -- \
#         --cmb      "$PWD/../data/招商对账单.pdf" \
#         --eastmoney "$PWD/../data/东财对账单.pdf" \
#         --ibkr-xlsx "$PWD/../data/ibkr导出.xlsx" \
#         --ibkr-activity "$PWD/../data/activity.csv"
#
# `--` 之后的参数原样透传给 scripts/import_hash_snapshot.py（四个输入类别各至
# 少一份，见该脚本的文档）。
#
# 为什么是一个脚本、而不是文档里的一串命令：那串命令**全程 fail-fast 不了**。
# 交互 shell 默认不因上一条命令失败而停止，而 `> before.json` 的重定向在命令
# 失败时仍会留下一个空文件；于是
#   - 任一次 python 跑挂了 → 空快照 vs 空快照 → `diff` 退出 0、零输出 → 假绿；
#   - `git checkout main` 挂了 → 两次都在同一分支上跑 → 两份非空快照天然相同
#     → 同样是零差异的假绿。
# 这里用 `set -euo pipefail` + 显式断言把这两条路都堵死，并且**不再切分支**：
# 基线跑在 `git worktree` 出来的独立目录里（工作树不动、无需 stash、也没有
# "脚本自身被 checkout 换掉"的隐患），只靠 PYTHONPATH 决定加载哪一侧的 app/。
#
# 环境变量（仅为 tests/test_import_hash_parity.py 的 shell 控制流回归而留）：
#   PARITY_PYTHON         解释器，默认 python3
#   PARITY_SNAPSHOT_TOOL  快照工具路径，默认 <repo>/backend/scripts/import_hash_snapshot.py
#   PARITY_KEEP           非空则保留临时目录（含两份快照与 diff）

set -euo pipefail

baseline=main
snapshot_args=()

usage() {
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --baseline)
            [ $# -ge 2 ] || { echo "--baseline 缺少取值" >&2; exit 2; }
            baseline=$2
            shift 2
            ;;
        --)
            shift
            snapshot_args=("$@")
            break
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数：$1（快照参数请放在 -- 之后）" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ ${#snapshot_args[@]} -eq 0 ]; then
    echo "❌ '--' 之后没有任何快照参数：四个输入类别各至少要给一份真实对账单。" >&2
    usage >&2
    exit 2
fi

python=${PARITY_PYTHON:-python3}
script_dir=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$script_dir" rev-parse --show-toplevel)
tool=${PARITY_SNAPSHOT_TOOL:-$repo/backend/scripts/import_hash_snapshot.py}

[ -f "$tool" ] || { echo "❌ 找不到快照工具：$tool" >&2; exit 1; }

base_sha=$(git -C "$repo" rev-parse --verify --quiet "${baseline}^{commit}") || {
    echo "❌ 基线 ref 解析失败：$baseline" >&2
    exit 1
}

# 工作树与基线逐字节一致时，两份快照必然相同——那个"零差异"什么都没证明。
# 结构上已经不可能再出现"checkout 失败导致两次跑在同一份代码上"，这里再兜一道。
if git -C "$repo" diff --quiet "$base_sha" --; then
    echo "❌ 工作树与基线 ${baseline} (${base_sha:0:8}) 完全一致，双跑无意义（必然零差异）。" >&2
    exit 1
fi

tmp=$(mktemp -d)
cleanup() {
    git -C "$repo" worktree remove --force "$tmp/baseline" >/dev/null 2>&1 || true
    git -C "$repo" worktree prune >/dev/null 2>&1 || true
    if [ -n "${PARITY_KEEP:-}" ]; then
        echo "# 临时目录保留在：$tmp" >&2
    else
        rm -rf "$tmp"
    fi
}
trap cleanup EXIT

# 两侧共用**同一份**工具：基线提交上未必有这个脚本（它往往就是本次新增的），
# 且工具本身的改动不该混进被比对的 hash 里。
cp "$tool" "$tmp/snap.py"

git -C "$repo" worktree add --detach "$tmp/baseline" "$base_sha" >/dev/null

run_snapshot() {  # $1=PYTHONPATH 指向的 backend 根 $2=输出文件 $3=标签
    echo "# ---- 快照：$3 ----" >&2
    if ! PYTHONPATH=$1 "$python" "$tmp/snap.py" "${snapshot_args[@]}" > "$2"; then
        echo "❌ $3 快照生成失败（见上方错误）。重定向已经留下一个空文件——" >&2
        echo "   正因为如此，这里必须立即中止：空快照 vs 空快照 diff 出来是零差异。" >&2
        exit 1
    fi
}

assert_usable() {  # $1=文件 $2=标签
    if [ ! -s "$1" ]; then
        echo "❌ $2 快照是空文件——不能拿它去 diff（空 vs 空 = 假的零差异）。" >&2
        exit 1
    fi
    if ! grep -q '"hashes"' "$1"; then
        echo "❌ $2 快照里一条 hash 都没有：$(head -c 200 "$1")" >&2
        exit 1
    fi
}

run_snapshot "$repo/backend" "$tmp/after.json" "after（当前工作树）"
assert_usable "$tmp/after.json" "after"

run_snapshot "$tmp/baseline/backend" "$tmp/before.json" "before（基线 ${baseline} ${base_sha:0:8}）"
assert_usable "$tmp/before.json" "before"

if diff -u "$tmp/before.json" "$tmp/after.json" > "$tmp/diff.txt"; then
    echo "✅ row_hash 零差异（before=${baseline} ${base_sha:0:8} vs after=当前工作树）"
    exit 0
fi

cat "$tmp/diff.txt" >&2
echo "❌ row_hash 有差异：HASH-CRITICAL 回归，或本次改动确实要改 hash（那就得升 PARSER_VERSION）。" >&2
exit 1
