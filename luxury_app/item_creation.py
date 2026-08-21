"""Shared, server-controlled item creation for Admin and Management."""

from models import now

from .audit import (
    insert_audit_logs,
    make_change_detail,
    make_change_status,
    push_item_audits,
)
from .constants import VALID_OWNERSHIPS
from .utils import (
    gen_sku_unique,
    money_int,
    parse_date_yyyy_mm_dd,
    parse_datetime_local_to_utc,
)


def create_item_from_form(
    items,
    audit_logs,
    form,
    *,
    ownership,
    initial_status="INBOUND",
):
    """Create one item while keeping ownership outside user-controlled form data."""
    if ownership not in VALID_OWNERSHIPS:
        raise ValueError("invalid ownership")
    if initial_status not in ("INBOUND", "RECEIVED"):
        raise ValueError("invalid initial status")

    entry_type = (form.get("entry_type") or "BUY_IN").strip().upper()
    if entry_type not in ("BUY_IN", "CONSIGNMENT"):
        entry_type = "BUY_IN"

    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    name_in_en = (form.get("name_in_EN") or "").strip()
    agreement_notes = (form.get("additional_notes_for_agreements") or "").strip()
    cost_currency = (
        form.get("cost_currency") or form.get("currency") or "SGD"
    ).strip().upper()
    cost = money_int(form.get("cost"))
    note = (form.get("note") or "").strip()
    serial_code = (form.get("serial_code") or "").strip()
    tracking_number = (form.get("tracking_number") or "").strip()
    accessories = (form.get("accessories") or "").strip()

    purchase_at = parse_datetime_local_to_utc(
        form.get("purchase_at") or "",
        form.get("tz_offset_min") or "0",
    )
    if not purchase_at:
        purchase_at = parse_date_yyyy_mm_dd(form.get("purchase_date") or "")

    brand_select = (form.get("brand_select") or "").strip()
    brand_custom = (form.get("brand_custom") or "").strip()
    brand = brand_custom if brand_select in ("其他", "OTHER") else brand_select

    created_at = now()
    sku = gen_sku_unique(items, brand)
    doc = {
        "sku": sku,
        "name": name,
        "name_in_EN": name_in_en,
        "brand": brand,
        "seller_name": (form.get("seller_name") or "").strip(),
        "seller_contact": (form.get("seller_contact") or "").strip(),
        "cost_currency": cost_currency,
        "cost": cost,
        "status": initial_status,
        "note": note,
        "additional_notes_for_agreements": agreement_notes,
        "created_at": created_at,
        "updated_at": created_at,
        "purchase_at": purchase_at or created_at,
        "received_at": created_at if initial_status == "RECEIVED" else None,
        "serial_code": serial_code,
        "tracking_number": tracking_number,
        "accessories": accessories,
        "source_type": entry_type,
        "is_buy_in": entry_type == "BUY_IN",
        "is_consignment": entry_type == "CONSIGNMENT",
        "ownership": ownership,
        "audit": [],
    }

    result = items.insert_one(doc)
    entries = [make_change_status(sku=sku, from_status="", to_status=initial_status)]
    for key in [
        "name", "name_in_EN", "brand", "seller_name", "seller_contact",
        "cost_currency", "cost", "purchase_at", "source_type", "ownership",
        "note", "serial_code", "tracking_number", "accessories",
        "additional_notes_for_agreements",
    ]:
        entries.append(
            make_change_detail(
                sku=sku,
                target=key,
                from_value="",
                to_value=doc.get(key, ""),
            )
        )
    push_item_audits(items, result.inserted_id, entries)
    insert_audit_logs(audit_logs, entries)
    return doc, result.inserted_id
