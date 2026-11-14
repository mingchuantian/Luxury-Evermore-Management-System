import re
from datetime import datetime, timezone
from math import ceil

from bson import ObjectId
from flask import abort, jsonify, redirect, render_template, request, session, url_for

from models import now

from ..auth import ROLE_ADMIN, require_roles


SKU_RE = re.compile(r"^[A-Z0-9_-]{2,64}$")
PAGE_SIZE = 20

# Where a check is done. Stored on the inventory check session only.
CHECK_STORE = "store"   # 店内陈列：每个款式一件，应该都已挂网
CHECK_ROOM = "room"     # 老板间：备货
CHECK_TYPES = {CHECK_STORE: "店内陈列盘查", CHECK_ROOM: "老板间盘查"}
LEGACY_LABEL = "旧盘查"

# In stock at the shop (display or back room). Used by the comparison.
IN_STOCK_STATUSES = ("RECEIVED", "ON_SHELF")


def _session_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _item_summary(item, scanned_at=None):
    summary = {
        "sku": item.get("sku") or "",
        "name": item.get("name") or "",
        "brand": item.get("brand") or "",
        "serial_code": item.get("serial_code") or "",
        "status": item.get("status") or "",
    }
    if scanned_at is not None:
        summary["scanned_at"] = scanned_at
    return summary


def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


def check_type_label(check_session):
    return CHECK_TYPES.get((check_session or {}).get("check_type"), LEGACY_LABEL)


