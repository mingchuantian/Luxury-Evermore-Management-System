import re
from math import ceil

from flask import render_template, request
from pymongo import DESCENDING

from ..constants import STATUS_ZH


def register(app, audit_logs):
    @app.get("/audit")
    def audit_history():
        q = (request.args.get("q") or "").strip()
        action = (request.args.get("action") or "").strip()
        user = (request.args.get("user") or "").strip()

        try:
            page = int(request.args.get("page", "1"))
        except Exception:
            page = 1
        if page < 1:
            page = 1
        per_page = 200

        filt = {}
        if action:
            filt["action"] = action
        if user:
            filt["by.username"] = user

        if q:
            q_esc = re.escape(q[:80])
            filt["$or"] = [
                {"sku": {"$regex": q_esc, "$options": "i"}},
                {"action": {"$regex": q_esc, "$options": "i"}},
                {"target": {"$regex": q_esc, "$options": "i"}},
                {"by.username": {"$regex": q_esc, "$options": "i"}},
            ]

        total = audit_logs.count_documents(filt)
        total_pages = max(1, ceil(total / per_page)) if total else 1
        if page > total_pages:
            page = total_pages

        skip = (page - 1) * per_page
        rows = list(audit_logs.find(filt).sort("at", DESCENDING).skip(skip).limit(per_page))

        # action dropdown options (recent distinct)
        actions = audit_logs.distinct("action")[:200]
        actions = sorted([a for a in actions if a], reverse=False)

        return render_template(
            "audit.html",
            rows=rows,
            q=q,
            action=action,
            user=user,
            page=page,
            per_page=per_page,
            total=total,
            total_pages=total_pages,
            actions=actions,
            STATUS_ZH=STATUS_ZH,
        )


