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
from .url_redirects import register as register_url_redirects
from .media import register as register_media
from .claude_analytics import register as register_claude_analytics


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
        inventory_check_sessions=inventory_check_sessions,
    )
    register_dashboard(
        app,
        items,
        maintenance_items=maintenance_items,
        maintenance_items_view=maintenance_items_view,
        maintenance_logs=maintenance_logs,
        background_jobs=background_jobs,
    )
    register_items(
        app, items, audit_logs=audit_logs,
        inventory_check_sessions=inventory_check_sessions,
    )
    register_sales(app, items, audit_logs=audit_logs)
    register_analytics(app, items)
    # Claude新增分析: read-only, always the real items collection.
    register_claude_analytics(
        app, management_items if management_items is not None else items
    )
    register_notes(app, notes)
    register_qr_check(app, items)
    register_shopify_files(app)
    if inventory_check_sessions is not None:
        register_inventory_check(app, items, inventory_check_sessions)
    # Shopify库存更新: always the real items collection, never items_view.
    register_shopify_stock_check(
        app,
        management_items if management_items is not None else items,
        audit_logs=audit_logs,
    )
    # URL重定向管理: reads the real items' SKUs, changes only Shopify redirects.
    register_url_redirects(app, management_items if management_items is not None else items)
    # 图片与视频 (Alibaba Cloud OSS): all roles; Management only its own items.
    register_media(app, items, management_items=management_items)


