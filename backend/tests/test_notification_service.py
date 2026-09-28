"""告警推送层（notification_service）：URL 规范化与脱敏、Bark 参数、Apprise 打桩发送。

全程不联网：Apprise 用假模块替换（`apprise_loader`），只校验我们交给它的 URL 与结果处理。
"""

import logging

import pytest

from app.services import notification_service as ns

KEY = "AbCdEfGhIjKlMnOpQrStUv"


def test_split_accepts_spaces_and_commas_but_keeps_query_commas():
    value = (
        f"https://api.day.app/{KEY} ,barks://bark.example.com/k2,"
        "mailtos://u:p@example.com?to=a@x.com,b@y.com\n feishu://token"
    )
    assert ns.split_notify_urls(value) == [
        f"https://api.day.app/{KEY}",
        "barks://bark.example.com/k2",
        "mailtos://u:p@example.com?to=a@x.com,b@y.com",
        "feishu://token",
    ]
    assert ns.split_notify_urls("") == []
    assert ns.split_notify_urls(None) == []


@pytest.mark.parametrize(
    "entry,expected",
    [
        (f"https://api.day.app/{KEY}", f"barks://api.day.app/{KEY}"),
        (f"https://api.day.app/{KEY}/", f"barks://api.day.app/{KEY}"),
        # Bark App 里复制的示例 URL 带推送内容：只取设备 key
        (f"https://api.day.app/{KEY}/这里改成你自己的推送内容", f"barks://api.day.app/{KEY}"),
        (f"https://API.DAY.APP/{KEY}?sound=bell", f"barks://API.DAY.APP/{KEY}?sound=bell"),
        (f"http://api.day.app/{KEY}", f"bark://api.day.app/{KEY}"),
        # 已是 Apprise 形态 / 其他 https webhook：原样透传
        (f"barks://bark.example.com/{KEY}", f"barks://bark.example.com/{KEY}"),
        ("https://hooks.example.com/abc", "https://hooks.example.com/abc"),
        ("feishu://token", "feishu://token"),
    ],
)
def test_normalize_url(entry, expected):
    assert ns.normalize_url(entry) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"barks://api.day.app/{KEY}",
        f"barks://api.day.app/{KEY}?group=x&level=critical",
        "mailtos://user:secretpass@example.com?to=a@x.com",
        "tgram://123456:SECRETBOTTOKEN/987654",
        f"feishu://{KEY}",
    ],
)
def test_mask_url_hides_keys_tokens_and_passwords(url):
    masked = ns.mask_url(url)
    for secret in (KEY, "secretpass", "SECRETBOTTOKEN", "987654", "user:"):
        assert secret not in masked
    assert masked.split("://")[0] == url.split("://")[0]


def test_mask_url_keeps_public_host_for_identification():
    assert ns.mask_url(f"barks://api.day.app/{KEY}") == "barks://api.day.app/Ab***"
    assert ns.mask_url("mailtos://u:p@example.com?to=a@x.com") == "mailtos://example.com?…"


def test_apply_severity_adds_group_and_time_sensitive_level_for_bark_only():
    base = f"barks://api.day.app/{KEY}"
    critical = ns.apply_severity(base, "critical")
    assert "level=timeSensitive" in critical and "group=investment-tracker" in critical
    warning = ns.apply_severity(base, "warning")
    assert "level=" not in warning and "group=investment-tracker" in warning
    # 用户显式写的参数不被覆盖
    custom = ns.apply_severity(f"{base}?level=critical&group=mine", "critical")
    assert "level=critical" in custom and "group=mine" in custom
    assert ns.apply_severity("feishu://token", "critical") == "feishu://token"


class FakeApprise:
    """记录 add/notify 的假 Apprise；按 URL 里的标记决定成败。"""

    calls: list = []

    def __init__(self):
        self.urls = []

    def add(self, url):
        if "invalid" in url:
            return False
        self.urls.append(url)
        return True

    def notify(self, title, body, notify_type):
        FakeApprise.calls.append({"urls": list(self.urls), "title": title, "body": body,
                                  "type": notify_type})
        if any("boom" in url for url in self.urls):
            raise RuntimeError(f"connection refused for {self.urls[0]}")
        return not any("fail" in url for url in self.urls)


class FakeNotifyType:
    INFO, SUCCESS, WARNING, FAILURE = "info", "success", "warning", "failure"


class FakeModule:
    Apprise = FakeApprise
    NotifyType = FakeNotifyType


@pytest.fixture
def fake_apprise(monkeypatch):
    FakeApprise.calls = []
    monkeypatch.setattr(ns, "apprise_loader", lambda: FakeModule)
    return FakeApprise


