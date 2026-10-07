import re

from flask import jsonify, make_response, render_template, request


SKU_RE = re.compile(r"^[A-Z0-9_-]{2,64}$")


def register(app, items):
    @app.get("/qr-check")
    def qr_check_page():
        return render_template("qr_check.html")

    @app.get("/qr-check/lookup")
    def qr_check_lookup():
        sku = (request.args.get("sku") or "").strip().upper()
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
                "shopify_sku_exist": 1,
                "shopify_details.title": 1,
            },
        )
        if not item:
            payload = {
                "ok": True,
                "found": False,
                "sku": sku,
                "linked": False,
                "message": "SKU was not found in this inventory database.",
            }
        else:
            linked = item.get("shopify_sku_exist") is True
            details = item.get("shopify_details") or {}
            if not isinstance(details, dict):
                details = {}
            payload = {
                "ok": True,
                "found": True,
                "sku": item.get("sku") or sku,
                "item_name": item.get("name") or "",
                "linked": linked,
                "shopify_title": (
                    (details.get("title") or "") if linked else ""
                ),
                "message": (
                    "Connected to Shopify."
                    if linked
                    else "Not connected to Shopify."
                ),
            }

        response = make_response(jsonify(payload))
        response.headers["Cache-Control"] = "no-store"
        return response
