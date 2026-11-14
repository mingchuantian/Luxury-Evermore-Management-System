"""店内陈列 / 老板间 inventory checks, website prompt, and the lost-bag comparison."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_ADMIN, ROLE_STAFF
from luxury_app.routes.inventory_check import register
from tests.test_ownership import FakeInventory


class Coll(FakeInventory):
    def find(self, query, projection=None):
        return super().find(query)

    def find_one(self, query, projection=None):
        return super().find_one(query)


T0 = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
LAYOUT_ENDPOINTS = (
    "index", "logout", "login", "list_items", "item_new_buyin_form",
    "item_new_consignment_form", "analytics", "audit_history", "notes_list",
    "users_list", "admin_management_payments", "management_index",
    "management_list_items", "management_item_new_buyin_form",
    "management_item_new_consignment_form",
)


def _item(sku, status, linked=False, **extra):
    doc = {"_id": ObjectId(), "sku": sku, "name": f"bag {sku}", "brand": "Chanel",
           "serial_code": "", "status": status, "shopify_sku_exist": linked,
           "created_at": T0 - timedelta(days=30), "received_at": T0 - timedelta(days=30)}
    doc.update(extra)
    return doc


def _session(check_type, skus, status="completed", started=T0):
    return {
        "_id": ObjectId(), "status": status, "check_type": check_type,
        "started_at": started, "completed_at": started + timedelta(hours=1),
        "started_by": {"username": "boss"}, "scanned_skus": list(skus),
        "scans": [{"sku": sku, "name": f"bag {sku}", "status": "X",
                   "scanned_at": started} for sku in skus],
    }


class LocationCheckTests(unittest.TestCase):
    def _app(self, items, sessions):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda value: str(value or "")
        for endpoint in LAYOUT_ENDPOINTS:
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        app.add_url_rule("/items/<item_key>", "item_detail", lambda item_key: "x")
        register(app, items, sessions)
        return app

    def _client(self, app, role=ROLE_ADMIN):
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="boss")
        return client

    def test_start_requires_a_type(self):
        sessions = MagicMock()
        sessions.insert_one.return_value = SimpleNamespace(inserted_id=ObjectId())
        client = self._client(self._app(MagicMock(), sessions))
        self.assertEqual(client.post("/inventory-check/sessions").status_code, 400)
        self.assertEqual(
            client.post("/inventory-check/sessions", data={"check_type": "x"}).status_code, 400
        )
        client.post("/inventory-check/sessions", data={"check_type": "room"})
        self.assertEqual(sessions.insert_one.call_args.args[0]["check_type"], "room")

    def test_scan_reports_whether_item_is_on_shopify(self):
        items = MagicMock()
        items.find_one.return_value = {"sku": "AAA1111", "name": "Bag", "status": "RECEIVED",
                                       "shopify_sku_exist": False}
        sessions = MagicMock()
        sessions.update_one.return_value = SimpleNamespace(modified_count=1)
        sessions.find_one.return_value = {"scanned_skus": ["AAA1111"]}
        client = self._client(self._app(items, sessions))
        response = client.post(f"/inventory-check/sessions/{ObjectId()}/scan", json={"sku": "AAA1111"})
        self.assertFalse(response.json["shopify_linked"])
        # Nothing extra is stored on the scan record.
        self.assertNotIn("shopify_linked", sessions.update_one.call_args.args[1]["$push"]["scans"])
        items.find_one.return_value["shopify_sku_exist"] = True
        sessions.find_one.return_value = {"status": "active", "scanned_skus": ["AAA1111"]}
        sessions.update_one.return_value = SimpleNamespace(modified_count=0)
        response = client.post(f"/inventory-check/sessions/{ObjectId()}/scan", json={"sku": "AAA1111"})
        self.assertTrue(response.json["shopify_linked"])

    def test_active_store_page_has_website_prompt_script(self):
        check = _session("store", [], status="active")
        client = self._client(self._app(Coll([]), Coll([check])))
        html = client.get(f"/inventory-check/sessions/{check['_id']}").get_data(as_text=True)
        self.assertIn("const isStoreCheck = true;", html)
        self.assertIn("⚠️ 该商品未上website", html)
        self.assertIn("店内陈列盘查", html)

    def test_typed_complete_freezes_nothing_but_status(self):
        check = _session("room", ["A"], status="active")
        sessions = Coll([check])
        client = self._client(self._app(Coll([]), sessions))
        client.post(f"/inventory-check/sessions/{check['_id']}/complete")
        doc = sessions.docs[0]
        self.assertEqual(doc["status"], "completed")
        self.assertNotIn("missing_items", doc)
        self.assertNotIn("eligible_count", doc)

    def test_store_result_lists_not_on_website_live(self):
        items = Coll([
            _item("ON00001", "ON_SHELF", linked=True),
            _item("OFF0001", "RECEIVED", linked=False),
        ])
        check = _session("store", ["ON00001", "OFF0001"])
        client = self._client(self._app(items, Coll([check])))
        html = client.get(f"/inventory-check/sessions/{check['_id']}").get_data(as_text=True)
        section = html.split("店内陈列但未上 website 的商品", 1)[1].split("本次全部扫描记录", 1)[0]
        self.assertIn("OFF0001", section)
        self.assertNotIn("ON00001", section)
        self.assertIn("其中未上 website", html)
        # After it is listed (hourly sync marks it linked), it disappears.
        items.docs[1]["shopify_sku_exist"] = True
        html = client.get(f"/inventory-check/sessions/{check['_id']}").get_data(as_text=True)
        self.assertIn("店内扫到的商品都已经在 Shopify 上", html)

    def test_room_result_has_no_website_section(self):
        check = _session("room", ["OFF0001"])
        client = self._client(self._app(Coll([_item("OFF0001", "RECEIVED")]), Coll([check])))
        html = client.get(f"/inventory-check/sessions/{check['_id']}").get_data(as_text=True)
        self.assertIn("老板间盘查", html)
        self.assertNotIn("未上 website 的商品", html)

    def test_history_offers_both_starts_and_comparison(self):
        store, room = _session("store", ["A"]), _session("room", ["B"])
        legacy = dict(_session("room", []), check_type=None, missing_count=3)
        legacy.pop("check_type")
        client = self._client(self._app(Coll([]), Coll([store, room, legacy])))
        html = client.get("/inventory-check").get_data(as_text=True)
        self.assertIn("开始店内陈列盘查", html)
        self.assertIn("开始老板间盘查", html)
        self.assertIn(f'<option value="{store["_id"]}">', html)
        self.assertIn(f'<option value="{room["_id"]}">', html)
        self.assertIn("旧盘查", html)

    def test_compare_lists_in_stock_items_scanned_nowhere(self):
        items = Coll([
            _item("STORE01", "ON_SHELF"),                 # scanned in store
            _item("ROOM001", "RECEIVED"),                 # scanned in room
            _item("LOST001", "RECEIVED"),                 # nowhere -> lost
            _item("LOST002", "ON_SHELF"),                 # nowhere -> lost
            _item("LATE001", "RECEIVED", received_at=T0 + timedelta(days=1)),  # arrived later
            _item("SOLD001", "SOLD"),                     # not in stock
            _item("REPR001", "REPARING"),                 # not in stock
        ])
        store = _session("store", ["STORE01", "BOTH001"])
        room = _session("room", ["ROOM001", "BOTH001"], started=T0 + timedelta(hours=2))
        sessions = Coll([store, room])
        before = [dict(d) for d in items.docs]
        client = self._client(self._app(items, sessions))
        html = client.get(f"/inventory-check/compare?store={store['_id']}&room={room['_id']}").get_data(as_text=True)
        self.assertIn("3 件两处都没扫到", html)
        for sku in ("LOST001", "LOST002", "LATE001"):
            self.assertIn(sku, html)
        for sku in ("STORE01", "ROOM001", "SOLD001", "REPR001"):
            self.assertNotIn(f">{sku}</a>", html)
        self.assertIn("到货时间晚于盘查开始", html)
        self.assertIn("BOTH001", html)
        self.assertEqual(items.docs, before)   # read-only

    def test_compare_requires_one_of_each_completed_type(self):
        store, room = _session("store", []), _session("room", [])
        active_room = _session("room", [], status="active")
        client = self._client(self._app(Coll([]), Coll([store, room, active_room])))
        self.assertEqual(client.get(f"/inventory-check/compare?store={room['_id']}&room={store['_id']}").status_code, 400)
        self.assertEqual(client.get(f"/inventory-check/compare?store={store['_id']}&room={active_room['_id']}").status_code, 400)
        self.assertEqual(client.get("/inventory-check/compare").status_code, 400)

    def test_staff_blocked(self):
        client = self._client(self._app(Coll([]), Coll([])), role=ROLE_STAFF)
        self.assertEqual(client.get("/inventory-check/compare").status_code, 403)


if __name__ == "__main__":
    unittest.main()
