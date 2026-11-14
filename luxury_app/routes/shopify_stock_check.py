"""Shopify库存更新 page (Admin only)."""

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
    current_state,
    interval_hours,
    remove_row,
    trigger_manual_refresh,
)


def register(app, items, background_jobs):
    @app.context_processor
    def inject_shopify_stock_alert():
        # Feeds the red count in the top menu and the dashboard alert box.
        if session.get("role") != ROLE_ADMIN:
            return {}
        return {
            "shopify_stock_alert_count": len(
                current_state(background_jobs).get("results") or []
            )
        }

    @app.get("/shopify-stock")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_page():
        state = current_state(background_jobs)
        return render_template(
            "shopify_stock_check.html",
            state=state,
            rows=state.get("results") or [],
            interval_label=f"{interval_hours():g}",
        )

    @app.post("/shopify-stock/refresh")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_refresh():
        if trigger_manual_refresh(items, background_jobs):
            flash("正在刷新，稍后页面会自动更新。", "ok")
        else:
            flash("刷新已在进行中，请稍候。", "error")
        return redirect(url_for("shopify_stock_page"))

    @app.post("/shopify-stock/zero")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_zero():
        variant_id = (request.form.get("variant_id") or "").strip()
        rows = current_state(background_jobs).get("results") or []
        row = next((r for r in rows if r.get("variant_id") == variant_id), None)
        if not row:
            flash("该商品已不在列表中，请刷新后再试。", "error")
            return redirect(url_for("shopify_stock_page"))

        result = zero_shopify_inventory_for_variant(variant_id)
        label = row.get("shopify_title") or row.get("sku") or variant_id
        status = result.get("status")
        if status in (STATUS_ZEROED, STATUS_ALREADY_ZERO, STATUS_NOT_FOUND):
            remove_row(background_jobs, variant_id)
            flash(f"已将「{label}」的 Shopify 库存设置为 0。", "ok")
        elif status == STATUS_NOT_TRACKED:
            flash(
                f"「{label}」在 Shopify 未开启库存追踪，无法设为 0，请通过后台链接手动下架（改为 Draft）。",
                "error",
            )
        else:
            flash(f"「{label}」设置失败：{result.get('message') or '未知错误'}", "error")
        return redirect(url_for("shopify_stock_page"))
