"""雪球采集器的纯函数层：签名金样、时间线/评论/全文解析、WAF 识别、Cookie 读取与到期判据。

fixtures 取自 2026-09-27 的真实响应（tests/fixtures/xueqiu_collector/），只裁掉解析
用不到的字段与登录账号信息；签名金样由原 archiver 的 JS 在固定时间戳下一次性生成。
"""

import json
from pathlib import Path

import pytest

from app.services.xueqiu_collector import client as client_mod
from app.services.xueqiu_collector import cookie_health
from app.services.xueqiu_collector.comments import (
    build_comments_url,
    comment_page_limit_for_post,
    matched_replies_on_page,
    utterance_from_reply,
)
from app.services.xueqiu_collector.common import CandidatePost, format_timestamp, reply_dedupe_key
from app.services.xueqiu_collector.detail import (
    DETAIL_EMPTY,
    fetch_post_detail,
    parse_post_detail_html,
    should_use_fetched_detail,
)
from app.services.xueqiu_collector.signer import (
    compress_to_base64,
    rolling_hash,
    sign_url,
    signature_for,
)
from app.services.xueqiu_collector.timeline import (
    build_user_timeline_url,
    candidates_and_utterances,
    filter_page_statuses,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xueqiu_collector"
AUTHOR = "1000000001"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #
GOLDENS = _load("signer_goldens.json")["goldens"]


@pytest.mark.parametrize("golden", GOLDENS, ids=lambda g: f"{g['now_ms']}-{g['url'][19:60]}")
def test_signer_matches_original_js_byte_for_byte(golden):
    assert sign_url(golden["url"], now_ms=golden["now_ms"]) == golden["signed"]


def test_signer_goldens_cover_the_endpoints_we_call():
    urls = {g["url"].split("?")[0] for g in GOLDENS}
    assert "https://xueqiu.com/v4/statuses/user_timeline.json" in urls
    assert "https://xueqiu.com/statuses/comments.json" in urls
    assert any("/statuses/" not in u and u.count("/") == 4 for u in urls)  # 全文页


def test_signer_replaces_existing_signature_param():
    golden = GOLDENS[0]
    resigned = sign_url(golden["signed"], now_ms=golden["now_ms"])
    assert resigned == golden["signed"]
    assert resigned.count("md5__1038=") == 1


def test_signer_building_blocks():
    # int32 溢出回绕：长串的哈希必须落在 int32 区间内（JS 的 `| 0` 语义）
    value = rolling_hash("https%3A%2F%2Fxueqiu.com%2F" * 50)
    assert -(2**31) <= value < 2**31
    # 站点调用 compressToBase64 不补 '='
    token = compress_to_base64("123|0|1758950400123|1")
    assert not token.endswith("=")
    assert compress_to_base64("123|0|1758950400123|1", pad=True).startswith(token)
    # 时钟注入：同一时刻同一签名
    url = build_user_timeline_url(AUTHOR, 1, 3)
    assert signature_for(url, now_ms=1) == signature_for(url, now_ms=1)
    assert signature_for(url, now_ms=1) != signature_for(url, now_ms=2)


# --------------------------------------------------------------------------- #
# 时间线
# --------------------------------------------------------------------------- #
def test_timeline_fixture_to_utterances_and_candidates():
    statuses = _load("timeline_user_1000000001_p1.json")["statuses"]
    candidates, utterances = candidates_and_utterances(statuses, AUTHOR)
    by_key = {u.utterance_key: u for u in utterances}
    assert len(by_key) == len(statuses)
    # 键形态 = 原仓库存量：profile:{uid}:{post_id}
    assert all(key.startswith(f"profile:{AUTHOR}:") for key in by_key)

    reply = by_key[f"profile:{AUTHOR}:410664844"]
    assert reply.kind == "homepage_reply"  # 有被转发原帖且正文以「回复」开头
    assert reply.source == "profile_timeline"
    assert reply.post_url == f"https://xueqiu.com/{AUTHOR}/410664844"  # 取 target
    assert reply.context_post_id == "410662398"
    assert reply.context_author_name == "陈达美股投资"
    # created_at 文本按业务时区（东八区），与 archiver 在宿主机上写的存量形态一致
    assert reply.created_at == "2026-09-27 15:07:50"
    assert reply.created_at == format_timestamp(reply.created_at_ms)

    assert by_key[f"profile:{AUTHOR}:410596061"].kind == "homepage_post"
    assert by_key[f"profile:{AUTHOR}:410596061"].context_post_id == ""
    assert by_key[f"profile:{AUTHOR}:410664808"].kind == "homepage_repost"
    assert "$" in by_key[f"profile:{AUTHOR}:410664808"].context_text  # cashtag 原样保留

    # 被转发原帖作者 user_id=-1（股票公告号）：作者 ID 置空，链接退回 statuses/
    stock_ctx = by_key[f"profile:{AUTHOR}:410611062"]
    assert stock_ctx.context_url.startswith("https://xueqiu.com/")

    sources = {c.post_id: c.source for c in candidates}
    assert sources["410596061"] == f"monitor:{AUTHOR}"  # 原创帖：候选即本帖
    assert sources["410662398"] == f"monitor-retweeted:{AUTHOR}"  # 转发：候选是原帖


def test_filter_page_statuses_window():
    statuses = _load("timeline_user_1000000001_p1.json")["statuses"]
    since_ms = 1790000000000  # 2026-09-21 左右
    kept, saw_recent = filter_page_statuses(statuses, since_ms)
    assert saw_recent is True
    assert all(int(s["created_at"]) >= since_ms for s in kept)
    assert len(kept) == len(statuses) - 1  # 置顶的 2015 年旧帖被窗口挡掉

    kept_old, saw_old = filter_page_statuses(statuses, 2_000_000_000_000)
    assert kept_old == [] and saw_old is False


# --------------------------------------------------------------------------- #
# 评论
# --------------------------------------------------------------------------- #
POST = CandidatePost(
    post_id="61213445",
    url="https://xueqiu.com/7911779762/61213445",
    author_id="7911779762",
    author_name="fuyuan乞士",
    text="粉有何用？问 @某作者 ？",
)


def test_comments_fixture_matches_target_user_only():
    comments = _load("comments_post_61213445_p1.json")["comments"]
    target = "6528381690"
    matches = matched_replies_on_page(comments, POST, target, since_ms=0)
    assert {m.comment_id for m in matches} == {"422678792", "422678077"}
    reply = next(m for m in matches if m.comment_id == "422678792")
    assert reply.post_url == POST.url
    assert reply.author_name
    assert reply.reply_to  # 回复别人时带「昵称: 原文」
    assert reply_dedupe_key(reply) == "422678792"

    utterance = utterance_from_reply(target, reply, POST)
    assert utterance.utterance_key == "comment:422678792"
    assert utterance.kind == "comment_reply" and utterance.source == "comment_scan"
    assert utterance.context_text == POST.text

    # 回看窗口：早于 since 的回复不收
    newest = max(int(c["created_at"]) for c in comments)
    assert matched_replies_on_page(comments, POST, target, since_ms=newest + 1) == []


def test_comment_without_reply_comment_has_empty_reply_to():
    comments = _load("comments_post_61213445_p1.json")["comments"]
    lone = next(c for c in comments if c["id"] == 422693278)
    matches = matched_replies_on_page([lone], POST, str(lone["user"]["id"]), since_ms=0)
    assert matches[0].reply_to == ""


def test_comments_url_and_stale_page_limit():
    assert build_comments_url("1", 2) == (
        "https://xueqiu.com/statuses/comments.json?id=1&count=20&page=2&reply=true&split=true"
    )
    now = 1_800_000_000.0
    old = CandidatePost(post_id="1", url="u", created_at_ms=int((now - 40 * 86400) * 1000))
    fresh = CandidatePost(post_id="2", url="u", created_at_ms=int((now - 1 * 86400) * 1000))
    assert comment_page_limit_for_post(old, 6, 30, 1, now=now) == 1
    assert comment_page_limit_for_post(fresh, 6, 30, 1, now=now) == 6
    assert comment_page_limit_for_post(old, 0, 30, 2, now=now) == 2  # 0 = 不限页，旧帖仍封顶


def test_reply_key_fallback_without_comment_id():
    comment = {"user": {"id": 5, "screen_name": "x"}, "created_at": 1000, "text": "hi"}
    reply = matched_replies_on_page([comment], POST, "5", since_ms=0)[0]
    assert reply_dedupe_key(reply) == "61213445|1000|5|hi"


# --------------------------------------------------------------------------- #
# 全文页 / WAF
# --------------------------------------------------------------------------- #
def test_detail_html_fixture():
    html = (FIXTURES / "detail_7911779762_61213445.html").read_text(encoding="utf-8")
    text = parse_post_detail_html(html)
    assert text.splitlines()[1:] == ["粉有何用？问", "@某作者", "？"]
    assert should_use_fetched_detail(text)
    assert parse_post_detail_html("<html><body>nothing</body></html>") == DETAIL_EMPTY
    assert parse_post_detail_html("") == DETAIL_EMPTY
    assert not should_use_fetched_detail(DETAIL_EMPTY)
    assert not should_use_fetched_detail("【抓取失败】：boom")


class FakeResponse:
    def __init__(self, body, status_code=200, content_type="application/json"):
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.content = self.text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


class NoWaitThrottle(client_mod.PoliteThrottle):
    def wait(self):
        pass

    def mark(self, min_delay, max_delay):
        pass


def _client(responder):
    return client_mod.XueqiuWebClient(
        {"xq_a_token": "x"}, throttle=NoWaitThrottle(),
        http_get=lambda url, **kwargs: responder(url),
    )


def test_waf_fixture_detected_before_status_check():
    waf = (FIXTURES / "waf_challenge.html").read_text(encoding="utf-8")
    assert client_mod.is_waf_challenge_text(waf)
    client = _client(lambda url: FakeResponse(waf, content_type="text/html"))
    with pytest.raises(client_mod.WafChallenge):
        client.get_json(build_user_timeline_url(AUTHOR, 1, 3), context="t")
    # 全文页不再把 WAF 吞成「未抓取到内容」（那会把帖子永久标成已补全）
    with pytest.raises(client_mod.WafChallenge):
        fetch_post_detail(client, POST.url)
    # 其他失败仍落成原语义的占位文本
    broken = _client(lambda url: FakeResponse("x", status_code=500, content_type="text/html"))
    assert fetch_post_detail(broken, POST.url).startswith("【抓取失败】")


def test_client_signs_every_request_and_parses_json():
    seen = []

    def responder(url):
        seen.append(url)
        return FakeResponse({"statuses": []})

    client = _client(responder)
    assert client.get_json(build_user_timeline_url(AUTHOR, 1, 3), context="t") == {"statuses": []}
    assert "md5__1038=" in seen[0]
    assert client.request_count == 1

    not_json = _client(lambda url: FakeResponse("<html>oops</html>", content_type="text/html"))
    assert not_json.get_json("https://xueqiu.com/x.json", context="t") is None
    array = _client(lambda url: FakeResponse([1, 2]))
    assert array.get_json("https://xueqiu.com/x.json", context="t") is None


def test_connection_errors_are_retried():
    import requests

    calls = {"n": 0}

    def flaky(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("dns")
        return FakeResponse({"ok": 1})

    client = client_mod.XueqiuWebClient(
        {"a": "b"}, throttle=NoWaitThrottle(), http_get=flaky, sleep=lambda s: None
    )
    assert client.get_json("https://xueqiu.com/x.json", context="t") == {"ok": 1}
    assert calls["n"] == 3


def test_polite_throttle_waits_between_requests():
    clock = {"t": 100.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    import random

    throttle = client_mod.PoliteThrottle(
        sleep=fake_sleep, monotonic=lambda: clock["t"], rng=random.Random(1)
    )
    throttle.wait()
    throttle.mark(10, 25)
    throttle.wait()
    assert len(sleeps) == 1 and 10 <= sleeps[0] <= 25


# --------------------------------------------------------------------------- #
# Cookie：读取（显式降级）与到期判据
# --------------------------------------------------------------------------- #
def test_cookies_unconfigured_is_explicit(monkeypatch):
    monkeypatch.setattr(client_mod.settings, "xueqiu_cookies", "")
    monkeypatch.setattr(client_mod.settings, "xueqiu_cookie_file", "")
    with pytest.raises(client_mod.CollectorUnavailable, match="未配置"):
        client_mod.load_collector_cookies()


def test_cookies_from_env_json_and_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        client_mod.settings, "xueqiu_cookies",
        json.dumps({"cookies": [{"name": "xq_a_token", "value": "t"}]}),
    )
    assert client_mod.load_collector_cookies() == {"xq_a_token": "t"}

    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"name": "xq_a_token", "value": "f"}]))
    monkeypatch.setattr(client_mod.settings, "xueqiu_cookies", "")
    monkeypatch.setattr(client_mod.settings, "xueqiu_cookie_file", str(path))
    assert client_mod.load_collector_cookies() == {"xq_a_token": "f"}

    monkeypatch.setattr(client_mod.settings, "xueqiu_cookie_file", str(tmp_path / "missing"))
    with pytest.raises(client_mod.CollectorUnavailable, match="无法读取"):
        client_mod.load_collector_cookies()


