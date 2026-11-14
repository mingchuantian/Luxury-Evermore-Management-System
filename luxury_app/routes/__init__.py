from .dashboard import register as register_dashboard
from .items import register as register_items
from .analytics import register as register_analytics
from .sales import register as register_sales
from .auth import register as register_auth
from .users import register as register_users
from .audit import register as register_audit
from .management import register as register_management
from .notes import register as register_notes
from .qr_check import register as register_qr_check
from .shopify_files import register as register_shopify_files
from .inventory_check import register as register_inventory_check
from .shopify_stock_check import register as register_shopify_stock_check


def register_all(
    app,
    items,
    users,
    audit_logs,
    notes,
    management_items=None,
    management_payments=None,
    maintenance_items=None,
    maintenance_items_view=None,
    maintenance_logs=None,
    background_jobs=None,
    inventory_check_sessions=None,
):
    register_auth(app, users)
    register_users(app, users)
    register_audit(app, audit_logs)
    register_management(
        app,
        management_items if management_items is not None else items,
        audit_logs=audit_logs,
        users=users,
        management_payments=management_payments,
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
    register_qr_check(app, items)
    register_shopify_files(app)
    if inventory_check_sessions is not None:
        register_inventory_check(app, items, inventory_check_sessions)
    if background_jobs is not None:
        # Shopify库存更新: always the real items collection, never items_view.
        register_shopify_stock_check(
            app,
            management_items if management_items is not None else items,
            background_jobs,
        )


