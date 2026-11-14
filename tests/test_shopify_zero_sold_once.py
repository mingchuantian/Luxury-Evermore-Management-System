"""ONE-TIME TOOL tests. Remove together with luxury_app/shopify_zero_sold_once.py."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

from luxury_app import shopify_zero_sold_once as job
from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT
from luxury_app.routes.shopify_zero_sold_once import register
from tests.test_ownership import FakeInventory


class FakeProvider:
    shop = "71eaf7.myshopify.com"

    def configuration_error(self):
        return None


class FakeJobs:
    def __init__(self):
        self.doc = None

    def find_one(self, query):
        return deepcopy(self.doc)

    def update_one(self, query, update):
        self.doc.update(deepcopy(update.get("$set", {})))


def _variant(sku, qty, tracked=True, status="ACTIVE", pid="1"):
    return {
        "sku": sku, "inventoryQuantity": qty, "inventoryItem": {"tracked": tracked},
        "product": {"id": f"gid://shopify/Product/{pid}", "title": f"T-{sku}",
                    "handle": f"h-{sku.lower()}", "status": status},
    }


def _pages():
    return [
        {"productVariants": {
            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            "nodes": [
                _variant("SOLD001", 1, pid="11"),        # sold here, still in stock -> zero
                _variant("SOLD002", 0, pid="12"),        # sold here, already 0 -> skip
                _variant("LIVE001", 1, pid="13"),        # not sold here -> skip
            ],
        }},
        {"productVariants": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                _variant("SOLD003", 2, status="DRAFT", pid="14"),
                _variant("SOLD004", 0, tracked=False, pid="15"),  # untracked -> manual
            ],
        }},
    ]


class ProjectingInventory(FakeInventory):
    def find(self, query, projection=None):
        return super().find(query)


def _items():
    return ProjectingInventory([
        {"_id": ObjectId(), "sku": "SOLD001", "name": "A", "status": "SOLD", "sold_record": [{"sold_price": 1}]},
        {"_id": ObjectId(), "sku": "SOLD002", "name": "B", "status": "SOLD", "sold_record": [{"sold_price": 1}]},
        {"_id": ObjectId(), "sku": "SOLD003", "name": "C", "status": "SOLD", "sold_record": []},
        {"_id": ObjectId(), "sku": "SOLD004", "name": "D", "status": "SOLD", "sold_record": [{"sold_price": 1}]},
        {"_id": ObjectId(), "sku": "LIVE001", "name": "E", "status": "ON_SHELF"},
    ])


class ZeroSoldOnceTests(unittest.TestCase):
    def _run(self, dry_run):
        pages = _pages()
        cursors = []

        def gql(query, variables, token_provider=None):
            cursors.append(variables.get("cursor"))
            return pages.pop(0)

        jobs = FakeJobs()
        jobs.doc = {"_id": job.JOB_ID, "status": "running"}
        zero_calls = []

        def zero(sku):
            zero_calls.append(sku)
            return {"status": "zeroed", "ok": True, "message": ""}

        items = _items()
        before = deepcopy(items.docs)
        job.run_job(items, jobs, dry_run=dry_run, gql=gql, zero=zero,
                    provider=FakeProvider(), sleep_seconds=0)
        self.assertEqual(items.docs, before)  # system data never changes
        self.assertEqual(cursors, [None, "c1"])
        return jobs.doc, zero_calls

    def test_dry_run_lists_candidates_without_touching_shopify(self):
        doc, zero_calls = self._run(dry_run=True)
        self.assertEqual(zero_calls, [])
        self.assertEqual(doc["status"], "done")
        self.assertEqual(doc["sold_count"], 4)
        results = {row["sku"]: row for row in doc["results"]}
        self.assertEqual(sorted(results), ["SOLD001", "SOLD003", "SOLD004"])
        self.assertEqual(results["SOLD001"]["result"], "would_zero")
        self.assertEqual(results["SOLD004"]["result"], "not_tracked")
        self.assertFalse(results["SOLD003"]["has_sale_record"])
        self.assertEqual(
            results["SOLD001"]["admin_url"],
            "https://admin.shopify.com/store/71eaf7/products/11",
        )
        self.assertTrue(results["SOLD001"]["storefront_url"].endswith("/products/h-sold001"))

    def test_real_run_zeroes_only_tracked_candidates(self):
        doc, zero_calls = self._run(dry_run=False)
        self.assertEqual(zero_calls, ["SOLD001", "SOLD003"])
        results = {row["sku"]: row["result"] for row in doc["results"]}
        self.assertEqual(results, {
            "SOLD001": "zeroed", "SOLD003": "zeroed", "SOLD004": "not_tracked",
        })

    def test_failure_is_recorded_not_raised(self):
        jobs = FakeJobs()
        jobs.doc = {"_id": job.JOB_ID, "status": "running"}

        def gql(*args, **kwargs):
            raise RuntimeError("ACCESS_DENIED")

        job.run_job(_items(), jobs, dry_run=True, gql=gql, provider=FakeProvider())
        self.assertEqual(jobs.doc["status"], "failed")
        self.assertIn("ACCESS_DENIED", jobs.doc["error"])


class ZeroSoldOnceRouteTests(unittest.TestCase):
    def _app(self, jobs):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda value: str(value or "")
        for endpoint in (
            "index", "logout", "login", "list_items", "item_new_buyin_form",
            "item_new_consignment_form", "analytics", "audit_history",
            "notes_list", "users_list", "admin_management_payments",
            "management_index", "management_list_items",
            "management_item_new_buyin_form",
            "management_item_new_consignment_form",
        ):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "placeholder")
        register(app, _items(), jobs)
        return app

    @staticmethod
    def _login(client, role):
        with client.session_transaction() as session:
            session["user_id"] = str(ObjectId())
            session["username"] = "boss"
            session["role"] = role

    def test_admin_only_and_starts_job(self):
        jobs = FakeJobs()
        app = self._app(jobs)
        with patch(
            "luxury_app.routes.shopify_zero_sold_once.start_job_thread",
            return_value=True,
        ) as start, app.test_client() as client:
            self._login(client, ROLE_MANAGEMENT)
            self.assertEqual(client.get("/admin/shopify/zero-sold-inventory").status_code, 403)
            self.assertEqual(client.post("/admin/shopify/zero-sold-inventory").status_code, 403)
            self._login(client, ROLE_ADMIN)
            page = client.get("/admin/shopify/zero-sold-inventory")
            started = client.post("/admin/shopify/zero-sold-inventory", data={"dry_run": "1"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("预览", page.get_data(as_text=True))
        self.assertEqual(started.status_code, 302)
        self.assertTrue(start.call_args.kwargs["dry_run"])

    def test_results_page_lists_urls(self):
        jobs = FakeJobs()
        jobs.doc = {"_id": job.JOB_ID, "status": "done", "dry_run": False,
                    "sold_count": 4, "candidate_count": 1, "processed": 1,
                    "results": [{
                        "sku": "SOLD001", "name": "A", "has_sale_record": True,
                        "shopify_title": "T", "product_status": "ACTIVE",
                        "inventory_before": 1, "tracked": True,
                        "storefront_url": "https://luxuryevermore.com/products/a",
                        "admin_url": "https://admin.shopify.com/store/71eaf7/products/11",
                        "result": "zeroed", "message": "",
                    }]}
        app = self._app(jobs)
        with app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            html = client.get("/admin/shopify/zero-sold-inventory").get_data(as_text=True)
        self.assertIn("https://luxuryevermore.com/products/a", html)
        self.assertIn("已改为 0", html)


class AcquireJobTests(unittest.TestCase):
    def test_only_one_run_at_a_time(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        coll = mongomock.MongoClient()["t"]["background_jobs"]
        self.assertTrue(job.acquire_job(coll, dry_run=True, started_by="a"))
        self.assertFalse(job.acquire_job(coll, dry_run=False, started_by="b"))
        coll.update_one({"_id": job.JOB_ID}, {"$set": {"status": "done"}})
        self.assertTrue(job.acquire_job(coll, dry_run=False, started_by="b"))


if __name__ == "__main__":
    unittest.main()
