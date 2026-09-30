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
# 加入自选时的即时报价与交易时段自动刷新都会外呼报价源；测试里默认关闭，
# 需要的用例用 monkeypatch 打开并对 fetch_stock_price 打桩。
os.environ.setdefault("QUOTE_AUTO_REFRESH_ENABLED", "false")
# 生产默认是 fail-closed（require_https=True），而 TestClient 走的是明文 http：
# 不显式放宽的话每一个登录测试都会拿到 400。与 DEVELOPMENT.md / playwright
# 的开发口径一致。默认值本身由 test_auth_security 里专门的用例覆盖。
os.environ.setdefault("REQUIRE_HTTPS", "false")

# 显式联网的用例（真实行情源冒烟）才放开网络与凭证；默认测试进程与外部世界隔离。
ALLOW_TEST_NETWORK = (
    os.getenv("ALLOW_TEST_NETWORK") == "1" or os.getenv("RUN_EXTERNAL_PRICE_TESTS") == "1"
)

# Settings 读 backend/.env（env_file=".env"），从 backend/ 跑 pytest 会拿到开发者本机的真实
# Key——漏打桩的用例曾真实调用 DeepSeek 并下载披露易年报（#274）。环境变量优先于 env_file，
# 所以必须**强制覆盖成空串**（setdefault 或 pop 都挡不住 .env），且要在任何 app 模块导入、
# Settings 实例化之前。
CREDENTIAL_ENV_VARS = (
    "LLM_REPORT_API_KEY",
    "TUSHARE_TOKEN",
    "TIINGO_API_TOKEN",
    "XUEQIU_COOKIES",
    "XUEQIU_COOKIE_FILE",
    "XUEQIU_COLLECTOR_PUSH_URL",
    "NOTIFY_URLS",
)
if not ALLOW_TEST_NETWORK:
    for _name in CREDENTIAL_ENV_VARS:
        os.environ[_name] = ""
    # 代理跑在回环地址上：requests/httpx 读到 HTTP(S)_PROXY 会先连本机代理再由它转发出去，
    # 回环放行规则就成了后门。守卫开启时清掉代理变量，所有请求直连、都经过守卫
    for _name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(_name, None)


# 被守卫拒绝的外呼：业务代码常用宽泛的 except 把守卫的 RuntimeError 吞成一条「缺口」，
# 测试照样绿、漏打桩悄无声息。每个用例结束时由 _fail_on_swallowed_network 检查这里。
REFUSED_CONNECTIONS: list = []


def _install_network_guard() -> None:
    """拦截一切非回环的出站连接。

    凭证置空只挡住要 token 的源；EDGAR 有占位 UA，披露易/腾讯/货币网/东财根本不要 token，
    漏打桩照样会真实外呼。requests/httpx/urllib3 都经 socket.socket.connect 建连，在这一层拦
    最彻底；libpq 在 C 层建连，不经过这里，数据库不受影响。"""
    import ipaddress
    import socket

    def _is_local(address) -> bool:
        if not isinstance(address, tuple) or not address:
            return True  # AF_UNIX 路径等
        host = address[0]
        if host in ("localhost", ""):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False  # 主机名：一律视为外部

    def _refuse(address):
        REFUSED_CONNECTIONS.append(address)
        raise RuntimeError(
            f"测试进程禁止外部网络连接：{address!r}。请对外部调用打桩；"
            "确需联网的用例用 ALLOW_TEST_NETWORK=1 显式运行。"
        )

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def guarded_connect(self, address):
        if self.family != socket.AF_UNIX and not _is_local(address):
            _refuse(address)
        return original_connect(self, address)

    def guarded_connect_ex(self, address):
        if self.family != socket.AF_UNIX and not _is_local(address):
            _refuse(address)
        return original_connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_local(address):
            _refuse(address)
        return original_create_connection(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection


if not ALLOW_TEST_NETWORK:
    _install_network_guard()

BACKEND_DIR = Path(__file__).parent
sys.path.insert(0, str(BACKEND_DIR))

# 应用日志改写到临时目录：app.main 导入时 configure_logging() 写相对目录 logs/，从 backend/
# 跑 pytest 就会写进 backend/logs——部署机上那是容器用户（uid 10001）的目录，收集阶段即
# PermissionError。必须在任何测试模块导入 app.main 之前替换（main 按名字导入该函数）。
import tempfile  # noqa: E402

import app.core.logging as _app_logging  # noqa: E402

_TEST_LOG_DIR = tempfile.mkdtemp(prefix="it-test-logs-")
_original_configure_logging = _app_logging.configure_logging


def _configure_logging_in_temp_dir(log_dir: str = "logs", **kwargs):
    return _original_configure_logging(log_dir=_TEST_LOG_DIR, **kwargs)


_app_logging.configure_logging = _configure_logging_in_temp_dir


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


@pytest.fixture(autouse=True)
def _clear_process_caches():
    """进程级外部数据缓存不得跨用例残留（各用例对同一 CIK 打桩不同的响应）。"""
    from app.services import report_fetchers

    report_fetchers.clear_edgar_submissions_cache()
    yield
    report_fetchers.clear_edgar_submissions_cache()


@pytest.fixture(autouse=True)
def _fail_on_swallowed_network():
    """用例期间有外呼被守卫拒绝就判失败——哪怕异常被业务代码吞掉了（PR #291 评审）。

    专门断言「被拒」的用例（test_test_isolation）在断言后清空 REFUSED_CONNECTIONS。
    """
    REFUSED_CONNECTIONS.clear()
    yield
    if REFUSED_CONNECTIONS and not ALLOW_TEST_NETWORK:
        refused = list(REFUSED_CONNECTIONS)
        REFUSED_CONNECTIONS.clear()
        pytest.fail(
            f"本用例触发了被拒绝的外部连接 {refused!r}（异常可能被业务代码吞掉）；请对外部调用打桩",
            pytrace=False,
        )
