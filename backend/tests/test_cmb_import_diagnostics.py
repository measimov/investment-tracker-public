"""招商对账单脱敏诊断报告。

诊断存在的理由：出错行在 parse_rows 里 `continue`，既不产出 ParsedFlow 也不落
broker_fund_flows，报错消息又只带行号——报障者那边除了重跑 PDF 没有任何归因手段，
而对账单本身（全部持仓与金额）不可能发出来。

因此这里最要紧的一条不是"字段齐不齐"，而是**脱敏回归**：报告里绝不能出现原始
金额、数量、价格与证券名称。
"""

from decimal import Decimal
import json

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.security_rule import SecurityRule
from app.models.transaction import Transaction
from app.services import broker_import_common
from app.services import cmb_fund_flow_importer as importer
from tests.helpers import reset_tables

RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CashEvent,
    CorporateAction,
    Transaction,
    ImportBatch,
    BrokerAccount,
    SecurityRule,
)


HEADER_POSITIONS = [
    ("发生日期", 21.2),
    ("市场", 75.8),
    ("币种", 146.0),
    ("银行代码", 195.3),
    ("证券账号", 262.0),
    ("证券代码", 312.7),
    ("证券名称", 352.4),
    ("业务标志", 392.0),
    ("发生数量", 473.7),
    ("成交均价", 513.3),
    ("成交金额", 559.2),
    ("佣金", 608.6),
    ("印花税", 643.4),
    ("其他费", 683.0),
    ("变动金额", 720.5),
    ("资金余额", 761.2),
    ("证券余额", 800.9),
]
# 行内各列的 x0（与表头略有偏移，与既有 parser 测试同构）
VALUE_POSITIONS = [
    21.2,
    75.8,
    146.0,
    195.3,
    262.0,
    312.7,
    352.4,
    392.0,
    474.6,
    517.3,
    557.0,
    605.9,
    645.6,
    685.2,
    716.5,
    757.2,
    801.8,
]


def make_row(
    *,
    market="上海",
    currency="人民币",
    shareholder="A123456789",
    code="600000",
    name="浦发银行",
    business="证券买入",
    quantity="100.00",
    price="10.00",
    trade_amount="1000.00",
    commission="5.00",
    stamp_tax="0.00",
    other_fee="0.06",
    amount="-1005.06",
    cash_balance="100.00",
    security_balance="100.00",
    trade_date="20240524",
):
    return [
        trade_date,
        market,
        currency,
        "招商银行",
        shareholder,
        code,
        name,
        business,
        quantity,
        price,
        trade_amount,
        commission,
        stamp_tax,
        other_fee,
        amount,
        cash_balance,
        security_balance,
    ]


def install_fake_pdf(monkeypatch, rows, *, section_titles=True, pages=1, pdf_metadata=None):
    """把若干行渲染成 pdfplumber 的逐词输出（既有 parser 测试同款替身）。"""
    words = [{"text": text, "x0": x0, "top": 10.0} for text, x0 in HEADER_POSITIONS]
    if section_titles:
        words.append({"text": importer.PDF_FLOW_SECTION_TITLE, "x0": 21.2, "top": 5.0})
    for index, values in enumerate(rows):
        top = 20.0 + index * 10.0
        words.extend(
            {"text": text, "x0": x0, "top": top}
            for text, x0 in zip(values, VALUE_POSITIONS)
        )

    class FakePage:
        def extract_words(self, **kwargs):
            return words

    class FakePdf:
        def __init__(self):
            self.pages = [FakePage() for _ in range(pages)]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeReader:
        is_encrypted = False
        metadata = dict(pdf_metadata or {"/Producer": "SomeOtherWriter 1.2", "/Creator": "Writer"})

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(importer, "ensure_pdf_is_readable", lambda contents: None)
    monkeypatch.setattr(importer.pdfplumber, "open", lambda source: FakePdf())
    monkeypatch.setattr(importer, "PdfReader", FakeReader)


def diagnose(monkeypatch, rows, **kwargs):
    install_fake_pdf(monkeypatch, rows, **kwargs)
    parsed, _counts, _total, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "电子对账单2502-2509.pdf"
    )
    return (
        importer.build_cmb_diagnostics(
            b"%PDF-fake",
            "电子对账单2502-2509.pdf",
            errors=errors,
            warnings=warnings,
            parsed_rows=parsed,
        ),
        errors,
    )


