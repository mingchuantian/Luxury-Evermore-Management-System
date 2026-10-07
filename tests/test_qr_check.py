import unittest

from flask import Flask

from luxury_app.auth import ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.collection_scope import RoleScopedItems
from luxury_app.routes.qr_check import register


class FakeItems:
    def __init__(self, docs):
        self.docs = docs
        self.last_query = None

    def find_one(self, query, projection=None):
        self.last_query = query
        for doc in self.docs:
            if doc.get("sku") == query.get("sku"):
                return dict(doc)
        return None


class QrCheckTests(unittest.TestCase):
    def _app(self, items):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret"

        @app.get("/login", endpoint="login")
        def login():
            return "login"

        register(app, items)
        return app

    def test_linked_item_returns_cached_shopify_title(self):
        items = FakeItems([{
            "sku": "ABC1234",
            "name": "Internal name",
            "shopify_sku_exist": True,
            "shopify_details": {"title": "Shopify Product Name"},
        }])
        app = self._app(items)
        with app.test_client() as client:
            response = client.get("/qr-check/lookup?sku=abc1234")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["linked"])
        self.assertEqual(response.json["shopify_title"], "Shopify Product Name")
        self.assertEqual(items.last_query, {"sku": "ABC1234"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_scanner_page_contains_camera_and_manual_fallback(self):
        app = self._app(FakeItems([]))
        with app.test_client() as client:
            response = client.get("/qr-check")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"html5-qrcode@2.3.8", response.data)
        self.assertIn(b'id="qr-reader"', response.data)
        self.assertIn(b'id="manual-form"', response.data)

    def test_management_scanner_page_is_in_english(self):
        app = self._app(FakeItems([]))
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["role"] = ROLE_MANAGEMENT
            response = client.get("/qr-check")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"QR Link Lookup", response.data)
        self.assertIn(b"Start Rear Camera", response.data)
        self.assertIn(b"Scan History", response.data)

    def test_unlinked_and_missing_skus_are_distinct(self):
        items = FakeItems([{
            "sku": "NOPE123",
            "name": "Unlinked Bag",
            "shopify_sku_exist": False,
        }])
        app = self._app(items)
        with app.test_client() as client:
            unlinked = client.get("/qr-check/lookup?sku=NOPE123")
            missing = client.get("/qr-check/lookup?sku=MISSING")

        self.assertTrue(unlinked.json["found"])
        self.assertFalse(unlinked.json["linked"])
        self.assertFalse(missing.json["found"])

    def test_staff_lookup_uses_items_view_collection(self):
        admin_items = FakeItems([{
            "sku": "SAME123", "shopify_sku_exist": False,
        }])
        staff_items = FakeItems([{
            "sku": "SAME123", "shopify_sku_exist": True,
            "shopify_details": {"title": "Staff Shopify Product"},
        }])
        app = self._app(RoleScopedItems(admin_items, staff_items))
        with app.test_client() as client:
            with client.session_transaction() as session:
                session["role"] = ROLE_STAFF
            response = client.get("/qr-check/lookup?sku=SAME123")

        self.assertTrue(response.json["linked"])
        self.assertEqual(response.json["shopify_title"], "Staff Shopify Product")

    def test_invalid_sku_is_rejected_without_database_query(self):
        items = FakeItems([])
        app = self._app(items)
        with app.test_client() as client:
            response = client.get("/qr-check/lookup?sku=bad%20sku")

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(items.last_query)


if __name__ == "__main__":
    unittest.main()
