"""Validation shared by manual and standard-CSV receipt entry."""

from decimal import Decimal

from ..core.timeutil import local_today


def validate_receipt_input(data, *, confirmed, today=None):
    if data.get("action_type") != "CASH_DIVIDEND":
        return data
    if not confirmed:
        raise ValueError("请核对到账凭证并明确确认已到账；公告预计股息请在预计清单中查看")
    payment_date = data.get("payment_date")
    if payment_date is None or payment_date > (today or local_today()):
        raise ValueError("请填写实际到账日期，不能留空或晚于今天")
    if data.get("broker_account_id") is None:
        raise ValueError("确认到账必须指定券商账户")
    gross, tax, net = (data.get(k) for k in ("total_dividend", "tax_withheld", "net_dividend"))
    if data.get("amount_basis") == "NET_ONLY":
        if net is None or net <= 0:
            raise ValueError("仅净额已知时必须填写实际净到账额（大于 0）")
        data.update(total_dividend=None, tax_withheld=None, tax_rate=None)
    else:
        if gross is None or gross <= 0 or tax is None:
            raise ValueError("请填写已核实的税前额和税额（免税填 0），或选择仅净到账额已知")
        if tax > gross:
            raise ValueError("税额不能超过税前股息")
        if net is not None and abs(Decimal(net) - (gross - tax)) > Decimal("0.00000001"):
            raise ValueError("净到账额须等于税前股息减去税额")
        data.update(net_dividend=gross - tax, amount_basis="GROSS_NET")
    data["receipt_status"] = "RECEIVED"
    return data
