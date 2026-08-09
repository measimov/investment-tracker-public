"""反代请求头的唯一解析点（issue #131 复审）。

uvicorn 自带的 ProxyHeadersMiddleware 被关掉了（所有启动命令带
`--no-proxy-headers`），因为它默认信任 `127.0.0.1`、会抢在应用之前把
`scope["scheme"]` 改写成 https，让应用层的信任开关形同虚设。

关掉之后不能只在认证端点里读 X-Forwarded-Proto —— **路由之前**发生的事也依赖
scheme。最典型的是 Starlette 的 slash redirect：`POST /api/auth/login/` 会用
`request.url` 生成一个**绝对** Location，scheme 取自 scope。反代后面 scope
是 http，于是 HTTPS 客户端收到 `307 Location: http://…`；非 HSTS 的 API 客户端
跟随 307 时会把原始 POST body 先明文发到 80 端口，nginx 再跳 HTTPS 已经太晚
——凭据已经在网上裸奔过一次了。

所以改写必须发生在最外层、路由之前。这里就是那个地方，且只有这一个。
"""

from typing import Any, Dict


class ProxyHeadersMiddleware:
    """按 `trust_proxy_headers` 决定是否采信反代头，改写 scheme 与 client。

    刻意直接实现 ASGI 而不是 BaseHTTPMiddleware：它必须包在最外层、在路由
    与任何重定向之前生效，BaseHTTPMiddleware 的请求/响应封装在这里是多余的
    开销，也拿不到同样干净的 scope 改写时机。

    不信任时**什么都不做**：直连后端的请求带上 `X-Forwarded-Proto: https`
    不会有任何效果，require_https 也就不会被一个请求头绕过。
    """

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Dict[str, Any], receive, send):
        if scope["type"] not in ("http", "websocket") or not self.settings.trust_proxy_headers:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}

        forwarded_proto = headers.get(b"x-forwarded-proto", b"").decode("latin1").strip().lower()
        if forwarded_proto in ("http", "https"):
            if scope["type"] == "websocket":
                scope["scheme"] = "wss" if forwarded_proto == "https" else "ws"
            else:
                scope["scheme"] = forwarded_proto

        # nginx 用 $proxy_add_x_forwarded_for **追加**，最右一项才是它自己看到
        # 的对端（单跳拓扑下即真实客户端）；左侧那些可能是客户端自己塞的。
        forwarded_for = headers.get(b"x-forwarded-for", b"").decode("latin1")
        candidates = [item.strip() for item in forwarded_for.split(",") if item.strip()]
        if candidates:
            port = scope["client"][1] if scope.get("client") else 0
            scope["client"] = (candidates[-1], port)

        await self.app(scope, receive, send)
