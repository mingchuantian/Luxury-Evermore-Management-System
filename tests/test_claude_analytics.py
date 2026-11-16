"""Claude新增分析: calculations, read-only guarantee, access and rendering."""

import unittest
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.claude_analytics import Options, build_report, norm_channel
from luxury_app.routes.claude_analytics import register


NOW = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)   # 16:00 on 16 Nov, UTC+8


def ago(days, hours=0):
    return NOW - timedelta(days=days, hours=hours)


def item(sku, status="RECEIVED", *, bought=30, cost=1000, ccy="SGD", brand="Chanel",
         source="BUY_IN", seller="Alice", listing=None, sale=None, audit=None, **extra):
    doc = {
        "sku": sku, "name": "Bag", "brand": brand, "status": status, "ownership": "admin",
        "source_type": source, "seller_name": seller, "cost": cost, "cost_currency": ccy,
        "purchase_at": ago(bought), "created_at": ago(bought), "serial_code": "S1",
        "audit": audit or [],
    }
    if listing is not None:
        doc.update(listing_price=listing, listing_currency="SGD")
    if sale is not None:
        doc["status"] = "SOLD"
        doc["sold_record"] = [dict({"sold_currency": "SGD", "buyer": "B", "sale_channel": "ig",
                                    "payment_method": "Paynow", "sold_by_username": "amy"}, **sale)]
    doc.update(extra)
    return doc


def status_change(to, days):
    return {"action": "Change_status", "to": to, "at": ago(days)}


def report(docs, **opts):
    return build_report(docs, Options(**dict({"months": 12}, **opts)), now=NOW)


class MoneyTests(unittest.TestCase):
    def test_currencies_are_converted_not_dropped(self):
        docs = [
            item("A", cost=5300, ccy="RMB", sale={"sold_at": ago(5), "sold_price": 1500}),
            item("B", cost=1000, ccy="SGD", sale={"sold_at": ago(5), "sold_price": 1000, "sold_currency": "USD"}),
        ]
        sgd = report(docs)["returns"]["overall"]
        self.assertAlmostEqual(sgd["cost"], 2000)
        self.assertAlmostEqual(sgd["revenue"], 1500 + 1300)
        self.assertAlmostEqual(sgd["profit"], 800)
        self.assertAlmostEqual(sgd["margin"], 0.4)
        rmb = report(docs, ccy="RMB")["returns"]["overall"]
        self.assertAlmostEqual(rmb["cost"], 2000 * 5.3)
        custom = report(docs, sgd_rmb=5.0)["returns"]["overall"]
        self.assertAlmostEqual(custom["cost"], 5300 / 5 + 1000)

    def test_unknown_currency_is_excluded_and_reported(self):
        r = report([item("A", cost=900, ccy="HKD", sale={"sold_at": ago(3), "sold_price": 1000})])
        self.assertEqual(r["returns"]["overall"]["cost"], 0)
        self.assertEqual(r["profit_missing_count"], 1)
        check = {c["label"]: c for c in r["quality"]["checks"]}["成本的币种认不出来"]
        self.assertEqual(check["count"], 1)
        self.assertEqual(check["samples"][0]["sku"], "A")

    def test_profit_basis(self):
        docs = [
            item("A", cost=1000, sale={"sold_at": ago(3), "sold_price": 1500}, profit=2120, profit_currency="RMB"),
            item("B", cost=1000, sale={"sold_at": ago(3), "sold_price": 1200}),
        ]
        recorded = report(docs)
        self.assertAlmostEqual(recorded["returns"]["overall"]["profit"], 400 + 200)
        self.assertEqual(recorded["profit_estimated_count"], 1)
        computed = report(docs, profit_basis="computed")
        self.assertAlmostEqual(computed["returns"]["overall"]["profit"], 500 + 200)
        self.assertEqual(computed["profit_estimated_count"], 0)


