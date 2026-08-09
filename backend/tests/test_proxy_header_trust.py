"""反代请求头的信任边界必须由应用层独占（issue #131 复审）。

uvicorn 的 ProxyHeadersMiddleware **默认开启**、默认信任 `127.0.0.1`：它会先用
`X-Forwarded-Proto` 把 ASGI `scope["scheme"]` 改成 `https`，于是应用层的
`trust_proxy_headers` 还没轮到判断就已经出局——`require_https` 被一个请求头绕过。

ASGITransport 测试完全看不到这层（它直接把 scope 递给 app），所以那些用例会误绿。
这里起**真实 uvicorn**，从 127.0.0.1（正是它默认信任的地址）发伪造头进去。

启动参数**从 Dockerfile 的 CMD 解析**，不写死：写死的话，从生产 CMD 里删掉
`--no-proxy-headers` 后这些测试仍然全绿，原缺陷可以静默回归。
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DOCKERFILE = BACKEND_DIR / "Dockerfile"

# 覆盖 JSON exec 形式 `CMD ["uvicorn", "app.main:app", ...]` 与 shell 形式
# `uvicorn app.main:app ...`。前一版的字符类只允许空白与引号，JSON 形式里
# "uvicorn" 后面是 `",`，Dockerfile 那一行**根本没进过检查**。
UVICORN_LINE_PATTERN = re.compile(r"""uvicorn["']?\s*,?\s*["']?app\.main:app""")


def _dockerfile_instructions() -> list[str]:
    """按指令切分 Dockerfile：先把 `\\` 续行并回一行，再逐条返回。

    不并续行的话，`HEALTHCHECK ... \\` 的下一行以 `CMD [` 开头，会被逐行
    扫描误认成容器的主 CMD（healthcheck 的探活命令首项是 python 不是
    uvicorn，#143 落地时 CI 因此变红）。
    """
    joined: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}" if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        joined.append(pending)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


def dockerfile_uvicorn_argv() -> list[str]:
    """Dockerfile 顶层 CMD 的参数列表（JSON exec 形式）。"""
    for instruction in _dockerfile_instructions():
        if not instruction.startswith("CMD"):
            continue
        payload = instruction[len("CMD") :].strip()
        if payload.startswith("["):
            argv = json.loads(payload)
            assert argv and argv[0] == "uvicorn", f"未预期的 CMD 形态: {argv}"
            return argv
    pytest.fail("Dockerfile 里找不到 JSON 形式的 uvicorn CMD")


def test_healthcheck_probe_is_not_mistaken_for_the_main_cmd():
    """HEALTHCHECK 的续行 CMD 不得被当成容器主 CMD（解析器按指令切分）。"""
    instructions = _dockerfile_instructions()
    healthchecks = [i for i in instructions if i.startswith("HEALTHCHECK")]
    assert healthchecks, "Dockerfile 应有 HEALTHCHECK"
    # 主 CMD 解析结果仍然是 uvicorn（healthcheck 的探活命令是 python）
    assert dockerfile_uvicorn_argv()[0] == "uvicorn"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _rewrite_host_port(argv: list[str], port: int) -> list[str]:
    """把 CMD 的 host/port 换成测试用的，其余参数（含安全相关的）原样保留。"""
    argv = list(argv)
    for flag, value in (("--host", "127.0.0.1"), ("--port", str(port))):
        if flag in argv:
            argv[argv.index(flag) + 1] = value
        else:
            argv += [flag, value]
    return argv


def _start_backend(port: int, argv: list[str], **env_overrides):
    env = {
        **os.environ,
        "REQUIRE_HTTPS": "true",
        "TRUST_PROXY_HEADERS": "false",
        "BACKGROUND_WORKER_ENABLED": "false",
        **env_overrides,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", *argv],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode(errors="replace") if process.stdout else ""
            process.wait()
            pytest.fail(f"uvicorn 启动即退出：\n{output}")
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return process, base_url
        except httpx.HTTPError:
            time.sleep(0.2)
    process.terminate()
    process.wait(timeout=10)
    pytest.fail("uvicorn 在 30 秒内没有就绪")


def _stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="module")
def untrusted_backend():
    """按 Dockerfile 的 CMD 起后端，require_https 打开、不信任反代。"""
    port = _free_port()
    argv = _rewrite_host_port(dockerfile_uvicorn_argv(), port)
    process, base_url = _start_backend(port, argv)
    try:
        yield base_url
    finally:
        _stop(process)


