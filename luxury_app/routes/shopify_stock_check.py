"""Shopify库存更新 page (Admin only). Reads the in-memory result; no DB writes."""

from flask import flash, redirect, render_template, request, session, url_for

from ..auth import ROLE_ADMIN, require_roles
from ..shopify_inventory import (
    STATUS_ALREADY_ZERO,
    STATUS_NOT_FOUND,
    STATUS_NOT_TRACKED,
    STATUS_ZEROED,
    zero_shopify_inventory_for_variant,
)
from ..shopify_stock_check import (
    REASONS,
    counts,
    interval_hours,
    refresh_in_background,
    remove_row,
    snapshot,
)


def register(app, items):
    @app.context_processor
    def inject_shopify_stock_alert():
        # Feeds the red count in the top menu and the dashboard alert box.
        if session.get("role") != ROLE_ADMIN:
            return {}
        rows = snapshot()["rows"]
        return {
            "shopify_stock_alert_count": len(rows),
            "shopify_stock_counts": counts(rows),
        }

    @app.get("/shopify-stock")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_page():
        state = snapshot()
        all_rows = state["rows"]
        reason = request.args.get("reason") or ""
        if reason not in REASONS:
            reason = ""
        return render_template(
            "shopify_stock_check.html",
            state=state,
            all_rows=all_rows,
            rows=[r for r in all_rows if not reason or r.get("reason") == reason],
            counts=counts(all_rows),
            reason=reason,
            interval_label=f"{interval_hours():g}",
        )

    @app.post("/shopify-stock/refresh")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_refresh():
        if refresh_in_background(items):
            flash("正在重新计算，稍后页面会自动更新。", "ok")
        else:
            flash("正在计算中，请稍候。", "error")
        return redirect(url_for("shopify_stock_page"))

    @app.post("/shopify-stock/zero")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_zero():
        variant_id = (request.form.get("variant_id") or "").strip()
        rows = snapshot()["rows"]
        row = next(
            (r for r in rows if variant_id and r.get("variant_id") == variant_id), None
        )
        if row and row.get("reason") != "sold":
            # Only “系统内已售出” rows may be zeroed. The other kinds need the SKU,
            # stock or status fixed in Shopify or in this system instead.
            flash("只有“系统内已售出，但 Shopify 上还在卖”的商品可以设置库存为0。", "error")
            return redirect(url_for("shopify_stock_page"))
        if not row:
            flash("该商品已不在列表中，请刷新后再试。", "error")
            return redirect(url_for("shopify_stock_page"))

        result = zero_shopify_inventory_for_variant(variant_id)
        label = row.get("shopify_title") or row.get("sku") or variant_id
        status = result.get("status")
        if status in (STATUS_ZEROED, STATUS_ALREADY_ZERO, STATUS_NOT_FOUND):
            remove_row(variant_id)
            flash(f"已将「{label}」的 Shopify 库存设置为 0。", "ok")
        elif status == STATUS_NOT_TRACKED:
            flash(
                f"「{label}」在 Shopify 未开启库存追踪，无法设为 0，请通过后台链接手动下架（改为 Draft）。",
                "error",
            )
        else:
            flash(f"「{label}」设置失败：{result.get('message') or '未知错误'}", "error")
        return redirect(url_for("shopify_stock_page"))
