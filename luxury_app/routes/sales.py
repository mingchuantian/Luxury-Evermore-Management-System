from datetime import timedelta, timezone
import re

from bson import ObjectId
from flask import flash, redirect, render_template, request, send_file, url_for

from models import now

from ..auth import ROLE_MANAGEMENT, require_roles
from ..audit import insert_audit_logs, make_change_detail, make_change_status, push_item_audits
from ..constants import (
    MANAGEMENT_SELLABLE_STATUSES,
    OWNERSHIP_ADMIN,
    OWNERSHIP_MANAGEMENT,
    SELLABLE_STATUSES,
)
from ..utils import money_int, parse_datetime_local_to_utc
from receipt_template.receipt_generation import generate_receipt_docx_bytes


MANAGEMENT_SALE_OWNERSHIPS = (OWNERSHIP_ADMIN, OWNERSHIP_MANAGEMENT)


def _object_id(value):
    try:
        return ObjectId(value)
    except Exception:
        return None


def _sale_form_defaults(item):
    sold_records = item.get("sold_record") or []
    last_sale = sold_records[-1] if sold_records else {}
    item["_sold"] = last_sale
    sold_currency = (
        last_sale.get("sold_currency")
        or item.get("listing_currency")
        or "SGD"
    ).strip().upper()
    local_now = now().astimezone(timezone(timedelta(hours=8)))
    sku = item.get("sku") or ""
    receipt_number = (
        f"{local_now.strftime('%Y-%m-%d')}_{sku}"
        if sku
        else local_now.strftime("%Y-%m-%d")
    )
    return sold_currency, receipt_number


def _send_receipt(item, record, receipt_number, sku):
    buffer = generate_receipt_docx_bytes(item=item, sold=record)
    filename_base = receipt_number or (f"{sku}_receipt" if sku else "receipt")
    filename_base = re.sub(r"[^0-9A-Za-z._-]+", "_", filename_base).strip("_") or "receipt"
    download_name = f"{filename_base}.docx"
    try:
        return send_file(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=download_name,
        )
    except TypeError:
        return send_file(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            attachment_filename=download_name,
        )


def _process_sale(
    items,
    audit_logs,
    item,
    oid,
    *,
    form_endpoint,
    success_url,
    update_scope=None,
    english=False,
):
    action = (request.form.get("action") or "sell").strip().lower()
    buyer = (request.form.get("buyer") or "").strip()
    sale_channel = (request.form.get("sale_channel") or "").strip()
    sold_price = money_int(request.form.get("sold_price"))
    sold_currency = (request.form.get("sold_currency") or "SGD").strip().upper()
    payment_method = (request.form.get("payment_method") or "").strip()
    receipt_number = (request.form.get("receipt_no") or "").strip()
    package_inclusion = (request.form.get("package_inclusion") or "").strip()
    sale_note = (request.form.get("sale_note") or "").strip()
    sold_at = parse_datetime_local_to_utc(
        request.form.get("sold_at") or "",
        request.form.get("tz_offset_min") or "0",
    ) or now()

    if sold_price <= 0:
        flash("Sold price must be greater than 0" if english else "成交价必须 > 0", "error")
        return redirect(url_for(form_endpoint, item_id=oid))
    if not package_inclusion:
        flash("Package inclusion is required" if english else "售出 inclusion 必填", "error")
        return redirect(url_for(form_endpoint, item_id=oid))

    sku = item.get("sku") or ""
    if not receipt_number:
        local_sold_at = sold_at
        if getattr(local_sold_at, "tzinfo", None) is None:
            local_sold_at = local_sold_at.replace(tzinfo=timezone.utc)
        local_sold_at = local_sold_at.astimezone(timezone(timedelta(hours=8)))
        receipt_number = (
            f"{local_sold_at.strftime('%Y-%m-%d')}_{sku}"
            if sku
            else local_sold_at.strftime("%Y-%m-%d")
        )

    record = {
        "sold_at": sold_at,
        "buyer": buyer,
        "sale_note": sale_note,
        "sold_currency": sold_currency,
        "sold_price": sold_price,
        "sale_channel": sale_channel,
        "payment_method": payment_method,
        "receipt_no": receipt_number,
        "package_inclusion": package_inclusion,
    }
    if action == "receipt":
        return _send_receipt(item, record, receipt_number, sku)

    old_status = item.get("status") or ""
    entries = [make_change_status(sku=sku, from_status=old_status, to_status="SOLD")]
    for key, value in [
        ("buyer", buyer), ("sale_channel", sale_channel), ("sale_note", sale_note),
        ("sold_price", sold_price), ("sold_currency", sold_currency),
        ("sold_at", sold_at), ("payment_method", payment_method),
        ("receipt_no", receipt_number), ("package_inclusion", package_inclusion),
    ]:
        entries.append(make_change_detail(sku=sku, target=key, from_value="", to_value=value))

    compare = {"_id": oid, "status": old_status}
    if update_scope:
        compare.update(update_scope)
    result = items.update_one(
        compare,
        {"$set": {"status": "SOLD", "updated_at": now()}, "$push": {"sold_record": record}},
    )
    if result.modified_count != 1:
        flash("Item status changed before the sale was saved. Please try again.", "error")
        return redirect(success_url)

    push_item_audits(items, oid, entries)
    insert_audit_logs(audit_logs, entries)
    flash("Marked as sold" if english else "已售出", "ok")
    return redirect(success_url)


