"""ONE-TIME TOOL: zero Shopify inventory for items already SOLD in this system.

Before automatic zero-on-sale existed, some items were marked SOLD here but
were left in stock on Shopify. This job finds them and sets their Shopify
"available" inventory to 0. Remove this module, its route and the dashboard
block once the backlog has been cleaned up.

It only reads the items collection (never writes to it). Job progress and the
result list are kept in one document in the existing background_jobs
collection so every web worker can show the same results page.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .shopify_inventory import zero_shopify_inventory_for_sku
from .shopify_maintenance import (
    SHOP,
    SLEEP_SECONDS,
    TOKEN_PROVIDER,
    build_product_url,
    shopify_gql,
)


logger = logging.getLogger(__name__)

JOB_ID = "shopify_zero_sold_inventory_once"
STALE_AFTER = timedelta(minutes=15)
PAGE_SIZE = 100

QUERY_ALL_VARIANTS = """
query AllVariants($cursor: String) {
  productVariants(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      sku
      inventoryQuantity
      inventoryItem { tracked }
      product { id title handle status }
    }
  }
}
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def admin_product_url(product_gid: str) -> str:
    numeric_id = (product_gid or "").rsplit("/", 1)[-1]
    store = SHOP.split(".", 1)[0]
    return f"https://admin.shopify.com/store/{store}/products/{numeric_id}" if numeric_id else ""


def _sold_items(items) -> Dict[str, Dict[str, Any]]:
    sold = {}
    for doc in items.find(
        {"status": "SOLD"}, {"sku": 1, "name": 1, "sold_record": 1}
    ):
        sku = (doc.get("sku") or "").strip()
        if sku:
            sold[sku] = {
                "name": doc.get("name") or "",
                "has_sale_record": bool(doc.get("sold_record")),
            }
    return sold


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


def find_sold_but_in_stock(items, *, gql: Optional[Callable] = None, provider=None):
    """Return Shopify variants that are SOLD here but still sellable online."""
    gql = gql or shopify_gql
    provider = provider or TOKEN_PROVIDER
    sold = _sold_items(items)
    rows = []
    for variant in _shopify_variants(gql, provider):
        sku = (variant.get("sku") or "").strip()
        if sku not in sold:
            continue
        tracked = (variant.get("inventoryItem") or {}).get("tracked")
        quantity = variant.get("inventoryQuantity")
        if tracked is not False and not (isinstance(quantity, int) and quantity > 0):
            continue
        product = variant.get("product") or {}
        rows.append({
            "sku": sku,
            "name": sold[sku]["name"],
            "has_sale_record": sold[sku]["has_sale_record"],
            "shopify_title": product.get("title") or "",
            "product_status": product.get("status") or "",
            "inventory_before": quantity,
            "tracked": tracked is not False,
            "storefront_url": build_product_url(product.get("handle")) or "",
            "admin_url": admin_product_url(product.get("id") or ""),
            "result": "pending",
            "message": "",
        })
    rows.sort(key=lambda row: row["sku"])
    return rows, len(sold)


def acquire_job(job_coll, *, dry_run: bool, started_by: str) -> bool:
    now = _utc_now()
    try:
        doc = job_coll.find_one_and_update(
            {"_id": JOB_ID, "$or": [
                {"status": {"$ne": "running"}},
                {"heartbeat_at": {"$lt": now - STALE_AFTER}},
            ]},
            {"$set": {
                "status": "running", "dry_run": dry_run,
                "started_by": started_by, "started_at": now,
                "heartbeat_at": now, "finished_at": None,
                "results": [], "sold_count": 0, "candidate_count": 0,
                "processed": 0, "error": "",
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return False
    return bool(doc)


def run_job(
    items,
    job_coll,
    *,
    dry_run: bool,
    gql: Optional[Callable] = None,
    zero: Optional[Callable[[str], Dict[str, Any]]] = None,
    provider=None,
    sleep_seconds: float = SLEEP_SECONDS,
) -> None:
    """Run after acquire_job(). Never raises; failures are stored on the job."""
    gql = gql or shopify_gql
    zero = zero or zero_shopify_inventory_for_sku
    provider = provider or TOKEN_PROVIDER
    rows: List[Dict[str, Any]] = []
    try:
        config_error = provider.configuration_error()
        if config_error:
            raise RuntimeError(config_error)
        rows, sold_count = find_sold_but_in_stock(items, gql=gql, provider=provider)
        job_coll.update_one({"_id": JOB_ID}, {"$set": {
            "sold_count": sold_count,
            "candidate_count": len(rows),
            "results": rows,
            "heartbeat_at": _utc_now(),
        }})

        for index, row in enumerate(rows, start=1):
            if not row["tracked"]:
                row["result"] = "not_tracked"
                row["message"] = "Shopify 未开启库存追踪，需手动下架"
            elif dry_run:
                row["result"] = "would_zero"
            else:
                outcome = zero(row["sku"])
                row["result"] = outcome.get("status") or "error"
                row["message"] = outcome.get("message") or ""
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            if index % 10 == 0 or index == len(rows):
                job_coll.update_one({"_id": JOB_ID}, {"$set": {
                    "results": rows, "processed": index,
                    "heartbeat_at": _utc_now(),
                }})

        job_coll.update_one({"_id": JOB_ID}, {"$set": {
            "status": "done", "results": rows, "processed": len(rows),
            "finished_at": _utc_now(),
        }})
    except Exception as exc:  # noqa: BLE001 - report everything on the page
        logger.exception("One-time Shopify zero-sold job failed.")
        job_coll.update_one({"_id": JOB_ID}, {"$set": {
            "status": "failed", "error": str(exc)[:500], "results": rows,
            "finished_at": _utc_now(),
        }})


def start_job_thread(items, job_coll, *, dry_run: bool, started_by: str) -> bool:
    if not acquire_job(job_coll, dry_run=dry_run, started_by=started_by):
        return False
    threading.Thread(
        target=run_job,
        args=(items, job_coll),
        kwargs={"dry_run": dry_run},
        name="ShopifyZeroSoldOnce",
        daemon=True,
    ).start()
    return True
