"""模型与迁移后的库结构全库零 diff（#285）。

约定是「模型是事实来源」：迁移里建的索引/约束必须回写模型，否则下一次
`alembic revision --autogenerate` 会提议删掉它（`ix_broker_fund_flows_skip_reason`
曾因此漏写）。此前只有雪球采集器七张表有这道检查，这里扩到全库。

autogenerate 不比对 server_default——那一层由 test_model_server_defaults 负责。
"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app import models  # noqa: F401  注册全部模型
from app.database import Base, engine


def test_models_match_migrated_schema():
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == [], "\n".join(repr(diff) for diff in diffs)
