"""Shopify库存更新: four kinds of mismatch, in-memory only, page and actions."""

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

from luxury_app import shopify_inventory
from luxury_app import shopify_stock_check as check
from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.routes.shopify_stock_check import register
from tests.test_ownership import FakeInventory


class FakeProvider:
    shop = "71eaf7.myshopify.com"

    def __init__(self, error=None):
        self.error = error

    def configuration_error(self):
        return self.error


class ReadOnlyInventory(FakeInventory):
    """Accepts projections and fails loudly on any write."""

    def find(self, query, projection=None):
        return super().find(query)

    def _no_write(self, *args, **kwargs):
        raise AssertionError("Shopify stock check must never write to items")

    insert_one = update_one = delete_one = _no_write


ProjectingInventory = ReadOnlyInventory


def _variant(vid, sku, qty=1, status="ACTIVE", tracked=True, image="https://cdn/x.jpg"):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}", "sku": sku,
        "inventoryQuantity": qty, "inventoryItem": {"tracked": tracked},
        "product": {"id": f"gid://shopify/Product/{vid}", "title": f"T{vid}",
                    "handle": f"h{vid}", "status": status,
                    "featuredImage": {"url": image} if image else None},
    }


def _scan(variants, items, pages=1):
    """Run find_mismatches over `variants` split into `pages` pages."""
    chunks = [variants[i::pages] for i in range(pages)]
    responses = [
        {"productVariants": {
            "pageInfo": {"hasNextPage": i < pages - 1, "endCursor": f"c{i}"},
            "nodes": chunk,
        }}
        for i, chunk in enumerate(chunks)
    ]
    cursors = []

    def gql(query, variables, token_provider=None):
        cursors.append(variables.get("cursor"))
        return responses.pop(0)

    with patch.object(check, "PAGE_DELAY_SECONDS", 0):
        rows = check.find_mismatches(
            ReadOnlyInventory(items), gql=gql, provider=FakeProvider()
        )
    return rows, cursors


def _item(sku, status, **extra):
    doc = {"_id": ObjectId(), "sku": sku, "name": sku.lower(), "status": status}
    doc.update(extra)
    return doc