def register(app, items, inventory_check_sessions):
    app.jinja_env.globals.setdefault("check_type_label", check_type_label)

    def _current_items(skus, fields):
        """Live item data for the given SKUs (read-only)."""
        if not skus:
            return {}
        projection = {"_id": 1, "sku": 1, **{field: 1 for field in fields}}
        return {
            doc.get("sku"): doc
            for doc in items.find({"sku": {"$in": list(skus)}}, projection)
        }

    @app.get("/inventory-check")
    @require_roles(ROLE_ADMIN)
    def inventory_check_history():
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1

        total = inventory_check_sessions.count_documents({})
        cursor = (
            inventory_check_sessions.find({})
            .sort("started_at", -1)
            .skip((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )

        def completed_of(check_type):
            return list(
                inventory_check_sessions.find(
                    {"status": "completed", "check_type": check_type},
                    {"started_at": 1, "completed_at": 1, "scanned_skus": 1},
                ).sort("started_at", -1).limit(50)
            )

        return render_template(
            "inventory_check_history.html",
            inventory_sessions=list(cursor),
            page=page,
            pages=max(1, ceil(total / PAGE_SIZE)),
            store_checks=completed_of(CHECK_STORE),
            room_checks=completed_of(CHECK_ROOM),
        )

    @app.post("/inventory-check/sessions")
    @require_roles(ROLE_ADMIN)
    def inventory_check_start():
        check_type = (request.form.get("check_type") or "").strip()
        if check_type not in CHECK_TYPES:
            abort(400, description="请选择盘查类型（店内陈列 / 老板间）")
        started_at = now()
        result = inventory_check_sessions.insert_one({
            "status": "active",
            "check_type": check_type,
            "started_at": started_at,
            "updated_at": started_at,
            "started_by": {
                "user_id": session.get("user_id") or "",
                "username": session.get("username") or "",
            },
            "scanned_skus": [],
            "scans": [],
        })
        return redirect(url_for(
            "inventory_check_session", session_id=result.inserted_id
        ))

    @app.get("/inventory-check/sessions/<session_id>")
    @require_roles(ROLE_ADMIN)
    def inventory_check_session(session_id):
        oid = _session_id(session_id)
        check_session = (
            inventory_check_sessions.find_one({"_id": oid}) if oid else None
        )
        if not check_session:
            return "inventory check session not found", 404

        # Store-display checks: which scanned items are not on the website.
        # Computed live from the current items, so it shrinks as you list them.
        linked = {}
        not_on_website = []
        if check_session.get("check_type") == CHECK_STORE:
            current = _current_items(
                check_session.get("scanned_skus") or [],
                ("name", "brand", "serial_code", "status", "shopify_sku_exist"),
            )
            for scan in check_session.get("scans") or []:
                doc = current.get(scan.get("sku"))
                is_linked = bool(doc and doc.get("shopify_sku_exist") is True)
                linked[scan.get("sku")] = is_linked
                if doc and not is_linked:
                    not_on_website.append(_item_summary(doc, scan.get("scanned_at")))

        return render_template(
            "inventory_check_session.html",
            check_session=check_session,
            is_store=check_session.get("check_type") == CHECK_STORE,
            linked=linked,
            not_on_website=not_on_website,
        )

    @app.post("/inventory-check/sessions/<session_id>/scan")
    @require_roles(ROLE_ADMIN)
    def inventory_check_scan(session_id):
        oid = _session_id(session_id)
        if not oid:
            return jsonify({"ok": False, "error": "not_found"}), 404

        body = request.get_json(silent=True) or request.form
        sku = (body.get("sku") or "").strip().upper()
        if not SKU_RE.fullmatch(sku):
            return jsonify({
                "ok": False,
                "error": "invalid_sku",
                "message": "QR code does not contain a valid SKU.",
            }), 400

        item = items.find_one(
            {"sku": sku},
            {
                "_id": 0,
                "sku": 1,
                "name": 1,
                "brand": 1,
                "serial_code": 1,
                "status": 1,
                "shopify_sku_exist": 1,
            },
        )
        if not item:
            return jsonify({
                "ok": True,
                "found": False,
                "duplicate": False,
                "sku": sku,
                "message": "SKU was not found in the inventory database.",
            })

        # Same signal the QR lookup page uses (hourly Shopify sync).
        shopify_linked = item.get("shopify_sku_exist") is True
        scanned_at = now()
        scan = _item_summary(item, scanned_at=scanned_at)
        update = inventory_check_sessions.update_one(
            {
                "_id": oid,
                "status": "active",
                "scanned_skus": {"$ne": sku},
            },
            {
                "$addToSet": {"scanned_skus": sku},
                "$push": {"scans": scan},
                "$set": {"updated_at": scanned_at},
            },
        )
        if update.modified_count:
            check_session = inventory_check_sessions.find_one(
                {"_id": oid}, {"scanned_skus": 1}
            ) or {}
            return jsonify({
                "ok": True,
                "found": True,
                "duplicate": False,
                "scan": scan,
                "shopify_linked": shopify_linked,
                "scan_count": len(check_session.get("scanned_skus") or []),
            })

        check_session = inventory_check_sessions.find_one(
            {"_id": oid}, {"status": 1, "scanned_skus": 1}
        )
        if not check_session:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if check_session.get("status") != "active":
            return jsonify({
                "ok": False,
                "error": "completed",
                "message": "This inventory check has already ended.",
            }), 409
        return jsonify({
            "ok": True,
            "found": True,
            "duplicate": True,
            "scan": scan,
            "shopify_linked": shopify_linked,
            "scan_count": len(check_session.get("scanned_skus") or []),
        })

    @app.post("/inventory-check/sessions/<session_id>/complete")
    @require_roles(ROLE_ADMIN)
    def inventory_check_complete(session_id):
        oid = _session_id(session_id)
        check_session = (
            inventory_check_sessions.find_one({"_id": oid}) if oid else None
        )
        if not check_session:
            return "inventory check session not found", 404
        if check_session.get("status") != "active":
            return redirect(url_for(
                "inventory_check_session", session_id=session_id
            ))

        completed_at = now()
        if check_session.get("check_type") in CHECK_TYPES:
            # One location alone cannot tell what is missing; that is answered
            # live by comparing a store check with a room check. Nothing else
            # is frozen into the session.
            inventory_check_sessions.update_one(
                {"_id": oid, "status": "active"},
                {"$set": {
                    "status": "completed",
                    "completed_at": completed_at,
                    "updated_at": completed_at,
                }},
            )
            return redirect(url_for(
                "inventory_check_session", session_id=session_id
            ))

        # Legacy sessions (before store / room types) keep the original result.
        received_items = list(items.find(
            {"status": "RECEIVED"},
            {
                "_id": 0,
                "sku": 1,
                "name": 1,
                "brand": 1,
                "serial_code": 1,
                "status": 1,
            },
        ))
        scanned_skus = set(check_session.get("scanned_skus") or [])
        missing_items = [
            _item_summary(item)
            for item in received_items
            if item.get("sku") not in scanned_skus
        ]
        inventory_check_sessions.update_one(
            {"_id": oid, "status": "active"},
            {"$set": {
                "status": "completed",
                "completed_at": completed_at,
                "updated_at": completed_at,
                "eligible_status": "RECEIVED",
                "eligible_count": len(received_items),
                "received_scanned_count": len(received_items) - len(missing_items),
                "missing_count": len(missing_items),
                "missing_items": missing_items,
            }},
        )
        return redirect(url_for(
            "inventory_check_session", session_id=session_id
        ))

    @app.get("/inventory-check/compare")
    @require_roles(ROLE_ADMIN)
    def inventory_check_compare():
        """In stock in the system, but scanned in neither location. Live, not stored."""
        store_id = _session_id(request.args.get("store"))
        room_id = _session_id(request.args.get("room"))
        store = inventory_check_sessions.find_one(
            {"_id": store_id, "check_type": CHECK_STORE, "status": "completed"}
        ) if store_id else None
        room = inventory_check_sessions.find_one(
            {"_id": room_id, "check_type": CHECK_ROOM, "status": "completed"}
        ) if room_id else None
        if not store or not room:
            return "请选择一次已完成的店内陈列盘查和一次已完成的老板间盘查。", 400

        scanned = set(store.get("scanned_skus") or []) | set(room.get("scanned_skus") or [])
        both = set(store.get("scanned_skus") or []) & set(room.get("scanned_skus") or [])
        first_start = min(
            filter(None, (_aware(store.get("started_at")), _aware(room.get("started_at"))))
        )

        lost = []
        for doc in items.find(
            {"status": {"$in": list(IN_STOCK_STATUSES)}},
            {"_id": 0, "sku": 1, "name": 1, "brand": 1, "serial_code": 1,
             "status": 1, "received_at": 1, "created_at": 1},
        ):
            if doc.get("sku") in scanned:
                continue
            arrived = _aware(doc.get("received_at") or doc.get("created_at"))
            row = _item_summary(doc)
            row["arrived_during_checks"] = bool(arrived and arrived > first_start)
            lost.append(row)
        lost.sort(key=lambda row: (row["arrived_during_checks"], row["status"], row["sku"]))

        return render_template(
            "inventory_check_compare.html",
            store=store,
            room=room,
            lost=lost,
            scanned_in_both=sorted(both),
        )
