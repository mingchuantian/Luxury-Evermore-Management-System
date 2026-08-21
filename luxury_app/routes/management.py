"""English Management/Staff pages backed by the shared items collection."""

import re
from datetime import datetime, timezone
from math import ceil

from bson import ObjectId
from flask import flash, redirect, render_template, request, url_for
from pymongo import DESCENDING

from models import now

from ..auth import ROLE_MANAGEMENT, ROLE_STAFF, require_roles
from ..audit import insert_audit_logs, make_change_detail, push_item_audits
from ..constants import (
    BRAND_OPTIONS,
    MANAGEMENT_STATUSES,
    OWNERSHIP_ADMIN,
    OWNERSHIP_MANAGEMENT,
)
from ..item_creation import create_item_from_form
from ..utils import money_int


VISIBLE_OWNERSHIPS = (OWNERSHIP_ADMIN, OWNERSHIP_MANAGEMENT)


def _is_object_id(value):
    try:
        ObjectId(str(value))
        return True
    except Exception:
        return False


def register(app, items, audit_logs=None):
    def _recent_admin_sold_ids():
        return [
            doc["_id"]
            for doc in items.find({
                "ownership": OWNERSHIP_ADMIN,
                "status": "SOLD",
            }).sort("sold_record.sold_at", DESCENDING).limit(50)
            if doc.get("_id") is not None
        ]

    def _management_owned_filter(extra=None):
        query = {
            "ownership": OWNERSHIP_MANAGEMENT,
            "status": {"$in": list(MANAGEMENT_STATUSES)},
        }
        if extra:
            query.update(extra)
        return query

    def _attach_sold_view_fields(doc):
        sold_records = doc.get("sold_record") or []
        doc["_sold"] = sold_records[-1] if sold_records else {}
        return doc

    def _activity_timestamp(doc):
        """Sort visible Admin inventory by its latest relevant business event."""
        value = None
        if doc.get("status") == "SOLD":
            value = (doc.get("_sold") or {}).get("sold_at")
        value = value or doc.get("purchase_at") or doc.get("created_at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return 0.0

    @app.get("/management")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def management_index():
        recent_admin_sold = _recent_admin_sold_ids()
        received = items.count_documents({
            "ownership": {"$in": list(VISIBLE_OWNERSHIPS)},
            "status": "RECEIVED",
        })
        on_shelf = items.count_documents({
            "ownership": {"$in": list(VISIBLE_OWNERSHIPS)},
            "status": "ON_SHELF",
        })
        management_sold = items.count_documents({
            "ownership": OWNERSHIP_MANAGEMENT,
            "status": "SOLD",
        })
        sold = management_sold + len(recent_admin_sold)
        total = received + on_shelf + sold
        return render_template(
            "management/index.html",
            total=total,
            received=received,
            on_shelf=on_shelf,
            sold=sold,
        )

    @app.get("/management/items")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def management_list_items():
        q = (request.args.get("q") or "").strip()
        status = (request.args.get("status") or "").strip()
        source_type = (request.args.get("source_type") or "").strip()
        try:
            page = max(1, int(request.args.get("page", "1")))
        except Exception:
            page = 1
        per_page = 1000

        recent_admin_sold = _recent_admin_sold_ids()
        admin_conditions = [
            {"ownership": OWNERSHIP_ADMIN},
            {"$or": [
                {"status": {"$in": ["RECEIVED", "ON_SHELF"]}},
                {"_id": {"$in": recent_admin_sold}},
            ]},
        ]
        admin_branch = {"$and": admin_conditions}
        management_branch = {"ownership": OWNERSHIP_MANAGEMENT}
        filt = {
            "status": {"$in": list(MANAGEMENT_STATUSES)},
            "$or": [admin_branch, management_branch],
        }
        if status in MANAGEMENT_STATUSES:
            filt["status"] = status
        elif status:
            status = ""
        if source_type in ("BUY_IN", "CONSIGNMENT"):
            # Admin item type is not part of the Management-visible field set.
            # Apply this filter only to Management-owned inventory.
            management_branch["source_type"] = source_type
        elif source_type:
            source_type = ""
        if q:
            q_esc = re.escape(q[:80])
            public_search = [
                {"sku": {"$regex": q_esc, "$options": "i"}},
                {"name": {"$regex": q_esc, "$options": "i"}},
                {"brand": {"$regex": q_esc, "$options": "i"}},
            ]
            admin_conditions.append({"$or": public_search})
            management_branch["$or"] = public_search + [
                {"note": {"$regex": q_esc, "$options": "i"}},
                {"tracking_number": {"$regex": q_esc, "$options": "i"}},
                {"serial_code": {"$regex": q_esc, "$options": "i"}},
            ]

        total = items.count_documents(filt)
        total_pages = max(1, ceil(total / per_page)) if total else 1
        page = min(page, total_pages)
        docs = list(
            items.find(filt)
            .sort("created_at", DESCENDING)
            .skip((page - 1) * per_page)
            .limit(per_page)
        )
        for doc in docs:
            _attach_sold_view_fields(doc)

        admin_items = [
            doc for doc in docs if doc.get("ownership") == OWNERSHIP_ADMIN
        ]
        admin_items.sort(key=_activity_timestamp, reverse=True)

        return render_template(
            "management/items.html",
            admin_items=admin_items,
            management_items=[doc for doc in docs if doc.get("ownership") == OWNERSHIP_MANAGEMENT],
            MANAGEMENT_STATUSES=MANAGEMENT_STATUSES,
            q=q,
            status=status,
            source_type=source_type,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
        )

    @app.get("/management/items/new/buyin")
    @require_roles(ROLE_MANAGEMENT)
    def management_item_new_buyin_form():
        return render_template(
            "management/item_new.html",
            entry_type="BUY_IN",
            entry_type_label="Add Buy-In",
            BRAND_OPTIONS=BRAND_OPTIONS,
        )

    @app.get("/management/items/new/consignment")
    @require_roles(ROLE_MANAGEMENT)
    def management_item_new_consignment_form():
        return render_template(
            "management/item_new.html",
            entry_type="CONSIGNMENT",
            entry_type_label="Add Consignment",
            BRAND_OPTIONS=BRAND_OPTIONS,
        )

    @app.post("/management/items/new")
    @require_roles(ROLE_MANAGEMENT)
    def management_item_new_create():
        entry_type = (request.form.get("entry_type") or "BUY_IN").strip().upper()
        try:
            doc, _ = create_item_from_form(
                items,
                audit_logs,
                request.form,
                ownership=OWNERSHIP_MANAGEMENT,
                initial_status="RECEIVED",
            )
        except ValueError:
            flash("Product name is required", "error")
            back = (
                "management_item_new_consignment_form"
                if entry_type == "CONSIGNMENT"
                else "management_item_new_buyin_form"
            )
            return redirect(url_for(back))

        flash("Item added", "ok")
        return redirect(url_for("management_item_detail", item_key=doc["sku"]))

    @app.get("/management/items/<item_key>")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def management_item_detail(item_key):
        item = None
        if _is_object_id(item_key):
            item = items.find_one(
                _management_owned_filter({"_id": ObjectId(item_key)})
            )
        if not item:
            item = items.find_one(_management_owned_filter({"sku": item_key}))
        if not item:
            return "Not found", 404
        _attach_sold_view_fields(item)
        return render_template(
            "management/item_detail.html",
            item=item,
            BRAND_OPTIONS=BRAND_OPTIONS,
        )

    @app.post("/management/items/<item_id>/update")
    @require_roles(ROLE_MANAGEMENT, ROLE_STAFF)
    def management_item_update(item_id):
        if not _is_object_id(item_id):
            return "invalid id", 400
        oid = ObjectId(item_id)
        item = items.find_one(_management_owned_filter({"_id": oid}))
        if not item:
            return "Not found", 404

        form = request.form
        set_fields = {
            "name": (form.get("name") or "").strip(),
            "brand": (
                (form.get("brand_custom") or "").strip()
                if (form.get("brand_select") or "").strip() in ("OTHER", "其他")
                else (form.get("brand_select") or "").strip()
            ),
            "seller_name": (form.get("seller_name") or "").strip(),
            "seller_contact": (form.get("seller_contact") or "").strip(),
            "cost": money_int(form.get("cost")),
            "note": (form.get("note") or "").strip(),
            "serial_code": (form.get("serial_code") or "").strip(),
            "tracking_number": (form.get("tracking_number") or "").strip(),
            "accessories": (form.get("accessories") or "").strip(),
            "updated_at": now(),
        }
        if "name_in_EN" in form:
            set_fields["name_in_EN"] = (form.get("name_in_EN") or "").strip()
        if "additional_notes_for_agreements" in form:
            set_fields["additional_notes_for_agreements"] = (
                form.get("additional_notes_for_agreements") or ""
            ).strip()
        if form.get("listing_price") not in (None, ""):
            set_fields["listing_price"] = money_int(form.get("listing_price"))
        if form.get("listing_currency"):
            set_fields["listing_currency"] = form.get("listing_currency").strip().upper()
        if form.get("profit") not in (None, ""):
            set_fields["profit"] = money_int(form.get("profit"))
        if form.get("profit_currency"):
            set_fields["profit_currency"] = form.get("profit_currency").strip().upper()

        entries = []
        sku = item.get("sku") or ""
        for key, new_value in set_fields.items():
            if key == "updated_at":
                continue
            old_value = item.get(key, "")
            if old_value != new_value:
                entries.append(
                    make_change_detail(
                        sku=sku,
                        target=key,
                        from_value=old_value,
                        to_value=new_value,
                    )
                )

        if not entries:
            flash("No changes", "ok")
            return redirect(url_for("management_item_detail", item_key=item_id))

        items.update_one(_management_owned_filter({"_id": oid}), {"$set": set_fields})
        push_item_audits(items, oid, entries)
        insert_audit_logs(audit_logs, entries)
        flash("Saved", "ok")
        return redirect(url_for("management_item_detail", item_key=item_id))
