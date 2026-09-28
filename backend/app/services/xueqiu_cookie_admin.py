"""雪球 Cookie 的界面更新（管理员）：解析校验 → 原子写入配置的 Cookie 文件 → 摘要。

Cookie 是登录凭证，本模块的每一个出口都只带**事实**不带值：
- 绝不记日志、绝不进 API 响应、绝不进错误信息（错误只说第几条/哪个 Cookie 名/什么问题）；
- 只写 `XUEQIU_COOKIE_FILE` 这一个位置（外加同目录一份 `.bak`），不进数据库；
- 只有管理员能调（权限在 API 层）。

读取方都不用改：落盘格式统一为 J2Team 导出（`{"url", "cookies": [{name, value,
expirationDate?...}]}`），私有库 `xueqiu_market.load_cookies`、采集器的本地兜底
`client._cookies_from_data` 与到期检查 `cookie_health.load_cookie_facts` 都读这一形状。
热加载：采集器每轮都重读文件；backend 的行情 client 单例按文件指纹（mtime/size/inode）
变化自动重建（`xueqiu_source.get_client`，在调用锁内、接续旧 client 的限速时钟）。
这里**不能** `reset_client()`：那会连同限速时钟一起丢掉旧 client，换完 Cookie 的下一次
请求立刻发出、绕过限速（PR #256 评审 P1）。原子 rename 必换 inode，指纹一定会变。

接受的输入：
1. 浏览器插件 J2Team 导出（`{"cookies": [...]}`）或裸 cookies 数组——保留
   expirationDate 等元数据，到期告警靠它；
2. `{name: value}` JSON；
3. 浏览器开发者工具里复制的请求头 `a=b; c=d`（可带 `Cookie:` 前缀）——没有到期时间，
   只能靠探活判断登录态。
"""

from __future__ import annotations

import errno
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config import settings
from ..core.logging import get_app_logger
from . import xueqiu_source
from .xueqiu_collector.cookie_health import (
    PRIMARY_AUTH_COOKIES,
    check_expiry,
    cookie_facts_from_data,
    expiration_seconds,
    load_cookie_facts,
)

logger = get_app_logger(__name__)

# 真实导出约 2-10KB；上限只防误贴大文件
MAX_CONTENT_BYTES = 256 * 1024
MAX_COOKIE_COUNT = 300
BACKUP_SUFFIX = ".bak"
# 属主读写 + 同组只读：backend 与 xueqiu-collector 是同一镜像、同一 uid（10001），属主位
# 已够两个容器读写；组读位留给宿主运维（目录按 DEPLOYMENT.md 设 setgid 后新文件归宿主
# 运维组）。其他人不可读——Cookie 是登录凭证，旧手册的 664 其实给多了。
FILE_MODE = 0o640
J2TEAM_URL = "https://xueqiu.com/"
# 两个管理员同时提交时，备份与替换成对串行（否则 .bak 可能是另一次提交的新文件）
_update_lock = threading.Lock()

# RFC 6265 cookie-name = token
_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# 值里不能有控制字符与分号（会破坏 Cookie 请求头）
_BAD_VALUE_RE = re.compile(r"[\x00-\x1f\x7f;]")
_HEADER_PREFIX_RE = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)
# J2Team 条目里除 name/value 外原样保留的元数据（其余字段丢弃，不保留未知内容）
_KEPT_FIELDS = (
    "domain", "hostOnly", "httpOnly", "path", "sameSite", "secure", "session", "storeId",
)

NOT_WRITABLE_HINT = (
    "请在宿主上执行一次（见 DEPLOYMENT.md 8.1）："
    'docker compose run --rm --user root backend sh -c '
    '"chown -R 10001:$(id -g) /app/secrets && chmod 2770 /app/secrets"'
    "；并确认 docker-compose.yml 里 backend 的 /app/secrets 挂载没有 :ro"
)


