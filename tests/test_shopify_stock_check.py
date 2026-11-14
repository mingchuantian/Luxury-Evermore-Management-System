"""Shopify库存更新: scan rules, scheduler lease, page, menu and dashboard alert."""

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

from luxury_app import shopify_inventory
from luxury_app import shopify_stock_check as check
from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.routes.shopify_stock_check import register
from tests.test_ownership import FakeInventory


class FakeProvider:
    shop = "71eaf7.myshopify.com"

    def __init__(self, error=None):
        self.error = error

    def configuration_error(self):
        return self.error


class ProjectingInventory(FakeInventory):
    def find(self, query, projection=None):
        return super().find(query)


class FakeJobs:
    """Single-document background_jobs stand-in (enough for routes)."""

    def __init__(self, doc=None):
        self.doc = deepcopy(doc)

    def find_one(self, query):
        return deepcopy(self.doc)

    def update_one(self, query, update):
        if self.doc is None:
            return
        self.doc.update(deepcopy(update.get("$set", {})))
        for key, cond in update.get("$pull", {}).items():
            self.doc[key] = [
                row for row in self.doc.get(key, [])
                if not all(row.get(k) == v for k, v in cond.items())
            ]


def _variant(vid, sku, qty=1, status="ACTIVE", tracked=True, image="https://cdn/x.jpg"):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}", "sku": sku,
        "inventoryQuantity": qty, "inventoryItem": {"tracked": tracked},
        "product": {"id": f"gid://shopify/Product/{vid}", "title": f"T{vid}",
                    "handle": f"h{vid}", "status": status,
                    "featuredImage": {"url": image} if image else None},
    }


def _pages():
    return [
        {"productVariants": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [
            _variant(1, "SOLD001"),                      # sold here, in stock -> listed
            _variant(2, "SOLD002", qty=0),               # sold here, 0 stock -> no
            _variant(3, "SOLD003", status="DRAFT"),      # draft -> hidden
            _variant(4, "LIVE001"),                      # on shelf here -> no
        ]}},
        {"productVariants": {"pageInfo": {"hasNextPage": False}, "nodes": [
            _variant(5, "UNKNOWN9"),                     # not in system -> listed
            _variant(6, ""),                             # no SKU -> listed
            _variant(7, "SOLD004", qty=0, tracked=False),  # untracked, sellable -> listed
            _variant(8, "UNKNOWN8", status="ARCHIVED"),  # archived -> hidden
        ]}},
    ]


def _items():
    return ProjectingInventory([
        {"_id": ObjectId(), "sku": "SOLD001", "brand": "Chanel", "name": "CF", "status": "SOLD"},
        {"_id": ObjectId(), "sku": "SOLD002", "name": "B", "status": "SOLD"},
        {"_id": ObjectId(), "sku": "SOLD003", "name": "C", "status": "SOLD"},
        {"_id": ObjectId(), "sku": "SOLD004", "name": "D", "status": "SOLD"},
        {"_id": ObjectId(), "sku": "LIVE001", "name": "E", "status": "ON_SHELF"},
    ])


def _gql_from(pages, seen=None):
    def gql(query, variables, token_provider=None):
        if seen is not None:
            seen.append(variables.get("cursor"))
        return pages.pop(0)
    return gql


class FindMismatchesTests(unittest.TestCase):
    def test_only_active_sellable_and_sold_or_unknown(self):
        seen = []
        with patch.object(check, "PAGE_DELAY_SECONDS", 0):
            rows = check.find_mismatches(
                _items(), gql=_gql_from(_pages(), seen), provider=FakeProvider()
            )
        self.assertEqual(seen, [None, "c1"])
        by_vid = {row["variant_id"].rsplit("/", 1)[-1]: row for row in rows}
        self.assertEqual(sorted(by_vid), ["1", "5", "6", "7"])
        self.assertEqual(by_vid["1"]["reason"], "sold")
        self.assertEqual(by_vid["1"]["system_name"], "Chanel CF")
        self.assertEqual(by_vid["5"]["reason"], "missing")
        self.assertEqual(by_vid["6"]["sku"], "")
        self.assertFalse(by_vid["7"]["tracked"])
        self.assertEqual(by_vid["1"]["image"], "https://cdn/x.jpg")
        self.assertEqual(by_vid["1"]["admin_url"], "https://admin.shopify.com/store/71eaf7/products/1")
        self.assertTrue(by_vid["1"]["storefront_url"].endswith("/products/h1"))