class FindMismatchesTests(unittest.TestCase):
    def test_four_kinds(self):
        items = [
            _item("SOLD001", "SOLD", brand="Chanel", name="CF"),
            _item("SOLD002", "SOLD"),
            _item("SOLD003", "SOLD"),
            _item("SOLD004", "SOLD"),
            _item("LIVE001", "ON_SHELF"),
            _item("ZERO001", "ON_SHELF"),
            _item("DRFT001", "ON_SHELF"),
            _item("DUPE001", "ON_SHELF"),
            _item("GONE001", "ON_SHELF", name_in_EN="Lady Dior Mini", shopify_details={
                "product_id": "gid://shopify/Product/77", "title": "Old title",
                "featured_image": "https://cdn/old.jpg", "url": "https://shop/products/old",
            }),
            _item("GONE002", "ON_SHELF", name_in_EN="Kelly 25"),
            _item("RECV001", "RECEIVED"),
        ]
        rows, cursors = _scan([
            _variant(1, "SOLD001"),                         # sold here, selling -> sold
            _variant(2, "SOLD002", qty=0),                  # sold, 0 stock -> fine
            _variant(3, "SOLD003", status="DRAFT"),         # sold, draft -> fine
            _variant(4, "SOLD004", qty=0, tracked=False),   # sold, untracked -> sold
            _variant(5, "LIVE001"),                         # on shelf + selling -> fine
            _variant(6, "TYPO99"),                          # unknown SKU -> missing
            _variant(7, ""),                                # empty SKU -> missing
            _variant(8, "ZERO001", qty=0),                  # on shelf, 0 stock -> unsellable
            _variant(9, "DRFT001", status="DRAFT"),         # on shelf, draft -> unsellable
            _variant(10, "DUPE001", status="DRAFT"),        # old draft copy ...
            _variant(11, "DUPE001"),                        # ... active copy sells -> fine
            _variant(12, "RECV001", qty=0),                 # not on shelf -> ignored
            _variant(13, "OLDX", status="ARCHIVED"),        # archived unknown -> ignored
        ], items, pages=2)

        self.assertEqual(cursors, [None, "c0"])
        kinds = [(row["reason"], row["sku"]) for row in rows]
        self.assertEqual(kinds, [
            ("sold", "SOLD001"), ("sold", "SOLD004"),
            ("missing", ""), ("missing", "TYPO99"),
            ("unsellable", "DRFT001"), ("unsellable", "ZERO001"),
            ("notfound", "GONE001"), ("notfound", "GONE002"),
        ])
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["SOLD001"]["system_name"], "Chanel CF")
        self.assertEqual(by_sku["SOLD001"]["image"], "https://cdn/x.jpg")
        self.assertEqual(by_sku["SOLD001"]["admin_url"],
                         "https://admin.shopify.com/store/71eaf7/products/1")
        self.assertFalse(by_sku["SOLD004"]["tracked"])
        self.assertEqual(by_sku["ZERO001"]["detail"], "Shopify库存为0")
        self.assertEqual(by_sku["DRFT001"]["detail"], "Shopify状态为Draft")
        # notfound: reuse the link the hourly sync left behind, else a search link.
        self.assertEqual(by_sku["GONE001"]["admin_url"],
                         "https://admin.shopify.com/store/71eaf7/products/77")
        self.assertEqual(by_sku["GONE001"]["image"], "https://cdn/old.jpg")
        self.assertEqual(by_sku["GONE002"]["admin_url"],
                         "https://admin.shopify.com/store/71eaf7/products?query=Kelly%2025")
        self.assertEqual(by_sku["GONE002"]["admin_link_label"], "在Shopify搜索")


