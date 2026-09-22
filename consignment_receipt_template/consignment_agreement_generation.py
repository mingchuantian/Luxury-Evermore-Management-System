from datetime import datetime, timedelta, timezone
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Dict
from zipfile import ZIP_DEFLATED, ZipFile


TEMPLATE_FILENAME = "Consignment-Agreement-Template.docx"
DOCUMENT_PART = "word/document.xml"


def _date_in_utc8(value: Any) -> str:
    if value is None or value == "":
        return ""

    dt = value if isinstance(value, datetime) else None
    if dt is None and isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if dt is None:
        return str(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%d %b %Y")


def _target_payout(item: Dict[str, Any]) -> str:
    currency = (item.get("cost_currency") or item.get("currency") or "").strip().upper()
    try:
        amount = int(item.get("cost") or 0)
    except (TypeError, ValueError):
        amount = 0

    if not currency and amount == 0:
        return ""
    if currency == "SGD":
        return f"SGD ${amount:,}"
    if currency:
        return f"{currency} {amount:,}"
    return f"{amount:,}"


def build_consignment_agreement_mapping(item: Dict[str, Any]) -> Dict[str, str]:
    return {
        "[AGREEMENT NUMBER]": (item.get("sku") or "").strip(),
        "[DD MMM YYYY]": _date_in_utc8(item.get("purchase_at") or item.get("created_at")),
        "[CONSIGNOR NAME]": (item.get("seller_name") or "").strip(),
        "[ITEM NAME AND DESCRIPTION]": (item.get("name_in_EN") or "").strip(),
        "SGD $[TARGET PAYOUT]": _target_payout(item),
        "[OPTIONAL NOTE]": (
            (item.get("additional_notes_for_agreements") or "").strip() or "N.A."
        ),
    }


def template_path() -> Path:
    return Path(__file__).resolve().parent / TEMPLATE_FILENAME


def generate_consignment_agreement_docx_bytes(*, item: Dict[str, Any]) -> BytesIO:
    """Fill the supplied DOCX template without rebuilding its layout or styles."""
    mapping = build_consignment_agreement_mapping(item)
    output = BytesIO()

    with ZipFile(template_path(), "r") as source, ZipFile(
        output, "w", compression=ZIP_DEFLATED
    ) as target:
        document_xml = source.read(DOCUMENT_PART)
        for placeholder, replacement in mapping.items():
            token = placeholder.encode("utf-8")
            if document_xml.count(token) != 1:
                raise ValueError(
                    f"Consignment agreement template slot must occur once: {placeholder}"
                )
            safe_value = escape(str(replacement), quote=False).encode("utf-8")
            document_xml = document_xml.replace(token, safe_value, 1)

        for info in source.infolist():
            data = document_xml if info.filename == DOCUMENT_PART else source.read(info)
            target.writestr(info, data)

    output.seek(0)
    return output