@pytest.fixture(scope="module")
def trusted_backend():
    """同样的 CMD，但显式信任反代——模拟 compose 里 nginx 后面的后端。"""
    port = _free_port()
    argv = _rewrite_host_port(dockerfile_uvicorn_argv(), port)
    process, base_url = _start_backend(port, argv, TRUST_PROXY_HEADERS="true")
    try:
        yield base_url
    finally:
        _stop(process)


def test_spoofed_forwarded_proto_cannot_bypass_require_https(untrusted_backend):
    """从 uvicorn 默认信任的 127.0.0.1 发伪造头，登录仍必须被拒。

    生产 CMD 里去掉 --no-proxy-headers 的话，uvicorn 会把 scheme 改成 https，
    这条断言立刻变红（启动参数就是从那份 CMD 解析来的）。
    """
    response = httpx.post(
        f"{untrusted_backend}/api/auth/login",
        json={"username": "demo", "password": "irrelevant"},
        headers={"X-Forwarded-Proto": "https"},
        timeout=10,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Login requires HTTPS"


def test_slash_redirect_behind_a_trusted_proxy_stays_on_https(trusted_backend):
    """带尾斜杠的登录请求不得被重定向到明文 HTTP。

    Starlette 的 slash redirect 用 `request.url` 生成**绝对** Location，scheme
    取自 scope。关掉 uvicorn 的代理头处理后，如果应用不在路由之前把 scheme
    改回来，反代后面的 scope 恒为 http —— HTTPS 客户端会收到
    `307 Location: http://…`，非 HSTS 的 API 客户端跟随它时会把原始 POST body
    先明文发到 80 端口。凭据已经裸奔过一次了，nginx 再跳 HTTPS 也晚了。
    """
    response = httpx.post(
        f"{trusted_backend}/api/auth/login/",
        json={"username": "demo", "password": "irrelevant"},
        headers={"X-Forwarded-Proto": "https", "Host": "app.example.com"},
        follow_redirects=False,
        timeout=10,
    )
    assert response.status_code in (301, 307, 308), response.status_code
    location = response.headers["location"]
    assert not location.startswith("http://"), f"重定向把请求降级到了明文：{location}"


def test_forwarded_for_is_only_trusted_when_configured(untrusted_backend, trusted_backend):
    """X-Forwarded-For 与 proto 走同一个信任开关，不能各行其是。"""
    for base_url, trusted in ((untrusted_backend, False), (trusted_backend, True)):
        response = httpx.post(
            f"{base_url}/api/auth/login",
            json={"username": "demo", "password": "irrelevant"},
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"},
            timeout=10,
        )
        # 信任时 proto 生效 → 过了 HTTPS 门、落到口令校验；不信任时被 400 挡住
        assert response.status_code == (401 if trusted else 400), response.text


UVICORN_COMMAND_FILES = [
    DOCKERFILE,
    REPO_ROOT / "DEVELOPMENT.md",
    REPO_ROOT / "frontend" / "playwright.config.ts",
]


def test_every_uvicorn_launch_command_disables_proxy_headers():
    """新增一条不带 --no-proxy-headers 的启动命令 = 悄悄恢复该绕过。

    集成用例只覆盖它自己起的进程；真正上线跑的是 Dockerfile 里的命令，E2E 跑的
    是 playwright 里的那条。这里把所有启动点一并钉死，并断言每个文件**确实**
    被匹配到过——正则漏匹配时静默通过，比没有这条测试更糟。
    """
    offenders = []
    inspected = {}
    for path in UVICORN_COMMAND_FILES:
        matches = 0
        for line in path.read_text().splitlines():
            if not UVICORN_LINE_PATTERN.search(line):
                continue
            matches += 1
            if "--no-proxy-headers" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()}")
        inspected[path.name] = matches

    missed = [name for name, count in inspected.items() if count == 0]
    assert not missed, f"这些文件里一条 uvicorn 启动命令都没匹配到（正则漂移了？）: {missed}"
    assert not offenders, "uvicorn 启动命令缺少 --no-proxy-headers：\n" + "\n".join(offenders)


def test_dockerfile_cmd_is_parsed_and_carries_the_flag():
    """直接对 CMD 的参数列表断言，杜绝"正则没匹配上却全绿"。"""
    argv = dockerfile_uvicorn_argv()
    assert "--no-proxy-headers" in argv, argv
