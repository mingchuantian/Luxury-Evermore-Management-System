import re
from math import ceil

from bson import ObjectId
from flask import jsonify, redirect, render_template, request, session, url_for

from models import now

from ..auth import ROLE_ADMIN, require_roles


SKU_RE = re.compile(r"^[A-Z0-9_-]{2,64}$")
PAGE_SIZE = 20


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


def register(app, items, inventory_check_sessions):
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
        return render_template(
            "inventory_check_history.html",
            inventory_sessions=list(cursor),
            page=page,
            pages=max(1, ceil(total / PAGE_SIZE)),
        )

    @app.post("/inventory-check/sessions")
    @require_roles(ROLE_ADMIN)
    def inventory_check_start():
        started_at = now()
        result = inventory_check_sessions.insert_one({
            "status": "active",
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
        return render_template(
            "inventory_check_session.html",
            check_session=check_session,
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
        completed_at = now()
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
