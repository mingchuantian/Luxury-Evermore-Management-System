import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from luxury_app import items_view_maintenance as maintenance
from luxury_app.auth import ROLE_ADMIN, ROLE_STAFF
from luxury_app.routes.dashboard import register as register_dashboard
from fix_items_view_negative_profits import fix_negative_profits


class FakeLogs:
    def __init__(self):
        self.finished = None

    def insert_one(self, document):
        self.started = document
        return SimpleNamespace(inserted_id="log-1")

    def update_one(self, query, update):
        self.finished = update["$set"]
        return SimpleNamespace(modified_count=1)


class FakeLocks:
    def __init__(self):
        self.released = None

    def update_one(self, query, update):
        self.released = update
        return SimpleNamespace(modified_count=1)


class FakeOpenAIResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class ItemsViewMaintenanceTests(unittest.TestCase):
    def test_new_profit_is_always_positive(self):
        with patch(
            "luxury_app.items_view_maintenance.random.randint",
            return_value=12,
        ):
            self.assertEqual(maintenance._positive_profit(1000), 120)

    def test_correction_script_only_updates_negative_profit(self):
        items_view = MagicMock()
        items_view.update_many.return_value = SimpleNamespace(modified_count=7)

        changed = fix_negative_profits(items_view)

        self.assertEqual(changed, 7)
        query, pipeline = items_view.update_many.call_args.args
        self.assertEqual(query["status"], "SOLD")
        self.assertEqual(
            pipeline,
            [{"$set": {"profit": {"$abs": "$profit"}}}],
        )

    def test_invalid_translation_output_is_retried_before_succeeding(self):
        responses = [
            FakeOpenAIResponse({"output_text": "香奈儿 CF"}),
            FakeOpenAIResponse({"output_text": ""}),
            FakeOpenAIResponse({"output_text": "Chanel Classic Flap"}),
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False), patch(
            "luxury_app.items_view_maintenance.requests.post",
            side_effect=responses,
        ) as post:
            translated = maintenance.translate_product_name("香奈儿 CF")

        self.assertEqual(translated, "Chanel Classic Flap")
        self.assertEqual(post.call_count, 3)

    def test_sold_price_maintenance_uses_cost_plus_profit_on_latest_sale(self):
        items_view = MagicMock()
        items_view.update_many.return_value = SimpleNamespace(modified_count=3)

        changed = maintenance._update_sold_prices(items_view)

        self.assertEqual(changed, 3)
        query, pipeline = items_view.update_many.call_args.args
        self.assertEqual(query["status"], "SOLD")
        self.assertEqual(query["changed_sold_price"], {"$ne": True})
        set_fields = pipeline[0]["$set"]
        self.assertTrue(set_fields["changed_sold_price"])
        latest_updates = set_fields["sold_record"]["$concatArrays"][1][0]
        replacement = latest_updates["$mergeObjects"][1]
        self.assertEqual(replacement["sold_price"], {"$add": ["$cost", "$profit"]})
        self.assertEqual(replacement["sold_currency"], "SGD")

    def test_extracts_text_from_raw_responses_api_payload(self):
        payload = {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "Lady Dior Medium"}],
            }]
        }
        self.assertEqual(
            maintenance._extract_response_text(payload),
            "Lady Dior Medium",
        )

    def test_missing_openai_key_is_a_clear_retryable_maintenance_error(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(
                maintenance.ItemsViewMaintenanceError,
                "OPENAI_API_KEY",
            ):
                maintenance.translate_product_name("迪奥五格")

    def test_maintenance_failure_is_logged_and_never_escapes_to_flask(self):
        logs = FakeLogs()
        locks = FakeLocks()
        with patch.object(
            maintenance,
            "maintain_items_view",
            side_effect=RuntimeError("simulated maintenance failure"),
        ):
            result = maintenance.run_items_view_maintenance(
                object(),
                object(),
                logs,
                locks,
                triggered_by="scheduled",
                acquired_owner="owner-1",
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("simulated maintenance failure", result["message"])
        self.assertEqual(logs.finished["status"], "failed")
        self.assertIsNotNone(logs.finished["finished_at"])
        self.assertIsNotNone(locks.released)

    def test_quality_failure_is_logged_as_partial_completion(self):
        logs = FakeLogs()
        locks = FakeLocks()
        summary = {
            "name_failed": 1,
            "translation_fatal": False,
            "translation_error": "translation validation failed",
        }
        with patch.object(
            maintenance,
            "maintain_items_view",
            return_value=summary,
        ):
            result = maintenance.run_items_view_maintenance(
                object(),
                object(),
                logs,
                locks,
                triggered_by="scheduled",
                acquired_owner="owner-1",
            )

        self.assertEqual(result["status"], "warning")
        self.assertEqual(logs.finished["status"], "warning")

    def test_safe_error_scrubs_database_credentials_and_api_keys(self):
        error = RuntimeError(
            "mongodb+srv://user:password@example.test/ sk-secretvalue123"
        )
        message = maintenance._safe_error(error)
        self.assertNotIn("user:password", message)
        self.assertNotIn("secretvalue123", message)

    def test_manual_trigger_failure_does_not_escape_to_the_request(self):
        with patch.object(
            maintenance,
            "_acquire_job_lease",
            side_effect=RuntimeError("database temporarily unavailable"),
        ):
            started = maintenance.trigger_items_view_maintenance(
                object(), object(), object(), object(), triggered_by="admin:test"
            )
        self.assertFalse(started)

    def test_manual_route_is_admin_only_and_starts_background_work(self):
        app = Flask(__name__)
        app.secret_key = "test-secret"
        dependencies = [object(), object(), object(), object()]
        register_dashboard(
            app,
            object(),
            maintenance_items=dependencies[0],
            maintenance_items_view=dependencies[1],
            maintenance_logs=dependencies[2],
            background_jobs=dependencies[3],
        )

        with app.test_client() as client, patch(
            "luxury_app.routes.dashboard.trigger_items_view_maintenance",
            return_value=True,
        ) as trigger:
            with client.session_transaction() as session:
                session["role"] = ROLE_ADMIN
                session["username"] = "owner"
            response = client.post("/admin/items-view-maintenance/run")
            self.assertEqual(response.status_code, 302)
            trigger.assert_called_once()

            with client.session_transaction() as session:
                session["role"] = ROLE_STAFF
            response = client.post("/admin/items-view-maintenance/run")
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