def flatten(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 脱敏：报告的全部价值都建立在"能发出来"上
# ---------------------------------------------------------------------------


def test_diagnostics_never_leak_amounts_names_or_full_codes(monkeypatch):
    """原始金额/数量/价格/证券名称/完整代码一个都不得出现。

    这是本模块最重要的断言：脱敏一旦破了，报障者就不该再发这份报告，
    整条排查通道随之失效。
    """
    report, _errors = diagnose(
        monkeypatch,
        [
            make_row(
                market="港股通",
                code="113050",
                name="南银转债",
                quantity="700.00",
                price="119.50",
                trade_amount="987654.32",
                amount="-987659.38",
            )
        ],
    )
    blob = flatten(report)

    for secret in ("987654", "119.50", "700.00", "南银转债", "113050", "A123456789"):
        assert secret not in blob, f"诊断报告泄露了 {secret}：{blob}"
    # 该留的都留着：标签词表原样、代码只剩前缀
    assert "港股通" in blob
    assert "11****" in blob
    assert "A1********" in blob


def test_diagnostics_never_leak_filename_or_pdf_metadata(monkeypatch):
    """文件名与 PDF 元数据是身份信息的常见载体，一个字都不得回传。

    文件名常被存成「张伟-招商对账单2502-2509.pdf」；`/Creator` 常形如
    「Microsoft Word - 李娜的对账单.docx」或直接带本机用户名。报告标着
    "可直接发给维护者"，这两处原样透出就等于把姓名发出去。
    """
    install_fake_pdf(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
        pdf_metadata={
            "/Producer": "LibreOffice 6.1",
            "/Creator": "Microsoft Word - 李娜的对账单.docx (C:/Users/wangwei/Desktop)",
        },
    )
    parsed, _counts, _total, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "cmb.pdf"
    )
    report = importer.build_cmb_diagnostics(
        b"%PDF-fake",
        "张伟-招商对账单2502-2509.pdf",
        errors=errors,
        warnings=warnings,
        parsed_rows=parsed,
    )
    blob = flatten(report)

    for secret in ("张伟", "李娜", "wangwei", "Desktop", "Microsoft Word - ", "招商对账单"):
        assert secret not in blob, f"诊断报告泄露了 {secret}：{blob}"

    fingerprint = report["file_fingerprint"]
    # 结构与分类仍然保留——排查要的就是这些
    assert fingerprint["filename_extension"] == ".pdf"
    assert fingerprint["filename_digit_shape"] == [4, 4]
    assert len(fingerprint["filename_fingerprint"]) == 12
    assert fingerprint["pdf_producer_class"] == "libreoffice"
    # 未识别的生成器落 "other"，绝不把原文当"看起来无害"透出去
    assert fingerprint["pdf_creator_class"] == "microsoft word"
    assert fingerprint["pdf_creator_fingerprint"]


def test_unknown_pdf_generator_is_classified_not_echoed(monkeypatch):
    install_fake_pdf(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
        pdf_metadata={"/Producer": "内部报表工具 v3 by 张伟", "/Creator": ""},
    )
    report = importer.build_cmb_diagnostics(
        b"%PDF-fake", "x.pdf", errors=[], warnings=[], parsed_rows=[]
    )
    fingerprint = report["file_fingerprint"]

    assert fingerprint["pdf_producer_class"] == "other"
    assert fingerprint["pdf_creator_class"] == "absent"
    assert "张伟" not in flatten(report)
    assert "内部报表工具" not in flatten(report)


def test_column_spill_reports_the_direction_not_the_value(monkeypatch):
    """列错位正是待排查的假设之一——边界一偏，敏感值就会落进标签列。

    词表原样回传的话，证券名称/账号会被抄进报告。这里只报"哪一列溢出来了"，
    既不泄露持仓与账号，诊断价值反而更高（直接点名错位方向）。
    """
    report, _errors = diagnose(
        monkeypatch,
        [
            make_row(
                market="A123456789",  # 证券账号挤进了市场列（PDF 的证券账号列）
                shareholder="A123456789",
                trade_amount="920.00",
                amount="-925.06",
            )
        ],
    )
    blob = flatten(report)

    assert "A123456789" not in blob, blob
    assert report["error_rows"][0]["market"] == "<spilled:股东代码>"
    assert any(
        item["市场"] == "<spilled:股东代码>"
        for item in report["vocabulary"]["market_currency_business"]
    )


