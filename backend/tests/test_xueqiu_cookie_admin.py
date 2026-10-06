"""界面更新雪球 Cookie：解析校验、原子写入 + 备份 + 权限、配置拒绝、热加载、API 权限与不回显。

全部用合成 Cookie（`SYNTH_*`），不读任何真实 Cookie 文件。每个断言「响应/日志/错误里不含
Cookie 值」都按值子串逐一检查——Cookie 是登录凭证，任何一个出口带出值都是泄漏。
"""

import errno
import json
import logging
import os
import stat
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.user import User
from app.services import xueqiu_cookie_admin as admin
from app.services import xueqiu_source as xs
from app.services.xueqiu_collector import client as collector_client
from app.services.xueqiu_collector.cookie_health import check_expiry

NOW = 1_790_000_000.0  # 固定时钟：2026-09 附近
DAY = 86400

TOKEN_A = "SYNTH_A_TOKEN_7f3c9e0d1b2a4c5d"  # gitleaks:allow (synthetic test fixture)
TOKEN_AT = "SYNTH_XQAT_TOKEN_91e2d3c4b5a6"  # gitleaks:allow (synthetic test fixture)
TOKEN_R = "SYNTH_R_TOKEN_0a1b2c3d"  # gitleaks:allow (synthetic test fixture)
TOKEN_U = "SYNTH_U_1234567890"  # gitleaks:allow (synthetic test fixture)
SECRET_VALUES = (TOKEN_A, TOKEN_AT, TOKEN_R, TOKEN_U)


def j2team(*, a_days=12.0, at_days=12.0, a_value=TOKEN_A, drop=(), now=NOW):
    cookies = [
        {
            "domain": ".xueqiu.com",
            "expirationDate": now + a_days * DAY,
            "hostOnly": False,
            "httpOnly": True,
            "name": "xq_a_token",
            "path": "/",
            "sameSite": "unspecified",
            "secure": False,
            "session": False,
            "storeId": "0",
            "value": a_value,
        },
        {
            "domain": ".xueqiu.com",
            "expirationDate": now + at_days * DAY,
            "name": "xqat",
            "path": "/",
            "value": TOKEN_AT,
        },
        {
            "domain": ".xueqiu.com",
            "name": "xq_r_token",
            "path": "/",
            "value": TOKEN_R,
            "session": True,
            "unknownField": {"nested": TOKEN_R},
        },
        {"domain": ".xueqiu.com", "name": "u", "path": "/", "value": TOKEN_U},
    ]
    cookies = [c for c in cookies if c["name"] not in drop]
    return json.dumps({"url": "https://xueqiu.com", "cookies": cookies})


HEADER = f"Cookie: xq_a_token={TOKEN_A}; xqat={TOKEN_AT}; xq_r_token={TOKEN_R}; u={TOKEN_U}"


def assert_no_secret(text):
    for value in SECRET_VALUES:
        assert value not in text, "Cookie 值泄漏"


@pytest.fixture(autouse=True)
def _reset_singleton():
    xs.reset_client()
    yield
    xs.reset_client()


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    directory = tmp_path / "secrets"
    directory.mkdir()
    path = directory / "xueqiu.com.json"
    monkeypatch.setattr(settings, "xueqiu_cookies", "")
    monkeypatch.setattr(settings, "xueqiu_cookie_file", str(path))
    return path


def _block_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "xueqiu_market", None)


# --------------------------------------------------------------------------- #
# 解析与校验
# --------------------------------------------------------------------------- #


def test_parse_j2team_keeps_expiry_and_whitelisted_metadata():
    parsed = admin.parse_cookie_content(j2team())
    assert parsed.source_format == "j2team"
    assert parsed.as_dict()["xq_a_token"] == TOKEN_A
    first = parsed.items[0]
    assert first["expirationDate"] == NOW + 12 * DAY
    assert first["domain"] == ".xueqiu.com" and first["httpOnly"] is True
    # 未知字段（可能夹带任意内容）不落盘
    assert all("unknownField" not in item for item in parsed.items)
    admin.validate_cookies(parsed, now=NOW)


