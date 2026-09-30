"""配置契约静态检查：config.py ↔ .env.example ↔ docker-compose.yml 不再漂移。

三方各自的职责：
- `app/config.py` 的 Settings 是权威字段集；
- `.env.example` 必须覆盖全部字段（它是多数变量的唯一文档）；
- docker-compose 只透传需要在容器环境可调的变量，但凡透传的必须存在于
  示例文件，且文档化过的开关（如 BACKGROUND_WORKER_ENABLED）必须真的透传，
  否则用户在根 .env 里设置了也不生效。
"""

import re
from pathlib import Path

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


# `.env.example` 的条目：`KEY=value`，或注释掉的 `# KEY=value`——带默认值的旋钮刻意写成
# 注释（取消注释才覆盖），这样 `cp .env.example .env` 不会把今天的默认值钉死在部署里。
_EXAMPLE_ENTRY_RE = re.compile(r"^(?:#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")


def _example_entries() -> dict[str, str]:
    entries = {}
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        match = _EXAMPLE_ENTRY_RE.match(line.strip())
        if match:
            # 注释行的行内说明（`# KEY=v   # 说明`）不属于值
            entries[match.group(1)] = re.split(r"\s+#", match.group(2), maxsplit=1)[0].strip()
    return entries


def _env_example_keys() -> set[str]:
    return set(_example_entries())


def _compose_service_blocks() -> list[str]:
    """同一镜像的两个服务：backend（Web）与 xueqiu-collector（采集器常驻进程）。"""
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    backend_block = text.split("\n  backend:", 1)[1].split("\n  frontend:", 1)[0]
    collector_block = text.split("\n  xueqiu-collector:", 1)[1].split("\nsecrets:", 1)[0]
    return [backend_block, collector_block]


def _compose_env_keys(block: str) -> set[str]:
    # 两种写法都算透传：`KEY=${KEY:-默认}` 与裸键 `KEY`（值取宿主同名变量）。
    # 裸键是更好的默认：compose 里写 `:-默认` 会造出第二个默认值来源，#128 的
    # LLM_REPORT_MAX_OUTPUT_TOKENS 就是被那种写法把 config.py 的修复挡掉的。
    return set(re.findall(r"^\s+-\s+([A-Z][A-Z0-9_]*)(?:=|\s*$)", block, re.M))


def _compose_backend_env_keys() -> set[str]:
    return _compose_env_keys(_compose_service_blocks()[0])


def _compose_all_env_keys() -> set[str]:
    return set().union(*(_compose_env_keys(block) for block in _compose_service_blocks()))


def test_env_example_covers_every_settings_field():
    settings_fields = {name.upper() for name in Settings.model_fields}
    missing = settings_fields - _env_example_keys()
    assert not missing, f".env.example 缺少 config.py 字段: {sorted(missing)}"


def test_compose_passthrough_vars_exist_in_env_example():
    unknown = _compose_all_env_keys() - _env_example_keys()
    assert not unknown, f"docker-compose 透传了示例文件没有的变量: {sorted(unknown)}"


def test_compose_passes_documented_background_and_llm_switches():
    compose_keys = _compose_backend_env_keys()
    for key in (
        "BACKGROUND_WORKER_ENABLED",
        "BACKGROUND_JOB_POLL_SECONDS",
        "LLM_REPORT_API_KEY",
        "PRICE_REFRESH_FRESHNESS_SECONDS",
        "TUSHARE_GLOBAL_MIN_INTERVAL_SECONDS",
        # 安全基线：compose 只做变量插值，不会把根 .env 的键自动注入容器。
        # 漏透传的话用户按文档设了值、后端却静默用代码默认值——安全开关上
        # 这种"设了等于没设"比直接报错危险得多。
        "SESSION_ABSOLUTE_MAX_HOURS",
        "TRUST_PROXY_HEADERS",
        "REQUIRE_HTTPS",
        "ENABLE_DOCS",
    ):
        assert key in compose_keys, f"docker-compose 未透传已文档化的 {key}"


