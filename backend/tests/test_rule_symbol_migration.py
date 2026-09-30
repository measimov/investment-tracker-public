"""迁移 20260929_0036：存量特例规则的证券代码按手工入口口径归一（#278）。

在一次性 scratch 库上演练 0035 → 0036：港股纯数字补零、字母大写、RELISTING 的新代码按
新市场归一、CMB 业务名不动；归一后撞唯一键的行不改不删。
"""

import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

SCRATCH_DB = "investment_test_rule_symbol_migration"
# make_url 保留查询参数（本机要 ?gssencmode=disable，否则每次建连挂 180 秒）
_BASE_URL = sa.engine.make_url(os.environ["DATABASE_URL"])
ADMIN_URL = _BASE_URL.set(database="postgres").render_as_string(hide_password=False)
SCRATCH_URL = _BASE_URL.set(database=SCRATCH_DB).render_as_string(hide_password=False)


def _alembic_config(url: str) -> Config:
    here = os.path.dirname(__file__)
    config = Config(os.path.join(here, "..", "alembic.ini"))
    config.set_main_option("script_location", os.path.join(here, "..", "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_rule_symbols_are_normalized_and_conflicts_left_alone(monkeypatch, capsys):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", SCRATCH_URL)
    admin_engine = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {SCRATCH_DB}"))
        conn.execute(sa.text(f"CREATE DATABASE {SCRATCH_DB}"))
    try:
        config = _alembic_config(SCRATCH_URL)
        command.upgrade(config, "20260929_0035")

        engine = sa.create_engine(SCRATCH_URL)
        with engine.begin() as conn:
            user_id = conn.execute(
                sa.text(
                    "INSERT INTO users (username, hashed_password, is_active, is_admin) "
                    "VALUES ('owner', 'x', true, false) RETURNING id"
                )
            ).scalar_one()
            conn.execute(
                sa.text(
                    "INSERT INTO security_rules (user_id, rule_type, symbol, market, payload) VALUES "
                    "(:u, 'EXCLUDE', '700', '港股', NULL),"
                    "(:u, 'NAME_OVERRIDE', 'aapl', '美股', '{\"name\": \"Apple\"}'),"
                    "(:u, 'CASH_MANAGEMENT', '3900', '港股', NULL),"
                    "(:u, 'CASH_MANAGEMENT', '03900', '港股', NULL),"
                    "(:u, 'RELISTING', '1263', '港股', '{\"new_symbol\": \"700\","
                    ' "new_market": "港股", "new_currency": "HKD", "old_currency": "HKD"}\'),'
                    "(:u, 'RELISTING', '01263', '港股', '{\"new_symbol\": \"PCT\","
                    ' "new_market": "新加坡股", "new_currency": "SGD", "old_currency": "HKD"}\'),'
                    "(:u, 'RELISTING', 'PCT', '新加坡股', '{\"new_symbol\": \"700\","
                    ' "new_market": "港股", "new_currency": "HKD", "old_currency": "SGD"}\'),'
                    "(:u, 'CMB_CASH_BUSINESS', '银行转存', NULL, '{\"event_type\": \"DEPOSIT\"}')"
                ),
                {"u": user_id},
            )

        command.upgrade(config, "20260929_0036")

        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT rule_type, symbol, market, payload FROM security_rules ORDER BY id")
            ).all()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            ("EXCLUDE", "00700", "港股"),
            ("NAME_OVERRIDE", "AAPL", "美股"),
            ("CASH_MANAGEMENT", "3900", "港股"),  # 与 03900 撞键：不改不删，只报告
            ("CASH_MANAGEMENT", "03900", "港股"),
            ("RELISTING", "1263", "港股"),  # 撞键：symbol 不改…
            ("RELISTING", "01263", "港股"),
            ("RELISTING", "PCT", "新加坡股"),
            ("CMB_CASH_BUSINESS", "银行转存", None),
        ]
        assert rows[4][3]["new_symbol"] == "00700"  # …但 payload 的新代码照样归一（PR #307 评审）
        assert rows[6][3]["new_symbol"] == "00700"
        assert rows[1][3] == {"name": "Apple"}
        out = capsys.readouterr().out
        assert "'3900'→'03900'" in out

        # 0039：跑过旧版 0036 的库（撞键行的 payload 没归一）由补充迁移修正，已归一的不动
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE security_rules SET payload = jsonb_set(payload::jsonb, '{new_symbol}', '\"700\"')::json "
                    "WHERE rule_type = 'RELISTING' AND symbol = '1263'"
                )
            )
        command.upgrade(config, "20260929_0039")
        with engine.connect() as conn:
            relisted = dict(
                conn.execute(
                    sa.text(
                        "SELECT symbol, payload->>'new_symbol' FROM security_rules "
                        "WHERE rule_type = 'RELISTING'"
                    )
                ).all()
            )
        assert relisted == {"1263": "00700", "01263": "PCT", "PCT": "00700"}
        engine.dispose()
    finally:
        admin_engine = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
