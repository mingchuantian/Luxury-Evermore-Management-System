"""English Management/Staff pages backed by the shared items collection."""

import re
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

from bson import ObjectId
from flask import (
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from pymongo import DESCENDING

from models import now
from consignment_receipt_template.consignment_agreement_generation import (
    generate_consignment_agreement_docx_bytes,
)

from ..auth import ROLE_MANAGEMENT, require_roles
from ..audit import insert_audit_logs, make_change_detail, push_item_audits
from ..constants import (
    BRAND_OPTIONS,
    MANAGEMENT_STATUSES,
    OWNERSHIP_ADMIN,
    OWNERSHIP_MANAGEMENT,
)
from ..item_creation import create_item_from_form
from ..utils import BUSINESS_TZ, money_int


VISIBLE_OWNERSHIPS = (OWNERSHIP_ADMIN, OWNERSHIP_MANAGEMENT)


def _is_object_id(value):
    try:
        ObjectId(str(value))
        return True
    except Exception:
        return False


def register(app, items, audit_logs=None, users=None):
    def _dt8_date_str(value):
        if value is None or value == "":
            return ""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
        if not isinstance(value, datetime):
            return str(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(
            timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d")

    def _fmt_money(currency, amount):
        try:
            amount = int(amount or 0)
        except Exception:
            amount = 0
        currency = (currency or "").strip().upper()
        return f"{currency} {amount:,}".strip()

    def _render_purchase_agreement(item):
        template_path = (
            Path(__file__).absolute().parents[2]
            / "purchase_agreement_template"
            / "purchase_agreement.html"
        )
        html = template_path.read_text(encoding="utf-8")
        mapping = {
            "{{REF NUMBER}}": (item.get("sku") or "").strip(),
            "{{DATE}}": _dt8_date_str(
                item.get("purchase_at") or item.get("created_at")
            ),
            "{{NAME}}": (item.get("seller_name") or "").strip(),
            "{{PHONE OR EMAIL}}": (
                item.get("seller_contact") or ""
            ).strip(),
            "{{ITEM NAME}}": (item.get("name_in_EN") or "").strip(),
            "{{PURCHASE PRICE}}": _fmt_money(
                item.get("cost_currency") or item.get("currency"),
                item.get("cost"),
            ),
            "{{ADDITIONAL NOTE}}": (
                item.get("additional_notes_for_agreements") or ""
            ).strip() or "N.A.",
            "{{METHOD OF PAYMENT}}": "N.A.",
            "{{PAYMENT STATUS}}": "Paid",
        }
        for token, value in mapping.items():
            html = html.replace(token, value)
        return html

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

    def _display_date_timestamp(doc):
        """Sort by the same value rendered in the Admin Items Date column."""
        value = doc.get("purchase_at") or doc.get("created_at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return 0.0

    def _management_identities():
        user_ids = set()
        usernames = set()
        # The active Management account is authoritative even in lightweight
        # test/dev setups where the users collection is not supplied.
        if session.get("user_id"):
            user_ids.add(str(session["user_id"]))
        if session.get("username"):
            usernames.add(str(session["username"]))
        if users is not None:
            try:
                for user in users.find({"role": ROLE_MANAGEMENT}):
                    if user.get("_id") is not None:
                        user_ids.add(str(user["_id"]))
                    if user.get("username"):
                        usernames.add(str(user["username"]))
            except Exception:
                app.logger.exception(
                    "Failed to load Management users for commission history."
                )
        return user_ids, usernames

    def _legacy_sale_was_by_management(item, user_ids, usernames):
        for entry in reversed(item.get("audit") or []):
            if (
                entry.get("action") == "Change_status"
                and entry.get("to") == "SOLD"
            ):
                operator = entry.get("by") or {}
                operator_id = str(operator.get("user_id") or "")
                operator_name = str(operator.get("username") or "")
                return (
                    (operator_id and operator_id in user_ids)
                    or (operator_name and operator_name in usernames)
                )
        # Very old entries may predate usable audit data. Preserve the earlier
        # compatibility rule only when no SOLD audit can be found at all.
        return item.get("ownership") == OWNERSHIP_MANAGEMENT

    def _management_commission_sales(selected_month):
        """Build a month-filtered ledger from sale records made by Management."""
        rows = []
        available_months = set()
        management_user_ids, management_usernames = _management_identities()
        for item in items.find({
            "status": "SOLD",
            "ownership": {"$in": list(VISIBLE_OWNERSHIPS)},
        }):
            sale_records = item.get("sold_record") or []
            if not sale_records:
                continue
            sale = sale_records[-1] or {}
            sold_at = sale.get("sold_at")
            if not isinstance(sold_at, datetime):
                continue
            if sold_at.tzinfo is None:
                sold_at = sold_at.replace(tzinfo=timezone.utc)
            sold_at_local = sold_at.astimezone(BUSINESS_TZ)
            month = sold_at_local.strftime("%Y-%m")

            explicitly_management = sale.get("sold_by_role") == ROLE_MANAGEMENT
            legacy_management_sale = (
                not sale.get("sold_by_role")
                and _legacy_sale_was_by_management(
                    item, management_user_ids, management_usernames
                )
            )
            if not (explicitly_management or legacy_management_sale):
                continue

            available_months.add(month)
            if month != selected_month:
                continue
            sold_price = sale.get("sold_price") or 0
            try:
                sold_price = float(sold_price)
            except (TypeError, ValueError):
                sold_price = 0
            amount = sale.get("management_commission_amount")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                amount = round(sold_price * 0.01, 2)
            currency = (
                sale.get("management_commission_currency")
                or sale.get("sold_currency")
                or item.get("listing_currency")
                or "SGD"
            ).strip().upper()
            rows.append({
                "sku": item.get("sku") or "",
                "serial_code": item.get("serial_code") or "",
                "name": item.get("name") or "",
                "shopify_title": (
                    ((item.get("shopify_details") or {}).get("title") or "")
                    if item.get("shopify_sku_exist") is True else ""
                ),
                "sold_at": sold_at_local,
                "sold_price": sold_price,
                "currency": currency,
                "commission": amount,
                "paid": bool(
                    sale.get("management_commission_paid")
                    or sale.get("management_commission_paid_at")
                ),
            })
        rows.sort(key=lambda row: row["sold_at"], reverse=True)
        return rows, available_months

    @app.get("/management")
    @require_roles(ROLE_MANAGEMENT)
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

        current_month = now().astimezone(BUSINESS_TZ).strftime("%Y-%m")
        commission_month = (request.args.get("commission_month") or "").strip()
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", commission_month):
            commission_month = current_month
        commission_sales, available_months = _management_commission_sales(
            commission_month
        )
        available_months.add(current_month)
        available_months.add(commission_month)
        commission_totals = {}
        for row in commission_sales:
            currency = row["currency"]
            if not row["paid"]:
                commission_totals[currency] = (
                    commission_totals.get(currency, 0) + row["commission"]
                )
        return render_template(
            "management/index.html",
            total=total,
            received=received,
            on_shelf=on_shelf,
            sold=sold,
            commission_month=commission_month,
            commission_months=sorted(available_months, reverse=True),
            commission_sales=commission_sales,
            commission_totals=commission_totals,
        )

    @app.get("/management/items")
    @require_roles(ROLE_MANAGEMENT)
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
                {"serial_code": {"$regex": q_esc, "$options": "i"}},
            ]
            admin_conditions.append({"$or": public_search})
            management_branch["$or"] = public_search + [
                {"note": {"$regex": q_esc, "$options": "i"}},
                {"tracking_number": {"$regex": q_esc, "$options": "i"}},
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
        admin_items.sort(key=_display_date_timestamp, reverse=True)

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
    @require_roles(ROLE_MANAGEMENT)
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
    @require_roles(ROLE_MANAGEMENT)
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

    @app.get("/management/items/<item_id>/agreement/purchase")
    @require_roles(ROLE_MANAGEMENT)
    def management_item_purchase_agreement(item_id):
        if not _is_object_id(item_id):
            return "invalid id", 400
        item = items.find_one(_management_owned_filter({
            "_id": ObjectId(item_id),
            "source_type": "BUY_IN",
        }))
        if not item:
            return "Not found", 404
        return Response(
            _render_purchase_agreement(item),
            mimetype="text/html; charset=utf-8",
        )

    @app.get("/management/items/<item_id>/agreement/consignment")
    @require_roles(ROLE_MANAGEMENT)
    def management_item_consignment_agreement(item_id):
        if not _is_object_id(item_id):
            return "invalid id", 400
        item = items.find_one(_management_owned_filter({
            "_id": ObjectId(item_id),
            "source_type": "CONSIGNMENT",
        }))
        if not item:
            return "Not found", 404

        document = generate_consignment_agreement_docx_bytes(item=item)
        filename = re.sub(
            r"[^0-9A-Za-z._-]+", "_", (item.get("sku") or "")
        ).strip("_") or "consignment"
        kwargs = {
            "mimetype": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "as_attachment": True,
        }
        try:
            return send_file(
                document,
                download_name=f"{filename}_consignment_agreement.docx",
                **kwargs,
            )
        except TypeError:
            return send_file(
                document,
                attachment_filename=(
                    f"{filename}_consignment_agreement.docx"
                ),
                **kwargs,
            )

    @app.post("/management/items/<item_id>/action/delete")
    @require_roles(ROLE_MANAGEMENT)
    def management_item_delete(item_id):
        if not _is_object_id(item_id):
            return "invalid id", 400
        oid = ObjectId(item_id)
        scope = _management_owned_filter({"_id": oid})
        item = items.find_one(scope)
        if not item:
            return "Not found", 404
        if audit_logs is not None and item.get("sku"):
            insert_audit_logs(audit_logs, [make_change_detail(
                sku=item.get("sku"),
                target="deleted",
                from_value="",
                to_value=True,
            )])
        result = items.delete_one(scope)
        if result.deleted_count != 1:
            return "Not found", 404
        return "", 204
