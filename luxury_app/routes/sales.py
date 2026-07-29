from datetime import timedelta, timezone
from pathlib import Path
import re

from bson import ObjectId
from flask import flash, redirect, render_template, request, send_file, url_for

from models import now

from ..audit import insert_audit_logs, make_change_detail, make_change_status, push_item_audits
from ..constants import SELLABLE_STATUSES
from ..utils import money_int, parse_datetime_local_to_utc
from receipt_template.receipt_generation import generate_receipt_docx_bytes


def register(app, items, audit_logs=None):
    @app.get("/sales/new/<item_id>")
    def sale_new_form(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "未找到该商品", 404
        if it.get("status") not in SELLABLE_STATUSES:
            flash("该商品当前状态不可售出", "error")
            return redirect(url_for("item_detail", item_key=item_id))

        sr = it.get("sold_record") or []
        last_sr = sr[-1] if sr else {}
        it["_sold"] = last_sr
        sold_currency = (last_sr.get("sold_currency") or it.get("listing_currency") or "SGD").strip().upper()
        sku = it.get("sku") or ""
        # receipt_no default uses UTC+8 date for operator friendliness
        t = now().astimezone(timezone(timedelta(hours=8)))
        receipt_no_default = f"{t.strftime('%Y-%m-%d')}_{sku}" if sku else t.strftime("%Y-%m-%d")

        return render_template(
            "sale_new.html",
            item=it,
            sold_currency_default=sold_currency,
            receipt_no_default=receipt_no_default,
        )

    @app.post("/sales/new/<item_id>")
    def sale_new_create(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "未找到该商品", 404
        if it.get("status") not in SELLABLE_STATUSES:
            flash("该商品当前状态不可售出", "error")
            return redirect(url_for("item_detail", item_key=item_id))

        action = (request.form.get("action") or "sell").strip().lower()
        buyer = request.form.get("buyer", "").strip()
        sale_channel = request.form.get("sale_channel", "").strip()
        sold_price = money_int(request.form.get("sold_price"))
        sold_currency = (request.form.get("sold_currency", "SGD") or "SGD").strip().upper()
        payment_method = request.form.get("payment_method", "").strip()
        receipt_no = request.form.get("receipt_no", "").strip()
        package_inclusion = request.form.get("package_inclusion", "").strip()
        sale_note = request.form.get("sale_note", "").strip()

        sold_at = parse_datetime_local_to_utc(
            request.form.get("sold_at", ""),
            request.form.get("tz_offset_min", "0"),
        ) or now()

        if sold_price <= 0:
            flash("成交价必须 > 0", "error")
            return redirect(url_for("sale_new_form", item_id=item_id))
        if not package_inclusion:
            flash("售出 inclusion 必填", "error")
            return redirect(url_for("sale_new_form", item_id=item_id))

        sku = it.get("sku") or ""
        if not receipt_no:
            # default uses UTC+8 date for operator friendliness
            t8 = sold_at
            if hasattr(t8, "tzinfo") and t8.tzinfo is None:
                t8 = t8.replace(tzinfo=timezone.utc)
            t8 = t8.astimezone(timezone(timedelta(hours=8)))
            receipt_no = f"{t8.strftime('%Y-%m-%d')}_{sku}" if sku else t8.strftime("%Y-%m-%d")

        # Build a record dict from the form, used for receipt generation and/or persistence.
        record = {
            "sold_at": sold_at,
            "buyer": buyer,
            "sale_note": sale_note,
            "sold_currency": sold_currency,
            "sold_price": sold_price,
            "sale_channel": sale_channel,
            "payment_method": payment_method,
            "receipt_no": receipt_no,
            "package_inclusion": package_inclusion,
        }

        # Receipt-only: generate docx without touching MongoDB / status.
        if action == "receipt":
            buf = generate_receipt_docx_bytes(item=it, sold=record)
            filename_base = receipt_no or (f"{sku}_receipt" if sku else "receipt")
            filename_base = re.sub(r"[^0-9A-Za-z._-]+", "_", filename_base).strip("_") or "receipt"
            download_name = f"{filename_base}.docx"
            try:
                return send_file(
                    buf,
                    mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    as_attachment=True,
                    download_name=download_name,
                )
            except TypeError:
                # Flask < 2.0 compatibility
                return send_file(
                    buf,
                    mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    as_attachment=True,
                    attachment_filename=download_name,
                )

        old_status = it.get("status", "")
        entries = [make_change_status(sku=sku, from_status=old_status, to_status="SOLD")]
        for k, v in [
            ("buyer", buyer),
            ("sale_channel", sale_channel),
            ("sale_note", sale_note),
            ("sold_price", sold_price),
            ("sold_currency", sold_currency),
            ("sold_at", sold_at),
            ("payment_method", payment_method),
            ("receipt_no", receipt_no),
            ("package_inclusion", package_inclusion),
        ]:
            # Sale details are now stored in sold_record; we still write audit entries for traceability.
            entries.append(make_change_detail(sku=sku, target=k, from_value="", to_value=v))

        result = items.update_one(
            {"_id": ObjectId(item_id), "status": old_status},
            {
                "$set": {
                    "status": "SOLD",
                    "updated_at": now(),
                },
                "$push": {"sold_record": record},
            },
        )
        if result.modified_count != 1:
            flash("Item status changed before the sale was saved. Please try again.", "error")
            return redirect(url_for("item_detail", item_key=item_id))

        push_item_audits(items, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)

        flash("已售出", "ok")
        return redirect(url_for("item_detail", item_key=item_id))