def register(app, items, audit_logs=None):
    @app.get("/sales/new/<item_id>")
    def sale_new_form(item_id):
        oid = _object_id(item_id)
        item = items.find_one({"_id": oid}) if oid else None
        if not item:
            return "未找到该商品", 404
        if item.get("status") not in SELLABLE_STATUSES:
            flash("该商品当前状态不可售出", "error")
            return redirect(url_for("item_detail", item_key=item_id))
        sold_currency, receipt_number = _sale_form_defaults(item)
        return render_template(
            "sale_new.html", item=item, sold_currency_default=sold_currency,
            receipt_no_default=receipt_number, submit_endpoint="sale_new_create",
        )

    @app.post("/sales/new/<item_id>")
    def sale_new_create(item_id):
        oid = _object_id(item_id)
        item = items.find_one({"_id": oid}) if oid else None
        if not item:
            return "未找到该商品", 404
        if item.get("status") not in SELLABLE_STATUSES:
            flash("该商品当前状态不可售出", "error")
            return redirect(url_for("item_detail", item_key=item_id))
        return _process_sale(
            items, audit_logs, item, oid, form_endpoint="sale_new_form",
            success_url=url_for("item_detail", item_key=item_id),
        )

    @app.get("/management/sales/new/<item_id>")
    @require_roles(ROLE_MANAGEMENT)
    def management_sale_new_form(item_id):
        oid = _object_id(item_id)
        item = items.find_one({
            "_id": oid,
            "ownership": {"$in": list(MANAGEMENT_SALE_OWNERSHIPS)},
            "status": {"$in": list(MANAGEMENT_SELLABLE_STATUSES)},
        }) if oid else None
        if not item:
            return "Not found", 404
        sold_currency, receipt_number = _sale_form_defaults(item)
        return render_template(
            "management/sale_new.html", item=item,
            sold_currency_default=sold_currency,
            receipt_no_default=receipt_number,
            submit_endpoint="management_sale_new_create",
        )

    @app.post("/management/sales/new/<item_id>")
    @require_roles(ROLE_MANAGEMENT)
    def management_sale_new_create(item_id):
        oid = _object_id(item_id)
        ownership_scope = {"$in": list(MANAGEMENT_SALE_OWNERSHIPS)}
        item = items.find_one({
            "_id": oid,
            "ownership": ownership_scope,
            "status": {"$in": list(MANAGEMENT_SELLABLE_STATUSES)},
        }) if oid else None
        if not item:
            return "Not found", 404
        return _process_sale(
            items, audit_logs, item, oid,
            form_endpoint="management_sale_new_form",
            success_url=url_for("management_list_items"),
            update_scope={"ownership": ownership_scope},
            english=True,
        )
