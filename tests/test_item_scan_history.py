"""盘查记录 column in 库存列表 and section in item detail (derived, read-only)."""

import unittest
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_ADMIN, ROLE_STAFF
from luxury_app.inventory_scan_history import scan_history_for_skus
from luxury_app.routes.inventory_check import register as register_inventory_check
from luxury_app.routes.items import register as register_items


T0 = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)


class FakeSessions:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        wanted = set(query["scanned_skus"]["$in"])
        return [d for d in self.docs if wanted & set(d.get("scanned_skus") or [])]


def _check(check_type, scans, status="completed", started=T0, completed=None):
    doc = {
        "_id": ObjectId(), "status": status, "started_at": started,
        "completed_at": completed or (started + timedelta(hours=1)),
        "scanned_skus": [sku for sku, _ in scans],
        "scans": [{"sku": sku, **({"scanned_at": at} if at else {})} for sku, at in scans],
    }
    if check_type:
        doc["check_type"] = check_type
    return doc


class ScanHistoryTests(unittest.TestCase):
    def test_newest_first_with_location_and_real_scan_time(self):
        store = _check("store", [("A", T0 + timedelta(minutes=5)), ("B", T0)])
        room = _check("room", [("A", T0 + timedelta(days=1, minutes=3))], started=T0 + timedelta(days=1))
        legacy = _check(None, [("A", None)], started=T0 - timedelta(days=30))
        active = _check("store", [("C", T0 + timedelta(days=2))], status="active",
                        started=T0 + timedelta(days=2))
        history = scan_history_for_skus(FakeSessions([store, room, legacy, active]), ["A", "C", "Z"])

        self.assertEqual([r["location"] for r in history["A"]], ["老板间", "店内陈列", "旧盘查（未分地点）"])
        self.assertEqual(history["A"][0]["at"], T0 + timedelta(days=1, minutes=3))
        # Legacy scan without its own time falls back to the session end time.
        self.assertEqual(history["A"][2]["at"], legacy["completed_at"])
        self.assertTrue(history["C"][0]["active"])
        self.assertNotIn("Z", history)
        self.assertNotIn("B", history)  # not requested

    def test_no_sessions(self):
        self.assertEqual(scan_history_for_skus(None, ["A"]), {})
        self.assertEqual(scan_history_for_skus(FakeSessions([]), []), {})