class RunCheckTests(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        self.jobs = mongomock.MongoClient()["t"]["background_jobs"]

    def test_run_stores_results_and_schedules_next_in_two_hours(self):
        items = _items()
        before = deepcopy(items.docs)
        with patch.object(check, "PAGE_DELAY_SECONDS", 0):
            ran = check.run_check(items, self.jobs, gql=_gql_from(_pages()), provider=FakeProvider())
        doc = self.jobs.find_one({"_id": check.JOB_ID})
        self.assertTrue(ran)
        self.assertEqual(items.docs, before)  # never writes items
        self.assertEqual(len(doc["results"]), 4)
        self.assertFalse(doc["running"])
        gap = doc["next_run_at"] - doc["last_finished_at"]
        self.assertEqual(gap, timedelta(hours=2))
        # Not due yet -> scheduler skips; manual (force) still runs.
        self.assertFalse(check.run_check(items, self.jobs, gql=_gql_from(_pages()), provider=FakeProvider()))
        with patch.object(check, "PAGE_DELAY_SECONDS", 0):
            self.assertTrue(check.run_check(items, self.jobs, force=True,
                                            gql=_gql_from(_pages()), provider=FakeProvider()))

    def test_failure_keeps_previous_list(self):
        self.jobs.insert_one({"_id": check.JOB_ID, "results": [{"variant_id": "old"}]})

        def boom(*args, **kwargs):
            raise RuntimeError("Shopify down")

        check.run_check(_items(), self.jobs, force=True, gql=boom, provider=FakeProvider())
        doc = self.jobs.find_one({"_id": check.JOB_ID})
        self.assertEqual(doc["results"], [{"variant_id": "old"}])
        self.assertIn("Shopify down", doc["last_error"])
        self.assertFalse(doc["running"])

    def test_concurrent_run_is_blocked(self):
        owner = check._acquire(self.jobs, force=True)
        self.assertTrue(owner)
        self.assertIsNone(check._acquire(self.jobs, force=True))


class ZeroByVariantTests(unittest.TestCase):
    def test_zero_variant_uses_variant_lookup(self):
        calls = []

        def gql(query, variables, token_provider=None):
            calls.append(variables)
            if "productVariant(id:" in query:
                return {"productVariant": {"id": "gid://shopify/ProductVariant/9", "sku": "", "inventoryItem": {
                    "id": "gid://shopify/InventoryItem/7", "tracked": True,
                    "inventoryLevels": {"edges": [{"node": {
                        "location": {"id": "gid://shopify/Location/1"},
                        "quantities": [{"name": "available", "quantity": 1}],
                    }}]},
                }}}
            return {"inventorySetQuantities": {"userErrors": []}}

        with patch.object(shopify_inventory, "shopify_gql", side_effect=gql):
            result = shopify_inventory.zero_shopify_inventory_for_variant(
                "gid://shopify/ProductVariant/9", token_provider=FakeProvider()
            )
        self.assertEqual(result["status"], "zeroed")
        self.assertEqual(calls[0], {"id": "gid://shopify/ProductVariant/9"})
        self.assertEqual(calls[1]["input"]["quantities"][0]["changeFromQuantity"], 1)

    def test_rejects_non_variant_ids(self):
        result = shopify_inventory.zero_shopify_inventory_for_variant("123")
        self.assertEqual(result["status"], "error")


ROW = {
    "variant_id": "gid://shopify/ProductVariant/1", "sku": "SOLD001", "reason": "sold",
    "system_name": "Chanel CF", "shopify_title": "Chanel Classic Flap",
    "image": "https://cdn/x.jpg", "tracked": True,
    "storefront_url": "https://luxuryevermore.com/products/cf",
    "admin_url": "https://admin.shopify.com/store/71eaf7/products/1",
}
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


class RouteTests(unittest.TestCase):
    def _app(self, jobs):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda value: str(value or "")
        for endpoint in (
            "logout", "login", "list_items", "item_new_buyin_form",
            "item_new_consignment_form", "analytics", "audit_history",
            "notes_list", "users_list", "admin_management_payments",
            "management_index", "management_list_items",
            "management_item_new_buyin_form", "management_item_new_consignment_form",
        ):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "placeholder")
        app.add_url_rule("/items/<item_key>", "item_detail", lambda item_key: "x")

        from flask import render_template

        @app.get("/", endpoint="index")
        def index():
            # Render the real dashboard alert block only.
            return render_template("layout.html")

        register(app, _items(), jobs)
        return app

    @staticmethod
    def _login(client, role):
        with client.session_transaction() as session:
            session["user_id"] = str(ObjectId())
            session["username"] = "boss"
            session["role"] = role

    def test_page_rows_are_red_with_warning_and_image_no_old_columns(self):
        jobs = FakeJobs({"_id": check.JOB_ID, "results": [ROW], "last_success_at": NOW,
                         "next_run_at": NOW, "running": False})
        app = self._app(jobs)
        with app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            html = client.get("/shopify-stock").get_data(as_text=True)
        self.assertIn('class="stock-mismatch"', html)
        self.assertIn("系统内已sold或无此产品，shopify需更新sku或下架", html)
        self.assertIn('src="https://cdn/x.jpg"', html)
        self.assertIn("手动刷新", html)
        self.assertIn("每2小时自动更新", html)
        self.assertIn("已无同款，商品设置库存为0", html)
        self.assertIn("https://admin.shopify.com/store/71eaf7/products/1", html)
        for gone in ("原库存", "<th>结果</th>", "预览", "一键同步"):
            self.assertNotIn(gone, html)
        # Top menu link with red count.
        self.assertIn('href="/shopify-stock">Shopify库存更新', html)
        self.assertIn("(1)", html)

    def test_only_admin(self):
        app = self._app(FakeJobs({"_id": check.JOB_ID, "results": [ROW]}))
        with app.test_client() as client:
            for role in (ROLE_MANAGEMENT, ROLE_STAFF):
                self._login(client, role)
                self.assertEqual(client.get("/shopify-stock").status_code, 403)
                self.assertEqual(client.post("/shopify-stock/refresh").status_code, 403)
                self.assertEqual(client.post("/shopify-stock/zero").status_code, 403)
                self.assertNotIn("Shopify库存更新", client.get("/").get_data(as_text=True))

    def test_manual_refresh_triggers_background_run(self):
        app = self._app(FakeJobs({"_id": check.JOB_ID, "results": []}))
        with patch(
            "luxury_app.routes.shopify_stock_check.trigger_manual_refresh", return_value=True
        ) as trigger, app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            response = client.post("/shopify-stock/refresh")
        self.assertEqual(response.status_code, 302)
        trigger.assert_called_once()

    def test_zero_button_zeroes_variant_and_removes_row(self):
        jobs = FakeJobs({"_id": check.JOB_ID, "results": [ROW]})
        app = self._app(jobs)
        with patch(
            "luxury_app.routes.shopify_stock_check.zero_shopify_inventory_for_variant",
            return_value={"status": "zeroed", "ok": True, "message": ""},
        ) as zero, app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            client.post("/shopify-stock/zero", data={"variant_id": ROW["variant_id"]})
        zero.assert_called_once_with(ROW["variant_id"])
        self.assertEqual(jobs.doc["results"], [])

    def test_zero_button_ignores_variants_not_in_list(self):
        jobs = FakeJobs({"_id": check.JOB_ID, "results": [ROW]})
        app = self._app(jobs)
        with patch(
            "luxury_app.routes.shopify_stock_check.zero_shopify_inventory_for_variant"
        ) as zero, app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            client.post("/shopify-stock/zero", data={"variant_id": "gid://shopify/ProductVariant/999"})
        zero.assert_not_called()
        self.assertEqual(len(jobs.doc["results"]), 1)

    def test_failed_zero_keeps_row(self):
        jobs = FakeJobs({"_id": check.JOB_ID, "results": [ROW]})
        app = self._app(jobs)
        with patch(
            "luxury_app.routes.shopify_stock_check.zero_shopify_inventory_for_variant",
            return_value={"status": "error", "ok": False, "message": "boom"},
        ), app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            client.post("/shopify-stock/zero", data={"variant_id": ROW["variant_id"]})
        self.assertEqual(len(jobs.doc["results"]), 1)


if __name__ == "__main__":
    unittest.main()