def test_diagnostics_keep_labels_verbatim_and_reduce_numbers_to_ratios(monkeypatch):
    """标签原样、数值降级——脱敏不能把定位能力一起脱掉。"""
    report, _errors = diagnose(
        monkeypatch,
        [
            make_row(
                market="港股通",
                currency="人民币",
                code="00700",
                quantity="500.00",
                price="100.00",
                # 500 × 100 = 50000 HKD，按 0.92 结算 = 46000 CNY
                trade_amount="46000.00",
                amount="-46005.06",
            )
        ],
    )
    row = report["error_rows"][0]

    assert row["market"] == "港股通"
    assert row["currency"] == "人民币"
    assert row["business"] == "证券买入"
    assert row["security_code_masked"] == "00***"
    assert row["security_code_length"] == 5
    assert row["raw_patterns"]["成交价格"] == "ddd.dd"
    assert row["raw_patterns"]["PDF成交金额"] == "ddddd.dd"
    assert row["price_decimal_places"] == 2
    assert row["quantity_magnitude"] == "1e2"
    # 这一个比值就能分流根因：≈0.92 = 港股通结算汇率
    assert row["trade_amount_over_gross"] == Decimal("0.920000")
    assert row["deviation_over_tolerance"] > 1
    # 原始数量与金额不在字段面上
    assert "trade_amount" not in row
    assert "quantity" not in row


def test_fees_all_zero_is_none_when_no_fee_column_parses(monkeypatch):
    """三列费用全解析不了 = 未知，不是"全为零"。

    朴素写法（先过滤掉 None 再 `all(...)`）会得到 `all([]) == True`，把
    "费用未知"报成"费用全为零"——而维护者正会据此排除费用列错位这条线索。
    """
    report, _errors = diagnose(
        monkeypatch,
        [make_row(commission="--", stamp_tax="N/A", other_fee="")],
    )
    row = report["error_rows"][0]

    assert row["fees_all_zero"] is None
    assert row["unparsed_fee_columns"] == ["佣金", "印花税", "其他费用"]


def test_fees_all_zero_is_none_when_one_column_fails_and_rest_are_zero(monkeypatch):
    """一项不可解析、其余两项为零——同样是未知，不得报 True。"""
    report, _errors = diagnose(
        monkeypatch,
        [make_row(commission="1O.OO", stamp_tax="0.00", other_fee="0.00")],
    )
    row = report["error_rows"][0]

    assert row["fees_all_zero"] is None
    assert row["unparsed_fee_columns"] == ["佣金"]


def test_fees_all_zero_is_true_only_when_all_three_parse_to_zero(monkeypatch):
    """三项全部解析成功且均为零，才允许 True。"""
    report, _errors = diagnose(
        monkeypatch,
        [
            make_row(
                market="港股通",
                commission="0.00",
                stamp_tax="0.00",
                other_fee="0.00",
                trade_amount="920.00",
                amount="-920.00",
            )
        ],
    )
    row = report["error_rows"][0]

    assert row["fees_all_zero"] is True
    assert row["unparsed_fee_columns"] == []


def test_fees_all_zero_is_false_when_a_fee_is_non_zero(monkeypatch):
    report, _errors = diagnose(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
    )
    row = report["error_rows"][0]

    assert row["fees_all_zero"] is False
    assert row["unparsed_fee_columns"] == []


def test_diagnostics_flag_unrecognized_hk_connect_label(monkeypatch):
    """未被现行判据认出的港股通写法——本次报障最主要的候选根因。

    报告必须同时给出「市场标签原文」与「现行判据的判定结果」，
    否则拿到报告的人还是只能猜。
    """
    report, _errors = diagnose(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
    )
    row = report["error_rows"][0]

    assert row["market"] == "港股通"
    assert row["is_hk_connect_by_current_rule"] is False
    assert report["vocabulary"]["known_hk_connect_names"] == ["沪港通", "深港通"]


def test_diagnostics_recognized_hk_connect_takes_settlement_path(monkeypatch):
    """名单内的写法走结算汇率分支，压根不报 reconcile——用来对照证伪。"""
    report, errors = diagnose(
        monkeypatch,
        [make_row(market="沪港通", trade_amount="920.00", amount="-925.06")],
    )
    assert errors == []
    assert report["error_rows"] == []
    assert report["counts"]["errors_total"] == 0


# ---------------------------------------------------------------------------
# 版式指纹：另一条同样合理、PR #165 未排除的假设
# ---------------------------------------------------------------------------


