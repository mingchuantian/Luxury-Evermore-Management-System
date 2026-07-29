import os
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

import luxury_app.app_factory as app_factory
import luxury_app.shopify_maintenance as shopify_maintenance
from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, require_login
from luxury_app.routes.sales import register as register_sales
from luxury_app.utils import parse_date_yyyy_mm_dd


class FakeUsers:
    def __init__(self, user):
        self.user = deepcopy(user)

    def find_one(self, query):
        if query.get("_id") == self.user["_id"]:
            return deepcopy(self.user)
        return None


class FakeItems:
    def __init__(self, doc):
        self.doc = deepcopy(doc)
        self.fail_next_status_compare = False

    def find_one(self, query):
        if query.get("_id") == self.doc["_id"]:
            return deepcopy(self.doc)
        return None

    def update_one(self, query, update):
        if self.fail_next_status_compare and "status" in query:
            self.fail_next_status_compare = False
            self.doc["status"] = "RESERVED"

        if query.get("_id") != self.doc["_id"]:
            return SimpleNamespace(modified_count=0)
        if "status" in query and query["status"] != self.doc.get("status"):
            return SimpleNamespace(modified_count=0)

        for key, value in update.get("$set", {}).items():
            self.doc[key] = value
        for key, value in update.get("$push", {}).items():
            target = self.doc.setdefault(key, [])
            if isinstance(value, dict) and "$each" in value:
                target.extend(value["$each"])
            else:
                target.append(value)
        return SimpleNamespace(modified_count=1)


class CoreFixTests(unittest.TestCase):
    def test_missing_or_short_flask_secret_stops_startup(self):
        with patch.dict(os.environ, {"FLASK_SECRET": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
                app_factory.create_app()

        with patch.dict(os.environ, {"FLASK_SECRET": "too-short"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
                app_factory.create_app()

    def test_database_role_overrides_forged_session_role(self):
        user_id = ObjectId()
        users = FakeUsers({
            "_id": user_id,
            "username": "manager",
            "role": ROLE_MANAGEMENT,
        })
        app = Flask(__name__)
        app.secret_key = "test-secret-that-is-long-enough-for-tests"

        @app.get("/login", endpoint="login")
        def login():
            return "login"

        @app.get("/users")
        def users_page():
            return "users"

        require_login(app, users)

        with app.test_client() as client:
            with client.session_transaction() as session:
                session["user_id"] = str(user_id)
                session["username"] = "manager"
                session["role"] = ROLE_ADMIN
                session["last_seen_at"] = datetime.now(timezone.utc).isoformat()

            response = client.get("/users")
            self.assertEqual(response.status_code, 403)
            with client.session_transaction() as session:
                self.assertEqual(session["role"], ROLE_MANAGEMENT)

    def _make_sales_app(self, status="RECEIVED"):
        item_id = ObjectId()
        items = FakeItems({
            "_id": item_id,
            "sku": "TEST123",
            "name": "Test item",
            "status": status,
            "listing_currency": "SGD",
            "sold_record": [],
            "audit": [],
        })
        app = Flask(__name__)
        app.secret_key = "test-secret-that-is-long-enough-for-tests"

        @app.get("/items/<item_key>", endpoint="item_detail")
        def item_detail(item_key):
            return item_key

        register_sales(app, items)
        return app, items, item_id

    @staticmethod
    def _sale_payload():
        return {
            "action": "sell",
            "buyer": "Buyer",
            "sale_channel": "Store",
            "sold_price": "1000",
            "sold_currency": "SGD",
            "payment_method": "Card",
            "receipt_no": "R-1",
            "package_inclusion": "Bag",
            "sale_note": "",
            "sold_at": "2026-07-29T12:00",
            "tz_offset_min": "-480",
        }

    def test_non_sellable_status_is_rejected(self):
        app, items, item_id = self._make_sales_app(status="INBOUND")
        with app.test_client() as client:
            response = client.post(f"/sales/new/{item_id}", data=self._sale_payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(items.doc["status"], "INBOUND")
        self.assertEqual(items.doc["sold_record"], [])

    def test_sale_can_only_be_recorded_once(self):
        app, items, item_id = self._make_sales_app()
        with app.test_client() as client:
            first = client.post(f"/sales/new/{item_id}", data=self._sale_payload())
            second = client.post(f"/sales/new/{item_id}", data=self._sale_payload())

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(items.doc["status"], "SOLD")
        self.assertEqual(len(items.doc["sold_record"]), 1)

    def test_sale_compare_and_set_rejects_concurrent_status_change(self):
        app, items, item_id = self._make_sales_app()
        items.fail_next_status_compare = True
        with app.test_client() as client:
            response = client.post(f"/sales/new/{item_id}", data=self._sale_payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(items.doc["status"], "RESERVED")
        self.assertEqual(items.doc["sold_record"], [])

    def test_business_date_starts_at_utc_plus_8_midnight(self):
        parsed = parse_date_yyyy_mm_dd("2026-07-29")
        self.assertEqual(
            parsed,
            datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc),
        )

    def test_shopify_scheduler_is_not_started_without_token(self):
        provider = shopify_maintenance.ShopifyTokenProvider(
            shop="71eaf7.myshopify.com"
        )
        with patch.object(shopify_maintenance.threading, "Thread") as thread:
            scheduler_result = shopify_maintenance.start_shopify_maintenance_scheduler(
                object(),
                token_provider=provider,
            )
            task_result = shopify_maintenance.run_shopify_maintenance(
                object(),
                token_provider=provider,
            )
        self.assertIsNone(scheduler_result)
        self.assertEqual(task_result, {"skipped": "not_configured"})
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
