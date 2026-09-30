"""部署配置三处同步守护：`config.py` ⇄ `docker-compose.yml` ⇄ `.env.example`。

容器里的进程**只读 compose `environment:` 下传的变量**（镜像不带 `.env`，
`backend/.dockerignore` 排除了它）。所以一个 Settings 字段只要没写进对应服务的
`environment:`，运维在 `.env` 里怎么改都不生效——而且不报错，静默用 config.py 的默认值。
2026-09 的审计在 `.env.example` 里找到 13 个这样的「看起来能调、实际不生效」的旋钮。

同一个镜像跑两个服务：
- `backend`（Web 进程 + 进程内 worker）：每个 Settings 字段都要下传，只有「只在采集器进程里
  读取」的字段例外（`COLLECTOR_ONLY`，逐条写理由，并由 AST 扫描证明 Web 侧确实不读）；
- `xueqiu-collector`（`manage.py xueqiu-collector` 常驻进程）：必填字段、采集器包里直接读取的
  每个 `settings.<字段>`、以及它依赖的基础设施字段（`COLLECTOR_INFRA`）都要下传。

两个服务都不许用 `env_file:`——那会把整份 `.env` 灌进容器，让「只读 environment:」这条
可审计的边界失效。

`.env.example` 一侧：每个变量（含注释掉的 `# VAR=` 示例）都有去处（Settings 字段、compose
里的 `${VAR` 插值，或只给 backup.sh / 构建期用的白名单）；反过来每个 Settings 字段都有一行说明。

compose 优先用 PyYAML 解析；CI 的 requirements 里没有 PyYAML，因此另有一个只认本仓
compose 形状的最小解析器，两者在 PyYAML 可用时交叉核对。
"""

import ast
import re
from pathlib import Path

import pytest

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
COLLECTOR_PACKAGE = BACKEND_DIR / "app" / "services" / "xueqiu_collector"
MANAGE_PY = BACKEND_DIR / "manage.py"

WEB_SERVICE = "backend"
COLLECTOR_SERVICE = "xueqiu-collector"

_COLLECTOR_ONLY_REASON = (
    "只在 xueqiu-collector 进程里读取（app/services/xueqiu_collector/）；"
    "Web 进程不抓取雪球，下传给 backend 也无人读"
)
# backend 服务刻意不下传的 Settings 字段：键 = 环境变量名，值 = 理由。
# 下面的 AST 测试证明 app/ 里采集器包以外的代码确实不读它们。
COLLECTOR_ONLY: dict[str, str] = {
    name: _COLLECTOR_ONLY_REASON
    for name in (
        "XUEQIU_COLLECTOR_PUSH_URL",
        "XUEQIU_COLLECTOR_MIN_DELAY_SECONDS",
        "XUEQIU_COLLECTOR_MAX_DELAY_SECONDS",
        "XUEQIU_COLLECTOR_TIMEOUT_SECONDS",
        "XUEQIU_COLLECTOR_MAX_AUTHORS_PER_RUN",
        "XUEQIU_COLLECTOR_AUTHOR_GAP_MIN_SECONDS",
        "XUEQIU_COLLECTOR_AUTHOR_GAP_MAX_SECONDS",
        "XUEQIU_COLLECTOR_MONITOR_DAYS",
        "XUEQIU_COLLECTOR_PROFILE_PAGES",
        "XUEQIU_COLLECTOR_MAX_COMMENT_PAGES",
        "XUEQIU_COLLECTOR_STALE_POST_DAYS",
        "XUEQIU_COLLECTOR_STALE_COMMENT_PAGES",
        "XUEQIU_COLLECTOR_RESCAN_COOLDOWN_HOURS",
        "XUEQIU_COLLECTOR_MAX_WAF_HITS",
        "XUEQIU_COLLECTOR_HEARTBEAT_FILE",
        "XUEQIU_COLLECTOR_SYMBOL_COUNT",
        "XUEQIU_COLLECTOR_SYMBOLS_RETRY_MINUTES",
        "XUEQIU_COLLECTOR_SYMBOLS_MAX_ATTEMPTS",
    )
}

# 采集器进程经由基础设施模块间接读取、采集器包里不直接出现的字段。
COLLECTOR_INFRA: dict[str, str] = {
    "DATABASE_URL": "app.database 建连（采集器读写同一个库）",
    "DISPLAY_TIMEZONE": "core.timeutil：created_at 文本按业务时区格式化、每日按标的轮次的「今天」",
}