def test_parse_header_string_and_other_json_shapes():
    parsed = admin.parse_cookie_content(HEADER)
    assert parsed.source_format == "header"
    assert parsed.as_dict() == {
        "xq_a_token": TOKEN_A,
        "xqat": TOKEN_AT,
        "xq_r_token": TOKEN_R,
        "u": TOKEN_U,
    }
    assert parsed.expirations() == {}
    assert any("探活" in note for note in parsed.notes)
    admin.validate_cookies(parsed, now=NOW)

    as_dict = admin.parse_cookie_content(json.dumps({"xq_a_token": TOKEN_A, "xqat": TOKEN_AT}))
    assert as_dict.source_format == "json_dict"
    as_list = admin.parse_cookie_content(
        json.dumps([{"name": "xq_a_token", "value": TOKEN_A}, {"name": "xqat", "value": TOKEN_AT}])
    )
    assert as_list.source_format == "json_list"
    # 字节输入（文件上传）与 BOM
    from_bytes = admin.parse_cookie_content(("﻿" + j2team()).encode("utf-8"))
    assert from_bytes.as_dict()["xqat"] == TOKEN_AT
    # 多行请求头（从开发者工具复制时换行）
    multiline = admin.parse_cookie_content(f"xq_a_token={TOKEN_A};\nxqat={TOKEN_AT}\n")
    assert set(multiline.as_dict()) == {"xq_a_token", "xqat"}


@pytest.mark.parametrize(
    "content, fragment",
    [
        (j2team(drop=("xq_a_token",)), "xq_a_token"),
        (j2team(drop=("xqat",)), "xqat"),
        (j2team(a_value=""), "xq_a_token"),
        (f"xqat={TOKEN_AT}; u={TOKEN_U}", "xq_a_token"),
    ],
)
def test_missing_primary_credential_rejected(content, fragment):
    with pytest.raises(admin.CookieAdminError) as info:
        admin.validate_cookies(admin.parse_cookie_content(content), now=NOW)
    assert info.value.kind == "invalid"
    assert "缺少雪球登录凭证" in info.value.message and fragment in info.value.message
    assert_no_secret(info.value.message)


def test_expired_credential_rejected():
    with pytest.raises(admin.CookieAdminError) as info:
        admin.validate_cookies(admin.parse_cookie_content(j2team(a_days=-0.5)), now=NOW)
    assert info.value.kind == "invalid" and "已过期" in info.value.message
    assert "xq_a_token" in info.value.message
    assert_no_secret(info.value.message)


@pytest.mark.parametrize(
    "content, fragment",
    [
        ("", "内容为空"),
        ("   \n ", "内容为空"),
        ('{"cookies": [{"name": "xq_a_token", "value": "' + TOKEN_A + '"', "JSON 解析失败"),
        (f"just some garbage {TOKEN_A}", "name=value"),
        ('{"cookies": "' + TOKEN_A + '"}', "不是数组"),
        (json.dumps({"cookies": [TOKEN_A]}), "不是对象"),
        (json.dumps({"cookies": [{"name": "xq_a_token"}]}), "缺少 name 或 value"),
        (json.dumps({"xq_a_token": {"v": TOKEN_A}}), "不是字符串"),
        (json.dumps("just a string " + TOKEN_A), "结构无法识别"),
        (f"xq_a_token={TOKEN_A}\x01; xqat={TOKEN_AT}", "控制字符"),
        (f"bad name={TOKEN_A}; xqat={TOKEN_AT}", "名称不合法"),
        (b"\xff\xfe\x00garbage", "UTF-8"),
        ("x" * (admin.MAX_CONTENT_BYTES + 1), "过大"),
    ],
)
def test_garbage_rejected_without_echo(content, fragment):
    with pytest.raises(admin.CookieAdminError) as info:
        admin.parse_cookie_content(content)
    assert info.value.kind == "invalid"
    assert fragment in info.value.message, info.value.message
    assert_no_secret(info.value.message)


