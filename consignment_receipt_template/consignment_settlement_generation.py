from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Dict
from zipfile import ZIP_DEFLATED, ZipFile


TEMPLATE_FILENAME = "Consignment-Settlement-Template.docx"
DOCUMENT_PART = "word/document.xml"
MONEY_PLACES = Decimal("0.01")


def _date_in_utc8(value: Any) -> str:
    if value is None or value == "":
        return ""
    date_value = value if isinstance(value, datetime) else None
    if date_value is None and isinstance(value, str):
        try:
            date_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if date_value is None:
        return str(value)
    if date_value.tzinfo is None:
        date_value = date_value.replace(tzinfo=timezone.utc)
    return date_value.astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%d %b %Y")


def _amount(value: Any, field_name: str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(
            MONEY_PLACES, rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Invalid {field_name}")
    if not amount.is_finite():
        raise ValueError(f"Invalid {field_name}")
    return amount


def _money(currency: str, amount: Decimal) -> str:
    currency = (currency or "SGD").strip().upper()
    if currency == "SGD":
        return f"SGD ${amount:,.2f}"
    return f"{currency} {amount:,.2f}"


def build_consignment_settlement_mapping(
    item: Dict[str, Any],
    *,
    consignment_fee: Any,
    settlement_at: Any = None,
) -> Dict[str, str]:
    sold_records = item.get("sold_record") or []
    if not sold_records:
        raise ValueError("No sale record")
    sold = sold_records[-1] or {}
    sale_price = _amount(sold.get("sold_price"), "sold price")
    fee = _amount(consignment_fee, "consignment fee")
    if sale_price <= 0:
        raise ValueError("Sold price must be greater than zero")
    if fee < 0:
        raise ValueError("Consignment fee cannot be negative")
    if fee > sale_price:
        raise ValueError("Consignment fee cannot exceed sold price")

    receipt_number = (
        sold.get("receipt_no") or item.get("sku") or ""
    ).strip()
    currency = (
        sold.get("sold_currency")
        or item.get("listing_currency")
        or "SGD"
    ).strip().upper()
    settlement_at = settlement_at or datetime.now(timezone.utc)
    return {
        "[SETTLEMENT RECEIPT ID]": receipt_number,
        "[SOURCE SALES RECEIPT]": receipt_number,
        "[ITEM NAME AND DESCRIPTION]": (
            item.get("name_in_EN") or item.get("name") or ""
        ).strip(),
        "[SALE PRICE]": _money(currency, sale_price),
        "[CONSIGNMENT FEE]": _money(currency, fee),
        "[AMOUNT DUE]": _money(currency, sale_price - fee),
        "[SALE DATE]": _date_in_utc8(sold.get("sold_at")),
        "[SETTLEMENT DATE]": _date_in_utc8(settlement_at),
        "[PAYOUT REFERENCE]": "Settlement transfer",
    }


def template_path() -> Path:
    return Path(__file__).absolute().parent / TEMPLATE_FILENAME


def generate_consignment_settlement_docx_bytes(
    *,
    item: Dict[str, Any],
    consignment_fee: Any,
    settlement_at: Any = None,
) -> BytesIO:
    mapping = build_consignment_settlement_mapping(
        item,
        consignment_fee=consignment_fee,
        settlement_at=settlement_at,
    )
    output = BytesIO()
    with ZipFile(template_path(), "r") as source, ZipFile(
        output, "w", compression=ZIP_DEFLATED
    ) as target:
        document_xml = source.read(DOCUMENT_PART)
        for placeholder, replacement in mapping.items():
            token = placeholder.encode("utf-8")
            if document_xml.count(token) != 1:
                raise ValueError(
                    "Consignment settlement template slot must occur once: "
                    f"{placeholder}"
                )
            document_xml = document_xml.replace(
                token,
                escape(str(replacement), quote=False).encode("utf-8"),
                1,
            )
        for info in source.infolist():
            data = (
                document_xml
                if info.filename == DOCUMENT_PART
                else source.read(info)
            )
            target.writestr(info, data)
    output.seek(0)
    return output
