from decimal import Decimal

import pytest

from app.services.stock_price_service import (
    Market,
    configure_tushare_client_endpoint,
    get_exchange_type,
    parse_tencent_quote_price,
    to_tencent_quote_code,
    to_tushare_a_code,
    to_tushare_crypto_code,
    to_tushare_hk_code,
)


@pytest.mark.parametrize(
    ("symbol", "exchange_type", "tushare_code", "tencent_code"),
    [
        ("600000", "sh", "600000.SH", "sh600000"),
        ("000001", "sz", "000001.SZ", "sz000001"),
        ("830799", "bj", "830799.BJ", "bj830799"),
    ],
)
def test_a_share_code_conversion(symbol, exchange_type, tushare_code, tencent_code):
    assert get_exchange_type(symbol) == exchange_type
    assert to_tushare_a_code(symbol) == tushare_code
    assert to_tencent_quote_code(symbol, Market.A_STOCK) == tencent_code


def test_hk_and_crypto_code_conversion():
    assert to_tushare_hk_code("700") == "00700.HK"
    assert to_tushare_hk_code("00700.HK") == "00700.HK"
    assert to_tencent_quote_code("700", Market.HK_STOCK) == "hk00700"
    assert to_tushare_crypto_code("BTC") == "BTC_USDT"
    assert to_tushare_crypto_code("BTC-USD") == "BTC_USDT"
    assert to_tushare_crypto_code("ETH/USDT") == "ETH_USDT"


def test_parse_tencent_quote_price_extracts_positive_price():
    payload = 'v_sh600000="1~浦发银行~600000~9.58~9.40";'

    assert parse_tencent_quote_price(payload, "sh600000") == Decimal("9.58")


def test_parse_tencent_quote_price_rejects_missing_or_invalid_price():
    with pytest.raises(ValueError):
        parse_tencent_quote_price("", "sh600000")

    with pytest.raises(ValueError):
        parse_tencent_quote_price('v_sh600000="1~浦发银行~600000~0";', "sh600000")


def test_configure_tushare_client_endpoint_uses_https_default(monkeypatch):
    monkeypatch.delenv("TUSHARE_API_BASE_URL", raising=False)

    class DummyClient:
        _DataApi__http_url = "http://api.waditu.com/dataapi"

    client = DummyClient()

    configure_tushare_client_endpoint(client)

    assert client._DataApi__http_url == "https://api.waditu.com/dataapi"


def test_configure_tushare_client_endpoint_accepts_override(monkeypatch):
    monkeypatch.setenv("TUSHARE_API_BASE_URL", "https://api.tushare.pro/dataapi/")

    class DummyClient:
        _DataApi__http_url = "http://api.waditu.com/dataapi"

    client = DummyClient()

    configure_tushare_client_endpoint(client)

    assert client._DataApi__http_url == "https://api.tushare.pro/dataapi"
