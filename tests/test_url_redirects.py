"""URL重定向管理: “/SKU” redirects are kept pointing at the product carrying the SKU."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bson import ObjectId
from flask import Flask

from luxury_app import url_redirects as ur
from luxury_app.auth import ROLE_ADMIN, ROLE_STAFF


class FakeProvider:
    shop = "71eaf7.myshopify.com"

    def configuration_error(self):
        return None


class Items:
    def __init__(self, skus):
        self.skus = skus

    def find(self, query, projection=None):
        return [{"sku": sku} for sku in self.skus]


def variant(sku, handle, status="ACTIVE", pid=None, title=None):
    return {"id": f"gid://shopify/ProductVariant/{handle}-{sku}", "sku": sku,
            "inventoryQuantity": 1, "inventoryItem": {"tracked": True},
            "product": {"id": pid or f"gid://shopify/Product/{handle}", "title": title or handle,
                        "handle": handle, "status": status, "featuredImage": None}}


def redirect(rid, path, target):
    return {"id": f"gid://shopify/UrlRedirect/{rid}", "path": path, "target": target}


class FakeShopify:
    """Answers the read queries from lists and records every write."""

    def __init__(self, variants, redirects, fail=None):
        self.variants, self.redirects, self.fail = variants, redirects, fail or {}
        self.writes = []

    def __call__(self, query, variables, token_provider=None):
        if "productVariants(" in query:
            return {"productVariants": {"nodes": self.variants, "pageInfo": {"hasNextPage": False}}}
        if "urlRedirects(" in query:
            return {"urlRedirects": {"nodes": self.redirects, "pageInfo": {"hasNextPage": False}}}
        for name in ("urlRedirectCreate", "urlRedirectUpdate", "urlRedirectDelete"):
            if name in query:
                self.writes.append((name, variables))
                key = (variables.get("redirect") or {}).get("path") or variables.get("id")
                errors = [{"field": None, "message": self.fail[key]}] if key in self.fail else []
                return {name: {"userErrors": errors}}
        raise AssertionError(query)


SYSTEM = ["AAA1111", "BBB2222", "CCC3333", "DDD4444", "EEE5555", "FFF6666", "GGG7777", "HHH8888"]


def store():
    return FakeShopify(
        variants=[
            variant("AAA1111", "chanel-flap"),                         # redirect missing → create
            variant("BBB2222", "lv-speedy-1"),                         # duplicated listing: active copy
            variant("BBB2222", "lv-speedy", status="DRAFT"),           #   …and the drafted original
            variant("CCC3333", "dior-lady"),                           # already right
            variant("DDD4444", "hermes-kelly", status="DRAFT"),        # only a draft has it → still that page
            variant("EEE5555", "gucci-a"), variant("EEE5555", "gucci-b"),   # two active → conflict
            variant("ZZZ9999", "prada-x"),                             # on Shopify, not in the system
            variant("HHH8888", "fendi-new"),                           # SKU fixed from GGG7777 to HHH8888
        ],
        redirects=[
            redirect(1, "/BBB2222", "/products/lv-speedy"),            # points to the drafted original
            redirect(2, "/ccc3333", "https://luxuryevermore.com/products/dior-lady"),   # ok (case/full URL)
            redirect(3, "/DDD4444", "/collections/hermes"),            # not a product page
            redirect(4, "/FFF6666", "/products/deleted-bag"),          # SKU not on the website → delete
            redirect(5, "/GGG7777", "/products/fendi-new"),            # old SKU → now another bag's page
            redirect(6, "/about-us", "/pages/about"),                  # not ours
            redirect(7, "/QQQ0000", "/products/fendi-new"),            # 0 is not in the SKU alphabet → not ours
            redirect(8, "/KKK2345", "/products/old-bag"),               # SKU shape + product page → ours, unused → delete
            redirect(9, "/WALLETS", "/collections/wallets"),           # SKU shape but a collection → not ours
        ],
    )


class PlanTests(unittest.TestCase):
    def test_rows(self):
        rows, ok = ur.plan(Items(SYSTEM), gql=store(), provider=FakeProvider())
        by = {r["sku"]: r for r in rows}
        self.assertEqual(ok, 1)   # CCC3333
        self.assertEqual(sorted(by), ["AAA1111", "BBB2222", "DDD4444", "EEE5555", "FFF6666", "GGG7777",
                                      "HHH8888", "KKK2345", "ZZZ9999"])
        self.assertEqual((by["AAA1111"]["action"], by["AAA1111"]["target"]), ("create", "/products/chanel-flap"))
        self.assertEqual((by["BBB2222"]["action"], by["BBB2222"]["target"]), ("update", "/products/lv-speedy-1"))
        self.assertIn("另一个商品", by["BBB2222"]["reason"])
        self.assertEqual((by["DDD4444"]["action"], by["DDD4444"]["target"]), ("update", "/products/hermes-kelly"))
        self.assertIn("不是商品页", by["DDD4444"]["reason"])
        self.assertEqual(by["EEE5555"]["action"], "conflict")
        self.assertEqual(len(by["EEE5555"]["products"]), 2)
        self.assertEqual(by["GGG7777"]["action"], "delete")
        self.assertIn("HHH8888", by["GGG7777"]["reason"])
        self.assertIn("打开别的包", by["GGG7777"]["reason"])
        self.assertEqual(by["FFF6666"]["action"], "delete")
        self.assertIn("已经不用了", by["FFF6666"]["reason"])
        self.assertEqual((by["KKK2345"]["action"], by["KKK2345"]["in_system"]), ("delete", False))
        self.assertEqual(by["HHH8888"]["action"], "create")
        self.assertEqual(by["ZZZ9999"]["action"], "create")          # written on Shopify → wanted
        self.assertFalse(by["ZZZ9999"]["in_system"])
        # Order: create, update, delete, conflict.
        self.assertEqual([r["action"] for r in rows],
                         ["create", "create", "create", "update", "update", "delete", "delete", "delete", "conflict"])

    def test_website_states(self):
        variants = [
            variant("AAA1111", "a"),
            dict(variant("BBB2222", "b"), inventoryQuantity=0),
            variant("CCC3333", "c", status="DRAFT"),
            variant("DDD4444", "d", status="ARCHIVED"),
            variant("EEE5555", "e-old", status="DRAFT"), variant("EEE5555", "e-new"),   # active copy wins
            dict(variant("FFF6666", "f"), inventoryQuantity=0, inventoryItem={"tracked": False}),
        ]
        states = {sku: info["state"] for sku, info in ur.website_states(variants).items()}
        self.assertEqual(states, {"AAA1111": "selling", "BBB2222": "zero", "CCC3333": "draft",
                                  "DDD4444": "archived", "EEE5555": "selling", "FFF6666": "selling"})

    def test_items_only_in_the_system_get_nothing(self):
        rows, ok = ur.plan(Items(["NEW0001", "NEW0002"]), gql=FakeShopify([], []), provider=FakeProvider())
        self.assertEqual((rows, ok), ([], 0))

    def test_target_parsing(self):
        self.assertEqual(ur._target_handle("/products/Abc-1"), "abc-1")
        self.assertEqual(ur._target_handle("https://x.com/collections/all/products/abc?variant=1"), "abc")
        self.assertIsNone(ur._target_handle("/collections/abc"))
        self.assertEqual(ur._path_key("/abc1234/"), "ABC1234")


class RunTests(unittest.TestCase):
    def setUp(self):
        ur._reset_for_tests()
        patcher = patch.object(ur, "WRITE_DELAY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_check_only_changes_nothing(self):
        shop = store()
        self.assertTrue(ur.run(Items(SYSTEM), fix=False, gql=shop, provider=FakeProvider()))
        self.assertEqual(shop.writes, [])
        state = ur.snapshot()
        self.assertEqual(len(state["rows"]), 9)
        self.assertEqual(state["ok_count"], 1)
        self.assertIsNotNone(state["checked_at"])
        self.assertIsNone(state["last_fix"])
        website, at = ur.website_snapshot()
        self.assertEqual(at, state["checked_at"])
        self.assertEqual(website["DDD4444"]["state"], "draft")
        self.assertNotIn("FFF6666", website)

    def test_fix_applies_everything_but_conflicts(self):
        shop = store()
        ur.run(Items(SYSTEM), fix=True, auto=True, gql=shop, provider=FakeProvider())
        self.assertEqual(shop.writes, [
            ("urlRedirectCreate", {"redirect": {"path": "/AAA1111", "target": "/products/chanel-flap"}}),
            ("urlRedirectCreate", {"redirect": {"path": "/HHH8888", "target": "/products/fendi-new"}}),
            ("urlRedirectCreate", {"redirect": {"path": "/ZZZ9999", "target": "/products/prada-x"}}),
            ("urlRedirectUpdate", {"id": "gid://shopify/UrlRedirect/1", "redirect": {"target": "/products/lv-speedy-1"}}),
            ("urlRedirectUpdate", {"id": "gid://shopify/UrlRedirect/3", "redirect": {"target": "/products/hermes-kelly"}}),
            ("urlRedirectDelete", {"id": "gid://shopify/UrlRedirect/4"}),
            ("urlRedirectDelete", {"id": "gid://shopify/UrlRedirect/5"}),
            ("urlRedirectDelete", {"id": "gid://shopify/UrlRedirect/8"}),
        ])
        state = ur.snapshot()
        self.assertEqual([r["sku"] for r in state["rows"]], ["EEE5555"])
        self.assertEqual(state["ok_count"], 9)
        fix = state["last_fix"]
        self.assertEqual((fix["auto"], fix["done_count"], fix["failed_count"]), (True, 8, 0))
        self.assertEqual(fix["done"][3], {"sku": "BBB2222", "action": "update", "path": "/BBB2222",
                                          "from": "/products/lv-speedy", "to": "/products/lv-speedy-1",
                                          "message": ""})

    def test_failures_stay_listed(self):
        shop = store()
        shop.fail = {"/AAA1111": "Path has already been taken"}
        ur.run(Items(SYSTEM), fix=True, gql=shop, provider=FakeProvider())
        state = ur.snapshot()
        self.assertEqual([r["sku"] for r in state["rows"]], ["AAA1111", "EEE5555"])
        self.assertEqual(state["rows"][0]["error"], "Path has already been taken")
        self.assertEqual(state["last_fix"]["failed_count"], 1)

    def test_errors_keep_previous_result(self):
        ur.run(Items(SYSTEM), fix=False, gql=store(), provider=FakeProvider())

        def denied(query, variables, token_provider=None):
            raise RuntimeError('Shopify GraphQL errors: [{"extensions": {"code": "ACCESS_DENIED"}}]')

        ur.run(Items(SYSTEM), fix=True, gql=denied, provider=FakeProvider())
        state = ur.snapshot()
        self.assertEqual(len(state["rows"]), 9)
        self.assertIn("online_store_navigation", state["last_error"])

    def test_fix_one(self):
        shop = store()
        ur.run(Items(SYSTEM), fix=False, gql=shop, provider=FakeProvider())
        self.assertTrue(ur.fix_one("GGG7777", gql=shop, provider=FakeProvider())["ok"])
        self.assertEqual(shop.writes, [("urlRedirectDelete", {"id": "gid://shopify/UrlRedirect/5"})])
        self.assertNotIn("GGG7777", [r["sku"] for r in ur.snapshot()["rows"]])
        self.assertFalse(ur.fix_one("EEE5555", gql=shop, provider=FakeProvider())["ok"])   # conflict
        self.assertFalse(ur.fix_one("NOPE", gql=shop, provider=FakeProvider())["ok"])
        self.assertEqual(len(shop.writes), 1)


class PageTests(unittest.TestCase):
    def setUp(self):
        ur._reset_for_tests()
        ur.run(Items(SYSTEM), fix=False, gql=store(), provider=FakeProvider())

    def _client(self, role=ROLE_ADMIN):
        from luxury_app.routes.url_redirects import register
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in ("index", "logout", "login", "list_items", "item_new_buyin_form",
                         "item_new_consignment_form", "analytics", "audit_history",
                         "notes_list", "users_list", "admin_management_payments",
                         "management_index", "management_list_items",
                         "management_item_new_buyin_form", "management_item_new_consignment_form",
                         "sale_new_form"):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        app.add_url_rule("/items/<item_key>", "item_detail", lambda item_key: "x")
        register(app, Items(SYSTEM))
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="boss")
        return client

    def test_page(self):
        html = self._client().get("/url-redirects").get_data(as_text=True)
        self.assertIn("URL重定向管理", html)
        self.assertIn("<b>1</b> 个 SKU 重定向正常", html)
        self.assertIn("待创建 3", html)
        self.assertIn("指错了 2", html)
        self.assertIn("已不用 3", html)
        self.assertIn("SKU 冲突 1", html)
        self.assertIn("/BBB2222 → /products/lv-speedy", html)
        self.assertIn("→ /products/lv-speedy-1", html)
        self.assertIn("系统里没有这个 SKU", html)                # ZZZ9999
        self.assertEqual(html.count('class="js-fix"'), 8)        # no button for the conflict
        filtered = self._client().get("/url-redirects?action=delete").get_data(as_text=True)
        self.assertIn("GGG7777", filtered)
        self.assertNotIn("AAA1111</a>", filtered)

    def test_admin_only(self):
        self.assertEqual(self._client(ROLE_STAFF).get("/url-redirects").status_code, 403)
        self.assertEqual(self._client(ROLE_STAFF).post("/url-redirects/fix-all").status_code, 403)

    def test_fix_one_route(self):
        with patch.object(ur, "apply_row", return_value={"ok": True, "message": ""}) as apply:
            response = self._client().post("/url-redirects/fix-one", data={"sku": "aaa1111"},
                                            headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json, {"ok": True, "message": ""})
        self.assertEqual(apply.call_args[0][0]["sku"], "AAA1111")


class ItemListWebsiteStateTests(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        from luxury_app.routes.items import register as register_items
        self.db = mongomock.MongoClient()["t"]
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.db.items.insert_many([{"sku": sku, "name": "bag", "brand": "Chanel", "status": "ON_SHELF",
                                    "created_at": now, "purchase_at": now} for sku in
                                   ("AAA1111", "BBB2222", "CCC3333", "DDD4444", "NOT0NWEB")])
        ur._reset_for_tests()
        ur.run(Items([]), fix=False, provider=FakeProvider(), gql=FakeShopify(
            variants=[variant("AAA1111", "a"), dict(variant("BBB2222", "b"), inventoryQuantity=0),
                      variant("CCC3333", "c", status="DRAFT"), variant("DDD4444", "d", status="ARCHIVED")],
            redirects=[]))
        self.addCleanup(ur._reset_for_tests)
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in ("index", "logout", "login", "analytics", "audit_history", "notes_list",
                         "users_list", "admin_management_payments", "management_index",
                         "management_list_items", "management_item_new_buyin_form",
                         "management_item_new_consignment_form", "sale_new_form"):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        register_items(app, self.db.items)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s.update(role=ROLE_ADMIN, user_id=str(ObjectId()), username="boss")

    def _rows(self, query=""):
        import re
        html = self.client.get("/items" + query).get_data(as_text=True)
        body = html.split("</thead>", 1)[-1].split("</table>", 1)[0]
        return html, sorted(set(re.findall(r"\b([A-Z0-9]{7,8})\b", body)) & {
            "AAA1111", "BBB2222", "CCC3333", "DDD4444", "NOT0NWEB"})

    def test_badges(self):
        html, _ = self._rows()
        for label in ("网站在售", "网站上架·库存0", "网站草稿", "网站已归档", "不在网站上"):
            self.assertIn(label, html)
        self.assertIn('class="web-badge web-zero"', html)

    def test_filter(self):
        self.assertEqual(self._rows("?web=selling")[1], ["AAA1111"])
        self.assertEqual(self._rows("?web=zero")[1], ["BBB2222"])
        self.assertEqual(self._rows("?web=draft")[1], ["CCC3333"])
        self.assertEqual(self._rows("?web=any")[1], ["AAA1111", "BBB2222", "CCC3333", "DDD4444"])
        html, rows = self._rows("?web=none")
        self.assertEqual(rows, ["NOT0NWEB"])
        self.assertIn('<option value="none" selected>', html)
        self.assertEqual(self._rows("?web=none&q=AAA")[1], [])


if __name__ == "__main__":
    unittest.main()
