"""Shopify库存更新 page (Admin only). Reads the in-memory result.

Row actions answer with JSON when the page calls them with fetch(), so the page
updates the row in place instead of reloading (and jumping to the top). Without
JavaScript they fall back to flash + redirect.
"""

import re

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from models import now

from ..audit import insert_audit_logs, make_change_status, push_item_audits
from ..auth import ROLE_ADMIN, require_roles
from ..constants import STATUS_ZH
from ..shopify_inventory import (
    STATUS_ALREADY_ZERO,
    STATUS_NOT_FOUND,
    STATUS_NOT_TRACKED,
    STATUS_ZEROED,
    set_shopify_variant_sku,
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

SKU_RE = re.compile(r"^[A-Z0-9_-]{2,64}$")
SKU_EDITABLE_REASONS = ("sold", "missing")


def _wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


def _reply(ok, message, **extra):
    """JSON for fetch() callers; flash + redirect otherwise."""
    if _wants_json():
        return jsonify(dict({"ok": ok, "message": message}, **extra)), (200 if ok else 400)
    flash(message, "ok" if ok else "error")
    return redirect(url_for("shopify_stock_page"))


def _item_label(item, fallback):
    return f"{item.get('brand') or ''} {item.get('name') or ''}".strip() or fallback


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

    @app.get("/shopify-stock/lookup-sku")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_lookup_sku():
        """Read-only: what this SKU is in the system (for the change-SKU dialog)."""
        sku = (request.args.get("sku") or "").strip().upper()
        if not SKU_RE.fullmatch(sku):
            return jsonify({"found": False, "sku": sku})
        item = items.find_one(
            {"sku": sku}, {"_id": 0, "sku": 1, "brand": 1, "name": 1, "status": 1}
        )
        if not item:
            return jsonify({"found": False, "sku": sku})
        status = item.get("status") or ""
        return jsonify({
            "found": True,
            "sku": sku,
            "name": _item_label(item, sku),
            "status": status,
            "status_zh": STATUS_ZH.get(status, status),
            "usable": status != "SOLD",
        })

    @app.post("/shopify-stock/update-sku")
    @require_roles(ROLE_ADMIN)
    def shopify_stock_update_sku():
        """Write a new SKU to a Shopify variant (“已售出” / “无此SKU” rows only).

        Only Shopify changes. The new SKU must belong to an unsold item in this
        system; the hourly sync then links that item to the listing.
        """
        variant_id = (request.form.get("variant_id") or "").strip()
        new_sku = (request.form.get("sku") or "").strip().upper()
        row = next(
            (r for r in snapshot()["rows"] if variant_id and r.get("variant_id") == variant_id),
            None,
        )
        if not row:
            return _reply(False, "该商品已不在列表中，请刷新后再试。")
        if row.get("reason") not in SKU_EDITABLE_REASONS:
            return _reply(False, "只有“系统内已售出”和“系统中无此SKU”的商品可以在这里改 SKU。")
        if not SKU_RE.fullmatch(new_sku):
            return _reply(False, "SKU 格式不对：只能包含字母、数字、- 和 _。")
        if new_sku == (row.get("sku") or "").upper():
            return _reply(False, "新 SKU 和现在的一样。")
        item = items.find_one({"sku": new_sku}, {"brand": 1, "name": 1, "status": 1})
        if not item:
            return _reply(False, f"系统里找不到 {new_sku}，请先在系统中录入这件商品。")
        if item.get("status") == "SOLD":
            return _reply(False, f"{new_sku} 在系统里已经售出，不能用来挂网。")

        result = set_shopify_variant_sku(row.get("product_id") or "", variant_id, new_sku)
        if not result.get("ok"):
            return _reply(False, f"修改失败：{result.get('message') or '未知错误'}")
        remove_row(variant_id)
        name = _item_label(item, new_sku)
        return _reply(
            True,
            f"Shopify SKU 已改为 {new_sku}（{name}）。系统会在下一次每小时同步时连接这件商品。",
            sku=new_sku,
        )

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
            return _reply(False, "该商品已不在“Shopify 上找不到此SKU”列表中，请刷新后再试。")

        item = items.find_one({"sku": sku, "status": "ON_SHELF"})
        if not item:
            remove_notfound_row(sku)
            return _reply(False, f"{sku} 已不是“上架在售”，无需修改。")

        entry = make_change_status(sku=sku, from_status="ON_SHELF", to_status="RECEIVED")
        result = items.update_one(
            {"_id": item["_id"], "status": "ON_SHELF"},
            {"$set": {"status": "RECEIVED", "updated_at": now()}},
        )
        if getattr(result, "modified_count", 0) != 1:
            return _reply(False, f"{sku} 的状态刚刚被修改过，请刷新后再试。")
        push_item_audits(items, item["_id"], [entry])
        insert_audit_logs(audit_logs, [entry])
        remove_notfound_row(sku)
        return _reply(True, f"已将「{_item_label(item, sku)}」改回为“已到货”。")

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
            return _reply(False, "只有“系统内已售出，但 Shopify 上还在卖”的商品可以设置库存为0。")
        if not row:
            return _reply(False, "该商品已不在列表中，请刷新后再试。")

        result = zero_shopify_inventory_for_variant(variant_id)
        label = row.get("shopify_title") or row.get("sku") or variant_id
        status = result.get("status")
        if status in (STATUS_ZEROED, STATUS_ALREADY_ZERO, STATUS_NOT_FOUND):
            remove_row(variant_id)
            return _reply(True, f"已将「{label}」的 Shopify 库存设置为 0。")
        if status == STATUS_NOT_TRACKED:
            return _reply(
                False,
                f"「{label}」在 Shopify 未开启库存追踪，无法设为 0，请通过后台链接手动下架（改为 Draft）。",
            )
        return _reply(False, f"「{label}」设置失败：{result.get('message') or '未知错误'}")
