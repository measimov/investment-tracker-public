"""迁移 20260801_0005 的数据操作验证：excluded 并入 + 硬编码现值按用户播种。

在一次性 scratch 库上演练 0004 → 0005：既有用户与排除行必须原样迁移，
六类种子逐类落库；无用户的库播种为零（新库由 UI 自配规则）。
"""

import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

SCRATCH_DB = "investment_test_rules_migration"
ADMIN_URL = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
SCRATCH_URL = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + f"/{SCRATCH_DB}"


def _alembic_config(url: str) -> Config:
    config = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migration_merges_exclusions_and_seeds_per_user(monkeypatch):
    # alembic env.py 从 app settings 取 URL（单例），临时指向 scratch 库
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", SCRATCH_URL)
    admin_engine = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {SCRATCH_DB}"))
        conn.execute(sa.text(f"CREATE DATABASE {SCRATCH_DB}"))
    try:
        config = _alembic_config(SCRATCH_URL)
        command.upgrade(config, "20260731_0004")

        engine = sa.create_engine(SCRATCH_URL)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO users (username, hashed_password, is_active, is_admin, "
                    "created_at, updated_at) VALUES "
                    "('owner', 'x', true, false, now(), now()), "
                    "('family', 'x', true, false, now(), now())"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO excluded_securities (user_id, symbol, market, note) "
                    "SELECT id, '511880', 'A股', '货币基金' FROM users WHERE username='owner'"
                )
            )
        engine.dispose()

        command.upgrade(config, "20260801_0005")

        engine = sa.create_engine(SCRATCH_URL)
        with engine.connect() as conn:
            counts = dict(
                conn.execute(
                    sa.text(
                        "SELECT rule_type, count(*) FROM security_rules GROUP BY rule_type"
                    )
                ).all()
            )
            # 两用户各: CASH_MANAGEMENT 1 + RELISTING 1 + NAME_OVERRIDE 2 +
            # PRICE_GAP 2 + CMB_CASH_BUSINESS 17；EXCLUDE 仅 owner 迁入 1
            assert counts == {
                "EXCLUDE": 1,
                "CASH_MANAGEMENT": 2,
                "RELISTING": 2,
                "NAME_OVERRIDE": 4,
                "PRICE_GAP_EXEMPTION": 4,
                "CMB_CASH_BUSINESS": 34,
            }
            merged_note = conn.execute(
                sa.text("SELECT note FROM security_rules WHERE rule_type='EXCLUDE'")
            ).scalar()
            assert merged_note == "货币基金"
            relisting_payload = conn.execute(
                sa.text(
                    "SELECT payload->>'new_symbol' FROM security_rules "
                    "WHERE rule_type='RELISTING' LIMIT 1"
                )
            ).scalar()
            assert relisting_payload == "PCT"
            old_table = conn.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='excluded_securities'"
                )
            ).scalar()
            assert old_table == 0
        engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin_engine.dispose()
