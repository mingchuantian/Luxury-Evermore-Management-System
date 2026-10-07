from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from io import BytesIO
from pathlib import Path
import re
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
    payout_for_customer: Any,
    settlement_at: Any = None,
) -> Dict[str, str]:
    sold_records = item.get("sold_record") or []
    if not sold_records:
        raise ValueError("No sale record")
    sold = sold_records[-1] or {}
    sale_price = _amount(sold.get("sold_price"), "sold price")
    payout = _amount(payout_for_customer, "payout for customer")
    if sale_price <= 0:
        raise ValueError("Sold price must be greater than zero")
    if payout < 0:
        raise ValueError("Payout for customer cannot be negative")
    if payout > sale_price:
        raise ValueError("Payout for customer cannot exceed sold price")

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
        "[CONSIGNMENT FEE]": _money(currency, sale_price - payout),
        "[AMOUNT DUE]": _money(currency, payout),
        "[SALE DATE]": _date_in_utc8(sold.get("sold_at")),
        "[SETTLEMENT DATE]": _date_in_utc8(settlement_at),
        "[PAYOUT REFERENCE]": "Settlement transfer",
    }


def template_path() -> Path:
    return Path(__file__).absolute().parent / TEMPLATE_FILENAME


def _remove_table_row_with_slot(document_xml: bytes, placeholder: str) -> bytes:
    token = placeholder.encode("utf-8")
    if document_xml.count(token) != 1:
        raise ValueError(
            "Consignment settlement template slot must occur once: "
            f"{placeholder}"
        )
    token_position = document_xml.index(token)
    row_tags = list(re.finditer(
        rb"<w:tr(?:\s[^>]*)?>", document_xml[:token_position]
    ))
    row_start = row_tags[-1].start() if row_tags else -1
    row_end = document_xml.find(b"</w:tr>", token_position)
    if row_start < 0 or row_end < 0:
        raise ValueError(
            "Consignment settlement template slot is not inside a table row: "
            f"{placeholder}"
        )
    row_end += len(b"</w:tr>")
    return document_xml[:row_start] + document_xml[row_end:]


def generate_consignment_settlement_docx_bytes(
    *,
    item: Dict[str, Any],
    payout_for_customer: Any,
    settlement_at: Any = None,
    payout_only: bool = False,
) -> BytesIO:
    mapping = build_consignment_settlement_mapping(
        item,
        payout_for_customer=payout_for_customer,
        settlement_at=settlement_at,
    )
    output = BytesIO()
    with ZipFile(template_path(), "r") as source, ZipFile(
        output, "w", compression=ZIP_DEFLATED
    ) as target:
        document_xml = source.read(DOCUMENT_PART)
        hidden_slots = set()
        if payout_only:
            hidden_slots = {"[SALE PRICE]", "[CONSIGNMENT FEE]"}
            for placeholder in hidden_slots:
                document_xml = _remove_table_row_with_slot(
                    document_xml, placeholder
                )
        for placeholder, replacement in mapping.items():
            if placeholder in hidden_slots:
                continue
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
