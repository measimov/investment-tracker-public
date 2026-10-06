"""港股（披露易）年报 / 中期报告清单与目标选取——财报摘要与港股报表抽取共用（#281）。

此前两条管线各写一份：摘要 `report_digest_service._plan_hk_targets_detailed`（只取年报、
清单取 12 份）与报表 `report_statement_service.plan_statement_targets`（年报 + 中报、清单取
14 份），噪声词也各写一份。同一财年两边可能选中不同的那份报告（修订/重刊）。现在只有
`hk_report_targets` 一处：清单 → 去噪声 → 推期末日 → 同期取公告日最新 → 期末倒序取前 keep 份。

日期工具（标题年份含中文数字、披露易 DD/MM/YYYY 公告时间、财年末/中期末推断）一并收在这里。
"""

import re
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional

from . import report_fetchers

# 标题含这些词的不是正式报告（摘要版、补充/更正公告、英文版；中报另排除季度报告）
ANNUAL_NOISE = ("摘要", "補充", "补充", "更正", "英文")
INTERIM_NOISE = ANNUAL_NOISE + ("季度", "季報", "季报")
# 清单多取几份：噪声与同期重刊会占位
LISTING_EXTRA = 4


def hk_report_targets(symbol: str, report_type: str, keep: int) -> List[Dict[str, Any]]:
    """披露易 report_type（annual / interim）最近 keep 个会计期的报告，期末倒序。

    清单请求失败原样抛出（调用方记为不完整清单，不得当成「没有报告」缓存）。
    """
    reports = report_fetchers.hkex_reports(
        symbol, report_type=report_type, limit=keep + LISTING_EXTRA
    )
    noise = ANNUAL_NOISE if report_type == "annual" else INTERIM_NOISE
    infer_end = infer_hk_fiscal_end if report_type == "annual" else infer_hk_interim_end
    by_period: Dict[str, Dict[str, Any]] = {}
    for row in reports:
        title = row["title"]
        if any(word in title for word in noise):
            continue
        end_date = infer_end(title, row["ann_date"])
        if not end_date:
            continue
        existing = by_period.get(end_date)
        # 同一会计期出现多份（修订/重刊）取公告日更新的。**必须先解析成日期**：
        # 披露易的格式是 DD/MM/YYYY，按字符串比会认为 "30/03/2025" 比
        # "02/04/2025" 新，于是缓存住原件、重刊永远进不来（源指纹也不会变）
        if existing is None or hkex_sort_key(row["ann_date"]) > hkex_sort_key(existing["ann_date"]):
            by_period[end_date] = row
    return [
        {
            "period_key": f"{end_date}|{report_type}",
            "report_type": report_type,
            "end_date": end_date,
            "title": by_period[end_date]["title"],
            "ann_date": by_period[end_date]["ann_date"],
            "url": by_period[end_date]["url"],
        }
        for end_date in sorted(by_period, reverse=True)[:keep]
    ]


# 常见财年结束月（港股财年不统一：多数 12/31，也有 3/31、6/30）
_HK_FISCAL_MONTHS = (12, 9, 6, 3)
_MONTH_END_DAY = {3: "31", 6: "30", 9: "30", 12: "31"}


def infer_hk_fiscal_end(title: str, ann_date: str) -> Optional[str]:
    """港股年报标题年份 + 公告日 → 财年结束日（YYYYMMDD）。

    不能一律按 12/31：港股财年不统一（阿里 3/31），而结构化数据来自 Yahoo 的
    真实 asOfDate——两边对不上，LLM 拿到的就是同一年两个口径。

    判据是"年报须在财年结束后数月内刊发"：在标题年份的四个季末里，取公告日
    之前 2-6 个月那一个。落不进这个窗口就退回 12/31。
    """

    years = extract_report_years(title)
    if not years:
        return None
    ann = parse_hkex_datetime(ann_date)
    if ann is None:
        return f"{years[0]}1231"
    # 跨年财年标题（「2024/25 年報」）的候选年份按「后一年优先」排列：3 月财年的 2024/25 年报
    # 期末是 2025-03-31，按 2024 去试会先撞上 2024-12-31（#346-3）
    for year in years:
        for month in _HK_FISCAL_MONTHS:
            months_before = (ann.year - year) * 12 + (ann.month - month)
            if 2 <= months_before <= 6:
                return f"{year}{month:02d}{_MONTH_END_DAY[month]}"
    return f"{years[0]}1231"


