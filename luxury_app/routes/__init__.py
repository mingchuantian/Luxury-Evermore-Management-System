from .dashboard import register as register_dashboard
from .items import register as register_items
from .analytics import register as register_analytics
from .sales import register as register_sales
from .auth import register as register_auth
from .users import register as register_users
from .audit import register as register_audit
from .management import register as register_management
from .notes import register as register_notes


def register_all(app, items, users, audit_logs, notes):
    register_auth(app, users)
    register_users(app, users)
    register_audit(app, audit_logs)
    register_management(app, items, audit_logs=audit_logs)
    register_dashboard(app, items)
    register_items(app, items, audit_logs=audit_logs)
    register_sales(app, items, audit_logs=audit_logs)
    register_analytics(app, items)
    register_notes(app, notes)