class RouteTests(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        self.db = mongomock.MongoClient()["t"]
        self.db.items.insert_many([
            {"sku": "SCAN001", "name": "Scanned", "status": "RECEIVED", "created_at": T0, "note": "n1"},
            {"sku": "NEVER01", "name": "Never", "status": "RECEIVED", "created_at": T0},
        ])
        self.store = _check("store", [("SCAN001", T0 + timedelta(minutes=5))])
        self.room = _check("room", [("SCAN001", T0 + timedelta(days=1))], started=T0 + timedelta(days=1))
        self.db.inventory_check_sessions.insert_many([self.store, self.room])

    def _client(self, role):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in (
            "index", "logout", "login", "analytics", "audit_history", "notes_list",
            "users_list", "admin_management_payments", "management_index",
            "management_list_items", "management_item_new_buyin_form",
            "management_item_new_consignment_form", "sale_new_form",
        ):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        register_items(app, self.db.items, inventory_check_sessions=self.db.inventory_check_sessions)
        register_inventory_check(app, self.db.items, self.db.inventory_check_sessions)
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="u")
        return client

    def test_list_shows_latest_scan_next_to_note(self):
        html = self._client(ROLE_ADMIN).get("/items").get_data(as_text=True)
        self.assertIn("<th>备注</th><th>盘查记录</th>", html)
        scanned_row = html.split("SCAN001", 1)[1].split("</tr>", 1)[0]
        self.assertIn("2026-11-11 10:00", scanned_row)
        self.assertIn("老板间", scanned_row)
        never_row = html.split("NEVER01", 1)[1].split("</tr>", 1)[0]
        self.assertIn("无", never_row)

    def test_detail_lists_every_scan_above_audit(self):
        html = self._client(ROLE_ADMIN).get("/items/SCAN001").get_data(as_text=True)
        section = html.split("<h3>盘查记录</h3>", 1)[1].split("<h3>操作记录</h3>", 1)[0]
        self.assertIn("2026-11-11 10:00 - 老板间", section)
        self.assertIn("2026-11-10 10:05 - 店内陈列", section)
        self.assertLess(section.index("老板间"), section.index("店内陈列"))
        self.assertIn(f"/inventory-check/sessions/{self.store['_id']}", section)
        never = self._client(ROLE_ADMIN).get("/items/NEVER01").get_data(as_text=True)
        self.assertIn('<h3>盘查记录</h3>\n\n<p class="muted">无</p>', never)

    def test_staff_does_not_see_scan_records(self):
        client = self._client(ROLE_STAFF)
        self.assertNotIn("盘查记录", client.get("/items").get_data(as_text=True))
        self.assertNotIn("盘查记录", client.get("/items/SCAN001").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


class ManagementPagesTests(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        from luxury_app.constants import OWNERSHIP_ADMIN, OWNERSHIP_MANAGEMENT

        self.db = mongomock.MongoClient()["t"]
        self.db.items.insert_many([
            {"sku": "MGMT001", "name": "Mgmt bag", "status": "RECEIVED",
             "ownership": OWNERSHIP_MANAGEMENT, "created_at": T0, "note": "n"},
            {"sku": "MGMT002", "name": "Mgmt never", "status": "RECEIVED",
             "ownership": OWNERSHIP_MANAGEMENT, "created_at": T0},
            {"sku": "ADMN001", "name": "Admin bag", "status": "ON_SHELF",
             "ownership": OWNERSHIP_ADMIN, "created_at": T0},
        ])
        self.db.inventory_check_sessions.insert_many([
            _check("store", [("MGMT001", T0 + timedelta(minutes=5)), ("ADMN001", T0)]),
            _check("room", [("MGMT001", T0 + timedelta(days=1))], started=T0 + timedelta(days=1)),
        ])

    def _client(self):
        from luxury_app.auth import ROLE_MANAGEMENT
        from luxury_app.routes.management import register as register_management

        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in ("index", "logout", "login", "list_items", "item_new_buyin_form",
                         "item_new_consignment_form", "analytics", "audit_history",
                         "notes_list", "users_list"):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        from luxury_app.routes.sales import register as register_sales

        register_management(app, self.db.items,
                            inventory_check_sessions=self.db.inventory_check_sessions)
        register_sales(app, self.db.items)
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=ROLE_MANAGEMENT, user_id=str(ObjectId()), username="m")
        return client

    def test_list_shows_column_for_both_tables(self):
        html = self._client().get("/management/items").get_data(as_text=True)
        self.assertEqual(html.count("<th>Note</th><th>Inventory Check</th>"), 2)

        def row(sku):
            return html.split(sku, 1)[1].split("</tr>", 1)[0]

        self.assertIn("2026-11-11 10:00", row("MGMT001"))
        self.assertIn("Back room", row("MGMT001"))
        self.assertIn("None", row("MGMT002"))
        self.assertIn("Store display", row("ADMN001"))
        # Admin rows keep the same number of cells as the header.
        admin_table = html.split("<h3>Admin Items</h3>", 1)[1]
        header_cells = admin_table.split("</tr>", 1)[0].count("<th")
        admin_row = admin_table.split("ADMN001", 1)[0].rsplit("<tr", 1)[1] + row("ADMN001")
        self.assertEqual(admin_row.count("<td"), header_cells)

    def test_detail_lists_every_scan(self):
        html = self._client().get("/management/items/MGMT001").get_data(as_text=True)
        section = html.split("<h3>Inventory Check Records</h3>", 1)[1].split("<hr>", 1)[0]
        self.assertIn("2026-11-11 10:00 - Back room", section)
        self.assertIn("2026-11-10 10:05 - Store display", section)
        self.assertNotIn("/inventory-check/", section)  # Management cannot open check pages
        never = self._client().get("/management/items/MGMT002").get_data(as_text=True)
        self.assertIn("<h3>Inventory Check Records</h3>", never)
        self.assertIn('<p class="muted">None</p>', never)
