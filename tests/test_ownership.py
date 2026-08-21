import re
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.constants import OWNERSHIP_ADMIN, OWNERSHIP_MANAGEMENT
from luxury_app.routes.items import register as register_admin_items
from luxury_app.routes.management import register as register_management
from luxury_app.routes.sales import register as register_sales


def _matches(doc, query):
    for key, expected in (query or {}).items():
        if key == "$and":
            if not all(_matches(doc, branch) for branch in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(doc, branch) for branch in expected):
                return False
            continue
        actual = doc.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif isinstance(expected, dict) and "$regex" in expected:
            flags = re.I if "i" in expected.get("$options", "") else 0
            if not re.search(expected["$regex"], str(actual or ""), flags):
                return False
        elif actual != expected:
            return False
    return True


def _contains_ownership_scope(query):
    if "ownership" in (query or {}):
        return True
    return any(
        _contains_ownership_scope(branch)
        for branch in (query or {}).get("$or", [])
    )


class FakeCursor:
    def __init__(self, docs):
        self.docs = [deepcopy(doc) for doc in docs]

    def sort(self, key, direction):
        def nested_value(doc):
            value = doc
            for part in key.split("."):
                if isinstance(value, list):
                    values = [entry.get(part) for entry in value if isinstance(entry, dict)]
                    value = max((entry for entry in values if entry is not None), default=None)
                elif isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
            return value or datetime.min.replace(tzinfo=timezone.utc)

        self.docs.sort(key=nested_value, reverse=direction < 0)
        return self

    def skip(self, count):
        self.docs = self.docs[count:]
        return self

    def limit(self, count):
        self.docs = self.docs[:count]
        return self

    def __iter__(self):
        return iter(self.docs)


class FakeInventory:
    def __init__(self, docs=None):
        self.docs = [deepcopy(doc) for doc in (docs or [])]
        self.queries = []

    def count_documents(self, query, **kwargs):
        self.queries.append(deepcopy(query))
        return sum(1 for doc in self.docs if _matches(doc, query))

    def find(self, query):
        self.queries.append(deepcopy(query))
        return FakeCursor([doc for doc in self.docs if _matches(doc, query)])

    def find_one(self, query):
        self.queries.append(deepcopy(query))
        for doc in self.docs:
            if _matches(doc, query):
                return deepcopy(doc)
        return None

    def insert_one(self, doc):
        stored = deepcopy(doc)
        stored.setdefault("_id", ObjectId())
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def update_one(self, query, update):
        for doc in self.docs:
            if not _matches(doc, query):
                continue
            for key, value in update.get("$set", {}).items():
                doc[key] = deepcopy(value)
            for key, value in update.get("$push", {}).items():
                target = doc.setdefault(key, [])
                if isinstance(value, dict) and "$each" in value:
                    target.extend(deepcopy(value["$each"]))
                else:
                    target.append(deepcopy(value))
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)