# --------------------------------------------------------------------------- #
# 写入：原子替换、备份、权限、所有读取方都读得懂
# --------------------------------------------------------------------------- #


def test_update_writes_j2team_readable_by_every_loader(cookie_file, monkeypatch):
    result = admin.update_cookie(j2team(), now=NOW)
    assert result["source_format"] == "j2team"
    assert result["backup_created"] is False
    assert stat.S_IMODE(os.stat(cookie_file).st_mode) == admin.FILE_MODE

    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["cookies"]} == {"xq_a_token", "xqat", "xq_r_token", "u"}
    # 到期检查（采集器每轮自检、状态卡、探活脚本同一份）
    health = check_expiry(str(cookie_file), warn_days=7, critical_days=3, now=NOW)
    assert health["level"] == "normal" and round(health["days_left"]) == 12
    # 采集器的本地兜底读取（私有库缺失时）
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "xueqiu_market", None)
        assert collector_client.load_collector_cookies()["xq_a_token"] == TOKEN_A
    # 私有库的 load_cookies（已安装才测）
    try:
        from xueqiu_market import load_cookies
    except ImportError:
        load_cookies = None
    if load_cookies is not None:
        assert load_cookies(str(cookie_file))["xqat"] == TOKEN_AT

    status = result["status"]
    assert status["source"] == "file" and status["writable"] is True
    assert status["keys"] == ["u", "xq_a_token", "xq_r_token", "xqat"]
    days = {fact["name"]: fact["days_left"] for fact in status["primary"]}
    assert round(days["xq_a_token"]) == 12 and round(days["xqat"]) == 12
    assert_no_secret(json.dumps(result, default=str))


def test_header_input_is_normalized_to_j2team_file(cookie_file):
    result = admin.update_cookie(HEADER, now=NOW)
    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    assert data["url"] == admin.J2TEAM_URL
    assert data["cookies"][0] == {"name": "xq_a_token", "value": TOKEN_A}
    assert result["status"]["level"] == "unconfigured"  # 无到期时间 → 只能靠探活
    assert any("探活" in note for note in result["notes"])


def test_backup_keeps_previous_file_and_is_overwritten(cookie_file):
    cookie_file.write_text("first-version", encoding="utf-8")
    first = admin.update_cookie(j2team(), now=NOW)
    backup = cookie_file.with_name(cookie_file.name + ".bak")
    assert first["backup_created"] is True
    assert backup.read_text(encoding="utf-8") == "first-version"
    assert stat.S_IMODE(os.stat(backup).st_mode) == admin.FILE_MODE
    written = cookie_file.read_bytes()

    admin.update_cookie(HEADER, now=NOW)
    assert backup.read_bytes() == written  # 只保留上一份
    leftovers = [p.name for p in cookie_file.parent.iterdir()]
    assert sorted(leftovers) == ["xueqiu.com.json", "xueqiu.com.json.bak"]  # 无临时文件残留
    status = admin.cookie_status(now=NOW)
    assert status["backup_exists"] is True and status["backup_mtime"] is not None


def test_invalid_content_leaves_existing_file_untouched(cookie_file):
    admin.update_cookie(j2team(), now=NOW)
    before = cookie_file.read_bytes()
    with pytest.raises(admin.CookieAdminError):
        admin.update_cookie(j2team(drop=("xq_a_token",)), now=NOW)
    assert cookie_file.read_bytes() == before
    assert not cookie_file.with_name(cookie_file.name + ".bak").exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 无视目录权限")
def test_not_writable_directory_gives_actionable_error(cookie_file):
    directory = cookie_file.parent
    directory.chmod(0o555)
    try:
        status = admin.cookie_status(now=NOW)
        assert status["writable"] is False and "chown" in status["writable_reason"]
        with pytest.raises(admin.CookieAdminError) as info:
            admin.update_cookie(j2team(), now=NOW)
    finally:
        directory.chmod(0o755)
    assert info.value.kind == "not_writable"
    assert "10001" in info.value.message and ":ro" in info.value.message
    assert not cookie_file.exists()


