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


def _env_example_keys() -> set[str]:
    keys = set()
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line.strip())
        if match:
            keys.add(match.group(1))
    return keys


def _compose_backend_env_keys() -> set[str]:
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    backend_block = text.split("backend:", 1)[1].split("frontend:", 1)[0]
    return set(re.findall(r"^\s+-\s+([A-Z][A-Z0-9_]*)=", backend_block, re.M))


def test_env_example_covers_every_settings_field():
    settings_fields = {name.upper() for name in Settings.model_fields}
    missing = settings_fields - _env_example_keys()
    assert not missing, f".env.example 缺少 config.py 字段: {sorted(missing)}"


def test_compose_passthrough_vars_exist_in_env_example():
    unknown = _compose_backend_env_keys() - _env_example_keys()
    assert not unknown, f"docker-compose 透传了示例文件没有的变量: {sorted(unknown)}"


def test_compose_passes_documented_background_and_llm_switches():
    compose_keys = _compose_backend_env_keys()
    for key in (
        "BACKGROUND_WORKER_ENABLED",
        "BACKGROUND_JOB_POLL_SECONDS",
        "LLM_REPORT_API_KEY",
        "PRICE_REFRESH_FRESHNESS_SECONDS",
        "TUSHARE_GLOBAL_MIN_INTERVAL_SECONDS",
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