def hkex_sort_key(value: str) -> date:
    """公告日排序键；无法解析时给 date.min（确定性兜底：坏值永不胜出）。"""
    return parse_hkex_datetime(value) or date.min


_CN_DIGITS = {
    "零": "0",
    "〇": "0",
    # 「二○二四年」：○（U+25CB 白圈）在真实披露里当零用（01133 核数师报告日期「二○二六年」）
    "○": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


_CROSS_YEAR_RE = re.compile(r"(20\d{2})\s*[/／\-–]\s*(\d{2})(?!\d)")


def extract_report_years(title: str) -> List[int]:
    """标题里的报告年份候选（优先者在前）；认不出返回空列表。

    - 先做 NFKC 归一：全角数字「２０２４」原样认不出，报告被丢弃（#346-3）；
    - 跨年财年「2024/25」「2024-25」给出 [2025, 2024]（后一年优先，由调用方按刊发窗口挑）；
    - 中文数字年份「二零二五年」「二○二四年」必须认——只认阿拉伯数字年份会把中海油 00883、
      绿城 03900 这类公司的每一份年报都静默丢弃，清单变成 complete-empty。
    """
    text = unicodedata.normalize("NFKC", str(title or ""))
    cross = _CROSS_YEAR_RE.search(text)
    if cross:
        first = int(cross.group(1))
        second = first // 100 * 100 + int(cross.group(2))
        if second == first + 1:
            return [second, first]
    match = re.search(r"(20\d{2})", text)
    if match:
        return [int(match.group(1))]
    cn_match = re.search(r"[零〇○一二三四五六七八九]{4}", text)
    if cn_match:
        digits = "".join(_CN_DIGITS[ch] for ch in cn_match.group(0))
        if digits.startswith("20"):
            return [int(digits)]
    return []


def extract_report_year(title: str) -> Optional[int]:
    """标题里的报告年份（多个候选时取首选，见 `extract_report_years`）。"""
    years = extract_report_years(title)
    return years[0] if years else None


def parse_hkex_datetime(value: str) -> Optional[date]:
    """披露易公告时间 'DD/MM/YYYY HH:MM' → date。"""
    head = str(value or "").split(" ")[0]
    try:
        day, month, year = (int(part) for part in head.split("/"))
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


def infer_hk_interim_end(title: str, ann_date: str) -> Optional[str]:
    """中期报告标题年份 + 公告日 → 中期末日。中报须在期末后 1-4 个月内刊发：在标题年份的
    四个季末里取公告日之前 1-4 个月那一个（6/30 财年 12 月的公司中期末是 12/31、3 月财年
    是 9/30）。落不进窗口退回 6/30。"""
    years = extract_report_years(title)
    if not years:
        return None
    year = years[0]
    ann = parse_hkex_datetime(ann_date)
    if ann is None:
        return f"{year}0630"
    # 标题年份既可能是期末所在年（"2025 中期報告" = 2025-06-30），也可能是财年（6 月财年的
    # 01023 "2026 中期報告" = 截至 2025-12-31 止六個月，刊发于 2026 年 2-3 月）：两个年份的
    # 季末都试，取落在刊发窗口里的那个
    candidates = list(dict.fromkeys([*years, year - 1]))
    for candidate_year in candidates:
        for month, day in ((6, "30"), (9, "30"), (12, "31"), (3, "31")):
            months_before = (ann.year - candidate_year) * 12 + (ann.month - month)
            if 1 <= months_before <= 4:
                return f"{candidate_year}{month:02d}{day}"
    return f"{year}0630"
