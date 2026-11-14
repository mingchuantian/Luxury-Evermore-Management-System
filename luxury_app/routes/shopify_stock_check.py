"""Shopify库存更新 page (Admin only). Reads the in-memory result; no DB writes."""

from flask import flash, redirect, render_template, request, session, url_for

from models import now

from ..audit import insert_audit_logs, make_change_status, push_item_audits
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
    remove_notfound_row,
    remove_row,
    snapshot,
)


def register(app, items, audit_logs=None):
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

    @app.post("/shopify-stock/mark-received")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_mark_received():
        """“系统内上架在售，但 Shopify 上找不到此SKU” → set the item back to 已到货.

        The next scan no longer lists it (only ON_SHELF items are checked), and
        the hourly sync moves it to ON_SHELF again once the SKU is on Shopify.
        """
        sku = (request.form.get("sku") or "").strip()
        rows = snapshot()["rows"]
        if not sku or not any(
            r.get("reason") == "notfound" and r.get("sku") == sku for r in rows
        ):
            flash("该商品已不在“Shopify 上找不到此SKU”列表中，请刷新后再试。", "error")
            return redirect(url_for("shopify_stock_page"))

        item = items.find_one({"sku": sku, "status": "ON_SHELF"})
        if not item:
            remove_notfound_row(sku)
            flash(f"{sku} 已不是“上架在售”，无需修改。", "error")
            return redirect(url_for("shopify_stock_page"))

        entry = make_change_status(sku=sku, from_status="ON_SHELF", to_status="RECEIVED")
        result = items.update_one(
            {"_id": item["_id"], "status": "ON_SHELF"},
            {"$set": {"status": "RECEIVED", "updated_at": now()}},
        )
        if getattr(result, "modified_count", 0) != 1:
            flash(f"{sku} 的状态刚刚被修改过，请刷新后再试。", "error")
            return redirect(url_for("shopify_stock_page"))
        push_item_audits(items, item["_id"], [entry])
        insert_audit_logs(audit_logs, [entry])
        remove_notfound_row(sku)
        name = f"{item.get('brand') or ''} {item.get('name') or ''}".strip() or sku
        flash(f"已将「{name}」改回为“已到货”。", "ok")
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