class ScopeTests(unittest.TestCase):
    def test_ownership_and_period_filters(self):
        docs = [
            item("MINE", sale={"sold_at": ago(10), "sold_price": 1200}),
            item("OLD", bought=500, sale={"sold_at": ago(400), "sold_price": 1200}),
            item("MGMT", sale={"sold_at": ago(10), "sold_price": 1200}, ownership="management"),
        ]
        self.assertEqual(report(docs)["sold_in_period"], 1)
        self.assertEqual(report(docs, months=0)["sold_in_period"], 2)
        self.assertEqual(report(docs, ownership="all")["sold_in_period"], 2)
        self.assertEqual(report(docs, ownership="management")["item_count"], 1)


class SectionTests(unittest.TestCase):
    def test_aging_buckets_and_capital(self):
        r = report([
            item("NEW", bought=10, cost=1000),
            item("MID", bought=75, cost=2000, status="ON_SHELF"),
            item("OLD", bought=200, cost=4000),
            item("CONS", bought=120, cost=9000, source="CONSIGNMENT"),
            item("INB", bought=5, status="INBOUND"),
            item("SOLD", sale={"sold_at": ago(1), "sold_price": 1}),
        ])["aging"]
        self.assertEqual(r["labels"], ["0–30天", "31–60天", "61–90天", "91–180天", "180天以上"])
        self.assertEqual(r["counts"], [1, 0, 1, 1, 1])
        self.assertEqual(r["costs"], [1000, 0, 2000, 0, 4000])   # consignment ties up no capital
        self.assertEqual((r["stock_count"], r["consignment_count"], r["inbound_count"]), (4, 1, 1))
        self.assertEqual(r["buy_in_cost"], 7000)
        self.assertEqual((r["old_count"], r["old_cost"]), (2, 4000))
        self.assertEqual([i["sku"] for i in r["oldest"]], ["OLD", "CONS", "MID", "NEW"])

    def test_turnover(self):
        docs = [item(f"C{d}", bought=d + 2, sale={"sold_at": ago(2), "sold_price": 1500})
                for d in (5, 10, 40)]
        docs.append(item("H", brand="Hermes", bought=102, sale={"sold_at": ago(2), "sold_price": 1500}))
        t = report(docs)["turnover"]
        self.assertEqual(t["n"], 4)
        self.assertAlmostEqual(t["median"], 25)
        self.assertAlmostEqual(t["within_30"], 0.5)
        self.assertAlmostEqual(t["over_90"], 0.25)
        self.assertEqual(dict(zip(t["hist_labels"], t["hist_counts"]))["31–60天"], 1)
        self.assertEqual(t["months"], ["2026-11"])

    def test_returns_groups(self):
        docs = [item(f"C{i}", cost=1000, bought=22, sale={"sold_at": ago(2), "sold_price": 1300}) for i in range(3)]
        docs += [item("K", cost=4000, source="CONSIGNMENT", bought=42, sale={"sold_at": ago(2), "sold_price": 4400})]
        ret = report(docs)["returns"]
        chanel = ret["brands"][0]
        self.assertEqual((chanel["label"], chanel["n"]), ("Chanel", 4))
        bands = {b["label"]: b for b in ret["bands"]}
        self.assertEqual(bands["1,000–3,000"]["n"], 3)
        self.assertAlmostEqual(bands["1,000–3,000"]["margin"], 0.3)
        self.assertAlmostEqual(bands["1,000–3,000"]["annualized"], 0.3 * 365 / 20)
        self.assertEqual({s["label"]: s["n"] for s in ret["sources"]}, {"收货": 3, "寄卖": 1})
        self.assertEqual(ret["points"][0]["label"], "Chanel")

    def test_sourcing_merges_same_name(self):
        docs = [
            item("A", seller="Alice Tan", sale={"sold_at": ago(2), "sold_price": 1400}),
            item("B", seller="alice  tan", status="DAMAGED"),
            item("C", seller=""),
        ]
        s = report(docs)["sourcing"]
        self.assertEqual((s["seller_count"], s["unnamed"], s["total"]), (1, 1, 3))
        row = s["rows"][0]
        self.assertEqual((row["n"], row["sold"], row["problems"]), (2, 1, 1))
        self.assertAlmostEqual(row["profit"], 400)

    def test_discount(self):
        docs = [
            item("A", listing=1000, bought=12, sale={"sold_at": ago(2), "sold_price": 1000}),
            item("B", listing=1000, bought=122, sale={"sold_at": ago(2), "sold_price": 800}),
            item("C", sale={"sold_at": ago(2), "sold_price": 800}),          # no listing price
        ]
        d = report(docs)["discount"]
        self.assertEqual((d["n"], d["excluded"]), (2, 1))
        self.assertAlmostEqual(d["avg"], 0.1)
        self.assertAlmostEqual(d["at_list"], 0.5)
        self.assertEqual(dict(zip(d["age_labels"], d["age_avg"]))["91–180天"], 20.0)

    def test_channels_group_spellings(self):
        self.assertEqual(norm_channel(" IG story "), "Instagram")
        self.assertEqual(norm_channel("Walk-in"), "店内")
        self.assertEqual(norm_channel("delivery"), "delivery")   # "live" must not match inside a word
        self.assertEqual(norm_channel(None), "未填")
        docs = [
            item("A", sale={"sold_at": ago(2), "sold_price": 1000, "sale_channel": "ig"}),
            item("B", sale={"sold_at": ago(2), "sold_price": 3000, "sale_channel": "Instagram"}),
            item("C", sale={"sold_at": ago(2), "sold_price": 500, "sale_channel": "", "sold_by_username": ""}),
        ]
        c = report(docs)["channels"]
        self.assertEqual([(g["label"], g["n"]) for g in c["channel"]], [("Instagram", 2), ("未填", 1)])
        self.assertAlmostEqual(c["channel"][0]["share_revenue"], 4000 / 4500)
        self.assertAlmostEqual(c["channel_filled"], 2 / 3)
        self.assertAlmostEqual(c["staff_filled"], 2 / 3)

    def test_flow_steps_from_audit(self):
        audit = [status_change("INBOUND", 40), status_change("RECEIVED", 35),
                 status_change("ON_SHELF", 30), status_change("RECEIVED", 20)]
        docs = [
            item("A", bought=40, audit=audit, sale={"sold_at": ago(10), "sold_price": 1}),
            item("B", bought=9, audit=[{"action": "Change_details", "at": ago(9)}],
                 sale={"sold_at": ago(4), "sold_price": 1}),
        ]
        f = report(docs)["flow"]
        steps = {s["label"]: s for s in f["steps"]}
        self.assertAlmostEqual(steps["录入 → 到货"]["median"], 5)
        self.assertAlmostEqual(steps["到货 → 上架"]["median"], 5)
        self.assertAlmostEqual(steps["上架 → 售出"]["median"], 20)
        self.assertEqual(steps["收货 → 售出（全程）"]["n"], 2)
        self.assertEqual(f["sold_never_listed"], 1)

    def test_kpi_compares_same_days_of_month(self):
        def sold(sku, when, price):
            return item(sku, bought=400, cost=100, sale={"sold_at": when, "sold_price": price})

        docs = [
            sold("NOV", datetime(2026, 11, 10, 4, tzinfo=timezone.utc), 1000),
            sold("OCT-IN", datetime(2026, 10, 10, 4, tzinfo=timezone.utc), 400),
            sold("OCT-LATE", datetime(2026, 10, 25, 4, tzinfo=timezone.utc), 9999),   # after the 16th
            sold("LASTYEAR", datetime(2025, 11, 3, 4, tzinfo=timezone.utc), 500),
            # 31 Oct 17:00 UTC is already 1 Nov in UTC+8.
            sold("EDGE", datetime(2026, 10, 31, 17, tzinfo=timezone.utc), 200),
        ]
        k = report(docs)["kpi"]
        revenue = k["tiles"][0]
        self.assertEqual((k["month"], k["through"]), ("2026-11", "11-16"))
        self.assertAlmostEqual(revenue["value"], 1200)
        self.assertAlmostEqual(revenue["last_month"], 400)
        self.assertAlmostEqual(revenue["vs_last_month"], 2.0)
        self.assertAlmostEqual(revenue["last_year"], 500)
        self.assertEqual(k["tiles"][2]["value"], 2)

    def test_cash_flow_months(self):
        docs = [
            item("BUY", bought=1, cost=3000),
            item("SOLD", bought=45, cost=1000, sale={"sold_at": ago(2), "sold_price": 1500}),
            item("CONS", bought=45, cost=800, source="CONSIGNMENT", sale={"sold_at": ago(2), "sold_price": 1000}),
        ]
        c = report(docs)["cash"]
        self.assertEqual(len(c["months"]), 12)
        self.assertEqual(c["months"][-1], "2026-11")
        self.assertEqual((c["income"][-1], c["purchase"][-1], c["payout"][-1]), (2500, 3000, 800))
        self.assertEqual(c["net"][-1], -1300)
        self.assertEqual(c["purchase"][-2], 1000)          # bought in October
        self.assertEqual(c["cumulative"][-1], -2300)
        self.assertEqual(len(report(docs, months=0)["cash"]["months"]), 24)

    def test_consignment(self):
        docs = [
            item("IN", source="CONSIGNMENT", cost=2000, seller="Jo"),
            item("RECENT", source="CONSIGNMENT", cost=800, seller="Jo", bought=40,
                 sale={"sold_at": ago(10), "sold_price": 1000}),
            item("LONGAGO", source="CONSIGNMENT", cost=500, seller="Kim", bought=300,
                 sale={"sold_at": ago(200), "sold_price": 700}),
            item("BUY", cost=1),
        ]
        g = report(docs)["consignment"]
        self.assertEqual((g["total"], g["in_stock"], g["sold"], g["recent_n"]), (3, 1, 2, 1))
        self.assertEqual((g["in_stock_payout"], g["recent_payout"], g["fee_total"]), (2000, 800, 400))
        self.assertEqual([i["sku"] for i in g["recent"]], ["RECENT"])
        jo = g["people"][0]
        self.assertEqual((jo["label"], jo["in_stock"], jo["sold"], jo["fee"]), ("Jo", 1, 1, 200))

    def test_quality_checks(self):
        docs = [
            item("OK", sale={"sold_at": ago(2), "sold_price": 1500}, profit=500, profit_currency="SGD"),
            item("NOPROFIT", sale={"sold_at": ago(2), "sold_price": 1500, "buyer": "", "sale_channel": ""}),
            item("NORECORD", status="SOLD"),
            item("NOCOST", cost=0, brand="", seller="", serial_code=""),
        ]
        checks = {c["label"]: c["count"] for c in report(docs)["quality"]["checks"]}
        self.assertEqual(checks["已售出，但利润没填"], 2)
        self.assertEqual(checks["已售出，但没有售出记录"], 1)
        self.assertEqual(checks["成本是 0 或没填"], 1)
        self.assertEqual(checks["没有填品牌"], 1)
        self.assertEqual(checks["没有填卖家 / 寄卖人"], 1)
        self.assertEqual(checks["已售出，但没填销售渠道"], 1)
        self.assertEqual(checks["已售出，但没填买家"], 1)
        self.assertEqual(checks["在库，但没有包身编码"], 1)
        self.assertEqual(checks["没有收货日期"], 0)

    def test_messy_documents_never_crash(self):
        docs = [
            {}, {"status": "SOLD"}, {"status": "SOLD", "sold_record": [{}]},
            {"sku": "S", "status": "SOLD", "cost": "1,200", "cost_currency": "cny",
             "purchase_at": "2026-01-02T03:04:05Z", "audit": ["junk", {"action": "legacy", "detail": "x"}],
             "sold_record": [{"sold_at": "2026-02-01T00:00:00", "sold_price": "300", "sold_currency": "sgd"}]},
            {"sku": "R", "status": "RECEIVED", "cost": None, "sold_record": "oops", "audit": None, "purchase_at": 123},
        ]
        for opts in ({}, {"ownership": "all", "months": 0, "ccy": "RMB", "profit_basis": "computed"}):
            self.assertEqual(report(docs, **opts)["item_count"], 5)
        self.assertEqual(report([])["quality"]["issue_total"], 0)