def test_diagnostics_expose_section_fallback_and_file_fingerprint(monkeypatch):
    """无节标题 = 走了回退分支，未回业务流水会被当成正常流水收进来。"""
    report, _errors = diagnose(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
        section_titles=False,
    )
    fingerprint = report["file_fingerprint"]

    assert fingerprint["has_section_titles"] is False
    assert report["error_rows"][0]["section_fallback"] is True
    assert fingerprint["parser_version"] == importer.PARSER_VERSION
    assert fingerprint["header_columns_missing"] == []
    assert fingerprint["pdf_producer_class"] == "other"
    # 文件名同样脱敏：区间数字会暴露账户活跃期
    assert fingerprint["filename_digit_shape"] == [4, 4]
    assert "电子对账单" not in flatten(fingerprint)


def test_diagnostics_provenance_aligns_with_dataframe_rows(monkeypatch):
    """provenance 与 df 逐下标对齐——错位的话每条错误行都会张冠李戴。"""
    provenance = []
    install_fake_pdf(
        monkeypatch,
        [make_row(code="600000"), make_row(code="600519"), make_row(code="000001")],
    )
    frame = importer.read_cmb_statement_pdf(b"%PDF-fake", provenance=provenance)

    assert len(provenance) == len(frame) == 3
    assert all(item["page_index"] == 0 for item in provenance)
    assert [item["columns_filled"] for item in provenance] == [17, 17, 17]


def test_diagnostics_vocabulary_lists_every_market_business_combination(monkeypatch):
    """未知的市场/业务写法要在词表里一眼可见。"""
    report, _errors = diagnose(
        monkeypatch,
        [
            make_row(market="上海"),
            make_row(market="上海"),
            make_row(market="沪市港股通", trade_amount="920.00", amount="-925.06"),
        ],
    )
    histogram = report["vocabulary"]["market_currency_business"]

    assert histogram[0] == {
        "市场": "上海",
        "币种": "人民币",
        "业务名称": "证券买入",
        "count": 2,
    }
    assert {"市场": "沪市港股通", "币种": "人民币", "业务名称": "证券买入", "count": 1} in histogram


# ---------------------------------------------------------------------------
# 计数与健壮性
# ---------------------------------------------------------------------------


def test_diagnostics_report_untruncated_counts(monkeypatch):
    """列表截断到 50 条，真实条数必须另有出口——否则"看到 8 条"与"共 8 条"不可分。"""
    rows = [
        make_row(market="港股通", trade_amount="920.00", amount="-925.06") for _ in range(60)
    ]
    report, errors = diagnose(monkeypatch, rows)

    assert len(errors) == 60
    assert report["counts"]["errors_total"] == 60
    assert report["counts"]["error_source_rows"] == 60
    assert len(report["error_rows"]) == 60


def test_diagnostics_tolerate_error_rows_outside_the_dataframe(monkeypatch):
    """持仓预检等非行级错误不带有效行号，不得让整份报告挂掉。"""
    install_fake_pdf(monkeypatch, [make_row()])
    report = importer.build_cmb_diagnostics(
        b"%PDF-fake",
        "cmb.pdf",
        errors=["row 99999: nonexistent", "招商证券账户持仓预检失败：整批未导入"],
        warnings=[],
        parsed_rows=[],
    )

    assert [row["row_number"] for row in report["error_rows"]] == [99999]
    assert report["error_rows"][0]["row_found"] is False
    assert report["counts"]["errors_total"] == 2


def _preview_with_account(monkeypatch, rows):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商诊断账户",
            base_currency="CNY",
            account_number_masked="****6789",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        install_fake_pdf(monkeypatch, rows)
        return importer.preview_cmb_fund_flow(
            db, 1, b"%PDF-fake", "电子对账单2502-2509.pdf", broker_account_id=account.id
        )
    finally:
        db.close()


def test_preview_attaches_diagnostics_when_rows_fail(monkeypatch):
    """预览真的把诊断挂出来了——这是报障者唯一能看见它的地方。"""
    preview = _preview_with_account(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
    )

    assert preview["errors"]
    assert preview["errors_total"] == len(preview["errors"])
    assert preview["diagnostics"]["error_rows"][0]["market"] == "港股通"
    assert preview["diagnostics"]["error_rows"][0]["is_hk_connect_by_current_rule"] is False


def test_preview_omits_diagnostics_on_a_clean_statement(monkeypatch):
    """干净预览不带诊断：它是排查辅助，不是常规响应负担。"""
    preview = _preview_with_account(monkeypatch, [make_row()])

    assert preview["errors"] == []
    assert preview["warnings"] == []
    assert preview.get("diagnostics") is None


