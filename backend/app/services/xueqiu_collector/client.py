"""xueqiu.com 网页端 HTTP 客户端（签名 + 礼貌限速 + WAF 识别）。

与 `services/xueqiu_source.py`（stock.xueqiu.com 行情/基本面，私有库 xueqiu-market）
刻意分开：站点不同（这里要 md5__1038 签名）、频率不同（这里 10–25s/次），只共用
**同一份 Cookie**（`XUEQIU_COOKIES` / `XUEQIU_COOKIE_FILE`）——两份 Cookie 过期时要改
两处，已经实际踩过。

约束：
- Cookie 由调用方供给，未配置 → `CollectorUnavailable`（显式降级，不发请求）。
- **全局**礼貌限速：进程内所有 client 共享一个节流时钟（按 [min, max] 随机间隔串行）。
- 阿里云 WAF 挑战页（`aliyun_waf_aa/_bb`）→ `WafChallenge`，由 runner 冷却并中止本轮。
- 请求头照搬原实现（浏览器 UA）。
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from ...config import settings
from ...core.logging import get_app_logger
from .signer import sign_url

logger = get_app_logger(__name__)

CONNECT_RETRIES = 3

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "priority": "u=0, i",
    "referer": "https://xueqiu.com/",
    "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
}


class CollectorUnavailable(RuntimeError):
    """采集器不可用（未配置 Cookie / Cookie 文件无法解析）。"""


class WafChallenge(RuntimeError):
    """命中阿里云 WAF 挑战页。"""


class CollectorFetchError(RuntimeError):
    """一次抓取没拿到合法响应（非 JSON / 非对象 / 缺关键数组 / 网络或 HTTP 错误）。

    与「合法的空结果」（`statuses: []`、`comments: []`）严格区分：把失败当空页会让
    整轮以 ok 收尾，scan_runs 写成功、观点页活性被刷新，采集停摆就此被掩盖。
    """


def require_list(payload: Optional[Dict[str, Any]], key: str, *, context: str) -> List[Any]:
    """payload 必须是含 `key` 数组的对象，否则抛 CollectorFetchError。"""
    if payload is None:
        raise CollectorFetchError(f"{context} 返回的不是 JSON 对象")
    value = payload.get(key)
    if not isinstance(value, list):
        detail = {k: payload[k] for k in ("error_code", "error_description") if k in payload}
        raise CollectorFetchError(
            f"{context} 响应缺少 {key} 数组" + (f"：{detail}" if detail else "")
        )
    return value


def is_waf_challenge_text(text: str) -> bool:
    head = (text or "")[:500]
    return "aliyun_waf_aa" in head or "aliyun_waf_bb" in head


def _cookies_from_data(data: Any) -> Dict[str, str]:
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if isinstance(data, list):
        return {str(item["name"]): str(item["value"]) for item in data}
    if isinstance(data, dict):
        return {str(key): str(value) for key, value in data.items()}
    raise CollectorUnavailable("雪球 Cookie 结构无法识别（需 {name: value} 或 J2Team 导出）")


def load_collector_cookies() -> Dict[str, str]:
    """按 settings 读取 Cookie（与行情客户端同一份配置，XUEQIU_COOKIES 优先）。

    文件形态优先交给私有库 `xueqiu_market.load_cookies`（支持的写法最多）；公开快照
    没有该库时用本地等价实现兜底——采集器不该因为缺一个行情私有包而整体失明。
    """
    raw = (settings.xueqiu_cookies or "").strip()
    path = (settings.xueqiu_cookie_file or "").strip()
    if not raw and not path:
        raise CollectorUnavailable("未配置雪球 Cookie（XUEQIU_COOKIES / XUEQIU_COOKIE_FILE）")
    try:
        if raw:
            cookies = _cookies_from_data(json.loads(raw))
        else:
            try:
                from xueqiu_market import load_cookies as library_load_cookies
            except ImportError:
                library_load_cookies = None
            if library_load_cookies is not None:
                cookies = dict(library_load_cookies(path))
            else:
                cookies = _cookies_from_data(json.loads(Path(path).read_text(encoding="utf-8")))
    except CollectorUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CollectorUnavailable(f"雪球 Cookie 无法读取：{type(exc).__name__}: {exc}") from exc
    if not cookies:
        raise CollectorUnavailable("雪球 Cookie 为空")
    return cookies


class PoliteThrottle:
    """串行节流：两次请求之间随机停顿 [min_delay, max_delay] 秒。线程安全。"""

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 rng: Optional[random.Random] = None) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()

    def wait(self) -> None:
        with self._lock:
            now = self._monotonic()
            if now < self._next_at:
                self._sleep(self._next_at - now)

    def mark(self, min_delay: float, max_delay: float) -> None:
        with self._lock:
            delay = self._rng.uniform(min_delay, max(min_delay, max_delay))
            self._next_at = self._monotonic() + delay


# 进程级共享：runner 每轮新建 client，但限速时钟必须跨 client 连续
GLOBAL_THROTTLE = PoliteThrottle()


class XueqiuWebClient:
    """xueqiu.com 网页端 GET。每次请求都重新签名（签名含时间戳）。"""

    def __init__(
        self,
        cookies: Dict[str, str],
        *,
        min_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        timeout: Optional[float] = None,
        throttle: Optional[PoliteThrottle] = None,
        http_get: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        on_request: Optional[Callable[[], None]] = None,
    ) -> None:
        self.cookies = cookies
        self.min_delay = (
            settings.xueqiu_collector_min_delay_seconds if min_delay is None else min_delay
        )
        self.max_delay = (
            settings.xueqiu_collector_max_delay_seconds if max_delay is None else max_delay
        )
        self.timeout = settings.xueqiu_collector_timeout_seconds if timeout is None else timeout
        self.throttle = throttle or GLOBAL_THROTTLE
        self._http_get = http_get or requests.get
        self._sleep = sleep
        self._on_request = on_request
        self.request_count = 0

    def get(self, url: str, *, context: str = "", sign: bool = True) -> Any:
        """发一次 GET；命中 WAF 挑战页抛 `WafChallenge`（状态码检查之前判定）。"""
        self.throttle.wait()
        try:
            # 宿主 DNS 偶发解析失败，连接错误重试几次（原实现同款）
            for attempt in range(CONNECT_RETRIES):
                try:
                    target = sign_url(url) if sign else url
                    response = self._http_get(
                        target, headers=HEADERS, cookies=self.cookies, timeout=self.timeout
                    )
                    break
                except requests.exceptions.ConnectionError:
                    if attempt == CONNECT_RETRIES - 1:
                        raise
                    self._sleep(2**attempt)
        finally:
            self.request_count += 1
            self.throttle.mark(self.min_delay, self.max_delay)
            if self._on_request is not None:
                self._on_request()
        if is_waf_challenge_text(getattr(response, "text", "") or ""):
            raise WafChallenge(f"{context or url} 触发阿里云 WAF 挑战页")
        return response

    def get_json(self, url: str, *, context: str) -> Optional[Dict[str, Any]]:
        """GET → JSON 对象；非 JSON / 非对象返回 None 并告警（原 parse_json_response）。

        HTTP 错误状态 `raise_for_status` 照常抛出（由调用方决定跳过还是失败）。
        """
        response = self.get(url, context=context)
        response.raise_for_status()
        return parse_json_payload(response, context)


def parse_json_payload(response: Any, context: str) -> Optional[Dict[str, Any]]:
    if is_waf_challenge_text(response.text):
        raise WafChallenge(f"{context} 触发阿里云 WAF 挑战页")
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        snippet = response.text[:200].replace("\n", " ").strip()
        content_type = response.headers.get("content-type", "unknown")
        logger.warning(
            "%s 返回的不是 JSON，已跳过。状态码=%s，content-type=%s，错误=%s，响应片段=%r",
            context, response.status_code, content_type, exc, snippet,
        )
        return None
    if not isinstance(payload, dict):
        logger.warning("%s 返回 JSON 不是对象，已跳过。类型=%s", context, type(payload).__name__)
        return None
    return payload
