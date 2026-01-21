"""
Shopify 商品详情维护任务
定期从 Shopify 同步商品信息到 MongoDB
"""
import os
import json
import time
import random
import threading
import logging
from typing import Any, Dict, Optional
import requests
from pymongo.collection import Collection

logger = logging.getLogger(__name__)

# =========================
# Config
# =========================
SHOP = "71eaf7.myshopify.com"
TOKEN = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN")  # 必须从环境变量读取
API_VERSION = "2025-10"

# 前台商品链接用这个域名拼
STOREFRONT_BASE = None  # 留空则使用 myshopify.com 域名

# 轮询间隔（避免太快）
SLEEP_SECONDS = 0.15

# 只处理状态为某些值的 item（留空则处理全部非 SOLD）
STATUS_FILTER = ""  # e.g. "available,listed"

SESSION = requests.Session()

# =========================
# Shopify GraphQL
# =========================
QUERY_MIN_FOR_MONGO = """
query MinForMongo($q: String!) {
  productVariants(first: 2, query: $q) {
    edges {
      node {
        id
        sku
        price
        compareAtPrice
        inventoryQuantity

        product {
          title
          handle
          featuredImage { url }
        }
      }
    }
  }
}
"""


def shopify_gql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """执行 Shopify GraphQL 查询"""
    if not TOKEN:
        raise RuntimeError("Missing env var SHOPIFY_ADMIN_ACCESS_TOKEN")

    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": TOKEN,
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = SESSION.post(
                url,
                headers=headers,
                json={"query": query, "variables": variables},
                timeout=30,
            )

            # 429 限流：按 Retry-After 等待后重试（算作一次 attempt）
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else 2.0
                logger.warning(f"Rate limited, waiting {wait_s}s...")
                time.sleep(wait_s)
                continue

            resp.raise_for_status()
            payload = resp.json()

            if "errors" in payload and payload["errors"]:
                raise RuntimeError("GraphQL errors:\n" + json.dumps(payload["errors"], ensure_ascii=False, indent=2))

            return payload["data"]

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # 网络抖动：重试几次，最后一次仍失败则抛出
            if attempt == max_attempts:
                raise
            backoff = 1.0 * attempt + random.random()  # 轻量退避
            logger.warning(f"Network error (attempt {attempt}/{max_attempts}), retrying after {backoff:.1f}s...")
            time.sleep(backoff)
            continue


def build_product_url(handle: Optional[str]) -> Optional[str]:
    """构建商品前台链接"""
    if not handle:
        return None

    # 你没填 STOREFRONT_BASE，就无法保证拼出来是可访问的前台域名
    # （myshopify.com 也能访问，但不一定是你的对外展示域名）
    if STOREFRONT_BASE:
        base = STOREFRONT_BASE.rstrip("/")
        return f"{base}/products/{handle}"

    # fallback：用 myshopify 域名拼（可能可用，但不建议当最终展示）
    return f"https://{SHOP}/products/{handle}"


def fetch_shopify_details_by_sku(sku: str) -> Dict[str, Any]:
    """
    从 Shopify 获取商品详情
    
    Returns:
      { ok: true, details: {...} }
      or { ok: false, error: "SKU not found", sku: sku }
    Any other error -> raises RuntimeError
    """
    q = f"sku:{sku}"
    data = shopify_gql(QUERY_MIN_FOR_MONGO, {"q": q})

    edges = data["productVariants"]["edges"]
    if not edges:
        return {"ok": False, "error": "SKU not found", "sku": sku}
    if len(edges) > 1:
        raise RuntimeError(f"Duplicate SKU found in Shopify for sku={sku} (matches={len(edges)})")

    node = edges[0]["node"]
    product = node["product"] or {}

    handle = product.get("handle")
    featured_image = (product.get("featuredImage") or {}).get("url")

    details = {
        "url": build_product_url(handle),
        "title": product.get("title"),
        "price": node.get("price"),
        "compare_at_price": node.get("compareAtPrice"),
        "inventory_quantity": node.get("inventoryQuantity"),
        "featured_image": featured_image,
    }
    return {"ok": True, "details": details}