def test_preview_counts_agree_between_alert_and_diagnostics(monkeypatch):
    """持仓预检失败没有 `row N:` 前缀，但同样阻塞导入。

    它若不并进 errors 全集，界面显示"共 1 条"而报告写 errors_total=0——
    两个数字打架，排查会先怀疑报告本身。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商诊断账户",
            base_currency="CNY",
            account_number_masked="****6789",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        # 无持仓时直接卖出 → 触发持仓预检失败
        install_fake_pdf(
            monkeypatch,
            [make_row(business="证券卖出", quantity="-100.00", amount="994.94")],
        )
        preview = importer.preview_cmb_fund_flow(
            db, 1, b"%PDF-fake", "cmb.pdf", broker_account_id=account.id
        )
    finally:
        db.close()

    assert any("持仓预检失败" in message for message in preview["errors"])
    assert preview["errors_total"] == len(preview["errors"])
    assert preview["diagnostics"]["counts"]["errors_total"] == preview["errors_total"]
    # 非行级错误不该伪造出一条错误行
    assert preview["diagnostics"]["counts"]["error_source_rows"] == 0


def test_preview_survives_diagnostics_failure(monkeypatch):
    """诊断炸了也不能把预览弄挂——用户该拿到的预览结果照常返回。"""

    def boom(*args, **kwargs):
        raise RuntimeError("diagnostics exploded")

    monkeypatch.setattr(importer, "build_cmb_diagnostics", boom)
    preview = _preview_with_account(
        monkeypatch,
        [make_row(market="港股通", trade_amount="920.00", amount="-925.06")],
    )

    assert preview["diagnostics"] == {"diagnostics_error": "RuntimeError"}
    assert preview["errors"], "预览本身必须照常返回"
    assert preview["total_rows"] == 1


# ---------------------------------------------------------------------------
# 不变性：诊断是只读的第二遍分析，不得改动入账口径
# ---------------------------------------------------------------------------


def test_diagnostics_do_not_change_parser_version(monkeypatch):
    """诊断纯只读：入账语义零变化，PARSER_VERSION 必须停在 11。

    这条变红 = 动了不该动的东西（多半是顺手改了 HK_CONNECT 判据）。
    """
    assert importer.PARSER_VERSION == "11"
    assert importer.HK_CONNECT_MARKET_NAMES == {"沪港通", "深港通"}


def test_provenance_out_param_does_not_change_extraction(monkeypatch):
    """传不传 provenance，提取结果必须逐字节一致（否则 row_hash 会漂）。"""
    install_fake_pdf(monkeypatch, [make_row(), make_row(code="600519")])
    without = importer._extract_pdf_flow_rows(b"%PDF-fake")
    collected = []
    with_param = importer._extract_pdf_flow_rows(b"%PDF-fake", provenance=collected)

    assert without == with_param
    assert len(collected) == len(without)


# ---------------------------------------------------------------------------
# 脱敏原语（公共层）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1,234.56", "d,ddd.dd"),
        ("73.04", "dd.dd"),
        ("-1005.06", "-dddd.dd"),
        ("深Ｂ", "深Ｂ"),
        ("沪港通", "沪港通"),
        # 全角数字单列成 D：它是解析失败的直接线索，混进 d 就看不见了
        ("１２.３", "DD.D"),
        # 列错位的签名：邻词被吃进同一格
        ("73.04上海", "dd.dd上海"),
        (None, ""),
    ],
)
def test_digit_class_patterns(raw, expected):
    assert broker_import_common.digit_class(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("113050", "11****"), ("00700", "00***"), ("", ""), ("6", "6")],
)
def test_mask_code_keeps_only_the_prefix(raw, expected):
    assert broker_import_common.mask_code(raw) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (Decimal("1234"), "1e3"),
        (Decimal("0"), "0"),
        (Decimal("-0.5"), "1e-1"),
        (Decimal("1"), "1e0"),
        (None, None),
    ],
)
def test_magnitude_buckets(value, expected):
    assert broker_import_common.magnitude(value) == expected


def test_safe_ratio_returns_none_instead_of_raising_or_faking_zero():
    assert broker_import_common.safe_ratio(Decimal("100"), Decimal("110")) == Decimal("0.909091")
    assert broker_import_common.safe_ratio(Decimal("1"), Decimal("0")) is None
    assert broker_import_common.safe_ratio(None, Decimal("2")) is None
    assert broker_import_common.safe_ratio(Decimal("2"), None) is None


def test_label_histogram_is_ordered_and_reproducible():
    rows = [{"a": "y"}, {"a": "x"}, {"a": "x"}, {"a": "z"}]
    assert broker_import_common.label_histogram(rows, ["a"]) == [
        {"a": "x", "count": 2},
        {"a": "y", "count": 1},
        {"a": "z", "count": 1},
    ]
