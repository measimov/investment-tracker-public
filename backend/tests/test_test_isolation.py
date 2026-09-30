"""测试进程与外部世界隔离（#274）：conftest 置空凭证并拦截非回环出站连接。

漏打桩的用例曾在本机真实调用 DeepSeek、下载披露易年报——CI 没有 Key 所以一直是绿的，
只有开发者本机在悄悄花钱。这里守住守卫本身：凭证确实为空、外部连接确实被拒、回环照常可用。
"""

import socket
import threading

import pytest

import conftest
from app.config import settings

pytestmark = pytest.mark.skipif(
    conftest.ALLOW_TEST_NETWORK, reason="显式联网运行时守卫与凭证置空一并关闭"
)


def test_credentials_from_dotenv_are_blanked():
    assert settings.llm_report_api_key == ""
    assert settings.tushare_token == ""
    assert settings.tiingo_api_token == ""
    assert settings.xueqiu_cookies == ""
    assert settings.notify_urls == ""


@pytest.mark.parametrize("address", [("203.0.113.10", 443), ("api.deepseek.com", 443)])
def test_outbound_connection_is_refused(address):
    with pytest.raises(RuntimeError, match="禁止外部网络连接"):
        socket.create_connection(address, timeout=1)
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="禁止外部网络连接"):
            sock.connect(address)
    finally:
        sock.close()
    assert conftest.REFUSED_CONNECTIONS == [address, address]
    conftest.REFUSED_CONNECTIONS.clear()  # 本用例就是要断言「被拒」


def test_requests_are_refused_before_leaving_the_process():
    import requests

    with pytest.raises(Exception, match="禁止外部网络连接"):
        # IP 字面量：不依赖 DNS，结论只取决于守卫本身
        requests.get("https://203.0.113.10/", timeout=1)
    conftest.REFUSED_CONNECTIONS.clear()


def test_loopback_connections_still_work():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted = threading.Event()

    def accept():
        conn, _ = server.accept()
        accepted.set()
        conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        client.close()
        assert accepted.wait(timeout=2)
    finally:
        server.close()


def test_swallowed_refusals_are_recorded_for_the_teardown_check():
    """业务代码吞掉守卫异常时，记录仍在——autouse fixture 据此让用例失败。"""
    try:
        socket.create_connection(("203.0.113.10", 443), timeout=1)
    except Exception:  # noqa: BLE001 - 模拟业务代码的宽泛 except
        pass
    assert conftest.REFUSED_CONNECTIONS == [("203.0.113.10", 443)]
    conftest.REFUSED_CONNECTIONS.clear()