def test_no_stale_capability_claims_in_docs_or_ui_copy():
    """能力文案防漂移：现金闭环已入账的能力，文档与前端提示不得再声称未支持。

    这是一个短语黑名单 tripwire（非完备语义检查）：历史上 README、专项文档与
    导入对话框曾三方互相矛盾（评审两轮抓出），列入曾出错的表述防止回潮。
    """
    stale_phrases = (
        "外汇、利息暂不导入",
        "现金类记录暂不导入",
        "利息和完整现金活动尚未入账",
    )
    files = (
        "SAMPLE_DATA.md",
        "BROKER_DATA_SOURCES.md",
        "README.md",
        "frontend/src/views/Transactions.vue",
    )
    for relative in files:
        text = (REPO_ROOT / relative).read_text()
        for phrase in stale_phrases:
            assert phrase not in text, f"{relative} 仍包含过时能力表述: {phrase}"


def _config_default(key: str):
    return Settings.model_fields[key.lower()].default


def _bare_passthrough_keys() -> set[str]:
    backend_block = "\n".join(_compose_service_blocks())
    return set(re.findall(r"^\s+-\s+([A-Z][A-Z0-9_]*)\s*$", backend_block, re.M))


def _env_example_values() -> dict[str, str]:
    return _example_entries()


def _same_value(example: str, default) -> bool:
    example = example.strip().strip("'\"")
    if isinstance(default, bool):
        return example.lower() == str(default).lower()
    if isinstance(default, (int, float)):
        try:
            return float(example) == float(default)
        except ValueError:
            return False
    return example == str(default)


def test_bare_passthrough_examples_match_config_defaults():
    """裸键透传项的示例值必须等于 config.py 的默认值。

    裸键的意义是"未设置就用代码默认值"，但文档教用户 `cp .env.example .env`
    —— 示例文件里写了另一个值，等于给同一个配置立了第二个默认值来源，而且
    是**悄悄**生效的。#128 就是 LLM_REPORT_MAX_OUTPUT_TOKENS 走了这条路：
    config.py 把上限提到 16384，Docker 部署仍恒为 8192，长报告继续被截断。
    """
    example = _env_example_values()
    drift = []
    for key in sorted(_bare_passthrough_keys()):
        if key not in example:
            continue  # 缺失由 test_env_example_covers_every_settings_field 负责
        default = _config_default(key)
        if not _same_value(example[key], default):
            drift.append(f"{key}: .env.example={example[key]!r} 但 config.py 默认={default!r}")
    assert not drift, "裸键透传项的示例值与代码默认值漂移:\n" + "\n".join(drift)


# 部署口径刻意与 config.py 默认不同的旋钮：生产走 nginx + HTTPS、不开 /docs，
# 而 config.py 的默认面向本地开发。只有这三项允许在 compose 里写 `:-默认`。
COMPOSE_DEFAULT_OVERRIDES = {
    "ENABLE_DOCS": "生产不暴露 /docs（与 config 默认同为 false，显式写出防误开）",
    "REQUIRE_HTTPS": "生产必须 HTTPS 登录",
    "TRUST_PROXY_HEADERS": "后端只经 nginx 访问，X-Forwarded-Proto 由 nginx 覆写，可信",
}


def test_compose_settings_have_no_second_default():
    """#278：Settings 字段在 compose 里一律裸键透传，`${X:-默认}` 只允许白名单。

    `:-默认` 等于给同一配置立了第二个默认值来源：改了 config.py 的默认值，Docker 部署
    不会跟着变（#128 的原型）。此前只校验裸键，这种写法完全不受检查。
    """
    settings_keys = {name.upper() for name in Settings.model_fields}
    offenders = []
    for block in _compose_service_blocks():
        for key, default in re.findall(
            r"^\s+-\s+([A-Z][A-Z0-9_]*)=\$\{\1:-([^}]*)\}\s*$", block, re.M
        ):
            if key in settings_keys and key not in COMPOSE_DEFAULT_OVERRIDES:
                offenders.append(f"{key}（:-{default}）")
    assert not offenders, "改成裸键 `- NAME`，默认值只留在 config.py：" + ", ".join(offenders)