def _j2team(tmp_path, cookies):
    path = tmp_path / "xueqiu.com.json"
    path.write_text(json.dumps({"url": "https://xueqiu.com", "cookies": cookies}))
    return str(path)


def test_cookie_expiry_levels(tmp_path):
    now = 1_800_000_000.0
    day = 86400
    path = _j2team(tmp_path, [
        {"name": "xq_a_token", "value": "v", "expirationDate": now + 10 * day},
        {"name": "xqat", "value": "v", "expirationDate": now + 5 * day},
        {"name": "u", "value": "v"},
    ])
    result = cookie_health.check_expiry(path, warn_days=7, critical_days=3, now=now)
    assert result["level"] == "warning" and result["cookie"] == "xqat"
    assert round(result["days_left"]) == 5


def test_cookie_missing_primary_is_critical(tmp_path):
    """合并自原仓库脚本：主凭证缺失 = critical（此前本仓会静默只看剩下那个）。"""
    now = 1_800_000_000.0
    path = _j2team(tmp_path, [
        {"name": "xq_a_token", "value": "v", "expirationDate": now + 30 * 86400},
    ])
    result = cookie_health.check_expiry(path, warn_days=7, critical_days=3, now=now)
    assert result["level"] == "critical" and result["cookie"] == "xqat"


