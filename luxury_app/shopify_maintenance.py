"""
Periodically synchronize Shopify product details into MongoDB.

Dev Dashboard apps owned by the same organization as the store use Shopify's
client-credentials grant. The returned Admin API token expires after 24 hours,
so this module caches it in memory and refreshes it before expiration.
"""

import json
import html
import logging
import os
import random
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError


logger = logging.getLogger(__name__)


def _normalize_shop(value: str) -> str:
    shop = (value or "").strip().lower()
    if shop.startswith("https://"):
        shop = shop[len("https://"):]
    elif shop.startswith("http://"):
        shop = shop[len("http://"):]
    # Accept either the short store name, the myshopify.com hostname, or a full
    # Shopify admin URL. OAuth must always target the canonical myshopify host.
    shop = shop.split("/", 1)[0].rstrip("/")
    if shop and "." not in shop:
        shop = f"{shop}.myshopify.com"
    return shop


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s.", name, raw, default)
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# Shopify configuration. Secrets must only be supplied through cloud/runtime
# environment variables.
SHOP = _normalize_shop(os.getenv("SHOPIFY_SHOP", "71eaf7.myshopify.com"))
CLIENT_ID = (os.getenv("SHOPIFY_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("SHOPIFY_CLIENT_SECRET") or "").strip()

# Temporary migration fallback for an older Shopify custom-app token. New
# deployments should use SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET.
TOKEN = (os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN") or "").strip()

API_VERSION = (os.getenv("SHOPIFY_API_VERSION") or "2026-07").strip()
STOREFRONT_BASE = (os.getenv("SHOPIFY_STOREFRONT_BASE_URL") or "").strip() or None
STATUS_FILTER = (os.getenv("SHOPIFY_STATUS_FILTER") or "").strip()
SLEEP_SECONDS = _env_float("SHOPIFY_SYNC_REQUEST_DELAY_SECONDS", 0.15)
SYNC_INTERVAL_HOURS = _env_float("SHOPIFY_SYNC_INTERVAL_HOURS", 1.0, minimum=0.05)
SYNC_ENABLED = _env_bool("SHOPIFY_SYNC_ENABLED", True)
TOKEN_REFRESH_SKEW_SECONDS = 300
JOB_LEASE_MINUTES = _env_float("SHOPIFY_SYNC_LEASE_MINUTES", 30.0, minimum=5.0)
JOB_RETRY_SECONDS = _env_float("SHOPIFY_SYNC_RETRY_SECONDS", 60.0, minimum=10.0)

SESSION = requests.Session()


class ShopifyConfigurationError(RuntimeError):
    pass


class ShopifyAuthenticationError(RuntimeError):
    pass


class ShopifyTokenProvider:
    """Thread-safe access-token cache for Shopify Admin API requests."""

    def __init__(
        self,
        shop: str,
        client_id: str = "",
        client_secret: str = "",
        static_token: str = "",
        session: Optional[requests.Session] = None,
        clock=time.monotonic,
    ):
        self.shop = _normalize_shop(shop)
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.static_token = (static_token or "").strip()
        self.session = session or requests.Session()
        self.clock = clock
        self._cached_token = ""
        self._refresh_at = 0.0
        self._lock = threading.Lock()

    @property
    def uses_client_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def configured(self) -> bool:
        return bool(self.shop and (self.uses_client_credentials or self.static_token))

    def configuration_error(self) -> Optional[str]:
        if not self.shop:
            return "SHOPIFY_SHOP is not set."
        if bool(self.client_id) != bool(self.client_secret):
            return "SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET must be set together."
        if not self.uses_client_credentials and not self.static_token:
            return (
                "Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET. "
                "SHOPIFY_ADMIN_ACCESS_TOKEN is only a legacy fallback."
            )
        return None

    def invalidate(self):
        with self._lock:
            self._cached_token = ""
            self._refresh_at = 0.0

    def get_token(self, force_refresh: bool = False) -> str:
        config_error = self.configuration_error()
        if config_error:
            raise ShopifyConfigurationError(config_error)

        # Prefer automatically refreshed client credentials whenever they exist.
        if not self.uses_client_credentials:
            return self.static_token

        now_value = self.clock()
        if not force_refresh and self._cached_token and now_value < self._refresh_at:
            return self._cached_token

        with self._lock:
            now_value = self.clock()
            if not force_refresh and self._cached_token and now_value < self._refresh_at:
                return self._cached_token

            token_url = f"https://{self.shop}/admin/oauth/access_token"
            try:
                response = self.session.post(
                    token_url,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise ShopifyAuthenticationError(
                    "Unable to reach Shopify token endpoint."
                ) from exc

            if response.status_code >= 400:
                error_name = _safe_token_error_message(
                    response,
                    secrets=(self.client_id, self.client_secret),
                )
                lower_error = error_name.lower()
                if "shop_not_permitted" in lower_error:
                    error_name += (
                        " Verify that the app and store are in the same Shopify "
                        "organization and that the app is installed on this store."
                    )
                elif "app_not_installed" in lower_error:
                    error_name += (
                        f" Install this Dev Dashboard app on {self.shop}, approve "
                        "the read_products scope, and verify that these credentials "
                        "belong to that installed app."
                    )
                elif "invalid_client" in lower_error or "client_id" in lower_error:
                    error_name += (
                        " Verify SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, and "
                        "the installed Dev Dashboard app."
                    )
                raise ShopifyAuthenticationError(
                    f"Shopify token request failed (HTTP {response.status_code}: "
                    f"{error_name})."
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise ShopifyAuthenticationError(
                    f"Shopify token endpoint returned invalid JSON (HTTP {response.status_code})."
                ) from exc

            access_token = (payload.get("access_token") or "").strip()
            if not access_token:
                raise ShopifyAuthenticationError(
                    "Shopify token response did not contain access_token."
                )

            raw_scopes = payload.get("scope") or ""
            if isinstance(raw_scopes, (list, tuple, set)):
                granted_scopes = {
                    str(scope).strip()
                    for scope in raw_scopes
                    if str(scope).strip()
                }
            else:
                granted_scopes = {
                    scope
                    for scope in re.split(r"[\s,]+", str(raw_scopes).strip())
                    if scope
                }

            # Shopify can collapse read_products when write_products is granted,
            # because the write scope implicitly grants read access.
            if granted_scopes and not {
                "read_products",
                "write_products",
            }.intersection(granted_scopes):
                raise ShopifyConfigurationError(
                    "The installed Shopify app is missing product access "
                    "(read_products or write_products). Granted scopes: "
                    + ", ".join(sorted(granted_scopes))
                )

            try:
                expires_in = max(60, int(payload.get("expires_in") or 86399))
            except (TypeError, ValueError):
                expires_in = 86399

            refresh_skew = min(TOKEN_REFRESH_SKEW_SECONDS, max(30, expires_in // 10))
            self._cached_token = access_token
            self._refresh_at = self.clock() + max(30, expires_in - refresh_skew)
            logger.info(
                "Obtained Shopify Admin API token; it will be refreshed before expiry."
            )
            return self._cached_token


def _safe_token_error_message(response, secrets=()) -> str:
    """Return a short token-endpoint error without leaking credentials."""
    message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        raw_error = (
            payload.get("error_description")
            or payload.get("error")
            or payload.get("errors")
        )
        if isinstance(raw_error, (dict, list)):
            message = json.dumps(raw_error, ensure_ascii=False)
        elif raw_error is not None:
            message = str(raw_error)

    if not message:
        raw_text = getattr(response, "text", "") or ""
        decoded_text = html.unescape(raw_text)
        oauth_error = re.search(
            r"oauth\s+error\s+([a-z0-9_]+)",
            decoded_text,
            flags=re.IGNORECASE,
        )
        if oauth_error:
            message = f"Oauth error {oauth_error.group(1)}"
        else:
            without_assets = re.sub(
                r"<(style|script)\b[^>]*>.*?</\1>",
                " ",
                decoded_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            message = re.sub(r"<[^>]+>", " ", without_assets)
            message = re.sub(r"\s+", " ", message).strip()

    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")

    if not message:
        content_type = (response.headers or {}).get("Content-Type", "unknown")
        message = f"empty/non-JSON response; content-type={content_type}"
    return message[:500]


TOKEN_PROVIDER = ShopifyTokenProvider(
    shop=SHOP,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    static_token=TOKEN,
    session=SESSION,
)


QUERY_MIN_FOR_MONGO = """
query MinForMongo($q: String!) {
  productVariants(first: 10, query: $q) {
    edges {
      node {
        id
        sku
        price
        compareAtPrice
        inventoryQuantity
        product {
          id
          title
          handle
          featuredImage { url }
        }
      }
    }
  }
}
"""


def _graphql_errors_are_throttled(errors) -> bool:
    for error in errors or []:
        extensions = error.get("extensions") or {}
        if extensions.get("code") == "THROTTLED":
            return True
    return False


def shopify_gql(
    query: str,
    variables: Dict[str, Any],
    token_provider: Optional[ShopifyTokenProvider] = None,
) -> Dict[str, Any]:
    """Execute a Shopify GraphQL Admin API query with retry and token refresh."""
    provider = token_provider or TOKEN_PROVIDER
    token = provider.get_token()
    url = f"https://{provider.shop}/admin/api/{API_VERSION}/graphql.json"
    refreshed_after_unauthorized = False
    max_attempts = 4

    for attempt in range(1, max_attempts + 1):
        try:
            response = provider.session.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": token,
                },
                json={"query": query, "variables": variables},
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == max_attempts:
                raise
            backoff = attempt + random.random()
            logger.warning(
                "Shopify network error (attempt %s/%s); retrying in %.1fs.",
                attempt,
                max_attempts,
                backoff,
            )
            time.sleep(backoff)
            continue

        if (
            response.status_code == 401
            and provider.uses_client_credentials
            and not refreshed_after_unauthorized
        ):
            provider.invalidate()
            token = provider.get_token(force_refresh=True)
            refreshed_after_unauthorized = True
            logger.warning("Shopify rejected a cached token; refreshed it and retrying.")
            continue

        if response.status_code == 429:
            if attempt == max_attempts:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else 1.0
            except ValueError:
                wait_seconds = 1.0
            logger.warning("Shopify rate limit reached; retrying in %.1fs.", wait_seconds)
            time.sleep(max(1.0, wait_seconds))
            continue

        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            if _graphql_errors_are_throttled(errors) and attempt < max_attempts:
                wait_seconds = float(attempt)
                logger.warning(
                    "Shopify GraphQL query throttled; retrying in %.1fs.",
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue
            raise RuntimeError(
                "Shopify GraphQL errors:\n"
                + json.dumps(errors, ensure_ascii=False, indent=2)
            )

        actual_version = response.headers.get("X-Shopify-API-Version")
        if actual_version and actual_version != API_VERSION:
            logger.warning(
                "Shopify served API version %s instead of configured version %s. "
                "Update SHOPIFY_API_VERSION.",
                actual_version,
                API_VERSION,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Shopify GraphQL response did not contain data.")
        return data

    raise RuntimeError("Shopify GraphQL request exhausted all retry attempts.")


def build_product_url(handle: Optional[str]) -> Optional[str]:
    if not handle:
        return None
    if STOREFRONT_BASE:
        return f"{STOREFRONT_BASE.rstrip('/')}/products/{handle}"
    return f"https://{SHOP}/products/{handle}"


def fetch_shopify_details_by_sku(
    sku: str,
    token_provider: Optional[ShopifyTokenProvider] = None,
) -> Dict[str, Any]:
    """
    Fetch a Shopify product variant by exact SKU and return the fields persisted
    by the existing web app.
    """
    clean_sku = (sku or "").strip()
    escaped_sku = clean_sku.replace("\\", "\\\\").replace('"', '\\"')
    data = shopify_gql(
        QUERY_MIN_FOR_MONGO,
        {"q": f'sku:"{escaped_sku}"'},
        token_provider=token_provider,
    )

    edges = (data.get("productVariants") or {}).get("edges") or []
    matches = [
        edge.get("node") or {}
        for edge in edges
        if ((edge.get("node") or {}).get("sku") or "").strip() == clean_sku
    ]
    if not matches:
        return {"ok": False, "error": "SKU not found", "sku": clean_sku}
    if len(matches) > 1:
        raise RuntimeError(
            f"Duplicate SKU found in Shopify for sku={clean_sku} "
            f"(matches={len(matches)})"
        )

    node = matches[0]
    product = node.get("product") or {}
    details = {
        "url": build_product_url(product.get("handle")),
        "title": product.get("title"),
        "price": node.get("price"),
        "compare_at_price": node.get("compareAtPrice"),
        "inventory_quantity": node.get("inventoryQuantity"),
        "featured_image": (product.get("featuredImage") or {}).get("url"),
        "product_id": product.get("id"),
        "variant_id": node.get("id"),
    }
    return {"ok": True, "details": details}


JOB_LOCK_ID = "shopify-product-sync"
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _system_status_audit(
    sku: str,
    from_status: Optional[str],
    changed_at: datetime,
) -> Dict[str, Any]:
    return {
        "at": changed_at,
        "action": "Change_status",
        "sku": sku,
        "from": from_status or "",
        "to": "ON_SHELF",
        "by": {"user_id": None, "username": "shopify-sync"},
    }


def _insert_system_audit(audit_logs: Optional[Collection], entry: Dict[str, Any]):
    if audit_logs is not None:
        audit_logs.insert_one(entry)


def _reconcile_existing_shopify_links(
    items_collection: Collection,
    audit_logs: Optional[Collection] = None,
) -> int:
    """Immediately backfill historical Shopify-linked items to ON_SHELF."""
    cursor = items_collection.find(
        {
            "shopify_sku_exist": True,
            "status": {"$nin": ["ON_SHELF", "SOLD"]},
        },
        {"sku": 1, "status": 1},
    )
    updated = 0
    for doc in cursor:
        old_status = doc.get("status")
        changed_at = _utc_now()
        audit_entry = _system_status_audit(
            (doc.get("sku") or "").strip(),
            old_status,
            changed_at,
        )
        result = items_collection.update_one(
            {"_id": doc["_id"], "status": old_status},
            {
                "$set": {"status": "ON_SHELF", "updated_at": changed_at},
                "$push": {"audit": audit_entry},
            },
        )
        if getattr(result, "matched_count", 0) == 1:
            updated += 1
            _insert_system_audit(audit_logs, audit_entry)
    return updated


def _save_shopify_match(
    items_collection: Collection,
    doc: Dict[str, Any],
    details: Dict[str, Any],
    synced_at: datetime,
    audit_logs: Optional[Collection] = None,
) -> bool:
    """
    Persist a live Shopify match and atomically move the item to ON_SHELF.
    The status compare-and-set prevents a concurrent sale from being overwritten.
    """
    old_status = doc.get("status")
    status_changed = old_status != "ON_SHELF"
    update: Dict[str, Any] = {
        "$set": {
            "shopify_sku_exist": True,
            "shopify_details": details,
            "shopify_synced_at": synced_at,
            "status": "ON_SHELF",
        },
        "$unset": {"shopify_sync_error": ""},
    }
    audit_entry = None
    if status_changed:
        update["$set"]["updated_at"] = synced_at
        audit_entry = _system_status_audit(
            (doc.get("sku") or "").strip(),
            old_status,
            synced_at,
        )
        update["$push"] = {"audit": audit_entry}

    result = items_collection.update_one(
        {"_id": doc["_id"], "status": old_status},
        update,
    )
    matched = getattr(result, "matched_count", 0) == 1
    if matched and audit_entry is not None:
        _insert_system_audit(audit_logs, audit_entry)
    return matched and status_changed


def _acquire_job_lease(
    job_locks: Optional[Collection],
    interval_hours: float,
) -> bool:
    if job_locks is None:
        return True

    current_time = _utc_now()
    lease_until = current_time + timedelta(minutes=JOB_LEASE_MINUTES)
    query = {
        "_id": JOB_LOCK_ID,
        "$and": [
            {
                "$or": [
                    {"lease_until": {"$exists": False}},
                    {"lease_until": {"$lte": current_time}},
                ]
            },
            {
                "$or": [
                    {"next_run_at": {"$exists": False}},
                    {"next_run_at": {"$lte": current_time}},
                    {"schedule_interval_hours": {"$ne": interval_hours}},
                ]
            },
        ],
    }
    try:
        result = job_locks.update_one(
            query,
            {
                "$set": {
                    "owner": INSTANCE_ID,
                    "lease_until": lease_until,
                    "started_at": current_time,
                    "schedule_interval_hours": interval_hours,
                }
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return bool(result.matched_count or result.upserted_id)


def _renew_job_lease(job_locks: Optional[Collection]) -> bool:
    if job_locks is None:
        return True
    result = job_locks.update_one(
        {"_id": JOB_LOCK_ID, "owner": INSTANCE_ID},
        {
            "$set": {
                "lease_until": _utc_now() + timedelta(minutes=JOB_LEASE_MINUTES)
            }
        },
    )
    return result.matched_count == 1


def _release_job_lease(
    job_locks: Optional[Collection],
    interval_hours: float,
    success: bool,
):
    if job_locks is None:
        return
    current_time = _utc_now()
    retry_delay = interval_hours * 3600 if success else max(60.0, JOB_RETRY_SECONDS)
    job_locks.update_one(
        {"_id": JOB_LOCK_ID, "owner": INSTANCE_ID},
        {
            "$set": {
                "lease_until": current_time,
                "next_run_at": current_time + timedelta(seconds=retry_delay),
                "finished_at": current_time,
                "last_success": success,
            },
            "$unset": {"owner": ""},
        },
    )


def run_shopify_maintenance(
    items_collection: Collection,
    job_locks: Optional[Collection] = None,
    audit_logs: Optional[Collection] = None,
    token_provider: Optional[ShopifyTokenProvider] = None,
    interval_hours: float = SYNC_INTERVAL_HOURS,
):
    """Synchronize all non-sold local SKUs while preserving data on API errors."""
    provider = token_provider or TOKEN_PROVIDER
    config_error = provider.configuration_error()
    if config_error:
        logger.warning("Shopify maintenance skipped: %s", config_error)
        return {"skipped": "not_configured"}
    if not SYNC_ENABLED:
        logger.info("Shopify maintenance skipped: SHOPIFY_SYNC_ENABLED is false.")
        return {"skipped": "disabled"}
    if not _acquire_job_lease(job_locks, interval_hours):
        return {"skipped": "not_due_or_locked"}

    success = False
    processed = 0
    found = 0
    not_found = 0
    failed = 0
    status_updated = 0
    try:
        status_updated += _reconcile_existing_shopify_links(
            items_collection,
            audit_logs=audit_logs,
        )

        # Fail once at the start rather than retrying credentials for every SKU.
        provider.get_token()
        logger.info(
            "Starting Shopify maintenance task with Admin API %s.", API_VERSION
        )

        query: Dict[str, Any] = {"status": {"$ne": "SOLD"}}
        if STATUS_FILTER:
            allowed = [
                value.strip()
                for value in STATUS_FILTER.split(",")
                if value.strip() and value.strip() != "SOLD"
            ]
            if allowed:
                query["status"] = {"$in": allowed}

        cursor = items_collection.find(
            query,
            {"sku": 1, "_id": 1, "status": 1, "shopify_sku_exist": 1},
        )
        consecutive_errors = 0

        for doc in cursor:
            processed += 1
            sku = (doc.get("sku") or "").strip()
            synced_at = _utc_now()
            if not sku:
                items_collection.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "shopify_sku_exist": False,
                            "shopify_synced_at": synced_at,
                        },
                        "$unset": {"shopify_details": "", "shopify_sync_error": ""},
                    },
                )
                continue

            try:
                result = fetch_shopify_details_by_sku(
                    sku,
                    token_provider=provider,
                )
                consecutive_errors = 0
            except (ShopifyAuthenticationError, ShopifyConfigurationError):
                raise
            except Exception as exc:
                failed += 1
                consecutive_errors += 1
                logger.error("Error fetching Shopify details for SKU=%s: %s", sku, exc)
                items_collection.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "shopify_sync_error": str(exc)[:500],
                            "shopify_sync_attempted_at": synced_at,
                        }
                    },
                )
                if consecutive_errors >= 5:
                    raise RuntimeError(
                        "Aborting Shopify sync after five consecutive API errors."
                    )
                continue

            if result.get("ok") is False:
                if result.get("error") != "SKU not found":
                    raise RuntimeError(f"Unexpected Shopify result for SKU={sku}: {result}")
                not_found += 1
                items_collection.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "shopify_sku_exist": False,
                            "shopify_synced_at": synced_at,
                        },
                        "$unset": {"shopify_details": "", "shopify_sync_error": ""},
                    },
                )
            else:
                found += 1
                if _save_shopify_match(
                    items_collection,
                    doc,
                    result["details"],
                    synced_at,
                    audit_logs=audit_logs,
                ):
                    status_updated += 1

            if SLEEP_SECONDS > 0:
                time.sleep(SLEEP_SECONDS)

            if processed % 50 == 0:
                if not _renew_job_lease(job_locks):
                    raise RuntimeError("Lost the Shopify maintenance job lease.")
                logger.info(
                    "Shopify maintenance progress: processed=%s found=%s "
                    "not_found=%s failed=%s",
                    processed,
                    found,
                    not_found,
                    failed,
                )

        success = True
        summary = {
            "processed": processed,
            "found": found,
            "not_found": not_found,
            "failed": failed,
            "status_updated": status_updated,
        }
        logger.info("Shopify maintenance completed: %s", summary)
        return summary
    except Exception as exc:
        logger.error("Fatal error in Shopify maintenance task: %s", exc, exc_info=True)
        return {
            "error": str(exc),
            "processed": processed,
            "found": found,
            "not_found": not_found,
            "failed": failed,
            "status_updated": status_updated,
        }
    finally:
        _release_job_lease(job_locks, interval_hours, success)


_scheduler_thread = None
_scheduler_guard = threading.Lock()


def start_shopify_maintenance_scheduler(
    items_collection: Collection,
    job_locks: Optional[Collection] = None,
    audit_logs: Optional[Collection] = None,
    interval_hours: float = SYNC_INTERVAL_HOURS,
    token_provider: Optional[ShopifyTokenProvider] = None,
):
    """
    Start a non-blocking scheduler. A MongoDB lease ensures that only one cloud
    worker or app instance performs a due synchronization run.
    """
    global _scheduler_thread

    provider = token_provider or TOKEN_PROVIDER
    config_error = provider.configuration_error()
    if config_error:
        logger.warning("Shopify maintenance scheduler not started: %s", config_error)
        return None
    if not SYNC_ENABLED:
        logger.info("Shopify maintenance scheduler disabled by configuration.")
        return None

    with _scheduler_guard:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread

        def scheduler_loop():
            while True:
                try:
                    run_shopify_maintenance(
                        items_collection,
                        job_locks=job_locks,
                        audit_logs=audit_logs,
                        token_provider=provider,
                        interval_hours=interval_hours,
                    )
                except Exception:
                    logger.exception("Unexpected Shopify scheduler error.")

                # With a distributed schedule, poll cheaply so another worker can
                # take over after a crash. Without it, preserve the old interval.
                wait_seconds = (
                    min(60.0, interval_hours * 3600)
                    if job_locks is not None
                    else interval_hours * 3600
                )
                time.sleep(max(10.0, wait_seconds))

        _scheduler_thread = threading.Thread(
            target=scheduler_loop,
            daemon=True,
            name="ShopifyMaintenance",
        )
        _scheduler_thread.start()
        logger.info(
            "Shopify maintenance scheduler started (interval: %s hours, API: %s).",
            interval_hours,
            API_VERSION,
        )
        return _scheduler_thread
