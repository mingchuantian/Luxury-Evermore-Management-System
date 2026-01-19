import secrets

from flask import abort, request, session


def init_csrf(app):
    """轻量 CSRF：session token + (header or form field)."""

    def _get_csrf_token() -> str:
        tok = session.get("_csrf_token")
        if not tok:
            tok = secrets.token_urlsafe(32)
            session["_csrf_token"] = tok
        return tok

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": _get_csrf_token()}

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            token = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
            if not token or token != session.get("_csrf_token"):
                abort(400, description="CSRF token missing/invalid")


