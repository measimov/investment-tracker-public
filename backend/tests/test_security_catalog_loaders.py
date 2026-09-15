"""标的全集 loader 纯函数：港交所名單解析/过滤、Tushare 基础表映射、拼音/繁简/币种推断、
腾讯取名载荷。零网络、零 DB。"""

import io
from datetime import date

import openpyxl
import pandas as pd
import pytest

from app.services import security_catalog_service as svc
from app.services import stock_price_service as sps


def _xlsx(rows, *, header=("股份代號", "股份名稱", "分類", "次分類", "買賣單位", "國際證券號碼 (ISIN)", "到期日")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ListOfSecurities"
    ws.append(["證券名單"])
    ws.append(["截 至 04/09/2026"])
    if header:
        ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


HKEX_ROWS = [
    ("00001", "長和", "股本", "股本證券(主板)", "500", "KYG217651051", None),
    ("08001", "創業板股", "股本", "股本證券(創業板)", "1,000", "HK0000000001", None),
    ("09999", "某預託證券", "股本", "預託證券", "100", "US0000000001", None),
    ("02800", "盈富基金", "交易所買賣產品", "交易所買賣基金", "500", "HK2800008867", None),
    ("07200", "南方兩倍看多恒指", "交易所買賣產品", "槓桿及反向產品", "100", "HK0000000002", None),
    ("00808", "泓富產業信託", "房地產投資信託基金", None, "1,000", "HK0000000003", None),
    ("80700", "騰訊控股－Ｒ", "股本", "股本證券(主板)", "100", "KYG875721634", None),
    ("12345", "某窩輪", "衍生權證", None, "10,000", None, "2027-01-01"),
    ("60000", "某牛熊證", "牛熊證", None, "10,000", None, "2027-01-01"),
    ("04000", "某債券", "債券", None, "10,000", None, None),
]


def test_parse_hkex_list_locates_header_and_reads_rows():
    rows = svc.parse_hkex_list(_xlsx(HKEX_ROWS))
    assert len(rows) == 10
    by_code = {row.code: row for row in rows}
    assert by_code[1].name == "長和" and by_code[1].category == "股本"
    assert by_code[1].subcategory == "股本證券(主板)" and by_code[1].isin == "KYG217651051"
    assert by_code[808].subcategory is None


def test_parse_hkex_list_rejects_missing_header_or_empty_body():
    with pytest.raises(svc.CatalogFormatError):
        svc.parse_hkex_list(_xlsx(HKEX_ROWS, header=None))
    with pytest.raises(svc.CatalogFormatError):
        svc.parse_hkex_list(_xlsx([]))


def test_rows_from_hkex_lists_filters_and_maps_types():
    zh_rows = svc.parse_hkex_list(_xlsx(HKEX_ROWS))
    en_by_code = {1: svc.HkexListRow(1, "CKH HOLDINGS", "Equity", "Equity Securities (Main Board)", "500", None)}
    rows = svc.rows_from_hkex_lists(zh_rows, en_by_code)
    by_symbol = {row.symbol: row for row in rows}
    # 窝轮 / 牛熊证 / 债券 被剔除
    assert set(by_symbol) == {"00001", "08001", "09999", "02800", "07200", "00808", "80700"}
    assert by_symbol["00001"].security_type == "stock" and by_symbol["00001"].board == "主板"
    assert by_symbol["00001"].name_en == "CKH HOLDINGS" and by_symbol["00001"].name_trad == "長和"
    assert by_symbol["08001"].board == "创业板"
    assert by_symbol["09999"].security_type == "adr"
    assert by_symbol["02800"].security_type == "etf" and by_symbol["02800"].board is None
    assert by_symbol["07200"].security_type == "etf"
    assert by_symbol["07200"].detail["subcategory"] == "槓桿及反向產品"
    assert by_symbol["00808"].security_type == "reit"
    # 人民币柜台按代码段推断 CNY；其余港股币种留空等日报官方 CUR
    assert (by_symbol["80700"].currency, by_symbol["80700"].currency_source) == ("CNY", "inferred")
    assert by_symbol["00001"].currency is None
    # 全角归一：－Ｒ → -R
    assert by_symbol["80700"].name_trad == "騰訊控股-R"
    assert all(row.market == "港股" and row.list_status == "listed" for row in rows)
    assert all(row.exchange == "HKEX" for row in rows)
    if svc.ZHCONV_AVAILABLE:
        assert by_symbol["80700"].name == "腾讯控股-R"
    if svc.PINYIN_AVAILABLE:
        assert by_symbol["80700"].pinyin == "TXKGR"


def test_rows_from_stock_basic_maps_status_board_and_pinyin():
    listed = pd.DataFrame([
        {"ts_code": "600519.SH", "name": "贵州茅台", "market": "主板", "list_date": "20010827",
         "delist_date": None, "cnspell": "GZMT"},
        {"ts_code": "920001.BJ", "name": "北交所股", "market": "北交所", "list_date": "20240101",
         "delist_date": None, "cnspell": "BJSG"},
    ])
    rows = svc.rows_from_stock_basic(listed, list_status_code="L")
    assert [(r.symbol, r.exchange, r.board) for r in rows] == [
        ("600519", "SSE", "主板"), ("920001", "BSE", "北交所"),
    ]
    assert rows[0].pinyin == "GZMT" and rows[0].list_date == date(2001, 8, 27)
    assert rows[0].security_type == "stock" and rows[0].currency == "CNY"
    assert rows[0].list_status == "listed"

    delisted = pd.DataFrame([
        {"ts_code": "600518.SH", "name": "康美药业", "market": "主板", "list_date": "20010319",
         "delist_date": "20240620"},  # 无 cnspell 列
    ])
    rows = svc.rows_from_stock_basic(delisted, list_status_code="D")
    assert rows[0].list_status == "delisted" and rows[0].delist_date == date(2024, 6, 20)
    assert rows[0].pinyin == ("KMYY" if svc.PINYIN_AVAILABLE else None)

    assert svc.rows_from_stock_basic(None, list_status_code="D") == []


def test_rows_from_fund_basic_maps_fund_types():
    df = pd.DataFrame([
        {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF", "fund_type": "股票型", "status": "L"},
        {"ts_code": "508000.SH", "name": "华安张江产业园REIT", "fund_type": "REITs", "status": "L"},
        {"ts_code": "160105.SZ", "name": "南方积配LOF", "fund_type": "混合型", "status": "D"},
    ])
    rows = {r.symbol: r for r in svc.rows_from_fund_basic(df)}
    assert rows["510300"].security_type == "etf" and rows["510300"].exchange == "SSE"
    assert rows["508000"].security_type == "reit"
    assert rows["160105"].security_type == "fund" and rows["160105"].list_status == "delisted"
    assert rows["510300"].detail == {"fund_type": "股票型"}


def test_rows_from_hk_basic_prefers_inferred_cny_for_rmb_counters():
    df = pd.DataFrame([
        {"ts_code": "00700.HK", "name": "腾讯控股", "enname": "Tencent Holdings Ltd.",
         "cn_spell": "TXKG", "curr_type": "HKD", "market": "主板", "list_status": "L",
         "list_date": "20040616"},
        {"ts_code": "80700.HK", "name": "腾讯控股-R", "enname": "Tencent Holdings Ltd.",
         "cn_spell": "TXKGR", "curr_type": "HKD", "market": "主板", "list_status": "L",
         "list_date": "20230619"},
        {"ts_code": "0001.HK", "name": "长和", "enname": "CK Hutchison", "cn_spell": "CH",
         "curr_type": None, "market": "主板", "list_status": "D", "list_date": None},
    ])
    rows = {r.symbol: r for r in svc.rows_from_hk_basic(df)}
    assert (rows["00700"].currency, rows["00700"].currency_source) == ("HKD", "tushare")
    assert (rows["80700"].currency, rows["80700"].currency_source) == ("CNY", "inferred")
    assert rows["00001"].list_status == "delisted" and rows["00001"].currency is None
    assert rows["00700"].security_type is None  # 港交所名單权威
    assert rows["00700"].pinyin == "TXKG" and rows["00700"].name_en == "Tencent Holdings Ltd."


def test_rows_from_us_basic_maps_classify_and_delisting():
    df = pd.DataFrame([
        {"ts_code": "AAPL", "name": "苹果", "enname": "APPLE INC.", "classify": "EQ",
         "list_date": "19801212", "delist_date": None},
        {"ts_code": "BABA", "name": "阿里巴巴", "enname": "ALIBABA ADR", "classify": "ADR",
         "list_date": "20140919", "delist_date": None},
        {"ts_code": "OLD", "name": "老股", "enname": "OLD CO", "classify": "PF",
         "list_date": "20000101", "delist_date": "20200101"},
        {"ts_code": "GDRX", "name": "存托", "enname": "GDR CO", "classify": "GDR",
         "list_date": None, "delist_date": None},
    ])
    rows = {r.symbol: r for r in svc.rows_from_us_basic(df)}
    assert rows["AAPL"].security_type == "stock" and rows["AAPL"].currency == "USD"
    assert rows["BABA"].security_type == "adr"
    assert rows["OLD"].security_type == "pref" and rows["OLD"].list_status == "delisted"
    assert rows["GDRX"].security_type == "gdr" and rows["GDRX"].exchange == "US"
    assert rows["AAPL"].pinyin == ("PG" if svc.PINYIN_AVAILABLE else None)


def test_pinyin_and_simplified_degrade_explicitly(monkeypatch):
    assert svc.pinyin_abbreviation("任意", provided=" gzmt ") == "GZMT"
    if svc.PINYIN_AVAILABLE:
        assert svc.pinyin_abbreviation("贵州茅台") == "GZMT"
        assert svc.pinyin_abbreviation("宝信Ｂ") == "BXB"
        assert svc.pinyin_abbreviation("沪深300ETF") == "HS300ETF"
    monkeypatch.setattr(svc, "PINYIN_AVAILABLE", False)
    assert svc.pinyin_abbreviation("贵州茅台") is None
    assert svc.pinyin_abbreviation("贵州茅台", provided="GZMT") == "GZMT"
    if svc.ZHCONV_AVAILABLE:
        assert svc.to_simplified("騰訊控股") == "腾讯控股"
    monkeypatch.setattr(svc, "ZHCONV_AVAILABLE", False)
    assert svc.to_simplified("騰訊控股") is None
    assert svc.to_simplified(None) is None


def test_currency_inference():
    assert svc.infer_b_share_currency("900926") == "USD"
    assert svc.infer_b_share_currency("200596") == "HKD"
    assert svc.infer_b_share_currency("600519") is None
    assert svc.infer_hk_currency("80700") == ("CNY", "inferred")
    assert svc.infer_hk_currency("89999") == ("CNY", "inferred")
    assert svc.infer_hk_currency("00700") == (None, None)
    assert svc.infer_hk_currency("ABC") == (None, None)


def test_tencent_name_code_and_fields():
    assert sps.to_tencent_name_code("600519", "A股") == "sh600519"
    assert sps.to_tencent_name_code("900926", "B股") == "sh900926"
    assert sps.to_tencent_name_code("200596", "B股") == "sz200596"
    assert sps.to_tencent_name_code("920001", "A股") == "bj920001"
    assert sps.to_tencent_name_code("700", "港股") == "hk00700"
    assert sps.to_tencent_name_code("00700.HK", "港股") == "hk00700"
    assert sps.to_tencent_name_code("pdd", "美股") == "usPDD"
    assert sps.to_tencent_name_code("D05", "新加坡股") is None
    assert sps.to_tencent_name_code("", "A股") is None

    payload = 'v_sh900926="1~宝信Ｂ~900926~0.838~0.832~0.835~6799~2015";\nv_pv_none_match="1";'
    fields = sps.parse_tencent_quote_fields(payload, "sh900926")
    assert fields[1] == "宝信Ｂ" and fields[3] == "0.838"
    with pytest.raises(ValueError):
        sps.parse_tencent_quote_fields(payload, "sz200596")
    # 价格解析行为不变
    assert sps.parse_tencent_quote_price(payload, "sh900926") == sps.positive_decimal_price("0.838")


def test_row_values_placeholders_for_unknown_columns():
    values = svc._row_values(svc.CatalogRow(symbol="00700", market="港股"), "tencent-quote")
    assert values["security_type"] == "unknown" and values["list_status"] == "unknown"
    assert values["detail"] == {} and values["source"] == "tencent-quote"


def test_us_basic_loader_pages_through_offsets(monkeypatch):
    """us_basic 单次最多 6000 行、全表 2.4 万+：必须按 offset 翻页直到不足一页。"""
    calls = []

    def fake_frame(api_name, **kwargs):
        calls.append((api_name, kwargs["offset"], kwargs["limit"]))
        size = 3 if kwargs["offset"] < 6 else 1
        start = kwargs["offset"]
        return pd.DataFrame([
            {"ts_code": f"T{start + i}", "name": f"名{start + i}", "enname": "X", "classify": "EQ",
             "list_date": None, "delist_date": None}
            for i in range(size)
        ])

    monkeypatch.setattr(svc, "_tushare_frame", fake_frame)
    monkeypatch.setattr(svc, "US_BASIC_PAGE_SIZE", 3)
    rows = svc.load_tushare_us_basic()
    assert [row.symbol for row in rows] == ["T0", "T1", "T2", "T3", "T4", "T5", "T6"]
    assert calls == [("us_basic", 0, 3), ("us_basic", 3, 3), ("us_basic", 6, 3)]

    monkeypatch.setattr(svc, "US_BASIC_MAX_PAGES", 2)
    with pytest.raises(svc.CatalogFormatError):
        svc.load_tushare_us_basic()  # 前两页都满、没到末页 → 不得静默截断

    # 第一页就空 = 主查询失败，不是"美股没有标的"
    monkeypatch.setattr(svc, "US_BASIC_MAX_PAGES", 12)
    monkeypatch.setattr(svc, "_tushare_frame", lambda api_name, required, **kwargs: (
        (_ for _ in ()).throw(svc.CatalogFormatError("empty")) if required else None
    ))
    with pytest.raises(svc.CatalogFormatError):
        svc.load_tushare_us_basic()


def _empty_query(empty_when):
    """打桩 tushare_query：命中 empty_when(api, kwargs) 的调用按真实行为抛"返回空数据"。"""

    def fake(api_name, **kwargs):
        if empty_when(api_name, kwargs):
            raise ValueError(f"tushare {api_name} 返回空数据")
        if api_name == "stock_basic":
            return pd.DataFrame([{"ts_code": "600519.SH", "name": "贵州茅台", "market": "主板",
                                  "list_date": "20010827", "delist_date": None, "cnspell": "GZMT"}])
        if api_name == "hk_basic":
            return pd.DataFrame([{"ts_code": "00700.HK", "name": "腾讯控股", "enname": "Tencent",
                                  "cn_spell": "TXKG", "curr_type": "HKD", "market": "主板",
                                  "list_status": "L", "list_date": "20040616"}])
        if api_name == "fund_basic":
            return pd.DataFrame([{"ts_code": "510300.SH", "name": "沪深300ETF", "fund_type": "股票型",
                                  "status": "L"}])
        if api_name == "us_basic":
            return pd.DataFrame([{"ts_code": "AAPL", "name": "苹果", "enname": "APPLE", "classify": "EQ",
                                  "list_date": None, "delist_date": None}])
        raise AssertionError(api_name)

    return fake


def test_primary_tushare_query_empty_fails_loader_but_optional_legs_may_be_empty(monkeypatch):
    """上游故障/权限/契约漂移导致主查询空表 → loader 必须失败（否则 0 行被标 ok 并冻结一周）；
    退市腿与翻页尾页为空是合法的。"""
    # 退市腿空：合法
    monkeypatch.setattr(svc, "tushare_query", _empty_query(
        lambda api, kw: kw.get("list_status") == "D"))
    assert [r.symbol for r in svc.load_tushare_stock_basic()] == ["600519"]
    assert [r.symbol for r in svc.load_tushare_hk_basic()] == ["00700"]
    # 上市腿空：失败
    monkeypatch.setattr(svc, "tushare_query", _empty_query(
        lambda api, kw: kw.get("list_status") == "L"))
    with pytest.raises(svc.CatalogFormatError, match="stock_basic 主查询返回空表"):
        svc.load_tushare_stock_basic()
    with pytest.raises(svc.CatalogFormatError, match="hk_basic 主查询返回空表"):
        svc.load_tushare_hk_basic()
    # fund_basic 只有主查询
    monkeypatch.setattr(svc, "tushare_query", _empty_query(lambda api, kw: api == "fund_basic"))
    with pytest.raises(svc.CatalogFormatError, match="fund_basic 主查询返回空表"):
        svc.load_tushare_fund_basic()
    # us_basic：第一页空失败；尾页空合法（第一页不足 page_size 直接收尾）
    monkeypatch.setattr(svc, "tushare_query", _empty_query(lambda api, kw: api == "us_basic"))
    with pytest.raises(svc.CatalogFormatError, match="us_basic 主查询返回空表"):
        svc.load_tushare_us_basic()
    monkeypatch.setattr(svc, "tushare_query", _empty_query(
        lambda api, kw: api == "us_basic" and kw.get("offset", 0) > 0))
    assert [r.symbol for r in svc.load_tushare_us_basic()] == ["AAPL"]
    # 非"空数据"类异常原样上抛（限速/权限文案交给 sync 分类）
    def boom(api_name, **kwargs):
        raise ValueError("抱歉，您没有访问该接口的权限")
    monkeypatch.setattr(svc, "tushare_query", boom)
    with pytest.raises(ValueError, match="权限"):
        svc.load_tushare_fund_basic()
