"""帖子全文页抓取（HTML，lxml 解析）。移植自 archive_timeline.fetch_post_detail。

占位文本 `【未抓取到帖子内容】` / `【抓取失败】：…` 逐字保留：原实现在候选帖本身
没有正文时会把它们写进 posts.text，存量数据里就是这两个形态。
"""

from __future__ import annotations

from typing import List

from lxml import etree

from .client import WafChallenge, XueqiuWebClient

DETAIL_EMPTY = "【未抓取到帖子内容】"
DETAIL_FAILED_PREFIX = "【抓取失败】"
ARTICLE_XPATH = '//*[@id="app"]/div[2]/div[2]/div[1]/article/div//text()'


def parse_post_detail_html(html: str) -> str:
    """全文页 HTML → 正文（逐行去空白、非空行以换行连接）；取不到返回 DETAIL_EMPTY。"""
    tree = etree.HTML(html) if html else None
    if tree is None:
        return DETAIL_EMPTY
    article_text: List[str] = tree.xpath(ARTICLE_XPATH)
    cleaned = [line.strip() for line in article_text if line.strip()]
    return "\n".join(cleaned) if cleaned else DETAIL_EMPTY


def fetch_post_detail(client: XueqiuWebClient, detail_url: str) -> str:
    """抓全文；失败返回 `【抓取失败】：…`（原语义）。

    与原实现唯一的差别：WAF 挑战页不再被吞成"未抓取到内容"（那会把帖子标成已补全、
    以后永不重抓），而是抛 `WafChallenge` 交给 runner 中止本轮。
    """
    try:
        response = client.get(detail_url, context=f"帖子全文 {detail_url}")
        response.raise_for_status()
        return parse_post_detail_html(response.content.decode("utf-8"))
    except WafChallenge:
        raise
    except Exception as exc:  # noqa: BLE001 - 原语义：任何失败都落成占位文本
        return f"{DETAIL_FAILED_PREFIX}：{exc}"


def should_use_fetched_detail(detail: str) -> bool:
    return bool(detail and not detail.startswith(DETAIL_FAILED_PREFIX) and detail != DETAIL_EMPTY)
