"""PDF 下载的总时长 / 最低速度 / 换连接重试（2026-09-26：披露易某 Akamai 边缘节点涓流 1.8KB/s，
`timeout=` 只管「多久没字节」永不触发，补跑卡死在一份 PDF 上）。纯函数级：伪造 requests.get
与单调时钟，不走网络。"""

import http.server
import socketserver
import threading
import time

import pytest
import requests

from app.services import report_fetchers as rf


class _Response:
    def __init__(self, chunks, clock, step):
        self._chunks, self._clock, self._step = chunks, clock, step
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            self._clock["now"] += self._step  # 每块耗时 step 秒
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 0.0}
    monkeypatch.setattr(rf, "_monotonic", lambda: state["now"])
    monkeypatch.setattr(rf, "_throttle", lambda *a, **k: None)
    return state


def _patch_get(monkeypatch, clock, plans):
    """plans：每次 requests.get 依次返回的 (chunks, 每块秒数)。"""
    calls = []

    def fake_get(url, **kwargs):
        chunks, step = plans[len(calls)]
        response = _Response(chunks, clock, step)
        calls.append(response)
        return response

    class _FakeSession:
        def get(self, url, **kwargs):
            return fake_get(url, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(rf, "_tracking_session", lambda sockets: _FakeSession())
    return calls


def test_normal_download_returns_bytes_and_closes(monkeypatch, clock):
    calls = _patch_get(monkeypatch, clock, [([b"a" * 65536] * 4, 0.1)])
    assert rf.download_report_pdf("https://www1.hkexnews.hk/x.pdf", source="hkexnews") == b"a" * 65536 * 4
    assert len(calls) == 1 and calls[0].closed


def test_trickle_connection_is_abandoned_and_retried_on_a_fresh_one(monkeypatch, clock):
    # 第一条连接：每 36 秒才来 64KB（≈1.8KB/s）→ 宽限期后判过慢；第二条正常
    calls = _patch_get(monkeypatch, clock, [([b"s" * 65536] * 100, 36.0), ([b"f" * 65536] * 3, 0.1)])
    body = rf.download_report_pdf("https://www1.hkexnews.hk/x.pdf", source="hkexnews")
    assert body == b"f" * 65536 * 3
    assert len(calls) == 2 and calls[0].closed
    assert clock["now"] < 120  # 没有在涓流连接上耗到几个小时


def test_deadline_applies_even_above_min_speed(monkeypatch, clock):
    # 速度刚好够（64KB/秒以上）但文件极大、总时长超限：两条连接都超时 → 抛 requests.Timeout
    fast_enough = ([b"x" * 65536] * 10_000, 1.0)
    calls = _patch_get(monkeypatch, clock, [fast_enough, fast_enough])
    monkeypatch.setattr(rf, "PDF_MAX_BYTES", 1 << 40)
    with pytest.raises(requests.Timeout, match="总时长"):
        rf.download_report_pdf("https://www1.hkexnews.hk/x.pdf", source="hkexnews")
    assert len(calls) == rf.PDF_DOWNLOAD_ATTEMPTS and all(c.closed for c in calls)


def test_size_cap_is_not_retried(monkeypatch, clock):
    calls = _patch_get(monkeypatch, clock, [([b"x" * 65536] * 20, 0.01)])
    monkeypatch.setattr(rf, "PDF_MAX_BYTES", 65536 * 5)
    with pytest.raises(ValueError, match="大小上限"):
        rf.download_report_pdf("https://www1.hkexnews.hk/x.pdf", source="hkexnews")
    assert len(calls) == 1 and calls[0].closed


def test_plain_http_is_still_rejected(monkeypatch, clock):
    _patch_get(monkeypatch, clock, [])
    with pytest.raises(ValueError, match="非 HTTPS"):
        rf.download_report_pdf("http://www1.hkexnews.hk/x.pdf", source="hkexnews")


# ---------------------------------------------------------------------------
# 真实流式读取（评审 P1）：本机 HTTP 服务按字节涓流，块永远攒不满——伪 Response 会主动
# yield，验证不了「阻塞中的读取也受墙钟约束」这个保证
# ---------------------------------------------------------------------------


class _TrickleHandler(http.server.BaseHTTPRequestHandler):
    header_delay = 0.0
    body_len = 40
    byte_interval = 0.01

    def do_GET(self):  # noqa: N802
        time.sleep(self.header_delay)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(self.body_len))
            self.end_headers()
            for _ in range(self.body_len):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(self.byte_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def trickle_server():
    servers = []

    def start(**attrs):
        handler = type("Handler", (_TrickleHandler,), attrs)
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/x.pdf"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fast_limits(monkeypatch):
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(rf, "PDF_MIN_SPEED_GRACE_SECONDS", 10.0)  # 只测总时长
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(rf, "_PDF_WATCHDOG_POLL_SECONDS", 0.005)


def test_deadline_bounds_a_blocked_read_that_never_fills_a_chunk(trickle_server, fast_limits):
    """评审复现：Content-Length=40、每 0.01s 写 1 字节，64KB 块永远攒不满、读超时也不触发。
    此前约 0.48s（响应结束）才抛；现在必须在 deadline 附近就放弃。"""
    url = trickle_server(body_len=40, byte_interval=0.01)
    started = time.monotonic()
    with pytest.raises(requests.Timeout, match="总时长"):
        rf._download_once(url, {})
    assert time.monotonic() - started < 0.2


def test_deadline_bounds_the_wait_for_response_headers(trickle_server, fast_limits, monkeypatch):
    """响应头迟迟不来（服务端先 sleep）：同样在 deadline 附近放弃，不等读超时/响应结束。"""
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_TIMEOUT_SECONDS", 5.0)
    url = trickle_server(header_delay=1.0, body_len=4, byte_interval=0.0)
    started = time.monotonic()
    with pytest.raises(requests.Timeout, match="总时长"):
        rf._download_once(url, {})
    assert time.monotonic() - started < 0.3


def test_min_speed_bounds_a_trickle_with_a_generous_deadline(trickle_server, monkeypatch):
    """总时长宽裕、但涓流远低于最低速度：宽限期一过就放弃，而不是等到总时长。"""
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_DEADLINE_SECONDS", 30.0)
    monkeypatch.setattr(rf, "PDF_MIN_SPEED_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(rf, "PDF_MIN_BYTES_PER_SECOND", 10_000)
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(rf, "_PDF_WATCHDOG_POLL_SECONDS", 0.005)
    url = trickle_server(body_len=2000, byte_interval=0.01)
    started = time.monotonic()
    with pytest.raises(requests.Timeout, match="过慢"):
        rf._download_once(url, {})
    assert time.monotonic() - started < 0.3


def test_real_stream_completes_within_limits(trickle_server, monkeypatch):
    monkeypatch.setattr(rf, "_PDF_WATCHDOG_POLL_SECONDS", 0.005)
    url = trickle_server(body_len=4096, byte_interval=0.0)
    assert rf._download_once(url, {}) == b"x" * 4096



class _EndlessHeaderServer:
    """发出状态行后持续涓流一个永不结束的响应头（评审 P2 复现）：`requests` 在响应头读完前
    拿不到 response，每个字节又重置读超时。记录服务端连接数与被对端关闭的连接数。"""

    def __init__(self):
        import socket as _socket

        self.sock = _socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/x.pdf"
        self.accepted = 0
        self.closed_by_peer = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.sock.settimeout(0.05)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with self.lock:
                self.accepted += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            while not self.stop.is_set():
                conn.sendall(b"a")
                time.sleep(0.01)
        except OSError:
            with self.lock:
                self.closed_by_peer += 1
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def _download_threads():
    return [t for t in threading.enumerate() if t.name == "pdf-download" and t.is_alive()]


def test_endless_header_trickle_releases_thread_and_connection(fast_limits):
    """评审 P2：响应头永不结束时，超时后工作线程与连接都必须释放——连续三次调用不累积。"""
    server = _EndlessHeaderServer()
    try:
        baseline = len(_download_threads())
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(requests.Timeout, match="总时长"):
                rf._download_once(server.url, {})
            assert time.monotonic() - started < 0.3
        # 超时抛出时工作线程已收尾（join 过），不是被遗弃
        assert len(_download_threads()) == baseline
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and server.closed_by_peer < 3:
            time.sleep(0.01)
        assert server.accepted == 3 and server.closed_by_peer == 3  # 三条连接都被我方关闭
    finally:
        server.close()


def test_body_trickle_timeout_also_releases_worker(trickle_server, fast_limits):
    url = trickle_server(body_len=400, byte_interval=0.01)
    baseline = len(_download_threads())
    for _ in range(2):
        with pytest.raises(requests.Timeout):
            rf._download_once(url, {})
    assert len(_download_threads()) == baseline



# ---------------------------------------------------------------------------
# HTTPS（生产路径，评审 P1）：wrap_socket 会 detach 原始 socket 的 fd，登记原始 socket 等于
# 登记了一个 fileno()=-1 的空壳。自签名证书只在测试里关闭校验
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tls_context(tmp_path_factory):
    import datetime
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls")
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


class _TlsServer:
    """mode="endless_header"：TLS 握手后发状态行，再每 0.01s 往不结束的头里写一个字节；
    mode="stall_handshake"：接受 TCP 后既不握手也不关闭（卡在 TLS 握手阶段）。"""

    def __init__(self, context, mode):
        import socket as _socket

        self.context, self.mode = context, mode
        self.sock = _socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"https://127.0.0.1:{self.sock.getsockname()[1]}/x.pdf"
        self.accepted = 0
        self.closed_by_peer = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.sock.settimeout(0.05)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with self.lock:
                self.accepted += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if self.mode == "stall_handshake":
                conn.settimeout(0.02)
                while not self.stop.is_set():
                    try:
                        if conn.recv(4096) == b"":
                            raise ConnectionResetError  # 客户端关闭了连接
                    except TimeoutError:
                        continue
                return
            tls = self.context.wrap_socket(conn, server_side=True)
            tls.recv(4096)
            tls.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            while not self.stop.is_set():
                tls.sendall(b"a")
                time.sleep(0.01)
        except (OSError, ConnectionResetError):
            with self.lock:
                self.closed_by_peer += 1
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.fixture
def insecure_tracking_session(monkeypatch):
    original = rf._tracking_session

    def factory(sockets):
        session = original(sockets)
        session.verify = False  # 仅测试：自签名证书
        return session

    monkeypatch.setattr(rf, "_tracking_session", factory)
    monkeypatch.setattr(rf, "_throttle", lambda *a, **k: None)


@pytest.mark.filterwarnings("ignore::urllib3.exceptions.InsecureRequestWarning")
@pytest.mark.parametrize("mode", ["endless_header", "stall_handshake"])
def test_https_timeout_releases_thread_and_connection(tls_context, insecure_tracking_session, monkeypatch, mode):
    """评审 P1 复现：HTTPS 下响应头永不结束 / TLS 握手卡住。走生产入口 download_report_pdf
    （两次尝试），每次都在 deadline 附近放弃，之后无存活 pdf-download 线程，连接均被我方关闭。"""
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_DEADLINE_SECONDS", 0.15)
    monkeypatch.setattr(rf, "PDF_MIN_SPEED_GRACE_SECONDS", 10.0)
    # 读超时远大于 deadline：证明是看门狗打断的，而不是读超时碰巧触发
    monkeypatch.setattr(rf, "PDF_DOWNLOAD_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(rf, "_PDF_WATCHDOG_POLL_SECONDS", 0.005)
    monkeypatch.setattr(rf, "_PDF_WORKER_JOIN_SECONDS", 0.5)
    server = _TlsServer(tls_context, mode)
    try:
        baseline = len(_download_threads())
        started = time.monotonic()
        with pytest.raises(requests.Timeout, match="总时长"):
            rf.download_report_pdf(server.url, source="hkexnews")
        assert time.monotonic() - started < 1.5  # 两次尝试 × (0.15s + 收尾)
        assert len(_download_threads()) == baseline
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and server.closed_by_peer < rf.PDF_DOWNLOAD_ATTEMPTS:
            time.sleep(0.01)
        assert server.accepted == rf.PDF_DOWNLOAD_ATTEMPTS
        assert server.closed_by_peer == rf.PDF_DOWNLOAD_ATTEMPTS
    finally:
        server.close()