class OwnershipTests(unittest.TestCase):
    def _make_app(self, items):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"

        @app.get("/logout", endpoint="logout")
        def logout():
            return "logout"

        register_management(app, items)
        register_sales(app, items)
        return app

    @staticmethod
    def _login(client, role):
        with client.session_transaction() as session:
            session["user_id"] = str(ObjectId())
            session["username"] = "employee"
            session["role"] = role

    @staticmethod
    def _payload(**extra):
        payload = {
            "entry_type": "BUY_IN",
            "name": "Test Bag",
            "brand_select": "Dior",
            "cost_currency": "RMB",
            "cost": "1000",
            "purchase_at": "2026-08-21T12:00:00",
            "tz_offset_min": "-480",
        }
        payload.update(extra)
        return payload

    def test_management_creation_sets_server_controlled_ownership(self):
        items = FakeInventory()
        app = self._make_app(items)

        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            response = client.post(
                "/management/items/new",
                data=self._payload(ownership=OWNERSHIP_ADMIN),
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(items.docs), 1)
        self.assertEqual(items.docs[0]["ownership"], OWNERSHIP_MANAGEMENT)
        self.assertEqual(items.docs[0]["source_type"], "BUY_IN")
        self.assertEqual(items.docs[0]["status"], "RECEIVED")
        self.assertIsNotNone(items.docs[0]["received_at"])

    def test_admin_creation_sets_server_controlled_ownership(self):
        items = FakeInventory()
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"

        @app.get("/login", endpoint="login")
        def login():
            return "login"

        register_admin_items(app, items)

        with app.test_client() as client:
            with client.session_transaction() as session:
                session["user_id"] = str(ObjectId())
                session["username"] = "admin"
                session["role"] = "Admin"
            response = client.post(
                "/items/new",
                data=self._payload(ownership=OWNERSHIP_MANAGEMENT),
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(items.docs), 1)
        self.assertEqual(items.docs[0]["ownership"], OWNERSHIP_ADMIN)

    def test_admin_items_can_filter_by_ownership(self):
        created_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
        items = FakeInventory([
            {
                "_id": ObjectId(), "sku": "ADMIN11", "name": "Admin Item",
                "brand": "Dior", "ownership": OWNERSHIP_ADMIN,
                "status": "RECEIVED", "source_type": "BUY_IN",
                "created_at": created_at, "purchase_at": created_at,
            },
            {
                "_id": ObjectId(), "sku": "MGMT011", "name": "Management Item",
                "brand": "Chanel", "ownership": OWNERSHIP_MANAGEMENT,
                "status": "RECEIVED", "source_type": "CONSIGNMENT",
                "created_at": created_at, "purchase_at": created_at,
            },
        ])
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"

        @app.get("/login", endpoint="login")
        def login():
            return "login"

        register_admin_items(app, items)

        with app.test_client() as client:
            response = client.get("/items?ownership=management")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MGMT011", response.data)
        self.assertNotIn(b"ADMIN11", response.data)
        self.assertIn(b'value="management" selected', response.data)

    def test_management_add_forms_are_english(self):
        items = FakeInventory()
        app = self._make_app(items)

        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            buy_in = client.get("/management/items/new/buyin")
            consignment = client.get("/management/items/new/consignment")

        self.assertEqual(buy_in.status_code, 200)
        self.assertIn(b"Add Buy-In", buy_in.data)
        self.assertIn(b"Product Name", buy_in.data)
        self.assertIn(b"Seller / Consignor", buy_in.data)
        self.assertEqual(consignment.status_code, 200)
        self.assertIn(b"Add Consignment", consignment.data)

    def test_staff_cannot_add_items(self):
        items = FakeInventory()
        app = self._make_app(items)

        with app.test_client() as client:
            self._login(client, ROLE_STAFF)
            get_response = client.get("/management/items/new/buyin")
            post_response = client.post(
                "/management/items/new",
                data=self._payload(),
            )

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertEqual(items.docs, [])

    def test_management_pages_only_read_management_owned_items(self):
        created_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
        management_id = ObjectId()
        admin_id = ObjectId()
        items = FakeInventory([
            {
                "_id": management_id,
                "sku": "MGMT001",
                "name": "Management Bag",
                "ownership": OWNERSHIP_MANAGEMENT,
                "status": "RECEIVED",
                "created_at": created_at,
                "purchase_at": created_at,
                "shopify_details": {
                    "featured_image": "https://cdn.shopify.com/management.jpg"
                },
                "audit": [],
            },
            {
                "_id": admin_id,
                "sku": "ADMIN01",
                "name": "Admin Bag",
                "ownership": OWNERSHIP_ADMIN,
                "status": "RECEIVED",
                "created_at": created_at,
                "purchase_at": created_at,
                "seller_name": "ADMIN-PRIVATE-SELLER",
                "cost": 987654321,
                "shopify_details": {
                    "featured_image": "https://cdn.shopify.com/admin.jpg"
                },
                "audit": [],
            },
            {
                "_id": ObjectId(),
                "sku": "HIDDEN1",
                "name": "Hidden Admin Workflow Item",
                "ownership": OWNERSHIP_ADMIN,
                "status": "INBOUND",
                "created_at": created_at,
                "purchase_at": created_at,
                "audit": [],
            },
        ])
        app = self._make_app(items)

        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            dashboard = client.get("/management")
            item_list = client.get("/management/items")
            public_search = client.get("/management/items?q=ADMIN01")
            private_search = client.get("/management/items?q=ADMIN-PRIVATE-SELLER")
            denied_detail = client.get(f"/management/items/{admin_id}")
            legacy_path = client.get("/outsider/items")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Total items: <b>2</b>", dashboard.data)
        self.assertEqual(item_list.status_code, 200)
        self.assertIn(b"MGMT001", item_list.data)
        self.assertIn(b"ADMIN01", item_list.data)
        self.assertIn(b"https://cdn.shopify.com/management.jpg", item_list.data)
        self.assertIn(b"https://cdn.shopify.com/admin.jpg", item_list.data)
        self.assertNotIn(b"HIDDEN1", item_list.data)
        self.assertNotIn(b"ADMIN-PRIVATE-SELLER", item_list.data)
        self.assertNotIn(b"987654321", item_list.data)
        self.assertNotIn(b"/management/items/ADMIN01", item_list.data)
        self.assertIn(f"/management/sales/new/{admin_id}".encode(), item_list.data)
        self.assertIn(b"ADMIN01", public_search.data)
        self.assertNotIn(b"ADMIN01", private_search.data)
        self.assertEqual(denied_detail.status_code, 404)
        self.assertEqual(legacy_path.status_code, 404)
        self.assertTrue(items.queries)
        self.assertTrue(all(
            _contains_ownership_scope(query)
            for query in items.queries
        ))

    def test_management_can_sell_admin_item_without_detail_access(self):
        item_id = ObjectId()
        created_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
        items = FakeInventory([{
            "_id": item_id,
            "sku": "ADMIN02",
            "name": "Admin Sale Item",
            "ownership": OWNERSHIP_ADMIN,
            "status": "ON_SHELF",
            "created_at": created_at,
            "purchase_at": created_at,
            "listing_currency": "SGD",
            "sold_record": [],
            "audit": [],
        }])
        app = self._make_app(items)
        sale_payload = {
            "action": "sell",
            "sold_price": "1200",
            "sold_currency": "SGD",
            "sold_at": "2026-08-21T12:00:00",
            "tz_offset_min": "-480",
            "buyer": "Buyer",
            "sale_channel": "Store",
            "payment_method": "Paynow",
            "receipt_no": "R-100",
            "package_inclusion": "Bag",
            "sale_note": "",
        }

        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            detail = client.get(f"/management/items/{item_id}")
            sale_form = client.get(f"/management/sales/new/{item_id}")
            sale_result = client.post(
                f"/management/sales/new/{item_id}",
                data=sale_payload,
            )

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(sale_form.status_code, 200)
        self.assertIn(b"Mark as Sold", sale_form.data)
        self.assertEqual(sale_result.status_code, 302)
        self.assertTrue(sale_result.headers["Location"].endswith("/management/items"))
        self.assertEqual(items.docs[0]["status"], "SOLD")
        self.assertEqual(len(items.docs[0]["sold_record"]), 1)

    def test_management_cannot_sell_hidden_admin_status(self):
        item_id = ObjectId()
        created_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
        items = FakeInventory([{
            "_id": item_id,
            "sku": "ADMIN03",
            "name": "Admin Inbound Item",
            "ownership": OWNERSHIP_ADMIN,
            "status": "INBOUND",
            "created_at": created_at,
            "purchase_at": created_at,
            "audit": [],
        }])
        app = self._make_app(items)
        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            response = client.get(f"/management/sales/new/{item_id}")
        self.assertEqual(response.status_code, 404)

    def test_management_only_sees_latest_fifty_admin_sales(self):
        base_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        docs = []
        for index in range(51):
            docs.append({
                "_id": ObjectId(),
                "sku": f"SOLD{index:03d}",
                "name": f"Sold Admin Item {index}",
                "brand": "Dior",
                "ownership": OWNERSHIP_ADMIN,
                "status": "SOLD",
                "created_at": base_date,
                "purchase_at": base_date,
                "sold_record": [{
                    "sold_at": base_date + timedelta(days=index),
                    "sold_price": 1000,
                }],
                "audit": [],
            })
        items = FakeInventory(docs)
        app = self._make_app(items)
        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            response = client.get("/management/items?status=SOLD")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SOLD050", response.data)
        self.assertNotIn(b"SOLD000", response.data)

    def test_admin_items_are_sorted_by_displayed_date_not_sold_date(self):
        base_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        items = FakeInventory([
            {
                "_id": ObjectId(), "sku": "OLDSOLD", "name": "Old Sold",
                "brand": "Dior", "ownership": OWNERSHIP_ADMIN,
                "status": "SOLD", "created_at": base_date,
                "purchase_at": base_date,
                "sold_record": [{
                    "sold_at": base_date + timedelta(days=200),
                    "sold_price": 1000,
                }],
            },
            {
                "_id": ObjectId(), "sku": "MIDSHLF", "name": "Middle Shelf",
                "brand": "Chanel", "ownership": OWNERSHIP_ADMIN,
                "status": "ON_SHELF",
                "created_at": base_date + timedelta(days=60),
                "purchase_at": base_date + timedelta(days=60),
            },
            {
                "_id": ObjectId(), "sku": "NEWRECV", "name": "New Received",
                "brand": "Hermes", "ownership": OWNERSHIP_ADMIN,
                "status": "RECEIVED",
                "created_at": base_date + timedelta(days=120),
                "purchase_at": base_date + timedelta(days=120),
            },
        ])
        app = self._make_app(items)
        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            response = client.get("/management/items")

        self.assertEqual(response.status_code, 200)
        self.assertLess(response.data.index(b"NEWRECV"), response.data.index(b"MIDSHLF"))
        self.assertLess(response.data.index(b"MIDSHLF"), response.data.index(b"OLDSOLD"))

    def test_management_has_full_edit_fields_for_own_item(self):
        item_id = ObjectId()
        created_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
        items = FakeInventory([{
            "_id": item_id,
            "sku": "MGMT002",
            "name": "Old Name",
            "brand": "Dior",
            "ownership": OWNERSHIP_MANAGEMENT,
            "status": "ON_SHELF",
            "created_at": created_at,
            "purchase_at": created_at,
            "cost": 1000,
            "cost_currency": "SGD",
            "audit": [],
        }])
        app = self._make_app(items)
        payload = {
            "name": "New Name",
            "brand_select": "Chanel",
            "brand_custom": "",
            "name_in_EN": "New English Name",
            "seller_name": "New Seller",
            "seller_contact": "123456",
            "cost": "2000",
            "serial_code": "SERIAL",
            "tracking_number": "TRACKING",
            "accessories": "Box",
            "listing_price": "3000",
            "listing_currency": "SGD",
            "profit": "500",
            "profit_currency": "RMB",
            "note": "New note",
            "additional_notes_for_agreements": "Agreement note",
        }
        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            detail = client.get(f"/management/items/{item_id}")
            response = client.post(f"/management/items/{item_id}/update", data=payload)

        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Seller / Consignor Contact", detail.data)
        self.assertIn(b"Listing Price", detail.data)
        self.assertIn(b"Profit Currency", detail.data)
        self.assertEqual(response.status_code, 302)
        updated = items.docs[0]
        self.assertEqual(updated["name"], "New Name")
        self.assertEqual(updated["brand"], "Chanel")
        self.assertEqual(updated["seller_name"], "New Seller")
        self.assertEqual(updated["seller_contact"], "123456")
        self.assertEqual(updated["cost"], 2000)
        self.assertEqual(updated["listing_price"], 3000)
        self.assertEqual(updated["profit"], 500)
        self.assertEqual(updated["status"], "ON_SHELF")
        self.assertEqual(updated["ownership"], OWNERSHIP_MANAGEMENT)


if __name__ == "__main__":
    unittest.main()
