import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

from db import get_db

from .auth import require_login
from .indexes import ensure_indexes
from .security import init_csrf
from .routes import register_all
from .shopify_maintenance import start_shopify_maintenance_scheduler


def create_app():
    # Ensure templates are loaded from project-root /templates (not luxury_app/templates)
    project_root = Path(__file__).resolve().parents[1]
    template_dir = project_root / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")
    app.url_map.strict_slashes = False

    # 20 minutes idle logout
    app.permanent_session_lifetime = timedelta(minutes=20)

    # upload cap
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024)))  # 25MB

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
    items_outsider = db["items_outsider"]
    users = db["users"]
    audit_logs = db["audit_logs"]
    notes = db["notes"]
    ensure_indexes(items, items_outsider, users, audit_logs, notes)

    photos_root = Path(os.getenv("PHOTOS_ROOT", "./photos")).resolve()
    register_all(app, items, items_outsider, photos_root, users, audit_logs, notes)

    # Must login to browse & operate
    require_login(app, users, idle_minutes=20)

    # 启动 Shopify 维护任务（后台线程，不阻塞 Flask）
    try:
        start_shopify_maintenance_scheduler(items, interval_hours=4.0)
    except Exception as e:
        # 如果启动失败，记录错误但不影响 Flask 应用启动
        import logging
        logging.getLogger(__name__).error(f"Failed to start Shopify maintenance scheduler: {e}", exc_info=True)

    return app


