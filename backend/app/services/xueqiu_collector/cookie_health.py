"""雪球 Cookie 到期检查 + Uptime Kuma 推送（纯函数 + 一次 HTTP）。

`scripts/check_xueqiu_cookie_expiry.py`（运维探活脚本）、采集器每轮自检与状态端点
共用这里：一份到期判据，三个出口不会各说各话。合并自 xueqiu-timeline-archiver 的
同名脚本：补上「主凭证缺失」判 critical 与 push URL 推送。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

# 登录态的主凭证：这两个一过期（或缺失），全部接口立刻失效
PRIMARY_AUTH_COOKIES = ("xq_a_token", "xqat")


def load_cookie_facts(cookie_file: Path) -> Dict[str, Any]:
    """读浏览器导出：{names: 出现的 Cookie 名, expirations: 主凭证的 expirationDate（秒）}。

    {name: value} 形状没有 expirationDate，expirations 为空；names 仍然可用于
    判断主凭证是否缺失。
    """
    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    cookies = data.get("cookies") if isinstance(data, dict) and "cookies" in data else data
    names: set = set()
    expirations: Dict[str, float] = {}
    if isinstance(cookies, list):
        for cookie in cookies:
            name = str(cookie.get("name", ""))
            names.add(name)
            expiration = cookie.get("expirationDate")
            if name in PRIMARY_AUTH_COOKIES and expiration is not None:
                expirations[name] = float(expiration)
    elif isinstance(cookies, dict):
        names = {str(key) for key in cookies}
    else:
        raise ValueError("Cookie 文件结构无法识别")
    return {"names": names, "expirations": expirations}


def check_expiry(
    cookie_file: Optional[str],
    *,
    warn_days: float,
    critical_days: float,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """到期日检查。返回 {level, message, days_left, cookie}。

    level：normal / warning / critical / unconfigured。
    """
    now = now or time.time()
    if not cookie_file:
        return {
            "level": "unconfigured",
            "message": "未配置 XUEQIU_COOKIE_FILE，跳过到期日检查"
                       "（XUEQIU_COOKIES 形状不含 expirationDate）",
            "days_left": None, "cookie": "",
        }
    path = Path(cookie_file)
    if not path.is_file():
        return {"level": "critical", "message": f"Cookie 文件不存在：{path}",
                "days_left": None, "cookie": ""}
    try:
        facts = load_cookie_facts(path)
    except (json.JSONDecodeError, OSError, ValueError, AttributeError) as exc:
        return {"level": "critical", "message": f"Cookie 文件无法解析：{exc}",
                "days_left": None, "cookie": ""}

    missing = [name for name in PRIMARY_AUTH_COOKIES if name not in facts["names"]]
    if missing:
        return {
            "level": "critical",
            "message": f"雪球登录凭证缺失：{', '.join(missing)}（请重新导出完整 Cookie）",
            "days_left": None, "cookie": missing[0],
        }

    expirations = facts["expirations"]
    if not expirations:
        return {
            "level": "unconfigured",
            "message": f"{path} 不含 expirationDate（非浏览器完整导出），"
                       "无法预判到期；请改用 --probe",
            "days_left": None, "cookie": "",
        }

    days = {name: (value - now) / 86400 for name, value in expirations.items()}
    cookie, days_left = min(days.items(), key=lambda item: item[1])
    if days_left <= critical_days:
        level = "critical"
    elif days_left <= warn_days:
        level = "warning"
    else:
        level = "normal"
    return {
        "level": level, "cookie": cookie, "days_left": days_left,
        "message": (f"雪球 Cookie {level}：{cookie} 还有 {days_left:.1f} 天到期"
                    f"（warn={warn_days:g}d critical={critical_days:g}d）"),
    }


def build_push_url(push_url: str, *, status: str, message: str, ping: str = "") -> str:
    """Uptime Kuma push：在原 URL 上覆盖 status/msg/ping 三个参数。"""
    parts = urlsplit(push_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["status"] = status
    query["msg"] = message
    query["ping"] = ping
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def push_status(push_url: str, *, status: str, message: str, timeout: float = 10) -> None:
    """推送 up/down；失败抛 requests.RequestException 由调用方决定如何处理。"""
    response = requests.get(
        build_push_url(push_url, status=status, message=message[:500]), timeout=timeout
    )
    response.raise_for_status()
