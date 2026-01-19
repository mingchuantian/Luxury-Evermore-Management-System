"""
Low-role (Management/Staff) routes/pages.

NOTE:
- These pages are intentionally separated from the Admin UI.
- Templates live under templates/outsider/ and are English-only.
- Data source: inventory.items_outsider
"""

import re
from math import ceil

from bson import ObjectId
from flask import flash, redirect, render_template, request, url_for
from pymongo import DESCENDING

from models import now
from models import STATUS

from ..auth import ROLE_MANAGEMENT, ROLE_STAFF, require_roles
from ..audit import insert_audit_logs, make_change_detail, push_item_audits


def _is_object_id(s: str) -> bool:
    try:
        ObjectId(str(s))
        return True
    except Exception:
        return False


def register(app, items_outsider, audit_logs=None):
    def _attach_sold_view_fields(doc: dict) -> dict:
        sr = doc.get("sold_record") or []
        doc["_sold"] = sr[-1] if sr else {}
        return doc

    @app.get("/outsider")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def outsider_index():
        total = items_outsider.count_documents({})
        on_shelf = items_outsider.count_documents({"status": "ON_SHELF"})
        in_stock = items_outsider.count_documents({"status": {"$in": ["INBOUND", "REPARING", "RECEIVED", "ON_SHELF", "RESERVED"]}})
        sold = items_outsider.count_documents({"status": "SOLD"})
        return render_template(
            "outsider/index.html",
            total=total,
            in_stock=in_stock,
            on_shelf=on_shelf,
            sold=sold,
        )

    @app.get("/outsider/items")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def outsider_list_items():
        q = (request.args.get("q") or "").strip()
        status = (request.args.get("status") or "").strip()
        source_type = (request.args.get("source_type") or "").strip()
        try:
            page = int(request.args.get("page", "1"))
        except Exception:
            page = 1
        if page < 1:
            page = 1
        per_page = 1000

        filt = {}
        if status:
            filt["status"] = status
        if source_type:
            filt["source_type"] = source_type
        if q:
            q_esc = re.escape(q[:80])
            filt["$or"] = [
                {"sku": {"$regex": q_esc, "$options": "i"}},
                {"name": {"$regex": q_esc, "$options": "i"}},
                {"brand": {"$regex": q_esc, "$options": "i"}},
                {"note": {"$regex": q_esc, "$options": "i"}},
                {"tracking_number": {"$regex": q_esc, "$options": "i"}},
                {"serial_code": {"$regex": q_esc, "$options": "i"}},
            ]

        total = items_outsider.count_documents(filt)
        total_pages = max(1, ceil(total / per_page)) if total else 1
        if page > total_pages:
            page = total_pages

        skip = (page - 1) * per_page
        docs = list(items_outsider.find(filt).sort("created_at", DESCENDING).skip(skip).limit(per_page))
        for d in docs:
            _attach_sold_view_fields(d)

        return render_template(
            "outsider/items.html",
            items=docs,
            STATUS=STATUS,
            q=q,
            status=status,
            source_type=source_type,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
        )

    @app.post("/outsider/items/<item_id>/action/set_tracking_number")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def outsider_set_tracking_number(item_id):
        it = items_outsider.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404
        if it.get("status") != "INBOUND":
            return "invalid status", 400

        data = request.get_json(silent=True) or {}
        tracking_number = (data.get("tracking_number") or "").strip()
        if not tracking_number:
            return "missing tracking_number", 400

        old_v = it.get("tracking_number", "")
        if old_v == tracking_number:
            return "", 204

        items_outsider.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": {"tracking_number": tracking_number, "updated_at": now()}}
        )

        sku = it.get("sku") or ""
        entry = make_change_detail(sku=sku, target="tracking_number", from_value=old_v, to_value=tracking_number)
        push_item_audits(items_outsider, ObjectId(item_id), [entry])
        insert_audit_logs(audit_logs, [entry])
        return "", 204

    @app.get("/outsider/items/<item_key>")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def outsider_item_detail(item_key):
        it = None
        if _is_object_id(item_key):
            it = items_outsider.find_one({"_id": ObjectId(item_key)})
        if not it:
            it = items_outsider.find_one({"sku": item_key})
        if not it:
            return "Not found", 404
        _attach_sold_view_fields(it)
        return render_template("outsider/item_detail.html", item=it)

    @app.post("/outsider/items/<item_id>/update")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def outsider_item_update(item_id):
        it = items_outsider.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "Not found", 404

        # Low-role edit policy: only allow a small safe subset.
        allowed_fields = ["name", "note", "accessories", "tracking_number", "serial_code"]
        set_fields = {"updated_at": now()}
        entries = []
        sku = it.get("sku") or ""

        for k in allowed_fields:
            if k not in request.form:
                continue
            new_v = (request.form.get(k) or "").strip()
            old_v = (it.get(k) or "")
            if old_v != new_v:
                set_fields[k] = new_v
                entries.append(make_change_detail(sku=sku, target=k, from_value=old_v, to_value=new_v))

        if len(set_fields) == 1:
            flash("No changes", "ok")
            return redirect(url_for("outsider_item_detail", item_key=item_id))

        items_outsider.update_one({"_id": ObjectId(item_id)}, {"$set": set_fields})
        push_item_audits(items_outsider, ObjectId(item_id), entries)
        insert_audit_logs(audit_logs, entries)

        flash("Saved", "ok")
        return redirect(url_for("outsider_item_detail", item_key=item_id))


