"""URL重定向管理: keep the store's “/SKU → product page” redirects correct.

The QR code on a label opens https://<store>/<SKU>. A Shopify URL redirect
turns that into the product page. This module replaces the Shopify Flow that
used to create those redirects, and also repairs them when products change.

Rules (decided with the owner). Every hour the whole store is scanned: every
product variant in any status (Active / Draft / Archived), whatever its
inventory. Every SKU found is “on the website”.
- A redirect is wanted for every SKU found. Items only in this system, not on
  Shopify, get none.
- It points to that product's page, /products/<handle>. If the product is
  Draft/Archived the page is a 404 for shoppers; that is accepted.
- If several products carry the SKU, the Active one wins. Two or more Active
  products with the same SKU is a conflict: listed, never changed automatically.
- A “/SKU” redirect whose SKU was not found in the scan is no longer used
  (SKU changed or removed, product deleted) → it is deleted.
- Only current-style SKUs count: exactly 7 letters/digits. Old-style SKUs
  (any other length or with other characters) are ignored completely, on the
  products and in the redirects.
- Only “/SKU” redirects are managed: the path is a 7-character SKU that is in
  this system or points to a product page. Every other redirect (pages,
  collections, old URLs, old-style SKUs…) is left alone.

The same scan also says, per SKU, what the website shows (website_state):
selling (Active, can be bought), zero (Active, inventory 0), draft, archived.
The item list uses it; it is kept in memory only.

Nothing is written to MongoDB. The result of the last check (and what the last
fix changed) lives only in this process's memory. A background thread checks
every hour and fixes what it found; the page can check or fix on demand.
"""

import logging
import os
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlparse

from .shopify_maintenance import TOKEN_PROVIDER, build_product_url, shopify_gql
from .shopify_stock_check import QUERY_ALL_VARIANTS, admin_product_url

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 1.0
PAGE_DELAY_SECONDS = 0.2
WRITE_DELAY_SECONDS = 0.25
RECENT_CHANGES_LIMIT = 300

# What a row asks for. "conflict" is never applied automatically.
ACTIONS = ("create", "update", "delete", "conflict")
# The SKUs in use now: exactly 7 letters/digits. Everything else is an old style
# and is ignored.
SKU_SHAPE = re.compile(r"^[A-Z0-9]{7}$")


def is_current_sku(sku: str) -> bool:
    return bool(SKU_SHAPE.match((sku or "").strip().upper()))
WEBSITE_STATES = ("selling", "zero", "draft", "archived")
WEBSITE_LABELS = {"selling": "网站在售", "zero": "网站上架·库存0", "draft": "网站草稿", "archived": "网站已归档"}
ACTION_ORDER = {action: index for index, action in enumerate(ACTIONS)}
STATUS_RANK = {"ACTIVE": 0, "DRAFT": 1, "ARCHIVED": 2}

QUERY_REDIRECTS = """
query UrlRedirects($cursor: String) {
  urlRedirects(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id path target }
  }
}
"""

MUTATION_CREATE = """
mutation CreateRedirect($redirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $redirect) {
    urlRedirect { id path target }
    userErrors { field message }
  }
}
"""

MUTATION_UPDATE = """
mutation UpdateRedirect($id: ID!, $redirect: UrlRedirectInput!) {
  urlRedirectUpdate(id: $id, urlRedirect: $redirect) {
    urlRedirect { id path target }
    userErrors { field message }
  }
}
"""

MUTATION_DELETE = """
mutation DeleteRedirect($id: ID!) {
  urlRedirectDelete(id: $id) {
    deletedUrlRedirectId
    userErrors { field message }
  }
}
"""

_state: Dict[str, Any] = {
    "rows": [],            # what needs doing, from the last check
    "website": {},         # SKU → {"state", "title", "admin_url", "storefront_url"}
    "ok_count": 0,         # managed redirects that were already right
    "checked_at": None,
    "fixed_at": None,      # last time fixes were applied (auto or manual)
    "last_fix": None,      # {"at", "auto", "done": [...], "failed": [...]}
    "last_error": "",
    "running": False,
    "progress": None,      # live progress of the current / last run (see _progress)
}
_state_lock = threading.Lock()
_scheduler_thread: Optional[threading.Thread] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def interval_hours() -> float:
    try:
        value = float(os.getenv("URL_REDIRECT_INTERVAL_HOURS") or DEFAULT_INTERVAL_HOURS)
    except ValueError:
        return DEFAULT_INTERVAL_HOURS
    return max(0.25, value)