def run_shopify_maintenance(items_collection: Collection):
    """
    执行 Shopify 维护任务
    
    Args:
        items_collection: MongoDB items collection
    """
    try:
        logger.info("Starting Shopify maintenance task...")
        
        query: Dict[str, Any] = {}

        # 统一排除 SOLD
        query["status"] = {"$ne": "SOLD"}

        # 如果你还想保留 STATUS_FILTER（可选），就让它叠加到同一个 status 条件里
        if STATUS_FILTER:
            allowed = [s.strip() for s in STATUS_FILTER.split(",") if s.strip()]
            if allowed:
                query["status"] = {"$in": [s for s in allowed if s != "SOLD"]}

        # 只取 sku + _id，减少 IO
        cursor = items_collection.find(query, {"sku": 1, "_id": 1})

        processed = 0
        found = 0
        not_found = 0

        for doc in cursor:
            processed += 1
            sku = (doc.get("sku") or "").strip()
            if not sku:
                # 没有 sku 的记录：标记为不存在
                items_collection.update_one(
                    {"_id": doc["_id"]},
                    {
                        "$set": {"shopify_sku_exist": False},
                        "$unset": {"shopify_details": ""}   # 完全删除字段
                    },
                )
                continue

            try:
                res = fetch_shopify_details_by_sku(sku)
            except Exception as e:
                # 任何非 "SKU not found" 的错误都记录但继续处理下一个
                logger.error(f"Error fetching Shopify details for SKU={sku}: {e}")
                # 不终止整个任务，继续处理下一个
                continue

            if res.get("ok") is False:
                # 只允许这一种 error 继续跑
                if res.get("error") == "SKU not found":
                    not_found += 1
                    items_collection.update_one(
                        {"_id": doc["_id"]},
                        {
                            "$set": {"shopify_sku_exist": False},
                            "$unset": {"shopify_details": ""}
                        },
                    )
                else:
                    logger.error(f"Unexpected error for SKU={sku}: {res}")
                    # 不终止，继续处理
            else:
                found += 1
                items_collection.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"shopify_sku_exist": True, "shopify_details": res["details"]}},
                )

            if SLEEP_SECONDS > 0:
                time.sleep(SLEEP_SECONDS)

            # 简单进度输出
            if processed % 50 == 0:
                logger.info(f"Shopify maintenance progress: Processed={processed} Found={found} NotFound={not_found}")
        
        logger.info(f"Shopify maintenance completed. Processed={processed} Found={found} NotFound={not_found}")
        
    except Exception as e:
        logger.error(f"Fatal error in Shopify maintenance task: {e}", exc_info=True)


def start_shopify_maintenance_scheduler(items_collection: Collection, interval_hours: float = 4.0):
    """
    启动 Shopify 维护任务的调度器（后台线程）
    
    Args:
        items_collection: MongoDB items collection
        interval_hours: 运行间隔（小时），默认4小时
    """
    interval_seconds = interval_hours * 3600
    
    def scheduler_loop():
        """调度器循环"""
        # 启动时立即运行一次
        logger.info("Running initial Shopify maintenance task...")
        run_shopify_maintenance(items_collection)
        
        # 然后每隔指定时间运行一次
        while True:
            try:
                logger.info(f"Waiting {interval_hours} hours before next Shopify maintenance run...")
                time.sleep(interval_seconds)
                run_shopify_maintenance(items_collection)
            except Exception as e:
                logger.error(f"Error in Shopify maintenance scheduler: {e}", exc_info=True)
                # 即使出错也继续调度，避免任务完全停止
                time.sleep(60)  # 出错后等待1分钟再继续
    
    # 启动后台线程
    thread = threading.Thread(target=scheduler_loop, daemon=True, name="ShopifyMaintenance")
    thread.start()
    logger.info(f"Shopify maintenance scheduler started (interval: {interval_hours} hours)")