# `.env.example` 里既不是 Settings 字段、也不被 compose 插值引用的变量：只给宿主脚本或构建用。
NON_SETTINGS_ENV_VARS = {
    "BACKUP_DIR": "backup.sh：备份目录",
    "BACKUP_MODE": "backup.sh：非交互模式",
    "BACKUP_TABLES": "backup.sh：表级备份的表清单",
    "BACKUP_PG_TOOL": "backup.sh：强制本机或容器 pg_dump",
    "BACKUP_PG_IMAGE": "backup.sh：容器模式使用的 postgres 镜像",
    "BACKUP_DOCKER_NETWORK": "backup.sh：容器模式的 docker 网络",
    "APP_BASE_URL": "backup.sh：Excel 导出访问地址",
    "APP_CA_CERT": "backup.sh：Excel 导出校验私有 CA",
    "INVESTMENT_TRACKER_TOKEN": "backup.sh：Excel 导出用的 Bearer token",
    "BACKUP_NOTIFY": "backup.sh：失败时经 backend 容器推送告警（manage.py notify）",
}

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def settings_env_names() -> set[str]:
    # 本项目的 Settings 不设 env_prefix / alias：环境变量名就是字段名大写
    assert not Settings.model_config.get("env_prefix")
    names = set()
    for name, field in Settings.model_fields.items():
        assert field.alias is None and field.validation_alias is None, name
        names.add(name.upper())
    return names


def required_settings_env_names() -> set[str]:
    return {name.upper() for name, field in Settings.model_fields.items() if field.is_required()}


def settings_reads(paths) -> set[str]:
    """AST 扫描 `settings.<字段>` 的属性读取，返回环境变量名集合。"""
    fields = set(Settings.model_fields)
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
                and node.attr in fields
            ):
                found.add(node.attr.upper())
    return found


def collector_code_paths() -> list[Path]:
    return sorted(COLLECTOR_PACKAGE.rglob("*.py"))


def web_code_paths() -> list[Path]:
    return sorted(
        path
        for path in (BACKEND_DIR / "app").rglob("*.py")
        if COLLECTOR_PACKAGE not in path.parents
    )


def manage_collector_reads() -> set[str]:
    """manage.py 中名字含 xueqiu 的函数里读取的 settings 字段。"""
    tree = ast.parse(MANAGE_PY.read_text(encoding="utf-8"))
    fields = set(Settings.model_fields)
    found = set()
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef) and "xueqiu" in func.name:
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "settings"
                    and node.attr in fields
                ):
                    found.add(node.attr.upper())
    return found


# --------------------------------------------------------------------------- #
# compose 解析
# --------------------------------------------------------------------------- #


def _env_key(entry: str) -> str:
    return entry.split("=", 1)[0].strip()


def _service_lines(text: str, service: str) -> list[tuple[int, str]]:
    """最小解析器：`services:` 下某个服务块的 (缩进, 去空白行) 列表（不含服务名那一行）。"""
    lines: list[tuple[int, str]] = []
    state = "top"
    service_indent = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if state == "top":
            if indent == 0 and stripped == "services:":
                state = "services"
            continue
        if indent == 0:
            break  # 离开 services 块
        if state == "services":
            if stripped == f"{service}:":
                state, service_indent = "service", indent
            continue
        if indent <= service_indent:
            break  # 下一个服务
        lines.append((indent, stripped))
    return lines


def service_keys_minimal(text: str, service: str) -> set[str]:
    lines = _service_lines(text, service)
    if not lines:
        return set()
    top = min(indent for indent, _ in lines)
    return {line.split(":", 1)[0] for indent, line in lines if indent == top and ":" in line}


def environment_keys_minimal(text: str, service: str) -> set[str]:
    keys: set[str] = set()
    env_indent = None
    for indent, line in _service_lines(text, service):
        if env_indent is None:
            if line == "environment:":
                env_indent = indent
            continue
        if indent <= env_indent:
            env_indent = None
            if line == "environment:":
                env_indent = indent
            continue
        if line.startswith("- "):
            keys.add(_env_key(line[2:].strip().strip("'\"")))
        elif ":" in line:
            keys.add(line.split(":", 1)[0].strip().strip("'\""))
    return keys


