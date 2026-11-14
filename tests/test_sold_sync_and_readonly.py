"""Shopify zero-on-sale, Management read-only sold items, and no SOLD via edit."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

from luxury_app import shopify_inventory
from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.constants import OWNERSHIP_ADMIN, OWNERSHIP_MANAGEMENT
from luxury_app.routes.items import register as register_admin_items
from luxury_app.routes.management import register as register_management
from luxury_app.routes.sales import register as register_sales
from tests.test_ownership import FakeInventory


CREATED = datetime(2026, 10, 6, tzinfo=timezone.utc)


class FakeProvider:
    shop = "example.myshopify.com"

    def __init__(self, error=None):
        self.error = error

    def configuration_error(self):
        return self.error


def _variant(sku="SKU1234", tracked=True, levels=((1, "gid://shopify/Location/1"),)):
    return {"productVariants": {"edges": [{"node": {
        "id": "gid://shopify/ProductVariant/9",
        "sku": sku,
        "inventoryItem": {
            "id": "gid://shopify/InventoryItem/7",
            "tracked": tracked,
            "inventoryLevels": {"edges": [
                {"node": {
                    "location": {"id": location, "name": "Store"},
                    "quantities": [{"name": "available", "quantity": qty}],
                }}
                for qty, location in levels
            ]},
        },
    }}]}}


def _mutation_ok():
    return {"inventorySetQuantities": {
        "inventoryAdjustmentGroup": {"changes": []}, "userErrors": [],
    }}


class ZeroShopifyInventoryTests(unittest.TestCase):
    def _run(self, responses, provider=None):
        calls = []

        def fake_gql(query, variables, token_provider=None):
            calls.append((query, variables))
            return responses.pop(0)

        with patch.object(shopify_inventory, "shopify_gql", side_effect=fake_gql):
            result = shopify_inventory.zero_shopify_inventory_for_sku(
                "SKU1234", token_provider=provider or FakeProvider()
            )
        return result, calls

    def test_sets_available_to_zero_with_cas_and_idempotency_key(self):
        result, calls = self._run([
            _variant(levels=((1, "gid://shopify/Location/1"), (0, "gid://shopify/Location/2"))),
            _mutation_ok(),
        ])
        self.assertEqual(result["status"], "zeroed")
        self.assertTrue(result["ok"])
        query, variables = calls[1]
        self.assertIn("@idempotent(key: $idempotencyKey)", query)
        self.assertTrue(variables["idempotencyKey"])
        self.assertEqual(variables["input"]["name"], "available")
        # Only the location that still has stock is changed, using compare-and-swap.
        self.assertEqual(variables["input"]["quantities"], [{
            "inventoryItemId": "gid://shopify/InventoryItem/7",
            "locationId": "gid://shopify/Location/1",
            "quantity": 0,
            "changeFromQuantity": 1,
        }])

    def test_already_zero_does_not_call_mutation(self):
        result, calls = self._run([_variant(levels=((0, "gid://shopify/Location/1"),))])
        self.assertEqual(result["status"], "already_zero")
        self.assertEqual(len(calls), 1)

    def test_sku_not_in_shopify(self):
        result, _ = self._run([{"productVariants": {"edges": []}}])
        self.assertEqual(result["status"], "not_found")
        self.assertTrue(result["ok"])

    def test_untracked_inventory_is_reported(self):
        result, calls = self._run([_variant(tracked=False)])
        self.assertEqual(result["status"], "not_tracked")
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_stale_quantity_is_reread_once(self):
        stale = {"inventorySetQuantities": {"userErrors": [
            {"code": "CHANGE_FROM_QUANTITY_STALE", "field": [], "message": "stale"},
        ]}}
        result, calls = self._run([
            _variant(levels=((1, "gid://shopify/Location/1"),)), stale,
            _variant(levels=((2, "gid://shopify/Location/1"),)), _mutation_ok(),
        ])
        self.assertEqual(result["status"], "zeroed")
        self.assertEqual(calls[3][1]["input"]["quantities"][0]["changeFromQuantity"], 2)

    def test_errors_are_returned_not_raised(self):
        def boom(*args, **kwargs):
            raise RuntimeError("Shopify GraphQL errors: ACCESS_DENIED")

        with patch.object(shopify_inventory, "shopify_gql", side_effect=boom):
            result = shopify_inventory.zero_shopify_inventory_for_sku(
                "SKU1234", token_provider=FakeProvider()
            )
        self.assertEqual(result["status"], "error")
        self.assertIn("write_inventory", result["message"])

    def test_not_configured(self):
        result, calls = self._run([], provider=FakeProvider(error="missing creds"))
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(calls, [])


def _item(**extra):
    doc = {
        "_id": ObjectId(), "sku": "SKU1234", "name": "Bag", "brand": "Chanel",
        "ownership": OWNERSHIP_ADMIN, "status": "ON_SHELF",
        "created_at": CREATED, "purchase_at": CREATED, "cost": 100,
        "listing_price": 1000, "listing_currency": "SGD",
        "sold_record": [], "audit": [],
    }
    doc.update(extra)
    return doc


SALE = {
    "action": "sell", "sold_price": "1200", "sold_currency": "SGD",
    "sold_at": "2026-10-06T12:00:00", "tz_offset_min": "-480",
    "buyer": "Buyer", "receipt_no": "R-1", "package_inclusion": "Bag",
}


class RouteTests(unittest.TestCase):
    def _app(self, items):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        for path, endpoint in (
            ("/", "index"), ("/logout", "logout"), ("/analytics", "analytics"),
            ("/audit", "audit_history"), ("/notes", "notes_list"),
            ("/users", "users_list"),
        ):
            app.add_url_rule(path, endpoint, lambda: "placeholder")
        register_admin_items(app, items)
        register_management(app, items)
        register_sales(app, items)
        return app

    @staticmethod
    def _login(client, role):
        with client.session_transaction() as session:
            session["user_id"] = str(ObjectId())
            session["username"] = "u"
            session["role"] = role

    def _sell(self, role, item, url):
        items = FakeInventory([item])
        app = self._app(items)
        with patch(
            "luxury_app.routes.sales.zero_shopify_inventory_for_sku",
            return_value={"status": "zeroed", "ok": True, "message": ""},
        ) as zero, app.test_client() as client:
            self._login(client, role)
            response = client.post(url.format(id=item["_id"]), data=SALE)
        return items, zero, response

    def test_admin_sale_zeroes_shopify_inventory(self):
        item = _item()
        items, zero, response = self._sell(ROLE_ADMIN, item, "/sales/new/{id}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(items.docs[0]["status"], "SOLD")
        zero.assert_called_once_with("SKU1234")

    def test_management_sale_zeroes_shopify_inventory(self):
        item = _item(ownership=OWNERSHIP_MANAGEMENT, status="RECEIVED")
        items, zero, _ = self._sell(ROLE_MANAGEMENT, item, "/management/sales/new/{id}")
        self.assertEqual(items.docs[0]["status"], "SOLD")
        zero.assert_called_once_with("SKU1234")

    def test_admin_quick_sale_zeroes_shopify_inventory(self):
        item = _item()
        items = FakeInventory([item])
        app = self._app(items)
        with patch(
            "luxury_app.routes.sales.zero_shopify_inventory_for_sku",
            return_value={"status": "zeroed", "ok": True, "message": ""},
        ) as zero, app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            response = client.post(f"/items/{item['_id']}/action/sale")
        self.assertEqual(response.status_code, 204)
        zero.assert_called_once_with("SKU1234")

    def test_staff_sale_never_touches_shopify(self):
        item = _item()
        items, zero, _ = self._sell(ROLE_STAFF, item, "/sales/new/{id}")
        self.assertEqual(items.docs[0]["status"], "SOLD")
        zero.assert_not_called()

    def test_shopify_failure_keeps_sale_and_warns(self):
        item = _item()
        items = FakeInventory([item])
        app = self._app(items)
        with patch(
            "luxury_app.routes.sales.zero_shopify_inventory_for_sku",
            return_value={"status": "error", "ok": False, "message": "timeout"},
        ), app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            client.post(f"/sales/new/{item['_id']}", data=SALE)
            with client.session_transaction() as session:
                flashes = session.get("_flashes", [])
        self.assertEqual(items.docs[0]["status"], "SOLD")
        self.assertTrue(any("Shopify" in text and cat == "error" for cat, text in flashes))

    def test_management_sold_item_is_read_only(self):
        item = _item(ownership=OWNERSHIP_MANAGEMENT, status="SOLD", sold_record=[{
            "sold_at": CREATED, "sold_price": 1200, "sold_currency": "SGD",
        }])
        items = FakeInventory([item])
        app = self._app(items)
        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            detail = client.get(f"/management/items/{item['_id']}")
            listing = client.get("/management/items")
            update = client.post(
                f"/management/items/{item['_id']}/update",
                data={"name": "Changed", "cost": "1"},
            )
            delete = client.post(f"/management/items/{item['_id']}/action/delete")

        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"read-only", detail.data)
        self.assertNotIn(b"Update Information", detail.data)
        self.assertNotIn(b"deleteManagementItem(\"", detail.data)
        self.assertNotIn(b">Delete</button>", listing.data)
        self.assertIn(b">View</button>", listing.data)
        self.assertEqual(update.status_code, 302)
        self.assertEqual(delete.status_code, 403)
        self.assertEqual(items.docs[0]["name"], "Bag")
        self.assertEqual(items.docs[0]["cost"], 100)

    def test_management_unsold_item_still_editable(self):
        item = _item(ownership=OWNERSHIP_MANAGEMENT, status="RECEIVED")
        items = FakeInventory([item])
        app = self._app(items)
        with app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            detail = client.get(f"/management/items/{item['_id']}")
            delete = client.post(f"/management/items/{item['_id']}/action/delete")
        self.assertIn(b"Update Information", detail.data)
        self.assertEqual(delete.status_code, 204)

    def test_edit_page_status_dropdown_has_no_sold(self):
        item = _item(status="ON_SHELF")
        app = self._app(FakeInventory([item]))
        with app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            page = client.get(f"/items/{item['_id']}").get_data(as_text=True)
        self.assertIn('<select name="status">', page)
        select = page.split('<select name="status">', 1)[1].split("</select>", 1)[0]
        self.assertNotIn('value="SOLD"', select)
        self.assertIn('value="RESERVED"', select)

    def test_edit_form_cannot_set_sold(self):
        item = _item(status="RESERVED")
        items = FakeInventory([item])
        app = self._app(items)
        with app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            response = client.post(
                f"/items/{item['_id']}/update",
                data={"status": "SOLD", "name": "Bag", "brand_select": "Chanel"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(items.docs[0]["status"], "RESERVED")
        self.assertEqual(items.docs[0]["sold_record"], [])


if __name__ == "__main__":
    unittest.main()
