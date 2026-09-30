"""告警推送：Apprise 多渠道发送（首选 Bark，iPhone/iPad 推送）。

- 渠道来自 `settings.notify_urls`（空格或逗号分隔）。Bark 直接填 App 里复制的
  `https://api.day.app/<key>`，这里换成 Apprise 的 `barks://api.day.app/<key>`；
  已是 Apprise 形态的 URL（`barks://自建服务器/<key>`、`feishu://…`、`mailtos://…`）
  原样透传——新增渠道只改配置不改代码。
- 未配置 → `send()` 返回 status=unconfigured 的显式结果，绝不抛异常、绝不假装发送成功。
- 发送失败只记日志并返回失败结果，**永不向业务代码抛异常**（告警推送挂了不能拖垮
  检查器或备份脚本）。
- URL 里含设备 key / token：日志、API、命令行输出一律只出现 `mask_url` 的结果。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import settings
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
SEVERITY_LABELS = {"info": "提示", "warning": "警告", "critical": "严重"}

BARK_HOSTS = frozenset({"api.day.app"})
BARK_SCHEMES = frozenset({"bark", "barks"})
BARK_GROUP = "investment-tracker"
# Apprise bark 插件的 level 取值之一：时效性通知，可突破「专注模式」
BARK_CRITICAL_LEVEL = "timeSensitive"

# 通知类型：alert=告警/提醒/升级，resolved=已恢复，test=测试通知
KIND_ALERT = "alert"
KIND_RESOLVED = "resolved"
KIND_TEST = "test"

STATUS_UNCONFIGURED = "unconfigured"
STATUS_SENT = "sent"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# 逗号只在后面紧跟一个新 scheme 时才算分隔符：Apprise URL 的查询串里本身就有逗号
# （如 mailto 的 `to=a@x.com,b@y.com`）
_SPLIT_RE = re.compile(r"\s+|,(?=\s*[A-Za-z][A-Za-z0-9+.-]*://)")
_DOMAIN_RE = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(:\d+)?")


@dataclass(frozen=True)
class Channel:
    url: str  # 规范化后的 Apprise URL（含密钥，不得外泄）
    kind: str  # bark / 其他 scheme；解析失败为 invalid
    masked: str
    error: Optional[str] = None  # 解析失败的原因（不含 URL 本身）


def split_notify_urls(value: Optional[str]) -> List[str]:
    return [item.strip().strip(",") for item in _SPLIT_RE.split(value or "") if item.strip(" ,")]


def normalize_url(entry: str) -> str:
    """`https://api.day.app/<key>[/...]` → `barks://api.day.app/<key>`；其余原样。

    Bark App 里复制出来的示例 URL 形如 `https://api.day.app/<key>/推送内容`，
    只取第一段路径作设备 key。自建 Bark 服务器的 https 地址无法与其他 https webhook
    区分，须直接写 Apprise 形态 `barks://host/<key>`。
    """
    parts = urlsplit(entry.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme in ("http", "https") and host in BARK_HOSTS:
        segments = [segment for segment in parts.path.split("/") if segment]
        key = segments[0] if segments else ""
        new_scheme = "barks" if scheme == "https" else "bark"
        return urlunsplit((new_scheme, parts.netloc, f"/{key}" if key else "", parts.query, ""))
    return entry.strip()


def channel_kind(url: str) -> str:
    scheme = urlsplit(url).scheme.lower()
    if scheme in BARK_SCHEMES:
        return "bark"
    return scheme or "unknown"


def _mask_segment(segment: str) -> str:
    if not segment:
        return segment
    return f"{segment[:2]}***" if len(segment) > 6 else "***"


_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def mask_url(url: str) -> str:
    """只留 scheme 与看起来像公网域名的主机；用户名/密码、路径（设备 key）、查询串全部遮掉。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        match = _SCHEME_RE.match(url or "")
        return f"{match.group(1)}://***" if match else "***"
    scheme = parts.scheme or "?"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    host = netloc if _DOMAIN_RE.fullmatch(netloc) else _mask_segment(netloc)
    path = "/".join(_mask_segment(segment) for segment in parts.path.split("/"))
    query = "?…" if parts.query else ""
    return f"{scheme}://{host}{path}{query}"


def parse_channels(value: Optional[str] = None) -> List[Channel]:
    """逐个渠道解析：一个写坏的 URL（如 `barks://[bad/key`，urlsplit 抛 ValueError）只让
    它自己变成 invalid 渠道，不影响其他渠道、不让调用方抛异常（PR #255 评审）。"""
    raw = settings.notify_urls if value is None else value
    channels = []
    for entry in split_notify_urls(raw):
        try:
            url = normalize_url(entry)
            kind = channel_kind(url)
            apply_severity(url, "critical")  # 发送时还要再拆一次查询串：现在就验证
            channels.append(Channel(url=url, kind=kind, masked=mask_url(url)))
        except Exception as exc:  # noqa: BLE001 - 坏 URL 只影响它自己
            channels.append(
                Channel(
                    url=entry,
                    kind="invalid",
                    masked=mask_url(entry),
                    error=f"URL 格式无效（{type(exc).__name__}）",
                )
            )
    return channels