def _yaml_service(text: str, service: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(text)["services"].get(service) or {}


def environment_keys_yaml(text: str, service: str) -> set[str]:
    environment = _yaml_service(text, service).get("environment") or []
    if isinstance(environment, dict):
        return set(environment)
    return {_env_key(str(entry)) for entry in environment}


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def environment_keys(text: str, service: str) -> set[str]:
    if _has_yaml():
        return environment_keys_yaml(text, service)
    return environment_keys_minimal(text, service)


def service_keys(text: str, service: str) -> set[str]:
    if _has_yaml():
        return set(_yaml_service(text, service))
    return service_keys_minimal(text, service)


def compose_interpolated_names(text: str) -> set[str]:
    return set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))


def env_example_names(text: str) -> set[str]:
    """`.env.example` 里的变量名：`VAR=` 与注释掉的 `# VAR=` 示例都算。"""
    names = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if _ENV_NAME_RE.match(name):
            names.add(name)
    return names


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example_text() -> str:
    return ENV_EXAMPLE_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 解析器自检
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("service", [WEB_SERVICE, COLLECTOR_SERVICE])
def test_minimal_parser_agrees_with_yaml(compose_text, service):
    assert environment_keys_minimal(compose_text, service) == environment_keys_yaml(
        compose_text, service
    )
    assert service_keys_minimal(compose_text, service) == set(_yaml_service(compose_text, service))


def test_minimal_parser_handles_mapping_form_and_next_service():
    text = (
        "services:\n"
        "  backend:\n"
        "    image: x\n"
        "    environment:\n"
        "      FOO: ${FOO:-1}\n"
        "      # comment\n"
        '      BAR: "2"\n'
        "    restart: always\n"
        "  xueqiu-collector:\n"
        "    environment:\n"
        "      - ONLY_COLLECTOR=1\n"
        "    env_file: .env\n"
    )
    assert environment_keys_minimal(text, "backend") == {"FOO", "BAR"}
    assert environment_keys_minimal(text, "xueqiu-collector") == {"ONLY_COLLECTOR"}
    assert service_keys_minimal(text, "xueqiu-collector") == {"environment", "env_file"}


# --------------------------------------------------------------------------- #
# backend（Web 进程）
# --------------------------------------------------------------------------- #


def test_every_setting_is_passed_to_backend_container(compose_text):
    passed = environment_keys(compose_text, WEB_SERVICE)
    missing = sorted(settings_env_names() - passed - set(COLLECTOR_ONLY))
    assert not missing, (
        "这些 Settings 字段没写进 docker-compose.yml backend 的 environment:，"
        "在 .env 里改它们对容器不生效（补成裸键 `- NAME` 即可，默认值只留在 config.py）："
        f"{missing}"
    )


def test_collector_only_allowlist_is_truthful(compose_text):
    passed_web = environment_keys(compose_text, WEB_SERVICE)
    passed_collector = environment_keys(compose_text, COLLECTOR_SERVICE)
    fields = settings_env_names()
    for name, reason in COLLECTOR_ONLY.items():
        assert reason.strip(), name
        assert name in fields, f"白名单里的 {name} 已不是 Settings 字段"
        assert name not in passed_web, f"{name} 已经下传给 backend，从 COLLECTOR_ONLY 删掉"
        assert name in passed_collector, f"{name} 只给采集器用，但 xueqiu-collector 没下传"
    # Web 侧真的不读：一旦 app/ 里采集器包以外的代码读了某个字段，它就必须下传给 backend
    web_reads = settings_reads(web_code_paths()) & set(COLLECTOR_ONLY)
    assert not web_reads, (
        f"这些字段被 Web 进程的代码读取，不能再算「只给采集器」，要下传给 backend：{sorted(web_reads)}"
    )


def test_backend_environment_keys_are_settings_fields(compose_text):
    unknown = sorted(environment_keys(compose_text, WEB_SERVICE) - settings_env_names())
    assert not unknown, f"compose backend environment: 里有 config.py 不认识的键：{unknown}"


# --------------------------------------------------------------------------- #
# xueqiu-collector（采集器进程）
# --------------------------------------------------------------------------- #