@pytest.mark.parametrize(
    "cookies,missing",
    [
        # 列表形态：值为空串 / 空白 / null / 缺 value 都等于没有
        ([{"name": "xq_a_token", "value": "v"}, {"name": "xqat", "value": ""}], "xqat"),
        ([{"name": "xq_a_token", "value": "  "}, {"name": "xqat", "value": "v"}], "xq_a_token"),
        ([{"name": "xq_a_token", "value": None}, {"name": "xqat", "value": "v"}], "xq_a_token"),
        ([{"name": "xq_a_token"}, {"name": "xqat", "value": "v"}], "xq_a_token"),
        # 同名后者覆盖前者（与加载器一致）：后来的空值让先前的有效值失效
        ([{"name": "xq_a_token", "value": "v"}, {"name": "xqat", "value": "ok"},
          {"name": "xqat", "value": ""}], "xqat"),
    ],
)
def test_cookie_empty_primary_value_is_critical(tmp_path, cookies, missing):
    """PR #255 评审：只有键、值为空的主凭证照样不可用，不得当成健康。"""
    now = 1_800_000_000.0
    for item in cookies:
        item["expirationDate"] = now + 30 * 86400
    path = _j2team(tmp_path, cookies)
    result = cookie_health.check_expiry(path, warn_days=7, critical_days=3, now=now)
    assert result["level"] == "critical" and result["cookie"] == missing
    assert "为空" in result["message"]


