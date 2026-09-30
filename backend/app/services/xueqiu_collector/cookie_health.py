"""雪球 Cookie 到期检查 + Uptime Kuma 推送（纯函数 + 一次 HTTP）。

`scripts/check_xueqiu_cookie_expiry.py`（运维探活脚本）、采集器每轮自检与状态端点
共用这里：一份到期判据，三个出口不会各说各话。合并自 xueqiu-timeline-archiver 的
同名脚本：补上「主凭证缺失」判 critical 与 push URL 推送。
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

# 登录态的主凭证：这两个一过期（或缺失），全部接口立刻失效
PRIMARY_AUTH_COOKIES = ("xq_a_token", "xqat")


def effective_cookie_values(data: Any) -> Dict[str, str]:
    """Cookie 导出 → 最终生效的 {name: value}，与两个加载器（`client._cookies_from_data`、
    行情库 `load_cookies`）同一语义：`{"cookies": ...}` 先解包；列表按顺序逐条写入，
    **同名后者覆盖前者**；字典直接取值。缺 value / value 为 null 按空串。"""
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    values: Dict[str, str] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("Cookie 列表条目不是对象")
            value = item.get("value")
            values[str(item.get("name", ""))] = "" if value is None else str(value)
    elif isinstance(data, dict):
        for key, value in data.items():
            values[str(key)] = "" if value is None else str(value)
    else:
        raise ValueError("Cookie 结构无法识别")
    return values


def missing_primary_credentials(values: Dict[str, str]) -> List[str]:
    """最终值缺失或为空白的主凭证：只有键没有值的登录态同样不可用（PR #255 评审）。"""
    return [name for name in PRIMARY_AUTH_COOKIES if not (values.get(name) or "").strip()]


def expiration_seconds(value: Any) -> float:
    """`expirationDate` → 秒（float）。非数字、非有限、或换算不成日期（多为误填毫秒：
    1800000000000 秒是公元 59009 年）一律抛 ValueError，文案不带原值。

    到期检查、状态摘要与界面更新的校验共用这一判据：能过这里的值，后续
    `datetime.fromtimestamp` 与天数运算都不会再炸。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("expirationDate 不是数字")
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError("expirationDate 不是数字") from None
    if not math.isfinite(seconds):
        raise ValueError("expirationDate 不是有限数字")
    try:
        datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("expirationDate 超出可表示的日期范围（是否误填了毫秒？）") from None
    return seconds


def effective_expirations(data: Any) -> Dict[str, float]:
    """主凭证**生效那一条**的 expirationDate（秒）：同名后者覆盖前者，后者没有到期日
    即视为没有（与 `effective_cookie_values` 取同一条，值与到期不会张冠李戴）。"""
    cookies = data.get("cookies") if isinstance(data, dict) and "cookies" in data else data
    expirations: Dict[str, float] = {}
    if isinstance(cookies, list):
        for cookie in cookies:
            name = str(cookie.get("name", ""))
            if name not in PRIMARY_AUTH_COOKIES:
                continue
            expiration = cookie.get("expirationDate")
            if expiration is None:
                expirations.pop(name, None)  # 生效的那条没有到期日
            else:
                expirations[name] = expiration_seconds(expiration)
    return expirations


def cookie_facts_from_data(data: Any) -> Dict[str, Any]:
    """已解析的 Cookie JSON → {names, values, missing, expirations}（见 `load_cookie_facts`）。"""
    values = effective_cookie_values(data)
    return {
        "names": set(values),
        "values": values,
        "missing": missing_primary_credentials(values),
        "expirations": effective_expirations(data),
    }


def load_cookie_facts(cookie_file: Path) -> Dict[str, Any]:
    """读浏览器导出：{names, values, missing, expirations}。

    values 是最终生效的值（同名后者覆盖）；missing 是最终值缺失或为空的主凭证；
    expirations 只取生效那一条的 expirationDate（秒）。{name: value} 形状没有
    expirationDate，expirations 为空。
    """
    return cookie_facts_from_data(json.loads(cookie_file.read_text(encoding="utf-8")))


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
            "days_left": None,
            "cookie": "",
        }
    path = Path(cookie_file)
    if not path.is_file():
        return {
            "level": "critical",
            "message": f"Cookie 文件不存在：{path}",
            "days_left": None,
            "cookie": "",
        }
    try:
        facts = load_cookie_facts(path)
    except (json.JSONDecodeError, OSError, ValueError, AttributeError, TypeError) as exc:
        return {
            "level": "critical",
            "message": f"Cookie 文件无法解析：{exc}",
            "days_left": None,
            "cookie": "",
        }

    missing = facts["missing"]
    if missing:
        return {
            "level": "critical",
            "message": f"雪球登录凭证缺失或为空：{', '.join(missing)}（请重新导出完整 Cookie）",
            "days_left": None,
            "cookie": missing[0],
        }

    expirations = facts["expirations"]
    if not expirations:
        return {
            "level": "unconfigured",
            "message": f"{path} 不含 expirationDate（非浏览器完整导出），"
            "无法预判到期；请改用 --probe",
            "days_left": None,
            "cookie": "",
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
        "level": level,
        "cookie": cookie,
        "days_left": days_left,
        "message": (
            f"雪球 Cookie {level}：{cookie} 还有 {days_left:.1f} 天到期"
            f"（warn={warn_days:g}d critical={critical_days:g}d）"
        ),
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
