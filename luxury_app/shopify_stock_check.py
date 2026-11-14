"""Shopify 库存更新: find ACTIVE Shopify products that are still sellable but
are already SOLD in this system, or whose SKU does not exist in this system.

The check is read-only for both MongoDB items and Shopify. It runs every
SHOPIFY_STOCK_CHECK_INTERVAL_HOURS (default 2) in a background thread, and an
Admin can refresh it manually. A MongoDB lease in background_jobs makes sure
only one web worker runs it at a time; the latest result list is stored on
the same background_jobs document so every worker shows the same page.

The only Shopify write is the per-row "已无同款，商品设置库存为0" action,
which sets that variant's available inventory to 0.
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .shopify_maintenance import (
    SHOP,
    TOKEN_PROVIDER,
    build_product_url,
    shopify_gql,
)


logger = logging.getLogger(__name__)

JOB_ID = "shopify_stock_check"
LEASE = timedelta(minutes=30)
DEFAULT_INTERVAL_HOURS = 2.0
PAGE_DELAY_SECONDS = 0.2

QUERY_ALL_VARIANTS = """
query StockCheckVariants($cursor: String) {
  productVariants(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      sku
      inventoryQuantity
      inventoryItem { tracked }
      product {
        id
        title
        handle
        status
        featuredImage { url }
      }
    }
  }
}
"""

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_guard = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def interval_hours() -> float:
    try:
        value = float(os.getenv("SHOPIFY_STOCK_CHECK_INTERVAL_HOURS") or DEFAULT_INTERVAL_HOURS)
    except ValueError:
        return DEFAULT_INTERVAL_HOURS
    return max(0.25, value)


def _enabled() -> bool:
    return (os.getenv("SHOPIFY_STOCK_CHECK_ENABLED") or "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def admin_product_url(product_gid: str) -> str:
    numeric_id = (product_gid or "").rsplit("/", 1)[-1]
    store = SHOP.split(".", 1)[0]
    return f"https://admin.shopify.com/store/{store}/products/{numeric_id}" if numeric_id else ""


def _system_items(items) -> Dict[str, Dict[str, Any]]:
    by_sku = {}
    for doc in items.find({}, {"sku": 1, "name": 1, "brand": 1, "status": 1}):
        sku = (doc.get("sku") or "").strip()
        if sku:
            by_sku[sku] = {
                "name": f"{doc.get('brand') or ''} {doc.get('name') or ''}".strip(),
                "status": doc.get("status") or "",
            }
    return by_sku


def _shopify_variants(gql: Callable, provider) -> List[Dict[str, Any]]:
    variants, cursor = [], None
    while True:
        data = gql(QUERY_ALL_VARIANTS, {"cursor": cursor}, token_provider=provider)
        connection = data.get("productVariants") or {}
        variants.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return variants
        cursor = page.get("endCursor")
        time.sleep(PAGE_DELAY_SECONDS)


def find_mismatches(items, *, gql: Optional[Callable] = None, provider=None) -> List[Dict[str, Any]]:
    """ACTIVE + still sellable on Shopify, but SOLD here or SKU unknown here."""
    gql = gql or shopify_gql
    provider = provider or TOKEN_PROVIDER
    system = _system_items(items)
    rows = []
    for variant in _shopify_variants(gql, provider):
        product = variant.get("product") or {}
        if (product.get("status") or "").upper() != "ACTIVE":
            continue  # DRAFT / ARCHIVED already means we took it off sale.
        tracked = (variant.get("inventoryItem") or {}).get("tracked") is not False
        quantity = variant.get("inventoryQuantity")
        sellable = (not tracked) or (isinstance(quantity, int) and quantity > 0)
        if not sellable:
            continue
        sku = (variant.get("sku") or "").strip()
        local = system.get(sku)
        if local and local["status"] != "SOLD":
            continue
        rows.append({
            "variant_id": variant.get("id") or "",
            "sku": sku,
            "reason": "sold" if local else "missing",
            "system_name": local["name"] if local else "",
            "shopify_title": product.get("title") or "",
            "image": ((product.get("featuredImage") or {}).get("url")) or "",
            "tracked": tracked,
            "storefront_url": build_product_url(product.get("handle")) or "",
            "admin_url": admin_product_url(product.get("id") or ""),
        })
    rows.sort(key=lambda row: (row["reason"], row["sku"]))
    return rows


def _acquire(job_coll, *, force: bool) -> Optional[str]:
    now = _utc_now()
    owner = uuid.uuid4().hex
    conditions = [{"$or": [
        {"locked_until": {"$exists": False}},
        {"locked_until": {"$lte": now}},
    ]}]
    if not force:
        conditions.append({"$or": [
            {"next_run_at": {"$exists": False}},
            {"next_run_at": {"$lte": now}},
        ]})
    try:
        doc = job_coll.find_one_and_update(
            {"_id": JOB_ID, "$and": conditions},
            {"$set": {
                "locked_until": now + LEASE,
                "lock_owner": owner,
                "running": True,
                "last_started_at": now,
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None
    return owner if doc and doc.get("lock_owner") == owner else None


def run_check(items, job_coll, *, force: bool = False, owner: Optional[str] = None,
              gql: Optional[Callable] = None, provider=None) -> bool:
    """Run one check if due (or forced). Never raises. Returns True if it ran."""
    provider = provider or TOKEN_PROVIDER
    owner = owner or _acquire(job_coll, force=force)
    if not owner:
        return False
    finished = None
    update: Dict[str, Any] = {}
    try:
        config_error = provider.configuration_error()
        if config_error:
            raise RuntimeError(config_error)
        rows = find_mismatches(items, gql=gql, provider=provider)
        finished = _utc_now()
        # Replace the list only after a complete, successful scan.
        update = {"results": rows, "last_success_at": finished, "last_error": ""}
    except Exception as exc:  # noqa: BLE001 - keep the previous list on failure
        logger.exception("Shopify stock check failed.")
        update = {"last_error": str(exc)[:500]}
    finally:
        finished = finished or _utc_now()
        update.update({
            "running": False,
            "locked_until": finished,
            "last_finished_at": finished,
            "next_run_at": finished + timedelta(hours=interval_hours()),
        })
        job_coll.update_one(
            {"_id": JOB_ID, "lock_owner": owner},
            {"$set": update, "$unset": {"lock_owner": ""}},
        )
    return True


def trigger_manual_refresh(items, job_coll) -> bool:
    owner = _acquire(job_coll, force=True)
    if not owner:
        return False
    threading.Thread(
        target=run_check, args=(items, job_coll),
        kwargs={"owner": owner}, name="ShopifyStockCheckManual", daemon=True,
    ).start()
    return True


def remove_row(job_coll, variant_id: str) -> None:
    """Drop one row after the Admin zeroed it, until the next scan confirms."""
    job_coll.update_one({"_id": JOB_ID}, {"$pull": {"results": {"variant_id": variant_id}}})


def current_state(job_coll) -> Dict[str, Any]:
    try:
        return job_coll.find_one({"_id": JOB_ID}) or {}
    except Exception:  # noqa: BLE001 - a dashboard must still render
        logger.exception("Failed to load Shopify stock check state.")
        return {}


def start_scheduler(items, job_coll):
    """Background loop: checks every minute whether a run is due."""
    global _scheduler_thread
    if not _enabled():
        logger.info("Shopify stock check scheduler disabled by configuration.")
        return None
    if TOKEN_PROVIDER.configuration_error():
        logger.warning("Shopify stock check scheduler not started: Shopify not configured.")
        return None
    with _scheduler_guard:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread

        def loop():
            while True:
                try:
                    run_check(items, job_coll)
                except Exception:  # noqa: BLE001
                    logger.exception("Unexpected Shopify stock check scheduler error.")
                time.sleep(60)

        _scheduler_thread = threading.Thread(
            target=loop, name="ShopifyStockCheck", daemon=True
        )
        _scheduler_thread.start()
        logger.info("Shopify stock check scheduler started (every %s hours).", interval_hours())
        return _scheduler_thread
