from .dashboard import register as register_dashboard
from .items import register as register_items
from .analytics import register as register_analytics
from .sales import register as register_sales
from .auth import register as register_auth
from .users import register as register_users
from .audit import register as register_audit
from .management import register as register_management
from .notes import register as register_notes


def register_all(
    app,
    items,
    users,
    audit_logs,
    notes,
    management_items=None,
    maintenance_items=None,
    maintenance_items_view=None,
    maintenance_logs=None,
    background_jobs=None,
):
    register_auth(app, users)
    register_users(app, users)
    register_audit(app, audit_logs)
    register_management(
        app,
        management_items if management_items is not None else items,
        audit_logs=audit_logs,
    )
    register_dashboard(
        app,
        items,
        maintenance_items=maintenance_items,
        maintenance_items_view=maintenance_items_view,
        maintenance_logs=maintenance_logs,
        background_jobs=background_jobs,
    )
    register_items(app, items, audit_logs=audit_logs)
    register_sales(app, items, audit_logs=audit_logs)
    register_analytics(app, items)
    register_notes(app, notes)


