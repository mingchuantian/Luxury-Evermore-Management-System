"""库存列表: click 收货/寄卖日期 or 成本 to change the order (read-only)."""

import re
import unittest
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_ADMIN, ROLE_STAFF
from luxury_app.routes.items import register as register_items

T0 = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
LAYOUT = (
    "index", "logout", "login", "analytics", "audit_history", "notes_list",
    "users_list", "admin_management_payments", "management_index",
    "management_list_items", "management_item_new_buyin_form",
    "management_item_new_consignment_form", "sale_new_form",
)


class ItemListSortTests(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        self.db = mongomock.MongoClient()["t"]

        def doc(sku, days, cost, ccy="SGD", **extra):
            d = {"sku": sku, "name": "bag", "brand": "Chanel", "status": "RECEIVED",
                 "cost": cost, "cost_currency": ccy, "created_at": T0 + timedelta(days=days),
                 "purchase_at": T0 + timedelta(days=days)}
            d.update(extra)
            return d

        self.db.items.insert_many([
            doc("D1", 1, 3000),
            doc("D2", 2, 10600, "RMB"),                   # ≈ 2000 SGD
            doc("D3", 3, 0),                              # no cost
            doc("D4", 4, 5000),
            # Entered last but its 收货日期 was set earlier: sorts by the date shown.
            doc("D5", 10, 1000, purchase_at=T0 - timedelta(days=5)),
            doc("D6", 6, 4000, purchase_at=None),         # old record: falls back to created_at
            doc("D7", 7, "2500"),                         # cost stored as text
            doc("D8", 8, 900, "HKD"),                     # unknown currency: can't compare → last
        ])

    def _client(self, role=ROLE_ADMIN):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in LAYOUT:
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        register_items(app, self.db.items)
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="boss")
        return client

    def _order(self, query="", role=ROLE_ADMIN):
        html = self._client(role).get("/items" + query).get_data(as_text=True)
        body = html.split("<tbody", 1)[1] if "<tbody" in html else html.split("</thead>", 1)[-1]
        return re.findall(r"\b(D\d)\b", body.split("</table>", 1)[0]), html

    @staticmethod
    def _unique(seq):
        seen = []
        for x in seq:
            if x not in seen:
                seen.append(x)
        return seen

    def test_default_is_newest_entered_first(self):
        order, html = self._order()
        # By 录入系统时间 (created_at), as before: D5 was entered last.
        self.assertEqual(self._unique(order), ["D5", "D8", "D7", "D6", "D4", "D3", "D2", "D1"])
        self.assertIn("录入系统时间 新→旧（默认）", html)
        self.assertNotIn("恢复默认", html)
        self.assertIn('href="/items?sort=date_desc"', html)
        self.assertIn('href="/items?sort=cost_desc"', html)

    def test_date_sort_by_shown_date_both_ways(self):
        order, html = self._order("?sort=date_desc")
        self.assertEqual(self._unique(order), ["D8", "D7", "D6", "D4", "D3", "D2", "D1", "D5"])
        self.assertIn("▼ 新→旧", html)
        self.assertIn('href="/items?sort=date_asc"', html)
        self.assertIn('<a href="/items">恢复默认</a>', html)
        order, html = self._order("?sort=date_asc")
        self.assertEqual(self._unique(order), ["D5", "D1", "D2", "D3", "D4", "D6", "D7", "D8"])
        self.assertIn("▲ 旧→新", html)
        self.assertIn('href="/items?sort=date_desc"', html)

    def test_cost_high_to_low_then_low_to_high(self):
        order, html = self._order("?sort=cost_desc")
        self.assertEqual(self._unique(order), ["D4", "D6", "D1", "D7", "D2", "D5", "D8", "D3"])
        self.assertIn("▼ 高→低", html)
        self.assertIn('href="/items?sort=cost_asc"', html)
        order, html = self._order("?sort=cost_asc")
        self.assertEqual(self._unique(order), ["D5", "D2", "D7", "D1", "D6", "D4", "D8", "D3"])  # no cost stays last
        self.assertIn("▲ 低→高", html)
        self.assertIn('href="/items?sort=cost_desc"', html)

    def test_sort_keeps_filters_and_pages(self):
        for i in range(600):
            self.db.items.insert_one({"sku": f"X{i:03d}", "name": "x", "status": "SOLD", "cost": i,
                                      "cost_currency": "SGD", "created_at": T0, "purchase_at": T0})
        html = self._client().get("/items?status=SOLD&sort=cost_desc").get_data(as_text=True)
        self.assertIn("X599", html)
        self.assertNotIn("X099", html)                     # page 1 = 500 most expensive
        self.assertIn('href="/items?page=2&amp;sort=cost_desc&amp;status=SOLD"', html)
        self.assertIn('<input type="hidden" name="sort" value="cost_desc">', html)
        page2 = self._client().get("/items?page=2&status=SOLD&sort=cost_desc").get_data(as_text=True)
        self.assertIn("X099", page2)
        self.assertNotIn("D1", page2)
        # Cost header link keeps the filter.
        self.assertIn('href="/items?sort=cost_asc&amp;status=SOLD"', html)

    def test_bad_sort_is_ignored_and_nothing_written(self):
        order, _ = self._order("?sort=evil")
        self.assertEqual(self._unique(order)[0], "D5")
        before = list(self.db.items.find({}, {"_id": 0}))
        self._order("?sort=cost_desc")
        self.assertEqual(list(self.db.items.find({}, {"_id": 0})), before)   # nothing written


if __name__ == "__main__":
    unittest.main()
