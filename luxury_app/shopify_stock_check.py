"""Shopify库存更新: compare Shopify with this system, four kinds of mismatch.

- sold:       Active and sellable on Shopify, but SOLD in this system.
- missing:    Active and sellable on Shopify, but the SKU is not in this system.
- unsellable: ON_SHELF here, the SKU is on Shopify, but nothing with that SKU
              can be bought (inventory 0 or Draft/Archived).
- notfound:   ON_SHELF here, but no Shopify variant has this SKU at all.

Nothing is written to MongoDB. The list is computed from live Shopify data and
the items collection, and kept only in this web process's memory: a background
thread recomputes it every SHOPIFY_STOCK_CHECK_INTERVAL_HOURS (default 2), and
"手动刷新" recomputes it immediately. Restarting the app simply recomputes it.
"""

import logging
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .shopify_maintenance import (
    SHOP,
    TOKEN_PROVIDER,
    build_product_url,
    shopify_gql,
)


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 2.0
PAGE_DELAY_SECONDS = 0.2
REASONS = ("sold", "missing", "unsellable", "notfound")
REASON_ORDER = {reason: index for index, reason in enumerate(REASONS)}

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

# In-memory result for this process only (never persisted).
_state: Dict[str, Any] = {
    "rows": [],
    "computed_at": None,
    "last_error": "",
    "running": False,
}
_state_lock = threading.Lock()
_scheduler_thread: Optional[threading.Thread] = None


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


def _store() -> str:
    return SHOP.split(".", 1)[0]


def admin_product_url(product_gid: str) -> str:
    numeric_id = (product_gid or "").rsplit("/", 1)[-1]
    return f"https://admin.shopify.com/store/{_store()}/products/{numeric_id}" if numeric_id else ""


def admin_search_url(text: str) -> str:
    text = (text or "").strip()
    return f"https://admin.shopify.com/store/{_store()}/products?query={quote(text)}" if text else ""


# ---------------------------------------------------------------- scanning