def test_collector_receives_every_setting_it_reads(compose_text):
    passed = environment_keys(compose_text, COLLECTOR_SERVICE)
    assert passed, "docker-compose.yml 里没有 xueqiu-collector 服务或它没有 environment:"
    needed = (
        required_settings_env_names()
        | settings_reads(collector_code_paths())
        | manage_collector_reads()
        | set(COLLECTOR_INFRA)
    )
    missing = sorted(needed - passed)
    assert not missing, (
        "采集器进程读取了这些 Settings 字段，但 xueqiu-collector 的 environment: 没下传"
        f"（改 .env 对采集器不生效，或必填项缺失进程起不来）：{missing}"
    )


def test_collector_scan_actually_sees_collector_settings():
    # 防止扫描失效（路径改名后扫到空集）而让上一条断言空转
    reads = settings_reads(collector_code_paths())
    assert "XUEQIU_COLLECTOR_MIN_DELAY_SECONDS" in reads
    assert "XUEQIU_COOKIE_FILE" in reads
    assert "XUEQIU_COLLECTOR_HEALTH_MAX_AGE_MINUTES" in manage_collector_reads()


def test_collector_infra_allowlist_is_truthful():
    fields = settings_env_names()
    for name, reason in COLLECTOR_INFRA.items():
        assert reason.strip() and name in fields, name


def test_collector_environment_keys_are_settings_fields(compose_text):
    unknown = sorted(environment_keys(compose_text, COLLECTOR_SERVICE) - settings_env_names())
    assert not unknown, (
        f"compose xueqiu-collector environment: 里有 config.py 不认识的键：{unknown}"
    )


@pytest.mark.parametrize("service", [WEB_SERVICE, COLLECTOR_SERVICE])
def test_services_only_read_explicit_environment(compose_text, service):
    keys = service_keys(compose_text, service)
    assert "environment" in keys, service
    assert "env_file" not in keys, (
        f"{service} 用了 env_file:，整份 .env 会灌进容器——只允许经 environment: 显式下传"
    )


def _secrets_mounts(text: str, service: str) -> list[str]:
    """服务里挂到容器 /app/secrets 的 volume 条目（短语法字符串）。"""
    entries: list[str] = []
    for _indent, line in _service_lines(text, service):
        if line.startswith("- ") and ":/app/secrets" in line:
            entries.append(line[2:].strip().strip("'\""))
    return entries


def test_cookie_dir_writable_only_for_backend(compose_text):
    """界面更新 Cookie 只由 backend 写：它的 /app/secrets 可写，采集器保持只读。

    采集器只读 Cookie；给它写权限只会扩大被攻破时能改凭证的进程面。backend 挂载若误加回
    `:ro`，界面更新会在运行时报「目录不可写」——这里在 CI 就拦下。
    """
    backend = _secrets_mounts(compose_text, WEB_SERVICE)
    collector = _secrets_mounts(compose_text, COLLECTOR_SERVICE)
    assert backend == ["${XUEQIU_COOKIE_HOST_DIR:-./backend/secrets}:/app/secrets"], backend
    assert collector == ["${XUEQIU_COOKIE_HOST_DIR:-./backend/secrets}:/app/secrets:ro"], collector
    if _has_yaml():
        assert backend == [
            v for v in _yaml_service(compose_text, WEB_SERVICE)["volumes"] if ":/app/secrets" in v
        ]
        assert collector == [
            v
            for v in _yaml_service(compose_text, COLLECTOR_SERVICE)["volumes"]
            if ":/app/secrets" in v
        ]


# --------------------------------------------------------------------------- #
# .env.example
# --------------------------------------------------------------------------- #


def test_env_example_variables_all_have_a_destination(compose_text, env_example_text):
    known = (
        settings_env_names() | compose_interpolated_names(compose_text) | set(NON_SETTINGS_ENV_VARS)
    )
    orphans = sorted(env_example_names(env_example_text) - known)
    assert not orphans, (
        ".env.example 里的这些变量没有任何东西读取（不是 Settings 字段、compose 没引用、"
        f"也不在 backup/构建白名单）：{orphans}"
    )


def test_every_setting_is_documented_in_env_example(env_example_text):
    undocumented = sorted(settings_env_names() - env_example_names(env_example_text))
    assert not undocumented, f".env.example 缺少这些 Settings 字段的说明：{undocumented}"


def test_compose_interpolations_are_documented_in_env_example(compose_text, env_example_text):
    undocumented = sorted(
        compose_interpolated_names(compose_text) - env_example_names(env_example_text)
    )
    assert not undocumented, f"compose 引用了但 .env.example 没写的变量：{undocumented}"