def test_cookie_later_valid_value_overrides_earlier_empty(tmp_path):
    now = 1_800_000_000.0
    path = _j2team(tmp_path, [
        {"name": "xq_a_token", "value": "v", "expirationDate": now + 30 * 86400},
        {"name": "xqat", "value": "", "expirationDate": now + 1 * 86400},
        {"name": "xqat", "value": "ok", "expirationDate": now + 20 * 86400},
    ])
    result = cookie_health.check_expiry(path, warn_days=7, critical_days=3, now=now)
    # 生效的是后一条：有值、20 天后到期（前一条的 1 天不作数）
    assert result["level"] == "normal" and round(result["days_left"]) == 20


def test_cookie_dict_file_with_empty_value_is_critical(tmp_path):
    path = tmp_path / "xueqiu.json"
    path.write_text(json.dumps({"xq_a_token": "v", "xqat": ""}))
    result = cookie_health.check_expiry(str(path), warn_days=7, critical_days=3)
    assert result["level"] == "critical" and result["cookie"] == "xqat"
    path.write_text(json.dumps({"xq_a_token": "v", "xqat": "v"}))
    assert cookie_health.check_expiry(str(path), warn_days=7, critical_days=3)["level"] == (
        "unconfigured"  # 字典形态没有到期日
    )


def test_effective_cookie_values_match_the_collector_loader():
    from app.services.xueqiu_collector.client import _cookies_from_data

    for data in (
        {"cookies": [{"name": "xqat", "value": "a"}, {"name": "xqat", "value": ""}]},
        [{"name": "xqat", "value": ""}, {"name": "xqat", "value": "b"}],
        {"xq_a_token": "", "xqat": "c"},
    ):
        assert cookie_health.effective_cookie_values(data) == _cookies_from_data(data)


def test_cookie_without_expiration_is_unconfigured(tmp_path):
    path = _j2team(tmp_path, [{"name": "xq_a_token", "value": "v"}, {"name": "xqat", "value": "v"}])
    assert cookie_health.check_expiry(path, warn_days=7, critical_days=3)["level"] == "unconfigured"
    assert cookie_health.check_expiry("", warn_days=7, critical_days=3)["level"] == "unconfigured"
    missing = cookie_health.check_expiry(str(tmp_path / "nope"), warn_days=7, critical_days=3)
    assert missing["level"] == "critical"


def test_uptime_kuma_push_url():
    url = cookie_health.build_push_url(
        "https://kuma.example/api/push/abc?status=up&msg=OK&ping=", status="down", message="坏了"
    )
    assert url.startswith("https://kuma.example/api/push/abc?")
    assert "status=down" in url and "ping=" in url
    assert "msg=%E5%9D%8F%E4%BA%86" in url
