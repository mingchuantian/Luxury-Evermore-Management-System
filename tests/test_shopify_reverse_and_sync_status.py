"""Linked-SKU-missing warning on sale, and the Shopify sync status line."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

from luxury_app import shopify_maintenance
from luxury_app.auth import ROLE_ADMIN
from luxury_app.constants import OWNERSHIP_ADMIN
from luxury_app.routes.items import register as register_admin_items
from luxury_app.routes.sales import register as register_sales
from luxury_app.shopify_inventory import sold_sync_flash
from tests.test_ownership import FakeInventory
from tests.test_shopify_stock_check import FakeProvider


class LinkedSkuMissingOnSaleTests(unittest.TestCase):
    def test_flash_rules(self):
        not_found = {"status": "not_found", "ok": True, "message": ""}
        self.assertIsNone(sold_sync_flash(not_found))
        message, category = sold_sync_flash(not_found, was_linked=True)
        self.assertEqual(category, "error")
        self.assertIn("SKU 可能在 Shopify 被改过", message)
        message, _ = sold_sync_flash(not_found, english=True, was_linked=True)
        self.assertIn("may have been changed in Shopify", message)

    def test_sale_of_linked_item_warns_when_sku_gone(self):
        item = {
            "_id": ObjectId(), "sku": "SKU1234", "name": "Bag", "status": "ON_SHELF",
            "ownership": OWNERSHIP_ADMIN, "shopify_sku_exist": True,
            "created_at": datetime(2026, 10, 6, tzinfo=timezone.utc),
            "sold_record": [], "audit": [],
        }
        items = FakeInventory([item])
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.add_url_rule("/", "index", lambda: "x")
        register_admin_items(app, items)
        register_sales(app, items)
        with patch(
            "luxury_app.routes.sales.zero_shopify_inventory_for_sku",
            return_value={"status": "not_found", "ok": True, "message": ""},
        ), app.test_client() as client:
            with client.session_transaction() as session:
                session.update(user_id=str(ObjectId()), username="u", role=ROLE_ADMIN)
            client.post(f"/sales/new/{item['_id']}", data={
                "sold_price": "1000", "package_inclusion": "Bag", "sold_currency": "SGD",
            })
            with client.session_transaction() as session:
                flashes = session.get("_flashes", [])
        self.assertEqual(items.docs[0]["status"], "SOLD")
        self.assertTrue(any(cat == "error" and "找不到" in text for cat, text in flashes))


class SyncStatusLineTests(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        self.db = mongomock.MongoClient()["t"]

    def _html(self, job_doc):
        if job_doc:
            self.db.background_jobs.insert_one(job_doc)
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda value: value.strftime("%Y-%m-%d %H:%M") if value else ""
        for endpoint in RouteTests_ENDPOINTS:
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        register_admin_items(app, self.db["items"])
        with patch.object(shopify_maintenance, "TOKEN_PROVIDER", FakeProvider()), \
                patch.object(shopify_maintenance, "SYNC_ENABLED", True), \
                app.test_client() as client:
            with client.session_transaction() as session:
                session.update(user_id=str(ObjectId()), username="u", role=ROLE_ADMIN)
            return client.get("/items").get_data(as_text=True)

    def test_recent_success_is_green(self):
        html = self._html({"_id": "shopify-product-sync", "last_success": True,
                           "finished_at": datetime.now(timezone.utc) - timedelta(minutes=20),
                           "schedule_interval_hours": 1.0})
        self.assertIn('class="sync-status "', html)
        self.assertIn("Shopify 同步（每1小时自动）：", html)
        self.assertIn("（成功）", html)

    def test_stale_or_failed_is_red(self):
        html = self._html({"_id": "shopify-product-sync", "last_success": True,
                           "finished_at": datetime.now(timezone.utc) - timedelta(hours=5),
                           "schedule_interval_hours": 1.0})
        self.assertIn('class="sync-status bad"', html)
        self.assertIn("已超过2.5小时没有完成同步", html)

    def test_failed_run_is_red(self):
        html = self._html({"_id": "shopify-product-sync", "last_success": False,
                           "finished_at": datetime.now(timezone.utc),
                           "schedule_interval_hours": 1.0})
        self.assertIn("上次同步失败", html)

    def test_never_synced(self):
        html = self._html(None)
        self.assertIn("尚未完成过同步", html)


RouteTests_ENDPOINTS = (
    "index", "logout", "login", "analytics", "audit_history", "notes_list",
    "users_list", "admin_management_payments", "management_index",
    "management_list_items", "management_item_new_buyin_form",
    "management_item_new_consignment_form", "sale_new_form",
)


if __name__ == "__main__":
    unittest.main()