@pytest.mark.parametrize(
    "error, kind",
    [(errno.EROFS, "not_writable"), (errno.EACCES, "not_writable"), (errno.ENOSPC, "write_failed")],
)
def test_os_errors_during_write_are_mapped(cookie_file, monkeypatch, error, kind):
    def failing_mkstemp(*args, **kwargs):
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(admin.tempfile, "mkstemp", failing_mkstemp)
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(j2team(), now=NOW)
    assert info.value.kind == kind
    assert_no_secret(info.value.message)


def test_inline_or_missing_config_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "xueqiu_cookies", json.dumps({"xq_a_token": TOKEN_A, "xqat": TOKEN_AT})
    )
    monkeypatch.setattr(settings, "xueqiu_cookie_file", str(tmp_path / "x.json"))
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(j2team(), now=NOW)
    assert info.value.kind == "unconfigurable" and "XUEQIU_COOKIES" in info.value.message
    assert not (tmp_path / "x.json").exists()
    status = admin.cookie_status(now=NOW)
    assert status["source"] == "inline" and status["writable"] is False
    assert status["keys"] == ["xq_a_token", "xqat"]
    assert_no_secret(json.dumps(status, default=str))

    monkeypatch.setattr(settings, "xueqiu_cookies", "")
    monkeypatch.setattr(settings, "xueqiu_cookie_file", "")
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(j2team(), now=NOW)
    assert info.value.kind == "unconfigurable" and "XUEQIU_COOKIE_FILE" in info.value.message
    assert admin.cookie_status(now=NOW)["source"] == "none"


def test_path_that_is_a_directory_refused(cookie_file):
    cookie_file.mkdir()  # Docker 挂缺失文件时误建的目录
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(j2team(), now=NOW)
    assert info.value.kind == "unconfigurable" and "目录" in info.value.message


def test_probe_only_when_requested(cookie_file, monkeypatch):
    calls = []

    def fake_probe():
        calls.append(xs.get_client)
        return {"ok": True, "detail": "探活成功"}

    monkeypatch.setattr(xs, "probe", fake_probe)
    assert admin.update_cookie(j2team(), now=NOW)["probe"] is None
    assert calls == []
    result = admin.update_cookie(j2team(), probe=True, now=NOW)
    assert result["probe"] == {"ok": True, "detail": "探活成功"} and len(calls) == 1


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_logs_never_contain_cookie_values(cookie_file, monkeypatch):
    # 直接挂在模块 logger 上：全量跑时别的用例跑过 alembic 的 fileConfig（默认
    # disable_existing_loggers=True 会把已存在的 logger 置为 disabled），caplog 也可能因
    # configure_logging 关掉传播而收不到——这里显式恢复，只为观察本模块写出的日志
    monkeypatch.setattr(xs, "probe", lambda: {"ok": False, "detail": "失败"})
    handler = _ListHandler()
    loggers = [admin.logger, xs.logger]
    previous = [(lg.level, lg.disabled) for lg in loggers]
    previous_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for lg in loggers:
        lg.addHandler(handler)
        lg.disabled = False
        lg.setLevel(logging.DEBUG)
    try:
        admin.update_cookie(j2team(), probe=True, actor="admin", now=NOW)
        with pytest.raises(admin.CookieAdminError):
            admin.update_cookie(j2team(drop=("xqat",)), now=NOW)
    finally:
        for lg, (level, disabled) in zip(loggers, previous):
            lg.removeHandler(handler)
            lg.setLevel(level)
            lg.disabled = disabled
        logging.disable(previous_disable)
    text = "\n".join(handler.messages)
    assert "在界面更新" in text
    assert_no_secret(text)


# --------------------------------------------------------------------------- #
# PR #256 第三轮评审：坏到期时间、空白主凭证、同名覆盖的到期元数据
# 共同要求：不合格的输入绝不替换现有文件
# --------------------------------------------------------------------------- #


def _seed_good_file(path):
    """先落一份合格文件，返回其字节，用于断言失败的更新没有改动它。"""
    admin.update_cookie(j2team(), now=NOW)
    return path.read_bytes()


