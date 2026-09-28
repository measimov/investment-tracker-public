"""根目录 `backup.sh` 的 shell 控制流回归（真起子进程跑 bash，不连真实数据库）。

覆盖三件事：
1. `--prune` 的选择规则——只删超出保留份数的**完整备份**及其 `.sha256`，表级备份与
   `.partial` 残留默认不碰，`--include-partial` 才处理且不删比最新完整备份更新的残留；
2. `.sha256` 记裸文件名，从备份目录内 `sha256sum -c` 能通过（换目录/换机器校验）；
3. 数据库备份的 `.partial → pg_dump 退出 0 → 非空 → pg_restore 读检 → 改名` 纪律，
   本机客户端与一次性容器客户端两条路径都走（pg_dump/pg_restore/docker 均为打桩）；
4. 本机 pg_dump 主版本低于数据库（server version mismatch）时自动改用容器重跑，
   强制 `BACKUP_PG_TOOL=local` 或其他失败原因则不重跑。
"""

import hashlib
import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")

FAKE_PG_DUMP = """#!/usr/bin/env bash
# 打桩 pg_dump：按 FAKE_DUMP_MODE 写出内容 / 空文件 / 失败；参数记进 FAKE_PG_LOG
[ -n "${FAKE_PG_LOG:-}" ] && printf '%s\\n' "$*" >> "$FAKE_PG_LOG"
out=""
for arg in "$@"; do
    case "$arg" in --file=*) out="${arg#--file=}" ;; esac
done
case "${FAKE_DUMP_MODE:-ok}" in
    ok) printf 'PGDMP-fake-dump' > "$out" ;;
    empty) : > "$out" ;;
    fail) printf 'half' > "$out"; exit 1 ;;
    mismatch)
        printf 'half' > "$out"
        echo "pg_dump: error: server version: 16.4; pg_dump version: 14.11" >&2
        echo "pg_dump: error: aborting because of server version mismatch" >&2
        exit 1 ;;
esac
"""

FAKE_PG_RESTORE = """#!/usr/bin/env bash
# 打桩 pg_restore：读检要求最后一个参数是存在且非空的文件
last="${@: -1}"
[ "${FAKE_RESTORE_MODE:-ok}" = ok ] && [ -s "$last" ]
"""

# 打桩 docker：没有 compose 插件；`run` 按参数模拟一次性 postgres 容器。
# 同时断言连接串不出现在进程参数里（只能经 -e 从环境传入）。
FAKE_DOCKER = """#!/usr/bin/env python3
import os, sys, pathlib
args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_DOCKER_LOG"])
with log.open("a") as fh:
    fh.write(" ".join(args) + "\\n")
if not args or args[0] != "run":
    sys.exit(1)
url = os.environ.get("DATABASE_URL", "")
if not url or any(url in a for a in args):
    print("连接串必须经环境变量传入", file=sys.stderr)
    sys.exit(3)
mount = None
for i, a in enumerate(args):
    if a == "-v":
        host, _, rest = args[i + 1].partition(":")
        mount = pathlib.Path(host)
if "pg_restore" in args:
    target = mount / pathlib.Path(args[-1]).name
    sys.exit(0 if target.exists() and target.stat().st_size > 0 else 1)
# pg_dump：sh -c '<script>' sh <文件名> [--table=...]
script_at = next(i for i, a in enumerate(args) if a.startswith("umask"))
(mount / args[script_at + 2]).write_bytes(b"PGDMP-fake-docker-dump")
"""


def _write_exe(path: pathlib.Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    base = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "BACKUP_DIR": str(backups),
        "LC_ALL": "C",
    }
    return {"env": base, "bin": fake_bin, "backups": backups, "tmp": tmp_path}