class ReadOnlyItems:
    """Only find() exists: any attempt to write raises AttributeError."""

    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def find(self, query, projection=None):
        self.calls.append((query, projection))
        return iter(self.docs)


LAYOUT_ENDPOINTS = (
    "index", "logout", "login", "list_items", "item_new_buyin_form",
    "item_new_consignment_form", "analytics", "audit_history", "notes_list",
    "users_list", "admin_management_payments", "management_index",
    "management_list_items", "management_item_new_buyin_form",
    "management_item_new_consignment_form",
)


class RouteTests(unittest.TestCase):
    def _client(self, items, role=ROLE_ADMIN):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if v else ""
        for endpoint in LAYOUT_ENDPOINTS:
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        app.add_url_rule("/items/<item_key>", "item_detail", lambda item_key: "x")
        register(app, items)
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="u")
        return client

    def _docs(self):
        now = datetime.now(timezone.utc)
        return [
            item("STOCK1", bought=0, purchase_at=now - timedelta(days=120), created_at=now - timedelta(days=120)),
            item("SOLD01", listing=1500, purchase_at=now - timedelta(days=40), created_at=now - timedelta(days=40),
                 sale={"sold_at": now - timedelta(days=3), "sold_price": 1400}),
            item("CONS01", source="CONSIGNMENT", seller="<b>Jo</b>", purchase_at=now - timedelta(days=30),
                 created_at=now - timedelta(days=30), sale={"sold_at": now - timedelta(days=2), "sold_price": 1300}),
        ]

    def test_page_renders_all_eleven_sections_read_only(self):
        items = ReadOnlyItems(self._docs())
        response = self._client(items).get("/claude-analytics")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        for number in range(1, 12):
            self.assertIn(f'id="ca-{number}"', html)
        self.assertIn('href="/claude-analytics">Claude新增分析</a>', html)
        self.assertIn("STOCK1", html)
        self.assertIn("&lt;b&gt;Jo&lt;/b&gt;", html)          # user text is escaped
        self.assertNotIn("<b>Jo</b>", html)
        # Exactly one read, with a projection, and nothing else touched.
        self.assertEqual(len(items.calls), 1)
        query, projection = items.calls[0]
        self.assertEqual(query, {})
        self.assertIn("sold_record", projection)

    def test_filters_and_bad_parameters(self):
        items = ReadOnlyItems(self._docs())
        client = self._client(items)
        ok = client.get("/claude-analytics?ownership=all&months=0&ccy=rmb&sgd_rmb=5.1&usd_sgd=1.35&profit=computed")
        self.assertEqual(ok.status_code, 200)
        self.assertIn("全部时间", ok.get_data(as_text=True))
        bad = client.get("/claude-analytics?ownership=x&months=abc&ccy=EUR&sgd_rmb=0&usd_sgd=nan&profit=?")
        self.assertEqual(bad.status_code, 200)
        self.assertIn("近 12 个月", bad.get_data(as_text=True))

    def test_empty_database_renders(self):
        response = self._client(ReadOnlyItems([])).get("/claude-analytics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("目前没有在库商品", response.get_data(as_text=True))

    def test_admin_only(self):
        for role in (ROLE_MANAGEMENT, ROLE_STAFF):
            items = ReadOnlyItems(self._docs())
            client = self._client(items, role=role)
            self.assertEqual(client.get("/claude-analytics").status_code, 403)
            self.assertEqual(items.calls, [])
            self.assertNotIn("Claude新增分析", client.get("/_index").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
