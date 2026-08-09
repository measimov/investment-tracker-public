import os
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/investment_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "test-admin-password")
os.environ.setdefault("DEMO_INITIAL_PASSWORD", "test-user-password")
# Tests drive job execution explicitly; the polling worker would race them.
os.environ.setdefault("BACKGROUND_WORKER_ENABLED", "false")
# 生产默认是 fail-closed（require_https=True），而 TestClient 走的是明文 http：
# 不显式放宽的话每一个登录测试都会拿到 400。与 DEVELOPMENT.md / playwright
# 的开发口径一致。默认值本身由 test_auth_security 里专门的用例覆盖。
os.environ.setdefault("REQUIRE_HTTPS", "false")

BACKEND_DIR = Path(__file__).parent
sys.path.insert(0, str(BACKEND_DIR))


def _assert_safe_test_database(database_url: str) -> None:
    parsed = urlparse(database_url)
    database_name = parsed.path.lstrip("/")

    if parsed.scheme not in {"postgresql", "postgresql+psycopg2"}:
        raise RuntimeError("Tests must run against PostgreSQL, not SQLite or another database.")

    if "test" not in database_name and "e2e" not in database_name:
        raise RuntimeError(
            f"Refusing to run tests against non-test database '{database_name}'. "
            "Use a disposable PostgreSQL database whose name contains 'test' or 'e2e'."
        )


def pytest_configure(config):
    database_url = os.environ["DATABASE_URL"]
    _assert_safe_test_database(database_url)
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    try:
        command.upgrade(alembic_cfg, "head")
        _seed_test_users(database_url)
    except OperationalError as exc:
        pytest.exit(
            "PostgreSQL test database is not reachable. "
            "Start a disposable test database and set DATABASE_URL, for example "
            "postgresql://postgres:postgres@127.0.0.1:5432/investment_test. "
            f"Original error: {exc}",
            returncode=2,
        )


def _seed_test_users(database_url: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (id, username, email, hashed_password, is_active, is_admin)
                VALUES
                    (1, 'admin', NULL, 'test-password-hash', true, true),
                    (2, 'demo', NULL, 'test-password-hash', true, false)
                ON CONFLICT (username) DO NOTHING
                """
            )
        )
        conn.execute(
            text(
                """
                SELECT setval(
                    pg_get_serial_sequence('users', 'id'),
                    GREATEST((SELECT MAX(id) FROM users), 1)
                )
                """
            )
        )