class InMemoryStateTests(unittest.TestCase):
    def setUp(self):
        check._reset_for_tests()

    def tearDown(self):
        check._reset_for_tests()

    def _page(self, variants):
        return lambda *a, **k: {"productVariants": {
            "pageInfo": {"hasNextPage": False}, "nodes": deepcopy(variants)}}

    def test_refresh_keeps_result_in_memory_only(self):
        items = ReadOnlyInventory([_item("SOLD001", "SOLD")])
        with patch.object(check, "PAGE_DELAY_SECONDS", 0):
            self.assertTrue(check.refresh_now(
                items, gql=self._page([_variant(1, "SOLD001")]), provider=FakeProvider()
            ))
        state = check.snapshot()
        self.assertEqual([row["sku"] for row in state["rows"]], ["SOLD001"])
        self.assertFalse(state["running"])
        self.assertEqual(state["next_run_at"] - state["computed_at"], timedelta(hours=2))

    def test_failure_keeps_previous_list(self):
        check._reset_for_tests(rows=[{"variant_id": "old", "reason": "sold"}],
                               computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

        def boom(*args, **kwargs):
            raise RuntimeError("Shopify down")

        check.refresh_now(ReadOnlyInventory([]), gql=boom, provider=FakeProvider())
        state = check.snapshot()
        self.assertEqual(state["rows"], [{"variant_id": "old", "reason": "sold"}])
        self.assertIn("Shopify down", state["last_error"])
        self.assertFalse(state["running"])

    def test_only_one_refresh_at_a_time(self):
        check._reset_for_tests(running=True)
        self.assertFalse(check.refresh_now(ReadOnlyInventory([]), provider=FakeProvider()))
        self.assertFalse(check.refresh_in_background(ReadOnlyInventory([])))

    def test_remove_row(self):
        check._reset_for_tests(rows=[{"variant_id": "a"}, {"variant_id": "b"}])
        check.remove_row("a")
        self.assertEqual(check.snapshot()["rows"], [{"variant_id": "b"}])


class ZeroByVariantTests(unittest.TestCase):
    def test_zero_variant_uses_variant_lookup(self):
        calls = []

        def gql(query, variables, token_provider=None):
            calls.append(variables)
            if "productVariant(id:" in query:
                return {"productVariant": {"id": "gid://shopify/ProductVariant/9", "sku": "", "inventoryItem": {
                    "id": "gid://shopify/InventoryItem/7", "tracked": True,
                    "inventoryLevels": {"edges": [{"node": {
                        "location": {"id": "gid://shopify/Location/1"},
                        "quantities": [{"name": "available", "quantity": 1}],
                    }}]},
                }}}
            return {"inventorySetQuantities": {"userErrors": []}}

        with patch.object(shopify_inventory, "shopify_gql", side_effect=gql):
            result = shopify_inventory.zero_shopify_inventory_for_variant(
                "gid://shopify/ProductVariant/9", token_provider=FakeProvider()
            )
        self.assertEqual(result["status"], "zeroed")
        self.assertEqual(calls[0], {"id": "gid://shopify/ProductVariant/9"})
        self.assertEqual(calls[1]["input"]["quantities"][0]["changeFromQuantity"], 1)

    def test_rejects_non_variant_ids(self):
        result = shopify_inventory.zero_shopify_inventory_for_variant("123")
        self.assertEqual(result["status"], "error")


ROW = {
    "variant_id": "gid://shopify/ProductVariant/1", "sku": "SOLD001", "reason": "sold",
    "detail": "", "system_name": "Chanel CF", "shopify_title": "Chanel Classic Flap",
    "image": "https://cdn/x.jpg", "tracked": True,
    "storefront_url": "https://luxuryevermore.com/products/cf",
    "admin_url": "https://admin.shopify.com/store/71eaf7/products/1",
    "admin_link_label": "打开",
}
MISSING_ROW = dict(ROW, variant_id="gid://shopify/ProductVariant/2", sku="TYPO01",
                   reason="missing", system_name="", shopify_title="Dior Lady")
UNSELLABLE_ROW = dict(ROW, variant_id="gid://shopify/ProductVariant/3", sku="ZERO001",
                      reason="unsellable", detail="Shopify库存为0", system_name="A")
NOTFOUND_ROW = dict(ROW, variant_id="", sku="GONE001", reason="notfound",
                    system_name="B", shopify_title="",
                    admin_url="https://admin.shopify.com/store/71eaf7/products?query=Kelly",
                    admin_link_label="在Shopify搜索")
ALL_ROWS = [ROW, MISSING_ROW, UNSELLABLE_ROW, NOTFOUND_ROW]
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)

LAYOUT_ENDPOINTS = (
    "logout", "login", "list_items", "item_new_buyin_form",
    "item_new_consignment_form", "analytics", "audit_history",
    "notes_list", "users_list", "admin_management_payments",
    "management_index", "management_list_items",
    "management_item_new_buyin_form", "management_item_new_consignment_form",
)


