"""ONE-TIME TOOL routes. Remove together with luxury_app/shopify_zero_sold_once.py."""

from flask import flash, redirect, render_template, request, session, url_for

from ..auth import ROLE_ADMIN, require_roles
from ..shopify_zero_sold_once import JOB_ID, start_job_thread


RESULT_LABELS = {
    "would_zero": "将改为 0（预览）",
    "zeroed": "已改为 0",
    "already_zero": "已经是 0",
    "not_found": "Shopify 中已找不到",
    "not_tracked": "未开启库存追踪，需手动下架",
    "not_configured": "Shopify 未配置",
    "error": "失败",
    "pending": "等待中",
}


def register(app, items, background_jobs):
    @app.post("/admin/shopify/zero-sold-inventory")
    @require_roles(ROLE_ADMIN)
    def shopify_zero_sold_start():
        dry_run = request.form.get("dry_run") == "1"
        started = start_job_thread(
            items,
            background_jobs,
            dry_run=dry_run,
            started_by=session.get("username") or "",
        )
        if not started:
            flash("同步任务已在运行中，请稍候。", "error")
        return redirect(url_for("shopify_zero_sold_status"))

    @app.get("/admin/shopify/zero-sold-inventory")
    @require_roles(ROLE_ADMIN)
    def shopify_zero_sold_status():
        job = background_jobs.find_one({"_id": JOB_ID}) or {}
        return render_template(
            "admin/shopify_zero_sold.html",
            job=job,
            rows=job.get("results") or [],
            RESULT_LABELS=RESULT_LABELS,
        )