def test_send_unconfigured_is_explicit_and_never_raises(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", "")
    result = ns.send("t", "b")
    assert result["status"] == "unconfigured" and result["ok"] is False
    assert "未配置" in result["message"]
    assert fake_apprise.calls == []


def test_send_bark_critical_uses_barks_url_with_level(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", f"https://api.day.app/{KEY}")
    result = ns.send("标题", "正文", severity="critical")
    assert result["status"] == "sent" and result["ok"] and result["sent"] == 1
    [call] = fake_apprise.calls
    assert call["urls"][0].startswith(f"barks://api.day.app/{KEY}?")
    assert "level=timeSensitive" in call["urls"][0]
    assert call["type"] == "failure" and call["title"] == "标题"
    assert result["channels"] == [
        {"kind": "bark", "channel": "barks://api.day.app/Ab***", "ok": True, "error": None}
    ]


def test_send_resolved_and_test_kinds_map_notify_type(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", f"barks://api.day.app/{KEY}")
    ns.send("t", "b", severity="critical", kind=ns.KIND_RESOLVED)
    ns.send_test()
    assert [call["type"] for call in fake_apprise.calls] == ["success", "info"]


def test_send_partial_failure_and_exception_are_reported_masked(monkeypatch, fake_apprise, caplog):
    monkeypatch.setattr(
        ns.settings,
        "notify_urls",
        f"barks://api.day.app/{KEY} barks://api.day.app/fail{KEY} "
        f"barks://api.day.app/boom{KEY} invalid://{KEY}",
    )
    with caplog.at_level(logging.WARNING):
        result = ns.send("t", "b")
    assert result["status"] == "partial" and result["ok"] and result["sent"] == 1
    errors = [item["error"] for item in result["channels"]]
    assert errors[0] is None
    assert "发送失败" in errors[1]
    assert errors[2] == "RuntimeError"  # 异常文本可能带 URL：只留类型名
    assert "无法识别" in errors[3]
    rendered = repr(result) + caplog.text
    assert KEY not in rendered and f"fail{KEY}" not in rendered


def test_send_all_failed(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", f"barks://api.day.app/fail{KEY}")
    result = ns.send("t", "b")
    assert result["status"] == "failed" and result["ok"] is False


def test_send_without_apprise_installed_degrades(monkeypatch):
    def missing():
        raise ImportError("no apprise")

    monkeypatch.setattr(ns, "apprise_loader", missing)
    monkeypatch.setattr(ns.settings, "notify_urls", f"barks://api.day.app/{KEY}")
    result = ns.send("t", "b")
    assert result["status"] == "failed" and "apprise" in result["message"]
    summary = ns.channel_summary()
    assert summary["apprise_available"] is False and summary["valid_count"] == 0


def test_channel_summary_counts_valid_channels(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", f"https://api.day.app/{KEY} invalid://x")
    summary = ns.channel_summary()
    assert summary["configured"] and summary["count"] == 2 and summary["valid_count"] == 1
    assert summary["channels"][0] == {
        "kind": "bark", "channel": "barks://api.day.app/Ab***", "valid": True
    }
    assert KEY not in repr(summary)
    assert fake_apprise.calls == []  # 概况不外呼


def test_real_apprise_recognizes_normalized_bark_url(monkeypatch):
    """真实 Apprise（不发送）：规范化后的 Bark URL 与严重告警参数都能被识别。"""
    apprise = pytest.importorskip("apprise")
    url = ns.apply_severity(ns.normalize_url(f"https://api.day.app/{KEY}"), "critical")
    notifier = apprise.Apprise()
    assert notifier.add(url)
    assert "level=timeSensitive" in notifier.urls()[0]


@pytest.mark.parametrize(
    "threshold,expected",
    [("warning", [False, True, True]), ("info", [True, True, True]),
     ("critical", [False, False, True]), ("bogus", [False, True, True])],
)
def test_should_push_threshold(monkeypatch, threshold, expected):
    monkeypatch.setattr(ns.settings, "notify_min_severity", threshold)
    assert [ns.should_push(s) for s in ("info", "warning", "critical")] == expected


# --------------------------------------------------------------------------- #
# PR #255 评审：一个写坏的 URL 不得让 send/channel_summary 抛异常或拖累其他渠道
# --------------------------------------------------------------------------- #
BAD_URL = f"barks://[bad/{KEY}"  # urlsplit 抛 ValueError: Invalid IPv6 URL


def test_malformed_url_is_an_invalid_channel_not_an_exception(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", f"{BAD_URL} https://api.day.app/{KEY}")
    channels = ns.parse_channels()
    assert [c.kind for c in channels] == ["invalid", "bark"]
    assert channels[0].error and KEY not in channels[0].masked
    assert channels[0].masked == "barks://***"

    result = ns.send("t", "b", severity="critical")
    assert result["status"] == "partial" and result["sent"] == 1
    assert result["channels"][0] == {
        "kind": "invalid", "channel": "barks://***", "ok": False,
        "error": "URL 格式无效（ValueError）",
    }
    assert result["channels"][1]["ok"] is True
    assert len(fake_apprise.calls) == 1  # 好渠道照常发送

    summary = ns.channel_summary()
    assert summary["count"] == 2 and summary["valid_count"] == 1
    assert summary["channels"][0]["valid"] is False
    assert KEY not in repr(result) + repr(summary)


def test_only_malformed_url_fails_without_raising(monkeypatch, fake_apprise):
    monkeypatch.setattr(ns.settings, "notify_urls", BAD_URL)
    result = ns.send("t", "b")
    assert result["status"] == "failed" and result["ok"] is False
    assert fake_apprise.calls == []