class RouteTests(unittest.TestCase):
    def setUp(self):
        check._reset_for_tests(rows=ALL_ROWS, computed_at=NOW)

    def tearDown(self):
        check._reset_for_tests()

    def _app(self):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda value: str(value or "")
        for endpoint in LAYOUT_ENDPOINTS:
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "placeholder")
        app.add_url_rule("/items/<item_key>", "item_detail", lambda item_key: "x")

        from flask import render_template

        @app.get("/", endpoint="index")
        def index():
            return render_template("layout.html")

        register(app, ReadOnlyInventory([]))
        return app

    @staticmethod
    def _login(client, role):
        with client.session_transaction() as session:
            session["user_id"] = str(ObjectId())
            session["username"] = "boss"
            session["role"] = role

    def _get(self, path, role=ROLE_ADMIN):
        app = self._app()
        with app.test_client() as client:
            self._login(client, role)
            return client.get(path)

    def test_each_row_shows_its_situation(self):
        html = self._get("/shopify-stock").get_data(as_text=True)
        for css in ("stock-sold", "stock-missing", "stock-unsellable", "stock-notfound"):
            self.assertEqual(html.count(f'<tr class="{css}">'), 2)  # legend + row
        self.assertIn('<div class="stock-sold-text">系统内已售出，但 Shopify 上还在卖</div>', html)
        self.assertIn('<div class="stock-missing-text">Shopify 上在卖，但系统中无此SKU</div>', html)
        self.assertIn('<div class="stock-unsellable-text">系统内上架在售，但 Shopify 上买不了（Shopify库存为0）</div>', html)
        self.assertIn('<div class="stock-notfound-text">系统内上架在售，但 Shopify 上找不到此SKU</div>', html)
        self.assertEqual(html.count("已无同款，商品设置库存为0</button>"), 1)
        self.assertIn(">在Shopify搜索</a>", html)
        self.assertIn('src="https://cdn/x.jpg"', html)
        self.assertIn("手动刷新", html)
        self.assertIn("每2小时自动更新", html)
        self.assertIn("不保存到数据库", html)
        for label in ("系统内已售出 1", "系统中无此SKU 1", "Shopify上买不了 1", "Shopify上找不到 1"):
            self.assertIn(label, html)
        # Menu shows the red total.
        self.assertIn('href="/shopify-stock">Shopify库存更新', html)
        self.assertIn("(4)", html)

    def test_filter_by_kind(self):
        html = self._get("/shopify-stock?reason=notfound").get_data(as_text=True)
        self.assertIn('<div class="stock-notfound-text">', html)
        self.assertNotIn('<div class="stock-sold-text">', html)

    def test_only_admin(self):
        app = self._app()
        with app.test_client() as client:
            for role in (ROLE_MANAGEMENT, ROLE_STAFF):
                self._login(client, role)
                self.assertEqual(client.get("/shopify-stock").status_code, 403)
                self.assertEqual(client.post("/shopify-stock/refresh").status_code, 403)
                self.assertEqual(client.post("/shopify-stock/zero").status_code, 403)
                self.assertNotIn("Shopify库存更新", client.get("/").get_data(as_text=True))

    def test_manual_refresh_triggers_recompute(self):
        app = self._app()
        with patch(
            "luxury_app.routes.shopify_stock_check.refresh_in_background", return_value=True
        ) as refresh, app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            response = client.post("/shopify-stock/refresh")
        self.assertEqual(response.status_code, 302)
        refresh.assert_called_once()

    def _zero(self, variant_id, status="zeroed"):
        app = self._app()
        with patch(
            "luxury_app.routes.shopify_stock_check.zero_shopify_inventory_for_variant",
            return_value={"status": status, "ok": status == "zeroed", "message": "boom"},
        ) as zero, app.test_client() as client:
            self._login(client, ROLE_ADMIN)
            client.post("/shopify-stock/zero", data={"variant_id": variant_id})
        return zero

    def test_zero_button_zeroes_sold_row_and_hides_it(self):
        zero = self._zero(ROW["variant_id"])
        zero.assert_called_once_with(ROW["variant_id"])
        self.assertNotIn(ROW["variant_id"], [r["variant_id"] for r in check.snapshot()["rows"]])

    def test_zero_refused_for_other_kinds_and_unknown_ids(self):
        for variant_id in (MISSING_ROW["variant_id"], UNSELLABLE_ROW["variant_id"],
                           "", "gid://shopify/ProductVariant/999"):
            self._zero(variant_id).assert_not_called()
        self.assertEqual(len(check.snapshot()["rows"]), 4)

    def test_failed_zero_keeps_row(self):
        self._zero(ROW["variant_id"], status="error")
        self.assertEqual(len(check.snapshot()["rows"]), 4)


if __name__ == "__main__":
    unittest.main()