def apply_severity(url: str, severity: str) -> str:
    """Bark：补 group（同一分组折叠），严重告警加 level=timeSensitive；用户已写的参数不覆盖。"""
    if channel_kind(url) != "bark":
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("group", BARK_GROUP)
    if severity == "critical":
        query.setdefault("level", BARK_CRITICAL_LEVEL)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def normalize_severity(value: Optional[str], default: str = "warning") -> str:
    value = (value or "").strip().lower()
    return value if value in SEVERITY_RANK else default


def should_push(severity: str) -> bool:
    threshold = normalize_severity(settings.notify_min_severity)
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK[threshold]


def _load_apprise():
    import apprise  # 延迟导入：缺库时显式降级，而不是整个 Web 进程起不来

    return apprise


# 测试替身注入点：返回 Apprise 模块（需有 Apprise 类与 NotifyType）
apprise_loader: Callable[[], Any] = _load_apprise


def _notify_type(apprise_module, severity: str, kind: str):
    types = apprise_module.NotifyType
    if kind == KIND_RESOLVED:
        return types.SUCCESS
    if kind == KIND_TEST:
        return types.INFO
    return {"critical": types.FAILURE, "warning": types.WARNING}.get(severity, types.INFO)


def channel_summary(value: Optional[str] = None) -> Dict[str, Any]:
    """渠道概况（不外呼）：每个渠道的类型、脱敏地址、Apprise 能否识别。"""
    channels = parse_channels(value)
    try:
        apprise_module = apprise_loader()
    except ImportError:
        apprise_module = None
    items = []
    for channel in channels:
        valid = False
        if channel.error is None and apprise_module is not None:
            try:
                valid = bool(apprise_module.Apprise().add(channel.url))
            except Exception:  # noqa: BLE001 - 识别失败按无效渠道报告
                valid = False
        items.append({"kind": channel.kind, "channel": channel.masked, "valid": valid})
    valid_count = sum(1 for item in items if item["valid"])
    return {
        "configured": bool(items),
        "count": len(items),
        "valid_count": valid_count,
        "apprise_available": apprise_module is not None,
        "min_severity": normalize_severity(settings.notify_min_severity),
        "channels": items,
    }


def send(
    title: str,
    body: str,
    *,
    severity: str = "warning",
    kind: str = KIND_ALERT,
    urls: Optional[str] = None,
) -> Dict[str, Any]:
    """向全部渠道发送一条通知。永不抛异常；返回逐渠道结果（地址已脱敏）。"""
    try:
        channels = parse_channels(urls)
    except Exception as exc:  # noqa: BLE001 - 兜底：配置读取本身出错也不得抛给调用方
        logger.error("通知渠道解析失败：%s", type(exc).__name__)
        return {
            "configured": 0,
            "sent": 0,
            "channels": [],
            "ok": False,
            "status": STATUS_FAILED,
            "message": "通知渠道配置无法解析",
        }
    base = {"configured": len(channels), "sent": 0, "channels": []}
    if not channels:
        return {
            **base,
            "ok": False,
            "status": STATUS_UNCONFIGURED,
            "message": "未配置通知渠道（NOTIFY_URLS 为空），未发送",
        }
    try:
        apprise_module = apprise_loader()
    except ImportError:
        logger.error("通知发送失败：未安装 apprise")
        return {
            **base,
            "ok": False,
            "status": STATUS_FAILED,
            "message": "未安装 apprise，无法发送通知",
        }

    results = []
    for channel in channels:
        entry = {"kind": channel.kind, "channel": channel.masked, "ok": False, "error": None}
        if channel.error is not None:
            entry["error"] = channel.error
            logger.warning("通知渠道 %s 无效：%s（%s）", channel.masked, channel.error, title)
            results.append(entry)
            continue
        try:
            notifier = apprise_module.Apprise()
            if not notifier.add(apply_severity(channel.url, severity)):
                entry["error"] = "URL 无法识别（Apprise 不支持该格式）"
            elif notifier.notify(
                title=title,
                body=body or title,
                notify_type=_notify_type(apprise_module, severity, kind),
            ):
                entry["ok"] = True
            else:
                entry["error"] = "发送失败（渠道返回错误或网络不可达，详见日志）"
        except Exception as exc:  # noqa: BLE001 - 推送失败不得影响调用方
            entry["error"] = f"{type(exc).__name__}"
        if not entry["ok"]:
            logger.warning("通知发送失败 %s：%s（%s）", channel.masked, entry["error"], title)
        results.append(entry)

    sent = sum(1 for item in results if item["ok"])
    if sent == len(results):
        status, message = STATUS_SENT, f"已发送到 {sent} 个渠道"
    elif sent:
        status, message = STATUS_PARTIAL, f"部分发送成功：{sent}/{len(results)} 个渠道"
    else:
        status, message = STATUS_FAILED, f"发送失败：{len(results)} 个渠道均未成功"
    return {
        **base,
        "sent": sent,
        "channels": results,
        "ok": sent > 0,
        "status": status,
        "message": message,
    }


def send_test() -> Dict[str, Any]:
    return send(
        "投资追踪系统 · 测试通知",
        "这是一条测试通知：收到即说明告警推送渠道配置正确。",
        severity="warning",
        kind=KIND_TEST,
    )


# Apprise 自己的日志只要 WARNING 以上：DEBUG/INFO 里可能带请求细节
logging.getLogger("apprise").setLevel(logging.WARNING)