def _entry(name, value, expiration=None):
    item = {"domain": ".xueqiu.com", "name": name, "path": "/", "value": value}
    if expiration is not None:
        item["expirationDate"] = expiration
    return item


@pytest.mark.parametrize(
    "expiration",
    [
        1_800_000_000_000,  # 误填毫秒：fromtimestamp → year 59009 out of range
        1e300,  # OverflowError
        "1800000000000",  # 字符串形式同样拒绝
        "soon",  # 非数字
    ],
)
def test_unrepresentable_primary_expiry_rejected_before_write(cookie_file, expiration):
    before = _seed_good_file(cookie_file)
    content = json.dumps(
        {
            "cookies": [
                _entry("xq_a_token", TOKEN_A, expiration),
                _entry("xqat", TOKEN_AT, NOW + 5 * DAY),
            ]
        }
    )
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(content, now=NOW)
    assert info.value.kind == "invalid"
    assert "xq_a_token" in info.value.message and "expirationDate" in info.value.message
    assert_no_secret(info.value.message)
    assert cookie_file.read_bytes() == before


def test_bad_expiry_on_other_cookie_is_dropped_not_fatal(cookie_file):
    content = json.dumps(
        {
            "cookies": [
                _entry("xq_a_token", TOKEN_A, NOW + 5 * DAY),
                _entry("xqat", TOKEN_AT, NOW + 5 * DAY),
                _entry("u", TOKEN_U, 1_800_000_000_000),
            ]
        }
    )
    result = admin.update_cookie(content, now=NOW)
    assert any("u 的 expirationDate" in note for note in result["notes"])
    written = {c["name"]: c for c in json.loads(cookie_file.read_text())["cookies"]}
    assert "expirationDate" not in written["u"]


def test_status_degrades_when_file_already_has_bad_expiry(cookie_file):
    """文件已被宿主写进坏元数据：状态查询照常返回（critical + 原因），不抛异常。"""
    cookie_file.write_text(
        json.dumps(
            {
                "cookies": [
                    _entry("xq_a_token", TOKEN_A, 1_800_000_000_000),
                    _entry("xqat", TOKEN_AT, NOW + 5 * DAY),
                ]
            }
        ),
        encoding="utf-8",
    )
    health = check_expiry(str(cookie_file), warn_days=7, critical_days=3, now=NOW)
    assert health["level"] == "critical" and "超出" in health["message"]
    status = admin.cookie_status(now=NOW)
    assert status["level"] == "critical"
    assert "超出" in status["read_error"]
    assert_no_secret(json.dumps(status, default=str))


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"xq_a_token": "   ", "xqat": " "}),
        json.dumps({"xq_a_token": TOKEN_A, "xqat": "　 "}),  # 全角空格同样是空白
        f"xq_a_token=   ; xqat={TOKEN_AT}",
        json.dumps({"cookies": [_entry("xq_a_token", "  "), _entry("xqat", TOKEN_AT)]}),
        # 同名后者覆盖：最终值是空白
        json.dumps(
            {
                "cookies": [
                    _entry("xq_a_token", TOKEN_A),
                    _entry("xqat", TOKEN_AT),
                    _entry("xq_a_token", " "),
                ]
            }
        ),
    ],
)
def test_whitespace_primary_credentials_rejected(cookie_file, content):
    before = _seed_good_file(cookie_file)
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(content, now=NOW)
    assert info.value.kind == "invalid" and "缺少雪球登录凭证" in info.value.message
    assert cookie_file.read_bytes() == before


def test_status_reports_whitespace_credential_as_absent(cookie_file):
    cookie_file.write_text(json.dumps({"xq_a_token": TOKEN_A, "xqat": "  "}), encoding="utf-8")
    present = {fact["name"]: fact["present"] for fact in admin.cookie_status(now=NOW)["primary"]}
    assert present == {"xq_a_token": True, "xqat": False}


