import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

from db import get_db

from .auth import require_login
from .collection_scope import RoleScopedItems
from .indexes import ensure_indexes, ensure_item_indexes
from .items_view_maintenance import start_items_view_maintenance_scheduler
from .security import init_csrf
from .routes import register_all
from .shopify_maintenance import start_shopify_maintenance_scheduler


def create_app():
    # Ensure templates are loaded from project-root /templates (not luxury_app/templates)
    project_root = Path(__file__).resolve().parents[1]
    template_dir = project_root / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    secret_key = (os.getenv("FLASK_SECRET") or "").strip()
    if len(secret_key) < 32:
        raise RuntimeError(
            "FLASK_SECRET must be set to a random value of at least 32 characters."
        )
    app.secret_key = secret_key
    app.url_map.strict_slashes = False

    # 20 minutes idle logout
    app.permanent_session_lifetime = timedelta(minutes=20)

    init_csrf(app)

    @app.template_filter("dt8")
    def dt8(value):
        """
        Display datetime as UTC+8 (Shanghai). Supports:
        - aware/naive datetime (assumes naive is UTC)
        - ISO string
        - other types (returned as-is)
        """
        if value is None or value == "":
            return ""
        dt = None
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
        else:
            return value

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt8_tz = timezone(timedelta(hours=8))
        return dt.astimezone(dt8_tz).strftime("%Y-%m-%d %H:%M:%S")

    db = get_db()
    items = db["items"]
    items_view = db["items_view"]
    users = db["users"]
    audit_logs = db["audit_logs"]
    notes = db["notes"]
    background_jobs = db["background_jobs"]
    items_view_maintenance_logs = db["items_view_maintenance_logs"]
    ensure_indexes(items, users, audit_logs, notes)
    ensure_item_indexes(items_view)
    try:
        items_view_maintenance_logs.create_index([("started_at", -1)])
    except Exception:
        # Logging indexes are helpful but never important enough to block startup.
        import logging
        logging.getLogger(__name__).exception(
            "Failed to ensure items_view maintenance log index."
        )

    role_scoped_items = RoleScopedItems(items, items_view)
    register_all(
        app,
        role_scoped_items,
        users,
        audit_logs,
        notes,
        management_items=items,
        maintenance_items=items,
        maintenance_items_view=items_view,
        maintenance_logs=items_view_maintenance_logs,
        background_jobs=background_jobs,
    )

    # Must login to browse & operate
    require_login(app, users, idle_minutes=20)

    # 启动 Shopify 维护任务（后台线程，不阻塞 Flask）
    try:
        start_shopify_maintenance_scheduler(
            items,
            job_locks=background_jobs,
            audit_logs=audit_logs,
        )
        start_shopify_maintenance_scheduler(
            items_view,
            job_locks=background_jobs,
            audit_logs=audit_logs,
        )
    except Exception as e:
        # 如果启动失败，记录错误但不影响 Flask 应用启动
        import logging
        logging.getLogger(__name__).error(f"Failed to start Shopify maintenance scheduler: {e}", exc_info=True)

    # Daily items_view maintenance is independently isolated from Flask and Shopify.
    try:
        start_items_view_maintenance_scheduler(
            items,
            items_view,
            items_view_maintenance_logs,
            background_jobs,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            f"Failed to start items_view maintenance scheduler: {e}",
            exc_info=True,
        )

    return app


