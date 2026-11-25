"""URL重定向管理 page (Admin only). Reads and changes Shopify URL redirects;
nothing is stored in MongoDB (see luxury_app/url_redirects.py)."""

from flask import flash, jsonify, redirect, render_template, request, url_for

from ..auth import ROLE_ADMIN, require_roles
from ..url_redirects import (
    ACTIONS,
    counts,
    fix_one,
    interval_hours,
    run_in_background,
    snapshot,
)


def register(app, items):
    @app.get("/url-redirects")
    @require_roles(ROLE_ADMIN)
    def url_redirects_page():
        state = snapshot()
        all_rows = state["rows"]
        action = request.args.get("action") or ""
        if action not in ACTIONS:
            action = ""
        return render_template(
            "url_redirects.html",
            state=state,
            all_rows=all_rows,
            rows=[r for r in all_rows if not action or r["action"] == action],
            counts=counts(all_rows),
            action=action,
            interval_label=f"{interval_hours():g}",
        )

    def _start(fix):
        if run_in_background(items, fix=fix):
            flash("正在检查并修复，稍后页面会自动更新。" if fix else "正在检查，稍后页面会自动更新。", "ok")
        else:
            flash("正在运行中，请稍候。", "error")
        return redirect(url_for("url_redirects_page"))

    @app.post("/url-redirects/check")
    @require_roles(ROLE_ADMIN)
    def url_redirects_check():
        return _start(False)

    @app.post("/url-redirects/fix-all")
    @require_roles(ROLE_ADMIN)
    def url_redirects_fix_all():
        return _start(True)

    @app.post("/url-redirects/fix-one")
    @require_roles(ROLE_ADMIN)
    def url_redirects_fix_one():
        sku = (request.form.get("sku") or "").strip().upper()
        result = fix_one(sku)
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(result), (200 if result["ok"] else 400)
        flash(f"{sku} 已修复。" if result["ok"] else f"{sku} 修复失败：{result['message']}",
              "ok" if result["ok"] else "error")
        return redirect(url_for("url_redirects_page"))