def test_later_session_cookie_drops_earlier_expiry(cookie_file):
    """旧的带过期时间的 xq_a_token 被后面无到期时间的新值覆盖：生效的是新会话 Cookie，
    到期时间随生效那一条（没有），不能拿旧条目的 1970 年判它已过期。"""
    content = json.dumps(
        {
            "cookies": [
                _entry("xq_a_token", "SYNTH_OLD_A", 1),
                _entry("xq_a_token", TOKEN_A),
                _entry("xqat", TOKEN_AT, NOW + 5 * DAY),
            ]
        }
    )
    result = admin.update_cookie(content, now=NOW)
    facts = {fact["name"]: fact for fact in result["status"]["primary"]}
    assert facts["xq_a_token"]["present"] is True
    assert facts["xq_a_token"]["days_left"] is None
    assert round(facts["xqat"]["days_left"]) == 5
    assert collector_client.load_collector_cookies()["xq_a_token"] == TOKEN_A


def test_later_expired_entry_wins_over_earlier_session_cookie(cookie_file):
    """反过来：后面的条目已过期，它就是生效值——拒绝，且不改动现有文件。"""
    before = _seed_good_file(cookie_file)
    content = json.dumps(
        {
            "cookies": [
                _entry("xq_a_token", TOKEN_A),
                _entry("xq_a_token", "SYNTH_OLD_A", 1),
                _entry("xqat", TOKEN_AT, NOW + 5 * DAY),
            ]
        }
    )
    with pytest.raises(admin.CookieAdminError) as info:
        admin.update_cookie(content, now=NOW)
    assert "已过期" in info.value.message and "1970-01-01" in info.value.message
    assert cookie_file.read_bytes() == before


def test_later_entry_with_new_expiry_replaces_old_expiry(cookie_file):
    content = json.dumps(
        {
            "cookies": [
                _entry("xq_a_token", "SYNTH_OLD_A", 1),
                _entry("xq_a_token", TOKEN_A, NOW + 9 * DAY),
                _entry("xqat", TOKEN_AT, NOW + 9 * DAY),
            ]
        }
    )
    result = admin.update_cookie(content, now=NOW)
    days = {fact["name"]: fact["days_left"] for fact in result["status"]["primary"]}
    assert round(days["xq_a_token"]) == 9


# --------------------------------------------------------------------------- #
# 热加载：backend 单例按文件指纹重建；采集器每次读取都重读文件
# --------------------------------------------------------------------------- #


class FakeMarketClient:
    def __init__(self, cookies=None, **kwargs):
        self.cookies = dict(cookies or {})
        self._next_request_at = 0.0


def _install_fake_library(monkeypatch):
    module = types.ModuleType("xueqiu_market")

    def load_cookies(source):
        data = json.loads(open(source, encoding="utf-8").read())
        if isinstance(data, dict) and "cookies" in data:
            return {c["name"]: c["value"] for c in data["cookies"]}
        return dict(data)

    module.load_cookies = load_cookies
    module.XueqiuMarketClient = FakeMarketClient
    monkeypatch.setitem(sys.modules, "xueqiu_market", module)


def test_backend_client_reloads_when_file_replaced(cookie_file, monkeypatch):
    _install_fake_library(monkeypatch)
    admin.update_cookie(j2team(), now=NOW)
    first = xs.get_client()
    assert first.cookies["xq_a_token"] == TOKEN_A
    assert xs.get_client() is first  # 文件没变：同一个单例
    first._next_request_at = 12345.0

    # 宿主上直接替换文件（不经 update_cookie、不 reset）也要被发现
    replacement = cookie_file.with_name("incoming.json")
    replacement.write_text(j2team(a_value="SYNTH_A_TOKEN_ROTATED"), encoding="utf-8")
    os.replace(replacement, cookie_file)
    second = xs.get_client()
    assert second is not first and second.cookies["xq_a_token"] == "SYNTH_A_TOKEN_ROTATED"
    assert second._next_request_at == 12345.0  # 限速时钟接续，不因换 Cookie 绕过限速

    # 经界面更新：同样靠新指纹重建
    admin.update_cookie(j2team(), now=NOW)
    assert xs.get_client().cookies["xq_a_token"] == TOKEN_A


