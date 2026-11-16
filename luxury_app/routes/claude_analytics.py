"""Claude新增分析 page (Admin only, read-only)."""

from flask import render_template, request

from ..auth import ROLE_ADMIN, require_roles
from ..claude_analytics import Options, build_report

# Only the fields the analyses read. One find(); no writes anywhere.
PROJECTION = {
    "sku": 1, "name": 1, "brand": 1, "status": 1, "ownership": 1,
    "source_type": 1, "is_consignment": 1, "seller_name": 1,
    "serial_code": 1, "shopify_sku_exist": 1,
    "cost": 1, "cost_currency": 1, "currency": 1,
    "listing_price": 1, "listing_currency": 1,
    "profit": 1, "profit_currency": 1,
    "purchase_at": 1, "created_at": 1, "received_at": 1,
    "sold_record": 1,
    "audit.action": 1, "audit.at": 1, "audit.to": 1,
}


def _float_arg(name, default):
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def register(app, items):
    @app.get("/claude-analytics")
    @require_roles(ROLE_ADMIN)
    def claude_analytics():
        opts = Options(
            ownership=request.args.get("ownership", "admin"),
            months=_int_arg("months", 12),
            ccy=(request.args.get("ccy") or "SGD").upper(),
            sgd_rmb=_float_arg("sgd_rmb", 5.3),
            usd_sgd=_float_arg("usd_sgd", 1.3),
            profit_basis=request.args.get("profit", "recorded"),
        )
        report = build_report(items.find({}, PROJECTION), opts)
        return render_template("claude_analytics.html", r=report, opts=opts)