class CookieAdminError(Exception):
    """界面更新 Cookie 失败。message 可直接给用户看（保证不含 Cookie 值）。

    kind：invalid（内容不合格，422）/ unconfigurable（部署配置不支持界面更新，409）/
    not_writable（目录不可写，409）/ write_failed（其他写入错误，500 级但文案可读）。
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass
class ParsedCookies:
    items: List[Dict[str, Any]]
    source_format: str  # j2team / json_list / json_dict / header
    notes: List[str] = field(default_factory=list)

    def facts(self) -> Dict[str, Any]:
        """与到期检查/告警**同一份**判据（`cookie_health.cookie_facts_from_data`）：
        最终值同名后者覆盖、空白即缺失，到期时间取生效那一条（它没有就是没有）。"""
        return cookie_facts_from_data({"cookies": self.items})

    def as_dict(self) -> Dict[str, str]:
        return self.facts()["values"]

    def expirations(self) -> Dict[str, float]:
        return self.facts()["expirations"]


def _invalid(message: str) -> CookieAdminError:
    return CookieAdminError(message, kind="invalid")


def _decode(content: Union[str, bytes]) -> str:
    if isinstance(content, bytes):
        if len(content) > MAX_CONTENT_BYTES:
            raise _invalid(f"内容过大（上限 {MAX_CONTENT_BYTES // 1024}KB），请确认选对了文件")
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise _invalid("内容不是 UTF-8 文本，请上传浏览器插件导出的 JSON 文件") from None
    if not isinstance(content, str):
        raise _invalid("内容必须是文本")
    if len(content.encode("utf-8", errors="replace")) > MAX_CONTENT_BYTES:
        raise _invalid(f"内容过大（上限 {MAX_CONTENT_BYTES // 1024}KB），请确认选对了文件")
    return content.lstrip("﻿")


def _check_name_value(name: Any, value: Any, position: int) -> tuple[str, str]:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _invalid(f"第 {position} 条 Cookie 的名称不合法")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _invalid(f"Cookie {name} 的值不是字符串")
    text = str(value)
    if _BAD_VALUE_RE.search(text):
        raise _invalid(f"Cookie {name} 的值含有控制字符或分号")
    return name, text


def _item_from_export(raw: Any, position: int, notes: List[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _invalid(f"第 {position} 条 Cookie 不是对象（需 J2Team 导出的 {{name, value}} 条目）")
    if "name" not in raw or "value" not in raw:
        raise _invalid(f"第 {position} 条 Cookie 缺少 name 或 value 字段")
    name, value = _check_name_value(raw["name"], raw["value"], position)
    item: Dict[str, Any] = {"name": name, "value": value}
    for key in _KEPT_FIELDS:
        if key in raw and isinstance(raw[key], (str, bool, int, float)):
            item[key] = raw[key]
    expiration = raw.get("expirationDate")
    if expiration is not None:
        try:
            item["expirationDate"] = expiration_seconds(expiration)
        except ValueError as exc:
            # 主凭证的到期时间是告警依据：写进文件后状态页与到期检查都会读它，必须在
            # 原子替换之前拒绝（误填毫秒会让 fromtimestamp 溢出）；其余 Cookie 丢掉该字段
            if name in PRIMARY_AUTH_COOKIES:
                raise _invalid(f"Cookie {name} 的 {exc}") from None
            notes.append(f"Cookie {name} 的 {exc}，已忽略该字段")
    return item


def _parse_json(text: str) -> ParsedCookies:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # 只报位置：JSONDecodeError 的 doc 属性里是原文，绝不往外带
        raise _invalid(f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）") from None
    notes: List[str] = []
    if isinstance(data, dict) and "cookies" in data:
        entries, source_format = data["cookies"], "j2team"
        if not isinstance(entries, list):
            raise _invalid("J2Team 导出的 cookies 字段不是数组")
    elif isinstance(data, list):
        entries, source_format = data, "json_list"
    elif isinstance(data, dict):
        items = []
        for position, (name, value) in enumerate(data.items(), start=1):
            name, value = _check_name_value(name, value, position)
            items.append({"name": name, "value": value})
        return ParsedCookies(items=items, source_format="json_dict", notes=notes)
    else:
        raise _invalid("JSON 结构无法识别（需 J2Team 导出、cookies 数组或 {name: value}）")
    items = [_item_from_export(raw, position, notes) for position, raw in enumerate(entries, 1)]
    return ParsedCookies(items=items, source_format=source_format, notes=notes)


def _parse_header(text: str) -> ParsedCookies:
    body = _HEADER_PREFIX_RE.sub("", text, count=1)
    items = []
    for position, part in enumerate(re.split(r"[;\r\n]+", body), start=1):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise _invalid(
                f"第 {position} 段不是 name=value 形式（请粘贴完整的 Cookie 请求头或插件导出的 JSON）"
            )
        name, value = part.split("=", 1)
        name, value = _check_name_value(name.strip(), value.strip(), position)
        items.append({"name": name, "value": value})
    notes = ["请求头格式不含到期时间：无法提前告警，建议勾选「更新后探活」确认登录态"]
    return ParsedCookies(items=items, source_format="header", notes=notes)


def parse_cookie_content(content: Union[str, bytes]) -> ParsedCookies:
    """把用户提交的文本解析为 J2Team 条目。失败抛 CookieAdminError(kind=invalid)。"""
    text = _decode(content).strip()
    if not text:
        raise _invalid("内容为空")
    parsed = _parse_json(text) if text[0] in "{[\"" else _parse_header(text)
    if not parsed.items:
        raise _invalid("没有解析到任何 Cookie")
    if len(parsed.items) > MAX_COOKIE_COUNT:
        raise _invalid(f"Cookie 条目过多（{len(parsed.items)} 条，上限 {MAX_COOKIE_COUNT}）")
    return parsed


def _format_epoch(seconds: float) -> str:
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def validate_cookies(parsed: ParsedCookies, *, now: Optional[float] = None) -> None:
    """主凭证必须齐全且非空、带到期时间的不能已过期（与 cookie_health 同一份名单）。"""
    now = time.time() if now is None else now
    facts = parsed.facts()
    missing = facts["missing"]
    if missing:
        raise _invalid(
            f"缺少雪球登录凭证或为空：{', '.join(missing)}"
            "（请在已登录雪球的浏览器里重新导出完整 Cookie）"
        )
    expired = [
        f"{name}（{_format_epoch(expiration)} 已过期）"
        for name, expiration in facts["expirations"].items()
        if expiration <= now
    ]
    if expired:
        raise _invalid(f"登录凭证已过期：{'；'.join(expired)}，请重新登录雪球后再导出")


def _primary_facts(
    missing: List[str], expirations: Dict[str, float], now: float
) -> List[Dict[str, Any]]:
    """present = 最终值非空白（与 cookie_health.missing_primary_credentials 同口径）。"""
    facts = []
    for name in PRIMARY_AUTH_COOKIES:
        expiration = expirations.get(name)
        facts.append({
            "name": name,
            "present": name not in missing,
            "expires_at": (
                datetime.fromtimestamp(expiration, tz=timezone.utc)
                if expiration is not None else None
            ),
            "days_left": (expiration - now) / 86400 if expiration is not None else None,
        })
    return facts


def _mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _configured_source() -> str:
    if (settings.xueqiu_cookies or "").strip():
        return "inline"
    if (settings.xueqiu_cookie_file or "").strip():
        return "file"
    return "none"


def _writable_check(target: Optional[Path]) -> Optional[CookieAdminError]:
    """不能在界面更新的原因（错误对象，文案可直接展示）；可写返回 None。"""
    source = _configured_source()
    if source == "inline":
        return CookieAdminError(
            "当前用 XUEQIU_COOKIES 环境变量内联配置 Cookie，它优先于文件，界面无法修改环境变量。"
            "请在 .env 清空 XUEQIU_COOKIES、改配 XUEQIU_COOKIE_FILE（如 /app/secrets/xueqiu.com.json）"
            "后执行 docker compose up -d backend xueqiu-collector",
            kind="unconfigurable",
        )
    if source == "none" or target is None:
        return CookieAdminError(
            "未配置 XUEQIU_COOKIE_FILE：请在 .env 设置 XUEQIU_COOKIE_FILE（如 "
            "/app/secrets/xueqiu.com.json）后执行 docker compose up -d backend xueqiu-collector",
            kind="unconfigurable",
        )
    if target.is_dir():
        return CookieAdminError(
            f"{target} 是一个目录而不是文件（Docker 挂载缺失的文件路径时会误建目录），"
            "请在宿主上删除该空目录",
            kind="unconfigurable",
        )
    directory = target.parent
    if not directory.is_dir():
        return CookieAdminError(
            f"Cookie 文件所在目录 {directory} 不存在，请检查 XUEQIU_COOKIE_FILE 与挂载",
            kind="unconfigurable",
        )
    if not os.access(directory, os.W_OK | os.X_OK):
        return CookieAdminError(
            f"Cookie 目录 {directory} 对后端进程不可写（只读挂载或属主不对）。{NOT_WRITABLE_HINT}",
            kind="not_writable",
        )
    return None


def _target_path() -> Optional[Path]:
    path = (settings.xueqiu_cookie_file or "").strip()
    return Path(path) if path else None


def _backup_path(target: Path) -> Path:
    return target.with_name(target.name + BACKUP_SUFFIX)


def _summarize_current(now: float) -> Dict[str, Any]:
    """读当前生效的 Cookie（文件或内联）→ 名称与主凭证到期事实（与到期检查同一份
    `cookie_facts_from_data`）。值不出本函数；读不了只报原因，不带原文。"""
    source = _configured_source()
    facts: Optional[Dict[str, Any]] = None
    read_error: Optional[str] = None
    try:
        if source == "inline":
            facts = cookie_facts_from_data(json.loads(settings.xueqiu_cookies))
        elif source == "file":
            target = _target_path()
            if target is not None and target.is_file():
                facts = load_cookie_facts(target)
    except json.JSONDecodeError as exc:
        read_error = f"Cookie 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）"
    except OSError as exc:
        read_error = f"无法读取 Cookie 文件：{exc.strerror or type(exc).__name__}"
    except ValueError as exc:
        # cookie_health 的 ValueError 文案都是固定说明（不带原值），如「expirationDate 超出
        # 可表示的日期范围」——文件里已有坏元数据时状态照常返回，只在这里报原因
        read_error = f"Cookie 无法解析：{exc}"
    except (AttributeError, TypeError):
        read_error = "Cookie 结构无法识别（需 J2Team 导出、cookies 数组或 {name: value}）"
    names = facts["names"] if facts else set()
    missing = facts["missing"] if facts else list(PRIMARY_AUTH_COOKIES)
    expirations = facts["expirations"] if facts else {}
    return {
        "keys": sorted(names),
        "primary": _primary_facts(missing, expirations, now),
        "read_error": read_error,
    }


def cookie_status(*, now: Optional[float] = None) -> Dict[str, Any]:
    """GET 端点：当前配置来源、Cookie 名、主凭证到期、文件时间、是否可在界面更新。"""
    now = time.time() if now is None else now
    source = _configured_source()
    target = _target_path() if source == "file" else None
    unwritable = _writable_check(target)
    if source == "file":
        health = check_expiry(
            str(target),
            warn_days=settings.xueqiu_cookie_warn_days,
            critical_days=settings.xueqiu_cookie_critical_days,
            now=now,
        )
    elif source == "inline":
        health = {
            "level": "unconfigured",
            "message": "Cookie 由 XUEQIU_COOKIES 内联配置：到期不可预判，界面不可更新",
        }
    else:
        health = {"level": "unconfigured", "message": "未配置雪球 Cookie：采集器与雪球行情均不可用"}
    backup = _backup_path(target) if target is not None else None
    return {
        "source": source,
        "file_path": str(target) if target is not None else None,
        "file_exists": bool(target is not None and target.is_file()),
        "file_mtime": _mtime(target) if target is not None else None,
        "backup_exists": bool(backup is not None and backup.is_file()),
        "backup_mtime": _mtime(backup) if backup is not None else None,
        "writable": unwritable is None,
        "writable_reason": unwritable.message if unwritable is not None else None,
        "level": health["level"],
        "message": health["message"],
        **_summarize_current(now),
    }


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    """同目录临时文件 + fsync + rename：读取方永远只看到完整的旧文件或新文件。"""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            # mkstemp 建出的是 0600；fchmod 不受 umask 影响，rename 后模式随文件带过去
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _raise_for_os_error(exc: OSError, directory: Path) -> None:
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        raise CookieAdminError(
            f"Cookie 目录 {directory} 对后端进程不可写（{exc.strerror}）。{NOT_WRITABLE_HINT}",
            kind="not_writable",
        ) from None
    raise CookieAdminError(
        f"写入 Cookie 文件失败：{exc.strerror or type(exc).__name__}", kind="write_failed"
    ) from None


def serialize_j2team(parsed: ParsedCookies) -> bytes:
    payload = {"url": J2TEAM_URL, "cookies": parsed.items}
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _backup_existing(target: Path, notes: List[str]) -> bool:
    """把现有文件复制为 `<name>.bak`（覆盖更早的备份）。读不了旧文件只记 note 不阻断。"""
    if not target.is_file():
        return False
    try:
        previous = target.read_bytes()
    except OSError as exc:
        notes.append(f"旧 Cookie 文件不可读（{exc.strerror}），未生成备份")
        return False
    _write_atomic(_backup_path(target), previous)
    return True


def update_cookie(
    content: Union[str, bytes],
    *,
    probe: bool = False,
    actor: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """解析校验 → 备份旧文件 → 原子写入 → 让 backend 单例重建 →（可选）探活 → 摘要。"""
    now = time.time() if now is None else now
    target = _target_path() if _configured_source() == "file" else None
    blocked = _writable_check(target)
    if blocked is not None:
        raise blocked
    assert target is not None

    parsed = parse_cookie_content(content)
    validate_cookies(parsed, now=now)
    notes = list(parsed.notes)
    with _update_lock:
        try:
            backup_created = _backup_existing(target, notes)
            _write_atomic(target, serialize_j2team(parsed))
        except OSError as exc:
            _raise_for_os_error(exc, target.parent)
        _fsync_directory(target.parent)

    # 不 reset 单例：下一次 get_client() 按新文件指纹重建并接续限速时钟（见模块 docstring）
    facts = parsed.facts()
    primary = _primary_facts(facts["missing"], facts["expirations"], now)
    logger.info(
        "雪球 Cookie 已由管理员 %s 在界面更新：%d 条（%s），主凭证剩余天数 %s，备份=%s",
        actor or "?",
        len(parsed.items),
        parsed.source_format,
        {fact["name"]: (round(fact["days_left"], 1) if fact["days_left"] is not None else None)
         for fact in primary},
        backup_created,
    )

    probe_result = None
    if probe:
        probe_result = xueqiu_source.probe()
        logger.info("雪球 Cookie 更新后探活：%s", "成功" if probe_result["ok"] else "失败")
    return {
        "status": cookie_status(now=now),
        "backup_created": backup_created,
        "source_format": parsed.source_format,
        "notes": notes,
        "probe": probe_result,
    }