def _system_items(items) -> Dict[str, Dict[str, Any]]:
    by_sku = {}
    for doc in items.find({}, {
        "sku": 1, "name": 1, "name_in_EN": 1, "brand": 1, "status": 1,
        "shopify_details": 1,
    }):
        sku = (doc.get("sku") or "").strip()
        if sku:
            details = doc.get("shopify_details")
            by_sku[sku] = {
                "name": f"{doc.get('brand') or ''} {doc.get('name') or ''}".strip(),
                "name_in_EN": doc.get("name_in_EN") or "",
                "status": doc.get("status") or "",
                "shopify_details": details if isinstance(details, dict) else {},
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


def _row(variant: Dict[str, Any], *, reason: str, sku: str, local, detail: str = ""):
    product = variant.get("product") or {}
    return {
        "variant_id": variant.get("id") or "",
        "sku": sku,
        "reason": reason,
        "detail": detail,
        "system_name": local["name"] if local else "",
        "shopify_title": product.get("title") or "",
        "image": ((product.get("featuredImage") or {}).get("url")) or "",
        "tracked": (variant.get("inventoryItem") or {}).get("tracked") is not False,
        "storefront_url": build_product_url(product.get("handle")) or "",
        "admin_url": admin_product_url(product.get("id") or ""),
        "admin_link_label": "打开",
    }


def _sellable(variant: Dict[str, Any]) -> bool:
    product = variant.get("product") or {}
    if (product.get("status") or "").upper() != "ACTIVE":
        return False
    if (variant.get("inventoryItem") or {}).get("tracked") is False:
        return True
    quantity = variant.get("inventoryQuantity")
    return isinstance(quantity, int) and quantity > 0


def find_mismatches(items, *, gql: Optional[Callable] = None, provider=None) -> List[Dict[str, Any]]:
    """One read-only scan of Shopify + items; returns rows of the four kinds."""
    gql = gql or shopify_gql
    provider = provider or TOKEN_PROVIDER
    system = _system_items(items)
    variants = _shopify_variants(gql, provider)
    rows = []

    by_sku: Dict[str, List[Dict[str, Any]]] = {}
    for variant in variants:
        sku = (variant.get("sku") or "").strip()
        if sku:
            by_sku.setdefault(sku, []).append(variant)
        if not _sellable(variant):
            continue
        local = system.get(sku)
        if local and local["status"] != "SOLD":
            continue
        rows.append(_row(variant, reason="sold" if local else "missing", sku=sku, local=local))

    for sku, local in system.items():
        if local["status"] != "ON_SHELF":
            continue
        candidates = by_sku.get(sku)
        if not candidates:
            # No Shopify variant has this SKU (it was changed or deleted there).
            # Use whatever link the hourly sync left on the item, if any.
            details = local["shopify_details"]
            product_id = details.get("product_id") or ""
            rows.append({
                "variant_id": "",
                "sku": sku,
                "reason": "notfound",
                "detail": "",
                "system_name": local["name"],
                "shopify_title": details.get("title") or "",
                "image": details.get("featured_image") or "",
                "tracked": True,
                "storefront_url": details.get("url") or "",
                "admin_url": (
                    admin_product_url(product_id) if product_id
                    else admin_search_url(local["name_in_EN"])
                ),
                "admin_link_label": "打开" if product_id else "在Shopify搜索",
            })
            continue
        if any(_sellable(variant) for variant in candidates):
            continue
        variant = next(
            (v for v in candidates
             if ((v.get("product") or {}).get("status") or "").upper() == "ACTIVE"),
            candidates[0],
        )
        status = ((variant.get("product") or {}).get("status") or "").upper()
        detail = "Shopify库存为0" if status == "ACTIVE" else f"Shopify状态为{status.title() or '未知'}"
        rows.append(_row(variant, reason="unsellable", sku=sku, local=local, detail=detail))

    rows.sort(key=lambda row: (REASON_ORDER.get(row["reason"], 9), row["sku"]))
    return rows


# ---------------------------------------------------------- in-memory state


def snapshot() -> Dict[str, Any]:
    with _state_lock:
        state = deepcopy(_state)
    computed_at = state["computed_at"]
    state["next_run_at"] = (
        computed_at + timedelta(hours=interval_hours()) if computed_at else None
    )
    return state


def counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    result = {reason: 0 for reason in REASONS}
    for row in rows:
        if row.get("reason") in result:
            result[row["reason"]] += 1
    return result


def refresh_now(items, *, gql: Optional[Callable] = None, provider=None) -> bool:
    """Recompute synchronously. Returns False if a refresh is already running."""
    provider = provider or TOKEN_PROVIDER
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
    rows, error = None, ""
    try:
        config_error = provider.configuration_error()
        if config_error:
            raise RuntimeError(config_error)
        rows = find_mismatches(items, gql=gql, provider=provider)
    except Exception as exc:  # noqa: BLE001 - keep the previous list on failure
        logger.exception("Shopify stock check failed.")
        error = str(exc)[:500]
    finally:
        with _state_lock:
            if rows is not None:
                _state["rows"] = rows
                _state["computed_at"] = _utc_now()
            _state["last_error"] = error
            _state["running"] = False
    return True


def refresh_in_background(items) -> bool:
    with _state_lock:
        if _state["running"]:
            return False
    threading.Thread(
        target=refresh_now, args=(items,), name="ShopifyStockCheckManual", daemon=True
    ).start()
    return True


def remove_row(variant_id: str) -> None:
    """Hide a row the Admin just fixed, until the next computation."""
    with _state_lock:
        _state["rows"] = [r for r in _state["rows"] if r.get("variant_id") != variant_id]


def remove_notfound_row(sku: str) -> None:
    """Hide a “Shopify 上找不到此SKU” row once the item is back to RECEIVED."""
    with _state_lock:
        _state["rows"] = [
            r for r in _state["rows"]
            if not (r.get("reason") == "notfound" and r.get("sku") == sku)
        ]


def start_scheduler(items):
    """Compute at startup, then every interval, in this process's memory."""
    global _scheduler_thread
    if not _enabled():
        logger.info("Shopify stock check scheduler disabled by configuration.")
        return None
    if TOKEN_PROVIDER.configuration_error():
        logger.warning("Shopify stock check scheduler not started: Shopify not configured.")
        return None
    with _state_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread

    def loop():
        while True:
            try:
                state = snapshot()
                due = state["computed_at"] is None or _utc_now() >= state["next_run_at"]
                if due:
                    refresh_now(items)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected Shopify stock check scheduler error.")
            time.sleep(60)

    thread = threading.Thread(target=loop, name="ShopifyStockCheck", daemon=True)
    with _state_lock:
        _scheduler_thread = thread
    thread.start()
    logger.info("Shopify stock check scheduler started (every %s hours).", interval_hours())
    return thread


def _reset_for_tests(rows=None, computed_at=None, running=False, last_error=""):
    with _state_lock:
        _state.update(
            rows=deepcopy(rows or []), computed_at=computed_at,
            running=running, last_error=last_error,
        )
