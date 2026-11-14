"""Set a sold item's Shopify inventory to zero.

Called right after an item is marked SOLD in this system so a one-of-a-kind
piece cannot be bought again on the online store. This module only talks to
Shopify; it never writes to MongoDB.

Shopify requirements (Admin GraphQL API 2026-04 and later):
- scopes: read_inventory + write_inventory
- inventorySetQuantities needs the @idempotent directive and an explicit
  changeFromQuantity for every quantity entry.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from .shopify_maintenance import (
    TOKEN_PROVIDER,
    ShopifyTokenProvider,
    shopify_gql,
)


logger = logging.getLogger(__name__)


QUERY_INVENTORY_BY_SKU = """
query InventoryBySku($q: String!) {
  productVariants(first: 10, query: $q) {
    edges {
      node {
        id
        sku
        inventoryItem {
          id
          tracked
          inventoryLevels(first: 50) {
            edges {
              node {
                location { id }
                quantities(names: ["available"]) { name quantity }
              }
            }
          }
        }
      }
    }
  }
}
"""

MUTATION_SET_QUANTITIES = """
mutation ZeroSoldInventory(
  $input: InventorySetQuantitiesInput!,
  $idempotencyKey: String!
) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup {
      changes { name delta quantityAfterChange }
    }
    userErrors { code field message }
  }
}
"""

STATUS_ZEROED = "zeroed"
STATUS_ALREADY_ZERO = "already_zero"
STATUS_NOT_FOUND = "not_found"
STATUS_NOT_TRACKED = "not_tracked"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_ERROR = "error"

# Statuses that mean the online store can no longer sell the item.
SAFE_STATUSES = {STATUS_ZEROED, STATUS_ALREADY_ZERO, STATUS_NOT_FOUND}


def _result(status: str, message: str = "", **extra) -> Dict[str, Any]:
    return {"status": status, "ok": status in SAFE_STATUSES, "message": message, **extra}


def _find_variant(sku: str, provider: ShopifyTokenProvider) -> Optional[Dict[str, Any]]:
    escaped = sku.replace("\\", "\\\\").replace('"', '\\"')
    data = shopify_gql(
        QUERY_INVENTORY_BY_SKU,
        {"q": f'sku:"{escaped}"'},
        token_provider=provider,
    )
    edges = (data.get("productVariants") or {}).get("edges") or []
    matches = [
        edge.get("node") or {}
        for edge in edges
        if ((edge.get("node") or {}).get("sku") or "").strip() == sku
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Shopify has {len(matches)} variants with SKU {sku}")
    return matches[0] if matches else None


def _nonzero_levels(variant: Dict[str, Any]) -> List[Dict[str, Any]]:
    inventory_item = variant.get("inventoryItem") or {}
    item_id = inventory_item.get("id")
    levels = []
    for edge in ((inventory_item.get("inventoryLevels") or {}).get("edges") or []):
        node = edge.get("node") or {}
        location_id = (node.get("location") or {}).get("id")
        available = None
        for quantity in node.get("quantities") or []:
            if quantity.get("name") == "available":
                available = quantity.get("quantity")
        if item_id and location_id and isinstance(available, int) and available != 0:
            levels.append({
                "inventoryItemId": item_id,
                "locationId": location_id,
                "quantity": 0,
                # Compare-and-swap: only change it if Shopify still has this value.
                "changeFromQuantity": available,
            })
    return levels


def zero_shopify_inventory_for_sku(
    sku: str,
    token_provider: Optional[ShopifyTokenProvider] = None,
) -> Dict[str, Any]:
    """Set "available" to 0 at every Shopify location stocking this SKU.

    Never raises: every outcome is returned as a dict with ``status``,
    ``ok`` (True when the item can no longer be sold online) and ``message``.
    """
    clean_sku = (sku or "").strip()
    if not clean_sku:
        return _result(STATUS_NOT_FOUND, "Item has no SKU.")

    provider = token_provider or TOKEN_PROVIDER
    config_error = provider.configuration_error()
    if config_error:
        return _result(STATUS_NOT_CONFIGURED, config_error)

    try:
        # Two attempts: if another sale/edit changed the quantity between our
        # read and write, Shopify answers CHANGE_FROM_QUANTITY_STALE; re-read once.
        for attempt in range(2):
            variant = _find_variant(clean_sku, provider)
            if variant is None:
                return _result(STATUS_NOT_FOUND, "SKU not found in Shopify.")
            if (variant.get("inventoryItem") or {}).get("tracked") is False:
                return _result(
                    STATUS_NOT_TRACKED,
                    "Inventory tracking is off for this product in Shopify.",
                )

            levels = _nonzero_levels(variant)
            if not levels:
                return _result(STATUS_ALREADY_ZERO, "Shopify inventory is already 0.")

            data = shopify_gql(
                MUTATION_SET_QUANTITIES,
                {
                    "input": {
                        "name": "available",
                        "reason": "correction",
                        "quantities": levels,
                    },
                    # Same key is reused by shopify_gql's network retries, so a
                    # retried request cannot be applied twice.
                    "idempotencyKey": str(uuid.uuid4()),
                },
                token_provider=provider,
            )
            payload = data.get("inventorySetQuantities") or {}
            errors = payload.get("userErrors") or []
            if not errors:
                logger.info(
                    "Shopify inventory set to 0 for SKU=%s at %s location(s).",
                    clean_sku, len(levels),
                )
                return _result(
                    STATUS_ZEROED,
                    f"Shopify inventory set to 0 at {len(levels)} location(s).",
                    locations=len(levels),
                )
            stale = any(
                (error.get("code") or "") == "CHANGE_FROM_QUANTITY_STALE"
                for error in errors
            )
            if stale and attempt == 0:
                continue
            message = "; ".join(
                (error.get("message") or error.get("code") or "unknown error")
                for error in errors
            )
            raise RuntimeError(message)
    except Exception as exc:  # noqa: BLE001 - a sale must never fail because of Shopify
        text = str(exc)
        if "ACCESS_DENIED" in text or "write_inventory" in text or "read_inventory" in text:
            text = (
                "The Shopify app needs the read_inventory and write_inventory "
                "access scopes."
            )
        logger.error("Failed to zero Shopify inventory for SKU=%s: %s", clean_sku, text)
        return _result(STATUS_ERROR, text[:300])

    return _result(STATUS_ERROR, "Shopify inventory changed repeatedly; please retry.")


def sold_sync_flash(result: Dict[str, Any], *, english: bool = False):
    """Return (message, category) for flashing after a sale, or None."""
    status = result.get("status")
    detail = result.get("message") or ""
    if status == STATUS_ZEROED:
        return (
            ("Shopify inventory set to 0." if english else "已同步：Shopify 库存已改为 0。"),
            "ok",
        )
    if status in (STATUS_ALREADY_ZERO, STATUS_NOT_FOUND):
        return None
    if status == STATUS_NOT_TRACKED:
        return (
            (
                "Sold, but this product does not track inventory in Shopify. "
                "Please unpublish it in Shopify manually."
            ) if english else
            "已售出，但该商品在 Shopify 未开启库存追踪，无法自动改为 0，请到 Shopify 后台手动下架。",
            "error",
        )
    return (
        (
            f"Sold, but Shopify inventory could not be set to 0 ({detail}). "
            "Please set it to 0 in Shopify manually."
        ) if english else
        f"已售出，但 Shopify 库存未能自动改为 0（{detail}），请到 Shopify 后台手动处理。",
        "error",
    )
