from datetime import datetime, timedelta, timezone

from bson import ObjectId
from flask import flash, redirect, request, session, url_for


ROLE_ADMIN = "Admin"
ROLE_MANAGEMENT = "Management"
ROLE_STAFF = "Staff"

ALL_ROLES = [ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF]


def _utc_now():
    return datetime.now(timezone.utc)


def _touch_last_seen():
    session["last_seen_at"] = _utc_now().isoformat()


def login_user(user_doc: dict):
    session.clear()
    session["user_id"] = str(user_doc["_id"])
    session["username"] = user_doc.get("username", "")
    session["role"] = user_doc.get("role", ROLE_STAFF)
    session.permanent = True
    _touch_last_seen()


def logout_user():
    session.clear()


def current_user(users_coll):
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        return users_coll.find_one({"_id": ObjectId(uid)})
    except Exception:
        return None


def require_login(app, users_coll, idle_minutes: int = 20):
    """
    Global login guard + idle timeout.
    Exempt routes: /login, /logout, /setup, /favicon.ico and static.
    """
    idle_delta = timedelta(minutes=idle_minutes)

    @app.before_request
    def _login_guard():
        path = request.path or "/"

        # Exempt
        if path.startswith("/static/") or path == "/favicon.ico":
            return None
        if path.startswith("/login") or path.startswith("/logout") or path.startswith("/setup"):
            return None
        # Require session
        if not session.get("user_id"):
            return redirect(url_for("login", next=path))

        # Idle timeout
        last_seen_raw = session.get("last_seen_at")
        if last_seen_raw:
            try:
                last_seen = datetime.fromisoformat(last_seen_raw)
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
            except Exception:
                last_seen = None
        else:
            last_seen = None

        if last_seen and (_utc_now() - last_seen) > idle_delta:
            logout_user()
            flash("已自动登出：超过 20 分钟未操作", "error")
            return redirect(url_for("login", next=path))

        # Ensure user still exists
        user_doc = current_user(users_coll)
        if not user_doc:
            logout_user()
            flash("账号已失效，请重新登录", "error")
            return redirect(url_for("login", next=path))

        # Authorization is always hydrated from MongoDB. A signed session proves
        # identity only; its cached role is never trusted as the source of truth.
        role = user_doc.get("role")
        if role not in ALL_ROLES:
            logout_user()
            return "forbidden", 403

        session["username"] = user_doc.get("username", "")
        session["role"] = role

        # Low-role restrictions + routing
        if role in (ROLE_MANAGEMENT, ROLE_STAFF):
            # hard-disable admin-only pages
            if path.startswith("/analytics") or path.startswith("/audit") or path.startswith("/users"):
                return "forbidden", 403

            # default landing + keep low-role on outsider UI
            if path == "/" or path.startswith("/items") or path.startswith("/sales"):
                # preserve a bit of intent for /items/<key>
                if path.startswith("/items/") and len(path.split("/")) >= 3:
                    key = path.split("/")[2]
                    return redirect(url_for("outsider_item_detail", item_key=key))
                if path.startswith("/items"):
                    return redirect(url_for("outsider_list_items"))
                return redirect(url_for("outsider_index"))

        _touch_last_seen()
        return None


def require_roles(*roles):
    roles_set = set(roles)

    def deco(fn):
        def wrapper(*args, **kwargs):
            role = session.get("role")
            if role not in roles_set:
                return "forbidden", 403
            return fn(*args, **kwargs)

        wrapper.__name__ = fn.__name__
        return wrapper

    return deco