def auto_fix_enabled() -> bool:
    return (os.getenv("URL_REDIRECT_AUTO_FIX") or "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ------------------------------------------------------------------ reading


def sku_path(sku: str) -> str:
    return "/" + sku


def product_path(handle: str) -> str:
    return f"/products/{handle}"


def _path_key(path: str) -> str:
    """'/abc1234/' → 'ABC1234' (paths are compared without case)."""
    return unquote((path or "").strip()).strip("/").upper()


def _target_handle(target: str) -> Optional[str]:
    """The product handle a redirect target points to, or None if not a product page."""
    path = unquote(urlparse((target or "").strip()).path or "")
    parts = [part for part in path.split("/") if part]
    if "products" in parts:
        index = parts.index("products")
        if index + 1 < len(parts):
            return parts[index + 1].lower()
    return None


def _all_pages(gql: Callable, provider, query: str, key: str, on_page=None) -> List[Dict[str, Any]]:
    nodes, cursor = [], None
    while True:
        data = gql(query, {"cursor": cursor}, token_provider=provider)
        connection = data.get(key) or {}
        nodes.extend(connection.get("nodes") or [])
        if on_page:
            on_page(len(nodes))
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return nodes
        cursor = page.get("endCursor")
        time.sleep(PAGE_DELAY_SECONDS)


def _system_skus(items) -> set:
    return {
        (doc.get("sku") or "").strip().upper()
        for doc in items.find({}, {"_id": 0, "sku": 1})
        if is_current_sku(doc.get("sku"))
    }


def _product_info(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_id": product.get("id") or "",
        "title": product.get("title") or "",
        "handle": (product.get("handle") or "").lower(),
        "status": (product.get("status") or "").upper(),
        "image": ((product.get("featuredImage") or {}).get("url")) or "",
        "admin_url": admin_product_url(product.get("id") or ""),
        "storefront_url": build_product_url(product.get("handle")) or "",
    }


def _sellable(variant: Dict[str, Any]) -> bool:
    if (variant.get("inventoryItem") or {}).get("tracked") is False:
        return True
    quantity = variant.get("inventoryQuantity")
    return isinstance(quantity, int) and quantity > 0


def website_states(variants) -> Dict[str, Dict[str, Any]]:
    """SKU → what the website shows for it (Active listing wins over others)."""
    result: Dict[str, Dict[str, Any]] = {}
    for variant in variants:
        sku = (variant.get("sku") or "").strip().upper()
        if not is_current_sku(sku):
            continue
        info = _product_info(variant.get("product") or {})
        if info["status"] == "ACTIVE":
            state = "selling" if _sellable(variant) else "zero"
        else:
            state = "draft" if info["status"] == "DRAFT" else "archived"
        current = result.get(sku)
        if current is None or WEBSITE_STATES.index(state) < WEBSITE_STATES.index(current["state"]):
            result[sku] = {"state": state, "title": info["title"],
                           "admin_url": info["admin_url"], "storefront_url": info["storefront_url"]}
    return result


def _managed(key: str, target: str, system: set) -> bool:
    """Is this a “/SKU” redirect (ours), as opposed to any other redirect?"""
    return is_current_sku(key) and (key in system or bool(_target_handle(target)))


def plan(items, *, gql: Optional[Callable] = None, provider=None, variants=None, on_redirect_page=None):
    """Read Shopify + the items' SKUs and work out what needs changing.

    Returns (rows, ok_count). Read-only.
    """
    gql = gql or shopify_gql
    provider = provider or TOKEN_PROVIDER
    if variants is None:
        variants = _all_pages(gql, provider, QUERY_ALL_VARIANTS, "productVariants")
    redirects = _all_pages(gql, provider, QUERY_REDIRECTS, "urlRedirects", on_redirect_page)
    system = _system_skus(items)

    # SKU → the distinct products carrying it; handle → the SKUs on that product.
    products_by_sku: Dict[str, Dict[str, Dict[str, Any]]] = {}
    skus_by_handle: Dict[str, set] = {}
    for variant in variants:
        product = variant.get("product") or {}
        info = _product_info(product)
        sku = (variant.get("sku") or "").strip().upper()
        if not is_current_sku(sku):
            sku = ""   # old-style SKU: ignored
        if info["handle"]:
            skus_by_handle.setdefault(info["handle"], set())
            if sku:
                skus_by_handle[info["handle"]].add(sku)
        if sku and info["product_id"]:
            products_by_sku.setdefault(sku, {})[info["product_id"]] = info

    by_path: Dict[str, Dict[str, Any]] = {}
    for redirect in redirects:
        key = _path_key(redirect.get("path"))
        if key and key not in by_path:
            by_path[key] = redirect

    rows: List[Dict[str, Any]] = []
    ok = 0

    for sku, products in products_by_sku.items():
        ranked = sorted(products.values(), key=lambda p: (STATUS_RANK.get(p["status"], 9), p["title"]))
        best_rank = STATUS_RANK.get(ranked[0]["status"], 9)
        best = [p for p in ranked if STATUS_RANK.get(p["status"], 9) == best_rank]
        existing = by_path.get(sku)
        base = {
            "sku": sku,
            "in_system": sku in system,
            "redirect_id": (existing or {}).get("id") or "",
            "path": (existing or {}).get("path") or sku_path(sku),
            "current_target": (existing or {}).get("target") or "",
        }
        if len(best) > 1 and best_rank == STATUS_RANK["ACTIVE"]:
            rows.append(dict(base, action="conflict", products=best, target="",
                             reason=f"{len(best)} 个上架商品都写了这个 SKU，不知道该指向哪个"))
            continue
        product = best[0]
        if not product["handle"]:
            continue
        wanted = product_path(product["handle"])
        if not existing:
            rows.append(dict(base, action="create", products=[product], target=wanted,
                             reason="网站商品上有这个 SKU，但还没有重定向"))
            continue
        current = _target_handle(existing.get("target"))
        if current == product["handle"]:
            ok += 1
            continue
        if current is None:
            why = "现在指向的不是商品页"
        elif current in skus_by_handle:
            why = "现在指向的是另一个商品（可能是复制/重新上架后的旧商品）"
        else:
            why = "现在指向的商品网址已经不存在（商品网址改过或商品被删除）"
        rows.append(dict(base, action="update", products=[product], target=wanted, reason=why))

    # “/SKU” redirects whose SKU was not found anywhere on the website: no longer used.
    for key, redirect in by_path.items():
        if key in products_by_sku or not _managed(key, redirect.get("target"), system):
            continue   # still in use (handled above), or not one of ours
        handle = _target_handle(redirect.get("target"))
        others = sorted(skus_by_handle.get(handle) or []) if handle else []
        product = next(
            (info for sku in others for info in products_by_sku.get(sku, {}).values()
             if info["handle"] == handle), None,
        )
        if handle in skus_by_handle:
            why = ("网站上已没有商品写这个 SKU，重定向却指向了另一个商品"
                   + (f"（现在的 SKU 是 {', '.join(others)}）" if others else "") + "，扫码会打开别的包")
        else:
            why = "网站上已没有商品写这个 SKU（SKU 改掉或商品已删除），这个重定向已经不用了"
        rows.append({
            "sku": key, "in_system": key in system, "action": "delete",
            "redirect_id": redirect.get("id") or "", "path": redirect.get("path") or "",
            "current_target": redirect.get("target") or "", "target": "",
            "products": [product] if product else [], "reason": why,
        })

    rows.sort(key=lambda row: (ACTION_ORDER.get(row["action"], 9), row["sku"]))
    return rows, ok


# ------------------------------------------------------------------ writing


def _user_errors(payload: Dict[str, Any]) -> str:
    return "; ".join(e.get("message") or "" for e in payload.get("userErrors") or [] if e)


def apply_row(row: Dict[str, Any], *, gql: Optional[Callable] = None, provider=None) -> Dict[str, Any]:
    """Make one change on Shopify. Never raises; returns {"ok", "message"}."""
    gql = gql or shopify_gql
    provider = provider or TOKEN_PROVIDER
    action = row.get("action")
    try:
        if action == "create":
            data = gql(MUTATION_CREATE, {"redirect": {"path": sku_path(row["sku"]), "target": row["target"]}},
                       token_provider=provider)
            payload = data.get("urlRedirectCreate") or {}
        elif action == "update":
            data = gql(MUTATION_UPDATE, {"id": row["redirect_id"], "redirect": {"target": row["target"]}},
                       token_provider=provider)
            payload = data.get("urlRedirectUpdate") or {}
        elif action == "delete":
            data = gql(MUTATION_DELETE, {"id": row["redirect_id"]}, token_provider=provider)
            payload = data.get("urlRedirectDelete") or {}
        else:
            return {"ok": False, "message": "这一项需要你在 Shopify 里手动处理。"}
    except Exception as exc:  # noqa: BLE001
        message = " ".join(str(exc).split())[:300]
        if "ACCESS_DENIED" in message:
            message = "Shopify app 没有 write_online_store_navigation 权限。"
        return {"ok": False, "message": message}
    errors = _user_errors(payload)
    if errors:
        return {"ok": False, "message": errors}
    return {"ok": True, "message": ""}


def _change_record(row: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sku": row["sku"], "action": row["action"], "path": row.get("path") or sku_path(row["sku"]),
        "from": row.get("current_target") or "", "to": row.get("target") or "",
        "message": result.get("message") or "",
    }


# --------------------------------------------------------- in-memory state


def snapshot() -> Dict[str, Any]:
    with _state_lock:
        state = deepcopy(_state)
    checked = state["checked_at"]
    state["next_run_at"] = checked + timedelta(hours=interval_hours()) if checked else None
    state["auto_fix"] = auto_fix_enabled()
    return state


def website_snapshot():
    """(SKU → website info, time of the scan). Empty before the first scan."""
    with _state_lock:
        return dict(_state["website"]), _state["checked_at"]


def counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    result = {action: 0 for action in ACTIONS}
    for row in rows:
        if row.get("action") in result:
            result[row["action"]] += 1
    return result


def _progress(**values) -> None:
    """Update the live progress shown on the page while a run is going."""
    with _state_lock:
        progress = _state["progress"]
        if progress is not None:
            progress.update(values)
            progress["updated_at"] = _utc_now()


def run(items, *, fix: bool, auto: bool = False, gql: Optional[Callable] = None, provider=None) -> bool:
    """Check, and if fix=True apply every fixable row. False if already running.

    Results are published step by step (website states, then the list of
    things to do, then each fix as it happens), so the page shows progress
    instead of waiting for the end.
    """
    gql = gql or shopify_gql
    provider = provider or TOKEN_PROVIDER
    started = _utc_now()
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["progress"] = {
            "phase": "读取网站上的商品 SKU", "fix": fix, "auto": auto,
            "started_at": started, "updated_at": started, "finished_at": None,
            "variants": 0, "redirects": None, "to_fix": None, "done": 0, "failed": 0,
            "recent": [],
        }
    error = ""
    try:
        config_error = provider.configuration_error()
        if config_error:
            raise RuntimeError(config_error)
        variants = _all_pages(gql, provider, QUERY_ALL_VARIANTS, "productVariants",
                              lambda n: _progress(variants=n))
        website = website_states(variants)
        with _state_lock:
            _state["website"] = website   # the item list can use it right away
        _progress(phase="读取 Shopify 上的 URL 重定向", redirects=0, skus=len(website))
        rows, ok = plan(items, gql=gql, provider=provider, variants=variants,
                        on_redirect_page=lambda n: _progress(redirects=n))
        fixable = [r for r in rows if r["action"] != "conflict"]
        with _state_lock:
            _state["rows"] = rows
            _state["ok_count"] = ok
            _state["checked_at"] = _utc_now()
        if fix and fixable:
            _progress(phase="修复中", to_fix=len(fixable))
            last_fix = {"at": _utc_now(), "auto": auto, "done": [], "failed": [],
                        "done_count": 0, "failed_count": 0}
            for row in fixable:
                result = apply_row(row, gql=gql, provider=provider)
                change = _change_record(row, result)
                with _state_lock:
                    if result["ok"]:
                        _state["rows"] = [r for r in _state["rows"] if r["sku"] != row["sku"]]
                        _state["ok_count"] += 1
                        last_fix["done_count"] += 1
                        if len(last_fix["done"]) < RECENT_CHANGES_LIMIT:
                            last_fix["done"].append(change)
                    else:
                        for r in _state["rows"]:
                            if r["sku"] == row["sku"]:
                                r["error"] = result["message"]
                        last_fix["failed_count"] += 1
                        if len(last_fix["failed"]) < RECENT_CHANGES_LIMIT:
                            last_fix["failed"].append(change)
                    _state["last_fix"] = deepcopy(last_fix)
                    _state["fixed_at"] = last_fix["at"]
                    progress = _state["progress"]
                    progress["done"] = last_fix["done_count"]
                    progress["failed"] = last_fix["failed_count"]
                    progress["recent"] = ([dict(change, ok=result["ok"])] + progress["recent"])[:15]
                    progress["updated_at"] = _utc_now()
                time.sleep(WRITE_DELAY_SECONDS)
    except Exception as exc:  # noqa: BLE001 - keep the previous result on failure
        logger.exception("URL redirect check failed.")
        error = " ".join(str(exc).split())[:500]
        if "ACCESS_DENIED" in error:
            granted = getattr(provider, "granted_scopes", None) or []
            error = ("Shopify app 没有读取 URL 重定向的权限（需要 read/write_online_store_navigation）。"
                     + (f"店铺给这个 app 的 token 目前只有：{', '.join(granted)}。" if granted else "")
                     + "如果 Dev Dashboard 里已经加了权限，请到店铺后台的 Apps 里批准这个 app 的新权限。")
    finally:
        with _state_lock:
            _state["last_error"] = error
            _state["running"] = False
            if _state["progress"] is not None:
                _state["progress"]["phase"] = "出错" if error else "完成"
                _state["progress"]["finished_at"] = _utc_now()
                _state["progress"]["updated_at"] = _utc_now()
    return True


def run_in_background(items, *, fix: bool) -> bool:
    with _state_lock:
        if _state["running"]:
            return False
    threading.Thread(
        target=run, args=(items,), kwargs={"fix": fix},
        name="UrlRedirectManual", daemon=True,
    ).start()
    return True


def fix_one(sku: str, *, gql: Optional[Callable] = None, provider=None) -> Dict[str, Any]:
    """Apply the pending change for one SKU (from the last check)."""
    with _state_lock:
        row = next((deepcopy(r) for r in _state["rows"] if r["sku"] == sku), None)
    if not row:
        return {"ok": False, "message": "这一项已不在列表里，请重新检查后再试。"}
    result = apply_row(row, gql=gql, provider=provider)
    with _state_lock:
        if result["ok"]:
            _state["rows"] = [r for r in _state["rows"] if r["sku"] != sku]
            _state["ok_count"] += 1
        else:
            for r in _state["rows"]:
                if r["sku"] == sku:
                    r["error"] = result["message"]
    return result


def start_scheduler(items):
    """Check at startup (no changes), then every interval check and fix."""
    global _scheduler_thread
    if (os.getenv("URL_REDIRECT_ENABLED") or "true").strip().lower() in {"0", "false", "no", "off"}:
        logger.info("URL redirect manager disabled by configuration.")
        return None
    if TOKEN_PROVIDER.configuration_error():
        logger.warning("URL redirect manager not started: Shopify not configured.")
        return None
    with _state_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread

    def loop():
        first = True
        while True:
            try:
                state = snapshot()
                due = state["checked_at"] is None or _utc_now() >= state["next_run_at"]
                if due:
                    # The first check after a (re)start only looks, so a new
                    # deployment never changes anything before it can be reviewed.
                    run(items, fix=auto_fix_enabled() and not first, auto=True)
                    first = False
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected URL redirect scheduler error.")
            time.sleep(60)

    thread = threading.Thread(target=loop, name="UrlRedirectManager", daemon=True)
    with _state_lock:
        _scheduler_thread = thread
    thread.start()
    logger.info("URL redirect manager started (every %s hours).", interval_hours())
    return thread


def _reset_for_tests(**values):
    with _state_lock:
        _state.update(rows=[], website={}, ok_count=0, checked_at=None, fixed_at=None,
                      last_fix=None, last_error="", running=False, progress=None)
        _state.update(deepcopy(values))