def _run(ctx, *args, extra_env=None):
    env = dict(ctx["env"])
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        cwd=ctx["tmp"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _touch(directory: pathlib.Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text(name)


def _names(directory: pathlib.Path) -> set:
    return {p.name for p in directory.iterdir()}


FULL = [
    "investment_20260101_010000.dump",
    "investment_20260201_010000.dump",
    "investment_20260301_010000.dump",
    "investment_20260401_010000.dump",
]
TABLES = [
    "investment_tables_20260110_010000.dump",
    "investment_tables_20260210_010000.dump",
    "investment_tables_20260310_010000.dump",
]
OLD_PARTIAL = "investment_20260315_010000.dump.partial"
NEW_PARTIAL = "investment_20260501_010000.dump.partial"
OTHERS = [
    "transactions_20260101_010000.xlsx",
    "transactions_20260101_010000.xlsx.sha256",
    "notes.txt",
    "investment_manual_copy.dump",
]


@pytest.fixture
def populated(env):
    backups = env["backups"]
    _touch(backups, *FULL, *(f"{n}.sha256" for n in FULL))
    _touch(backups, *TABLES, *(f"{n}.sha256" for n in TABLES))
    _touch(backups, OLD_PARTIAL, NEW_PARTIAL, *OTHERS)
    return env


def test_prune_keeps_newest_full_dumps_only(populated):
    before = _names(populated["backups"])
    result = _run(populated, "--prune", "--keep", "2")
    assert result.returncode == 0, result.stderr

    removed = before - _names(populated["backups"])
    assert removed == {
        FULL[0],
        f"{FULL[0]}.sha256",
        FULL[1],
        f"{FULL[1]}.sha256",
    }
    for name in removed:
        assert name in result.stdout  # 删了什么都要打印出来
    # 未完成残留只提示、不删
    assert OLD_PARTIAL in result.stdout and NEW_PARTIAL in result.stdout


def test_prune_default_keep_is_two(populated):
    result = _run(populated, "--prune")
    assert result.returncode == 0, result.stderr
    remaining_full = sorted(n for n in _names(populated["backups"]) if n in FULL)
    assert remaining_full == FULL[-2:]


def test_prune_dry_run_deletes_nothing(populated):
    before = _names(populated["backups"])
    result = _run(populated, "--prune", "--keep", "1", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert _names(populated["backups"]) == before
    assert f"将删除: {FULL[0]}" in result.stdout


def test_prune_include_partial(populated):
    before = _names(populated["backups"])
    result = _run(populated, "--prune", "--keep", "2", "--include-partial")
    assert result.returncode == 0, result.stderr

    removed = before - _names(populated["backups"])
    assert removed == {
        FULL[0],
        f"{FULL[0]}.sha256",
        FULL[1],
        f"{FULL[1]}.sha256",
        TABLES[0],
        f"{TABLES[0]}.sha256",
        OLD_PARTIAL,  # 早于最新完整备份（0401）
    }
    # 比最新完整备份更新的 .partial 可能是正在进行的备份，保留
    assert NEW_PARTIAL in _names(populated["backups"])


@pytest.mark.parametrize(
    "args",
    [
        ("--prune", "--keep", "0"),
        ("--prune", "--keep", "abc"),
        ("--prune", "--keep"),
        ("--keep", "3"),
        ("--dry-run",),
        ("--bogus",),
    ],
)
def test_invalid_arguments_are_rejected(populated, args):
    before = _names(populated["backups"])
    result = _run(populated, *args)
    assert result.returncode == 2
    assert _names(populated["backups"]) == before


def _install_local_pg(ctx):
    _write_exe(ctx["bin"] / "pg_dump", FAKE_PG_DUMP)
    _write_exe(ctx["bin"] / "pg_restore", FAKE_PG_RESTORE)


def _assert_verified_backup(backups: pathlib.Path, content: bytes, pattern="investment_*.dump"):
    dumps = sorted(backups.glob(pattern))
    assert len(dumps) == 1
    dump = dumps[0]
    assert not list(backups.glob("*.partial"))
    assert dump.read_bytes() == content

    checksum_line = (backups / f"{dump.name}.sha256").read_text()
    digest = hashlib.sha256(content).hexdigest()
    # 裸文件名：不带目录，换个目录/机器也能校验
    assert checksum_line == f"{digest}  {dump.name}\n"
    if shutil.which("sha256sum"):
        check = subprocess.run(
            ["sha256sum", "-c", f"{dump.name}.sha256"],
            cwd=backups,
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr


def test_local_backup_writes_verified_dump_with_bare_sha_filename(env):
    _install_local_pg(env)
    result = _run(
        env,
        extra_env={"BACKUP_MODE": "postgres", "DATABASE_URL": "postgresql://u:p@db:5432/x"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_verified_backup(env["backups"], b"PGDMP-fake-dump")


def test_table_level_backup_uses_tables_prefix_and_table_args(env):
    _install_local_pg(env)
    log = env["tmp"] / "pg.log"
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "BACKUP_TABLES": "security_profile_data alembic_version other.t",
            "FAKE_PG_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_verified_backup(env["backups"], b"PGDMP-fake-dump", pattern="investment_tables_*.dump")
    assert not list(env["backups"].glob("investment_2*.dump"))
    call = log.read_text()
    assert "--table=public.security_profile_data" in call
    assert "--table=public.alembic_version" in call
    assert "--table=other.t" in call


def test_table_level_backup_rejects_unsafe_names(env):
    _install_local_pg(env)
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "BACKUP_TABLES": "users;drop",
        },
    )
    assert result.returncode != 0
    assert not list(env["backups"].iterdir())


@pytest.mark.parametrize(
    "mode_env",
    [
        {"FAKE_DUMP_MODE": "fail"},
        {"FAKE_DUMP_MODE": "empty"},
        {"FAKE_RESTORE_MODE": "fail"},
    ],
)
def test_local_backup_failure_keeps_partial_and_never_renames(env, mode_env):
    _install_local_pg(env)
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            **mode_env,
        },
    )
    assert result.returncode != 0
    names = _names(env["backups"])
    assert not any(n.endswith(".dump") or n.endswith(".sha256") for n in names)
    assert any(n.endswith(".dump.partial") for n in names)


def test_backup_then_prune(env):
    _install_local_pg(env)
    _touch(env["backups"], *FULL, *(f"{n}.sha256" for n in FULL))
    result = _run(
        env,
        "--prune",
        "--keep",
        "1",
        extra_env={"BACKUP_MODE": "postgres", "DATABASE_URL": "postgresql://u:p@db:5432/x"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    dumps = sorted(p.name for p in env["backups"].glob("investment_*.dump"))
    assert len(dumps) == 1 and dumps[0] not in FULL  # 只剩刚生成的那份


def test_docker_client_backup_uses_one_off_postgres_container(env):
    _write_exe(env["bin"] / "docker", FAKE_DOCKER)
    log = env["tmp"] / "docker.log"
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "BACKUP_PG_TOOL": "docker",
            "DATABASE_URL": "postgresql://u:secret@db:5432/x",
            "FAKE_DOCKER_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_verified_backup(env["backups"], b"PGDMP-fake-docker-dump")

    calls = [line for line in log.read_text().splitlines() if line.startswith("run ")]
    assert len(calls) == 2  # pg_dump + pg_restore 读检
    dump_call, restore_call = calls
    for call in calls:
        assert "postgres:16" in call
        assert "secret" not in call
    assert "--network host" in dump_call and "-e DATABASE_URL" in dump_call
    assert "--table" not in dump_call
    assert "pg_restore --exit-on-error --file=/dev/null" in restore_call
    assert ":/backups:ro" in restore_call


def test_docker_client_honours_image_and_network_overrides(env):
    _write_exe(env["bin"] / "docker", FAKE_DOCKER)
    log = env["tmp"] / "docker.log"
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "BACKUP_PG_TOOL": "docker",
            "BACKUP_PG_IMAGE": "postgres:17",
            "BACKUP_DOCKER_NETWORK": "proj_default",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "FAKE_DOCKER_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    dump_call = next(line for line in log.read_text().splitlines() if line.startswith("run "))
    assert "--network proj_default" in dump_call and "postgres:17" in dump_call


def test_backup_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_docker_client_table_level_backup(env):
    _write_exe(env["bin"] / "docker", FAKE_DOCKER)
    log = env["tmp"] / "docker.log"
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "BACKUP_PG_TOOL": "docker",
            "BACKUP_TABLES": "alembic_version",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "FAKE_DOCKER_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_verified_backup(
        env["backups"], b"PGDMP-fake-docker-dump", pattern="investment_tables_*.dump"
    )
    dump_call = next(line for line in log.read_text().splitlines() if line.startswith("run "))
    assert dump_call.endswith("--table=public.alembic_version")


def test_local_version_mismatch_falls_back_to_docker(env):
    """#195：宿主 pg_dump 比数据库旧 → 自动改用一次性容器，不必手工加 BACKUP_PG_TOOL。"""
    _install_local_pg(env)
    _write_exe(env["bin"] / "docker", FAKE_DOCKER)
    log = env["tmp"] / "docker.log"
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "DATABASE_URL": "postgresql://u:secret@db:5432/x",
            "FAKE_DUMP_MODE": "mismatch",
            "FAKE_DOCKER_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "改用一次性 postgres:16 容器重试" in result.stderr
    # 本机那次的半截 .partial 已清掉，产物来自容器且经容器 pg_restore 读检
    _assert_verified_backup(env["backups"], b"PGDMP-fake-docker-dump")
    calls = [line for line in log.read_text().splitlines() if line.startswith("run ")]
    assert len(calls) == 2
    assert "pg_restore" in calls[1]
    assert all("secret" not in call for call in calls)


@pytest.mark.parametrize(
    "extra",
    [
        {"FAKE_DUMP_MODE": "mismatch", "BACKUP_PG_TOOL": "local"},  # 强制本机：不擅自换客户端
        {"FAKE_DUMP_MODE": "fail"},  # 其他失败原因：不是版本问题，换容器也没用
    ],
)
def test_local_failure_without_mismatch_or_forced_local_does_not_retry(env, extra):
    _install_local_pg(env)
    _write_exe(env["bin"] / "docker", FAKE_DOCKER)
    log = env["tmp"] / "docker.log"
    result = _run(
        env,
        extra_env={
            "BACKUP_MODE": "postgres",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "FAKE_DOCKER_LOG": str(log),
            **extra,
        },
    )
    assert result.returncode != 0
    assert not log.exists() or not [
        line for line in log.read_text().splitlines() if line.startswith("run ")
    ]
    names = _names(env["backups"])
    assert any(n.endswith(".dump.partial") for n in names)
    assert not any(n.endswith(".dump") for n in names)


# --------------------------------------------------------------------------- #
# BACKUP_NOTIFY=1：失败推送告警、成功标记恢复；尽力而为，不改变退出码
# --------------------------------------------------------------------------- #
# 打桩 docker-compose（独立二进制形态）：记录参数；`version` 恒成功，其余按 FAKE_COMPOSE_EXIT
FAKE_COMPOSE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_COMPOSE_LOG"
[ "$1" = version ] && exit 0
exit "${FAKE_COMPOSE_EXIT:-0}"
"""


def _install_notify_stubs(ctx):
    _install_local_pg(ctx)
    # 假 docker 没有 compose 插件（`docker compose version` 失败）→ 落到 docker-compose
    _write_exe(ctx["bin"] / "docker", FAKE_DOCKER)
    _write_exe(ctx["bin"] / "docker-compose", FAKE_COMPOSE)
    return ctx["tmp"] / "compose.log"


def _notify_calls(log: pathlib.Path) -> list:
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if "manage.py notify" in line]


def _backup_env(log, **extra):
    return {
        "BACKUP_MODE": "postgres",
        "DATABASE_URL": "postgresql://u:p@db:5432/x",
        "FAKE_COMPOSE_LOG": str(log),
        "FAKE_DOCKER_LOG": str(log.parent / "docker.log"),
        **extra,
    }


def test_backup_notify_resolves_on_success(env):
    log = _install_notify_stubs(env)
    result = _run(env, extra_env=_backup_env(log, BACKUP_NOTIFY="1"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _notify_calls(log) == [
        "exec -T backend python manage.py notify --resolve --key backup"
    ]


@pytest.mark.parametrize("compose_exit", ["0", "1"])
def test_backup_notify_raises_on_failure_and_keeps_exit_code(env, compose_exit):
    log = _install_notify_stubs(env)
    result = _run(
        env,
        extra_env=_backup_env(
            log, BACKUP_NOTIFY="1", FAKE_DUMP_MODE="fail", FAKE_COMPOSE_EXIT=compose_exit
        ),
    )
    assert result.returncode == 1  # 与不开通知时相同：pg_dump 失败的退出码
    calls = _notify_calls(log)
    assert len(calls) == 1
    assert "notify --key backup --severity critical --title 数据库备份失败" in calls[0]
    assert "退出码 1" in calls[0]
    if compose_exit != "0":
        assert "告警通知未送达" in result.stderr


def test_backup_notify_failure_never_breaks_a_successful_backup(env):
    log = _install_notify_stubs(env)
    result = _run(env, extra_env=_backup_env(log, BACKUP_NOTIFY="1", FAKE_COMPOSE_EXIT="1"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "告警通知未送达" in result.stderr
    _assert_verified_backup(env["backups"], b"PGDMP-fake-dump")


def test_backup_notify_is_opt_in_and_skips_prune_only_runs(env):
    log = _install_notify_stubs(env)
    assert _run(env, extra_env=_backup_env(log)).returncode == 0
    assert _run(env, extra_env=_backup_env(log, FAKE_DUMP_MODE="fail")).returncode != 0
    assert _notify_calls(log) == []
    # 只清理不是一次备份：即使开了通知也不发
    only_prune = _run(
        env, "--prune", extra_env={"BACKUP_NOTIFY": "1", "FAKE_COMPOSE_LOG": str(log)}
    )
    assert only_prune.returncode == 0, only_prune.stdout + only_prune.stderr
    assert _notify_calls(log) == []
