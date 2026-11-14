import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_ADMIN, ROLE_STAFF
from luxury_app.routes.inventory_check import register


class InventoryCheckTests(unittest.TestCase):
    def _app(self, items=None, sessions=None):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret"
        self.items = items or MagicMock()
        self.sessions = sessions or MagicMock()
        register(app, self.items, self.sessions)
        return app

    @staticmethod
    def _login(client, role=ROLE_ADMIN):
        with client.session_transaction() as flask_session:
            flask_session["role"] = role
            flask_session["user_id"] = str(ObjectId())
            flask_session["username"] = "tester"

    def test_start_creates_persistent_active_session(self):
        oid = ObjectId()
        sessions = MagicMock()
        sessions.insert_one.return_value = SimpleNamespace(inserted_id=oid)
        app = self._app(sessions=sessions)

        with app.test_client() as client:
            self._login(client)
            response = client.post(
                "/inventory-check/sessions", data={"check_type": "store"}
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(str(oid), response.headers["Location"])
        document = sessions.insert_one.call_args.args[0]
        self.assertEqual(document["status"], "active")
        self.assertEqual(document["check_type"], "store")
        self.assertEqual(document["scanned_skus"], [])
        self.assertEqual(document["started_by"]["username"], "tester")

    def test_scan_is_atomically_recorded_only_when_sku_is_new(self):
        oid = ObjectId()
        items = MagicMock()
        items.find_one.return_value = {
            "sku": "ABC1234",
            "name": "Dior Bag",
            "brand": "Dior",
            "serial_code": "SERIAL1",
            "status": "RECEIVED",
        }
        sessions = MagicMock()
        sessions.update_one.return_value = SimpleNamespace(modified_count=1)
        sessions.find_one.return_value = {"scanned_skus": ["ABC1234"]}
        app = self._app(items=items, sessions=sessions)

        with app.test_client() as client:
            self._login(client)
            response = client.post(
                f"/inventory-check/sessions/{oid}/scan",
                json={"sku": "abc1234"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["duplicate"])
        self.assertEqual(response.json["scan_count"], 1)
        query, update = sessions.update_one.call_args.args
        self.assertEqual(query["scanned_skus"], {"$ne": "ABC1234"})
        self.assertEqual(update["$addToSet"], {"scanned_skus": "ABC1234"})
        self.assertEqual(update["$push"]["scans"]["status"], "RECEIVED")

    def test_duplicate_scan_does_not_add_another_record(self):
        oid = ObjectId()
        items = MagicMock()
        items.find_one.return_value = {
            "sku": "ABC1234", "name": "Bag", "status": "RECEIVED",
        }
        sessions = MagicMock()
        sessions.update_one.return_value = SimpleNamespace(modified_count=0)
        sessions.find_one.return_value = {
            "status": "active", "scanned_skus": ["ABC1234"],
        }
        app = self._app(items=items, sessions=sessions)

        with app.test_client() as client:
            self._login(client)
            response = client.post(
                f"/inventory-check/sessions/{oid}/scan",
                json={"sku": "ABC1234"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["duplicate"])
        self.assertEqual(response.json["scan_count"], 1)

    def test_complete_freezes_received_items_that_were_not_scanned(self):
        oid = ObjectId()
        items = MagicMock()
        items.find.return_value = [
            {"sku": "FOUND01", "name": "Found", "status": "RECEIVED"},
            {"sku": "MISS001", "name": "Missing", "status": "RECEIVED"},
        ]
        sessions = MagicMock()
        sessions.find_one.return_value = {
            "_id": oid,
            "status": "active",
            "scanned_skus": ["FOUND01", "SHELF01"],
        }
        app = self._app(items=items, sessions=sessions)

        with app.test_client() as client:
            self._login(client)
            response = client.post(
                f"/inventory-check/sessions/{oid}/complete"
            )

        self.assertEqual(response.status_code, 302)
        query, update = sessions.update_one.call_args.args
        self.assertEqual(query, {"_id": oid, "status": "active"})
        result = update["$set"]
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["received_scanned_count"], 1)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["missing_items"][0]["sku"], "MISS001")

    def test_staff_cannot_open_or_mutate_inventory_check(self):
        oid = ObjectId()
        app = self._app()
        with app.test_client() as client:
            self._login(client, ROLE_STAFF)
            page = client.get("/inventory-check")
            scan = client.post(
                f"/inventory-check/sessions/{oid}/scan",
                json={"sku": "ABC1234"},
            )

        self.assertEqual(page.status_code, 403)
        self.assertEqual(scan.status_code, 403)
        self.sessions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