def test_update_cookie_keeps_rate_limit_clock(cookie_file, monkeypatch):
    """PR #256 评审 P1：界面更新不得丢掉旧 client 的限速时钟。

    旧实现写完文件就 reset_client()，下一次 get_client() 从零构造、`_next_request_at=0`，
    刚发过请求又换 Cookie 时下一次请求会立即发出。"""
    _install_fake_library(monkeypatch)
    admin.update_cookie(j2team(), now=NOW)
    old = xs.get_client()
    old._next_request_at = time.monotonic() + 3.5  # 刚发过一次请求，下一次要等

    admin.update_cookie(j2team(a_value="SYNTH_A_TOKEN_SWAPPED"), now=NOW)
    new = xs.get_client()
    assert new is not old and new.cookies["xq_a_token"] == "SYNTH_A_TOKEN_SWAPPED"
    assert new._next_request_at >= old._next_request_at


def test_update_cookie_probe_uses_carried_clock(cookie_file, monkeypatch):
    """探活是换 Cookie 后的第一次请求：它拿到的 client 必须已接续旧时钟。"""
    _install_fake_library(monkeypatch)
    admin.update_cookie(j2team(), now=NOW)
    old = xs.get_client()
    old._next_request_at = 98765.0
    seen = []

    def fake_probe():
        seen.append(xs.get_client()._next_request_at)
        return {"ok": True, "detail": "探活成功"}

    monkeypatch.setattr(xs, "probe", fake_probe)
    admin.update_cookie(j2team(a_value="SYNTH_A_TOKEN_SWAPPED"), probe=True, now=NOW)
    assert seen == [98765.0]


def test_backend_client_rebuild_failure_degrades_then_recovers(cookie_file, monkeypatch):
    _install_fake_library(monkeypatch)
    admin.update_cookie(j2team(), now=NOW)
    assert xs.get_client() is not None
    time.sleep(0.01)
    cookie_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        xs.get_client()
    admin.update_cookie(j2team(), now=NOW)
    assert xs.get_client().cookies["xqat"] == TOKEN_AT


@pytest.mark.parametrize("library", ["fake", "missing"])
def test_collector_loader_rereads_file_every_call(cookie_file, monkeypatch, library):
    if library == "fake":
        _install_fake_library(monkeypatch)
    else:
        _block_library(monkeypatch)
    admin.update_cookie(j2team(), now=NOW)
    assert collector_client.load_collector_cookies()["xq_a_token"] == TOKEN_A
    admin.update_cookie(j2team(a_value="SYNTH_A_TOKEN_NEXT"), now=NOW)
    assert collector_client.load_collector_cookies()["xq_a_token"] == "SYNTH_A_TOKEN_NEXT"


# --------------------------------------------------------------------------- #
# API：仅管理员；响应与 422 都不回显 Cookie
# --------------------------------------------------------------------------- #

PASSWORD = "cookie-admin-api-password"


@pytest.fixture
def clients():
    db = SessionLocal()
    users = {u.username: u for u in db.query(User).filter(User.id.in_([1, 2])).all()}
    originals = {name: user.hashed_password for name, user in users.items()}
    for user in users.values():
        user.hashed_password = get_password_hash(PASSWORD)
    db.commit()
    result = {"anonymous": TestClient(app)}
    for name in ("admin", "demo"):
        client = TestClient(app)
        token = client.post(
            "/api/auth/token", json={"username": name, "password": PASSWORD}
        ).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        result[name] = client
    try:
        yield result
    finally:
        for name, user in users.items():
            user.hashed_password = originals[name]
        db.commit()
        db.close()


def test_cookie_endpoints_admin_only(clients, cookie_file):
    body = {"content": j2team()}
    assert clients["anonymous"].get("/api/xueqiu-collector/cookie").status_code == 401
    assert clients["anonymous"].put("/api/xueqiu-collector/cookie", json=body).status_code == 401
    assert clients["demo"].get("/api/xueqiu-collector/cookie").status_code == 403
    assert clients["demo"].put("/api/xueqiu-collector/cookie", json=body).status_code == 403
    assert not cookie_file.exists()


