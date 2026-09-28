"""迁移链必须只有一个 head（PR #248/#249 评审 P1）。

并行分支各自接在旧的 head 上时，单独测试都绿，合并后 `alembic upgrade head` 却因
multiple heads 无法选择目标——conftest 的建库与生产升级一并失败。这里直接读迁移目录，
不连库，任何分叉在合并后的 CI 上立刻报红。
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parents[1]


def test_migration_graph_has_single_head():
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"迁移链出现多个 head：{sorted(heads)}，请把新迁移接到最新 head 上"