def test_cookie_update_via_api_never_returns_values(clients, cookie_file, monkeypatch):
    monkeypatch.setattr(xs, "probe", lambda: {"ok": True, "detail": "探活成功"})
    admin_client = clients["admin"]
    response = admin_client.put(
        "/api/xueqiu-collector/cookie", json={"content": HEADER, "probe": True}
    )
    assert response.status_code == 200, response.text
    assert_no_secret(response.text)
    payload = response.json()
    assert payload["source_format"] == "header"
    assert payload["probe"] == {"ok": True, "detail": "探活成功"}
    assert payload["status"]["keys"] == ["u", "xq_a_token", "xq_r_token", "xqat"]
    assert json.loads(cookie_file.read_text())["cookies"][0]["value"] == TOKEN_A

    status = admin_client.get("/api/xueqiu-collector/cookie")
    assert status.status_code == 200
    assert_no_secret(status.text)
    assert status.json()["file_exists"] is True and status.json()["writable"] is True

    collector_status = clients["demo"].get("/api/xueqiu-collector/status")
    assert collector_status.status_code == 200
    assert_no_secret(collector_status.text)


@pytest.mark.parametrize(
    "body",
    [
        {"content": j2team(drop=("xqat",))},  # 业务校验失败（422，服务层文案）
        {"content": json.loads(j2team())},  # 类型错误：对象而非字符串
        {"content": HEADER, "extra": TOKEN_R},  # 多余字段
        {"cookie": HEADER},  # 缺 content：默认错误会把整个 body 当 input 回显
        [HEADER],  # 顶层不是对象
    ],
)
def test_validation_errors_do_not_echo_cookie(clients, cookie_file, body):
    response = clients["admin"].put("/api/xueqiu-collector/cookie", json=body)
    assert response.status_code == 422, response.text
    assert_no_secret(response.text)
    assert not cookie_file.exists()


def test_raw_json_string_body_not_echoed(clients, cookie_file):
    response = clients["admin"].put(
        "/api/xueqiu-collector/cookie",
        content=json.dumps(HEADER),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert_no_secret(response.text)


def test_api_bad_expiry_is_422_and_get_stays_200(clients, cookie_file):
    admin_client = clients["admin"]
    good = admin_client.put(
        "/api/xueqiu-collector/cookie", json={"content": j2team(now=time.time())}
    )
    assert good.status_code == 200
    before = cookie_file.read_bytes()
    bad = json.dumps(
        {
            "cookies": [
                {"name": "xq_a_token", "value": TOKEN_A, "expirationDate": 1_800_000_000_000},
                {"name": "xqat", "value": TOKEN_AT},
            ]
        }
    )
    response = admin_client.put("/api/xueqiu-collector/cookie", json={"content": bad})
    assert response.status_code == 422
    assert_no_secret(response.text)
    assert cookie_file.read_bytes() == before

    # 宿主上已经写进了坏元数据：GET 仍然 200，把原因报出来
    cookie_file.write_text(bad, encoding="utf-8")
    status = admin_client.get("/api/xueqiu-collector/cookie")
    assert status.status_code == 200
    assert status.json()["level"] == "critical" and status.json()["read_error"]
    assert_no_secret(status.text)
    collector_status = clients["demo"].get("/api/xueqiu-collector/status")
    assert collector_status.status_code == 200


def test_inline_config_update_is_409(clients, monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "xueqiu_cookies", json.dumps({"xq_a_token": TOKEN_A, "xqat": TOKEN_AT})
    )
    monkeypatch.setattr(settings, "xueqiu_cookie_file", "")
    response = clients["admin"].put("/api/xueqiu-collector/cookie", json={"content": HEADER})
    assert response.status_code == 409
    assert "XUEQIU_COOKIES" in response.json()["detail"]
    assert_no_secret(response.text)
    status = clients["admin"].get("/api/xueqiu-collector/cookie")
    assert status.json()["source"] == "inline"
    assert_no_secret(status.text)
