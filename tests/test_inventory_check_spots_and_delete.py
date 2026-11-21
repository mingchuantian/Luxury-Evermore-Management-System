"""老板间 positions (货架/地面/沙发) and deleting an inventory check."""

import unittest
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from flask import Flask

from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF
from luxury_app.constants import OWNERSHIP_MANAGEMENT
from luxury_app.inventory_scan_history import ROOM_SPOTS, scan_history_for_skus
from luxury_app.routes.inventory_check import register as register_inventory_check
from luxury_app.routes.items import register as register_items

T0 = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
ADMIN_LAYOUT = (
    "index", "logout", "login", "analytics", "audit_history", "notes_list",
    "users_list", "admin_management_payments", "management_index",
    "management_list_items", "management_item_new_buyin_form",
    "management_item_new_consignment_form", "sale_new_form",
)


def _scan(sku, at, spot=None):
    scan = {"sku": sku, "name": f"bag {sku}", "status": "RECEIVED", "scanned_at": at}
    if spot is not None:
        scan["spot"] = spot
    return scan


def _session(check_type, scans, status="completed", started=T0):
    return {
        "_id": ObjectId(), "status": status, "check_type": check_type,
        "started_at": started, "completed_at": None if status == "active" else started + timedelta(hours=1),
        "updated_at": started, "started_by": {"username": "boss"},
        "scanned_skus": [s["sku"] for s in scans], "scans": scans,
    }


class Base(unittest.TestCase):
    def setUp(self):
        try:
            import mongomock
        except ImportError:
            self.skipTest("mongomock not installed")
        self.db = mongomock.MongoClient()["t"]
        self.db.items.insert_many([
            {"sku": f"BAG{i:03d}", "name": f"bag {i}", "brand": "Chanel", "status": "RECEIVED",
             "created_at": T0 - timedelta(days=30), "ownership": OWNERSHIP_MANAGEMENT if i == 9 else "admin"}
            for i in range(1, 10)
        ])
        self.sessions = self.db.inventory_check_sessions

    def _app(self):
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in ADMIN_LAYOUT:
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        register_items(app, self.db.items, inventory_check_sessions=self.sessions)
        register_inventory_check(app, self.db.items, self.sessions)
        return app

    def _client(self, role=ROLE_ADMIN, app=None):
        client = (app or self._app()).test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="boss")
        return client


class RoomSpotTests(Base):
    def test_scan_records_current_spot(self):
        room = _session("room", [], status="active")
        self.sessions.insert_one(room)
        client = self._client()
        url = f"/inventory-check/sessions/{room['_id']}/scan"
        first = client.post(url, json={"sku": "bag001", "spot": "shelf_a"}).json
        self.assertEqual(first["scan"]["spot"], "shelf_a")
        client.post(url, json={"sku": "BAG002", "spot": "made_up"})   # unknown spot → none
        client.post(url, json={"sku": "BAG003"})
        again = client.post(url, json={"sku": "BAG001", "spot": "sofa"}).json
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["scan"]["spot"], "shelf_a")             # keeps where it was found
        scans = self.sessions.find_one({"_id": room["_id"]})["scans"]
        self.assertEqual([s.get("spot") for s in scans], ["shelf_a", None, None])

    def test_change_spot_during_and_after_check(self):
        room = _session("room", [_scan("BAG001", T0, "shelf_a"), _scan("BAG002", T0)])
        self.sessions.insert_one(room)
        client = self._client()
        url = f"/inventory-check/sessions/{room['_id']}/spot"
        response = client.post(url, json={"sku": "BAG002", "spot": "floor"})
        self.assertEqual(response.json, {"ok": True, "sku": "BAG002", "spot": "floor", "spot_label": "地面"})
        client.post(url, json={"sku": "BAG001", "spot": ""})            # back to 未选位置
        scans = self.sessions.find_one({"_id": room["_id"]})["scans"]
        self.assertEqual([s.get("spot") for s in scans], ["", "floor"])
        # Other scan data is untouched.
        self.assertEqual([s["sku"] for s in scans], ["BAG001", "BAG002"])
        self.assertEqual(scans[1]["scanned_at"].replace(tzinfo=timezone.utc), T0)

    def test_change_spot_is_validated(self):
        room = _session("room", [_scan("BAG001", T0)])
        store = _session("store", [_scan("BAG002", T0)])
        self.sessions.insert_many([room, store])
        client = self._client()
        self.assertEqual(client.post(f"/inventory-check/sessions/{room['_id']}/spot",
                                     json={"sku": "BAG001", "spot": "roof"}).status_code, 400)
        self.assertEqual(client.post(f"/inventory-check/sessions/{room['_id']}/spot",
                                     json={"sku": "BAG009", "spot": "sofa"}).status_code, 404)
        self.assertEqual(client.post(f"/inventory-check/sessions/{store['_id']}/spot",
                                     json={"sku": "BAG002", "spot": "sofa"}).status_code, 404)
        self.assertEqual(client.post("/inventory-check/sessions/nope/spot",
                                     json={"sku": "BAG001", "spot": "sofa"}).status_code, 404)
        staff = self._client(ROLE_STAFF)
        self.assertEqual(staff.post(f"/inventory-check/sessions/{room['_id']}/spot",
                                    json={"sku": "BAG001", "spot": "sofa"}).status_code, 403)

    def test_active_room_page_has_dropdowns_defaulting_to_last_spot(self):
        room = _session("room", [_scan("BAG001", T0, "shelf_a"), _scan("BAG002", T0, "shelf_3")],
                        status="active")
        self.sessions.insert_one(room)
        html = self._client().get(f"/inventory-check/sessions/{room['_id']}").get_data(as_text=True)
        current = html.split('<select id="current-spot">', 1)[1].split("</select>", 1)[0]
        for _, zh, _ in ROOM_SPOTS:
            self.assertIn(zh, current)
        self.assertIn('<option value="shelf_3" selected>货架3</option>', current)
        self.assertEqual(html.count('class="js-spot spot-row-select" data-sku="BAG'), 2)

    def test_store_check_started_without_spots_keeps_old_scans(self):
        # A store check already half done before spots existed.
        store = _session("store", [_scan("BAG001", T0), _scan("BAG002", T0)], status="active")
        self.sessions.insert_one(store)
        client = self._client()
        html = client.get(f"/inventory-check/sessions/{store['_id']}").get_data(as_text=True)
        current = html.split('<select id="current-spot">', 1)[1].split("</select>", 1)[0]
        for zh in ("直播墙", "老板间墙", "中岛柜", "门口和对外展柜", "沙发"):
            self.assertIn(zh, current)
        self.assertNotIn("货架A", current)
        self.assertIn('<option value="" selected>未选位置</option>', current)
        self.assertEqual(html.count('class="js-spot spot-row-select" data-sku="BAG'), 2)

        url = f"/inventory-check/sessions/{store['_id']}/scan"
        new = client.post(url, json={"sku": "BAG003", "spot": "island"}).json
        self.assertEqual(new["scan"]["spot"], "island")
        client.post(url, json={"sku": "BAG004", "spot": "shelf_a"})     # a back-room spot: dropped
        doc = self.sessions.find_one({"_id": store["_id"]})
        self.assertEqual([s.get("spot") for s in doc["scans"]], [None, None, "island", None])
        untouched = {k: v for k, v in doc["scans"][0].items() if k != "scanned_at"}
        self.assertEqual(untouched, {k: v for k, v in store["scans"][0].items() if k != "scanned_at"})
        self.assertEqual(doc["status"], "active")
        # Default for the next scan is the last spot used.
        html = client.get(f"/inventory-check/sessions/{store['_id']}").get_data(as_text=True)
        self.assertIn('<option value="island" selected>中岛柜</option>', html.split('<select id="current-spot">', 1)[1])

    def test_spots_never_cross_between_kinds(self):
        store = _session("store", [_scan("BAG001", T0)])
        room = _session("room", [_scan("BAG002", T0)], status="active")
        self.sessions.insert_many([store, room])
        client = self._client()
        spot = lambda sid, sku, value: client.post(f"/inventory-check/sessions/{sid}/spot",
                                                   json={"sku": sku, "spot": value})
        self.assertEqual(spot(store["_id"], "BAG001", "entrance").json["spot_label"], "门口和对外展柜")
        self.assertEqual(spot(store["_id"], "BAG001", "sofa").status_code, 404)        # back-room sofa
        self.assertEqual(spot(room["_id"], "BAG002", "store_sofa").status_code, 404)   # store sofa
        self.assertEqual(spot(store["_id"], "BAG001", "").status_code, 200)
        room_scan = client.post(f"/inventory-check/sessions/{room['_id']}/scan",
                                json={"sku": "BAG003", "spot": "live_wall"}).json
        self.assertNotIn("spot", room_scan["scan"])

    def test_store_spot_shows_in_results_and_item_pages(self):
        store = _session("store", [_scan("BAG001", T0, "live_wall"), _scan("BAG002", T0, "store_sofa"),
                                   _scan("BAG003", T0)])
        self.sessions.insert_one(store)
        client = self._client()
        html = client.get(f"/inventory-check/sessions/{store['_id']}").get_data(as_text=True)
        chips = html.split('id="spot-chips"', 1)[1].split("</div>", 1)[0]
        self.assertIn('直播墙 <span data-spot-count="live_wall">1</span>', chips)
        self.assertIn('沙发 <span data-spot-count="store_sofa">1</span>', chips)
        self.assertIn('未选位置 <span data-spot-count="">1</span>', chips)
        # Not on the website (no item is linked here): listed with where it sits.
        missing = html.split("店内陈列但未上 website 的商品", 1)[1].split("</table>", 1)[0]
        self.assertIn("<td>直播墙</td>", missing)
        self.assertIn("<td>未选位置</td>", missing)
        history = scan_history_for_skus(self.sessions, ["BAG001", "BAG002"])
        self.assertEqual((history["BAG001"][0]["spot_label"], history["BAG001"][0]["spot_label_en"]),
                         ("直播墙", "Live-stream wall"))
        self.assertEqual(history["BAG002"][0]["spot_label"], "沙发")
        row = client.get("/items").get_data(as_text=True).split("BAG001", 1)[1].split("</tr>", 1)[0]
        self.assertIn("店内陈列 · <b>直播墙</b>", row)

    def test_completed_room_page_groups_by_spot(self):
        room = _session("room", [_scan("BAG001", T0, "shelf_a"), _scan("BAG002", T0, "shelf_a"),
                                 _scan("BAG003", T0, "sofa"), _scan("BAG004", T0)])
        self.sessions.insert_one(room)
        html = self._client().get(f"/inventory-check/sessions/{room['_id']}").get_data(as_text=True)
        chips = html.split('id="spot-chips"', 1)[1].split("</div>", 1)[0]
        self.assertIn("全部 4", chips)
        self.assertIn('货架A <span data-spot-count="shelf_a">2</span>', chips)
        self.assertIn('沙发 <span data-spot-count="sofa">1</span>', chips)
        self.assertIn('未选位置 <span data-spot-count="">1</span>', chips)
        self.assertNotIn("货架B", chips)
        self.assertEqual(html.count('<tr data-spot="shelf_a">'), 2)
        self.assertIn("<th>位置</th>", html)

    def test_history_and_item_pages_show_spot(self):
        self.sessions.insert_many([
            _session("store", [_scan("BAG001", T0)]),
            _session("room", [_scan("BAG001", T0 + timedelta(days=1), "shelf_b"),
                              _scan("BAG002", T0 + timedelta(days=1))], started=T0 + timedelta(days=1)),
        ])
        history = scan_history_for_skus(self.sessions, ["BAG001", "BAG002"])
        self.assertEqual((history["BAG001"][0]["spot_label"], history["BAG001"][0]["spot_label_en"]),
                         ("货架B", "Shelf B"))
        self.assertEqual(history["BAG001"][1]["spot"], "")
        self.assertEqual(history["BAG002"][0]["spot_label"], "")

        client = self._client()
        row = client.get("/items").get_data(as_text=True).split("BAG001", 1)[1].split("</tr>", 1)[0]
        self.assertIn("老板间 · <b>货架B</b>", row)
        detail = client.get("/items/BAG001").get_data(as_text=True)
        self.assertIn("2026-11-11 10:00 - 老板间 · <b>货架B</b>", detail)
        self.assertIn("2026-11-10 10:00 - 店内陈列\n", detail)

    def test_management_sees_spot_in_english(self):
        from luxury_app.routes.management import register as register_management
        from luxury_app.routes.sales import register as register_sales

        self.sessions.insert_one(_session("room", [_scan("BAG009", T0, "sofa")]))
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        for endpoint in ("index", "logout", "login", "list_items", "item_new_buyin_form",
                         "item_new_consignment_form", "analytics", "audit_history",
                         "notes_list", "users_list"):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        register_management(app, self.db.items, inventory_check_sessions=self.sessions)
        register_sales(app, self.db.items)
        client = self._client(ROLE_MANAGEMENT, app=app)
        listing = client.get("/management/items").get_data(as_text=True)
        self.assertIn("Back room · <b>Sofa</b>", listing.split("BAG009", 1)[1].split("</tr>", 1)[0])
        self.assertNotIn("沙发", listing)
        detail = client.get("/management/items/BAG009").get_data(as_text=True)
        self.assertIn("Back room · <b>Sofa</b>", detail)


class ReopenCheckTests(Base):
    def test_reopen_keeps_scans_and_allows_more(self):
        room = _session("room", [_scan("BAG001", T0, "shelf_a"), _scan("BAG002", T0, "sofa")])
        self.sessions.insert_one(room)
        client = self._client()
        page = client.get(f"/inventory-check/sessions/{room['_id']}").get_data(as_text=True)
        self.assertIn("重新打开盘查（继续扫描）", page)
        self.assertIn("已扫描的 2 件", page)

        response = client.post(f"/inventory-check/sessions/{room['_id']}/reopen")
        self.assertEqual(response.status_code, 302)
        doc = self.sessions.find_one({"_id": room["_id"]})
        self.assertEqual(doc["status"], "active")
        self.assertNotIn("completed_at", doc)
        self.assertEqual([(s["sku"], s["spot"]) for s in doc["scans"]], [("BAG001", "shelf_a"), ("BAG002", "sofa")])

        page = client.get(f"/inventory-check/sessions/{room['_id']}").get_data(as_text=True)
        self.assertIn("正在盘查库存", page)
        self.assertNotIn("重新打开盘查（继续扫描）", page)
        url = f"/inventory-check/sessions/{room['_id']}/scan"
        self.assertTrue(client.post(url, json={"sku": "BAG001", "spot": "floor"}).json["duplicate"])
        added = client.post(url, json={"sku": "BAG003", "spot": "floor"}).json
        self.assertFalse(added["duplicate"])
        self.assertEqual(added["scan_count"], 3)

        client.post(f"/inventory-check/sessions/{room['_id']}/complete")
        doc = self.sessions.find_one({"_id": room["_id"]})
        self.assertEqual(doc["status"], "completed")
        self.assertGreater(doc["completed_at"].replace(tzinfo=timezone.utc), T0 + timedelta(hours=1))
        self.assertEqual(doc["scanned_skus"], ["BAG001", "BAG002", "BAG003"])
        self.assertEqual(doc["scans"][0]["spot"], "shelf_a")       # earlier spot untouched

    def test_only_typed_completed_checks_by_admin(self):
        legacy = _session(None, [_scan("BAG001", T0)])
        legacy.pop("check_type")
        legacy["missing_items"] = []
        active = _session("store", [], status="active")
        store = _session("store", [_scan("BAG002", T0)])
        self.sessions.insert_many([legacy, active, store])
        client = self._client()
        client.post(f"/inventory-check/sessions/{legacy['_id']}/reopen")
        self.assertEqual(self.sessions.find_one({"_id": legacy["_id"]})["status"], "completed")
        self.assertNotIn("重新打开盘查（继续扫描）", client.get(f"/inventory-check/sessions/{legacy['_id']}").get_data(as_text=True))
        client.post(f"/inventory-check/sessions/{active['_id']}/reopen")
        self.assertEqual(self.sessions.find_one({"_id": active["_id"]})["status"], "active")
        self.assertEqual(client.post("/inventory-check/sessions/nope/reopen").status_code, 302)
        staff = self._client(ROLE_STAFF)
        self.assertEqual(staff.post(f"/inventory-check/sessions/{store['_id']}/reopen").status_code, 403)
        self.assertEqual(self.sessions.find_one({"_id": store["_id"]})["status"], "completed")


class NotInLastCheckFilterTests(Base):
    URL = "/items?status=NOT_IN_LAST_CHECK"

    def _skus(self, html):
        import re
        body = html.split("<tbody", 1)[-1] if "<tbody" in html else html.split("</thead>", 1)[-1]
        return sorted(set(re.findall(r"\b(BAG\d{3})\b", body.split("</table>", 1)[0])))

    def test_lists_received_and_on_shelf_items_missed_by_latest_checks(self):
        self.db.items.update_one({"sku": "BAG006"}, {"$set": {"status": "ON_SHELF"}})
        self.db.items.update_one({"sku": "BAG007"}, {"$set": {"status": "SOLD"}})
        self.db.items.update_one({"sku": "BAG008"}, {"$set": {"status": "INBOUND"}})
        old_store = _session("store", [_scan(f"BAG00{i}", T0) for i in range(1, 7)])
        new_store = _session("store", [_scan("BAG001", T0), _scan("BAG002", T0)], started=T0 + timedelta(days=1))
        room = _session("room", [_scan("BAG003", T0, "sofa")], started=T0 + timedelta(days=2))
        active_room = _session("room", [_scan("BAG004", T0)], status="active", started=T0 + timedelta(days=3))
        legacy = _session("store", [_scan("BAG005", T0)], started=T0 + timedelta(days=4))
        legacy.pop("check_type")
        self.sessions.insert_many([old_store, new_store, room, active_room, legacy])

        html = self._client().get(self.URL).get_data(as_text=True)
        # Seen in latest store (1, 2), latest room (3) or the room check in progress (4): left out.
        # 7 sold, 8 inbound: not this status. 5 only in a legacy check, 6 only in an older one.
        self.assertEqual(self._skus(html), ["BAG005", "BAG006", "BAG009"])
        self.assertIn('<option value="NOT_IN_LAST_CHECK" selected>已到货/上架 但未出现在最近一次盘库</option>', html)
        note = html.split('class="last-check-note"', 1)[1].split("</div>\n{% endif %}", 1)[0]
        self.assertIn(f'/inventory-check/sessions/{new_store["_id"]}', note)
        self.assertIn(f'/inventory-check/sessions/{room["_id"]}', note)
        self.assertNotIn(str(old_store["_id"]), note)
        self.assertIn(f'/inventory-check/sessions/{active_room["_id"]}', note)
        self.assertIn("进行中</span>", note)
        self.assertIn("目前已扫 1 件", note)
        # Combines with search, type and paging links.
        html = self._client().get(self.URL + "&q=BAG00").get_data(as_text=True)
        self.assertEqual(self._skus(html), ["BAG005", "BAG006", "BAG009"])
        self.assertEqual(self._skus(self._client().get(self.URL + "&ownership=management").get_data(as_text=True)),
                         ["BAG009"])

    def test_bags_scanned_in_both_are_shown_with_an_explanation(self):
        store = _session("store", [_scan("BAG001", T0 + timedelta(hours=5), "live_wall"),
                                   _scan("BAG002", T0 + timedelta(hours=5))], started=T0 + timedelta(hours=4))
        room = _session("room", [_scan("BAG001", T0 + timedelta(hours=1), "shelf_a"),
                                 _scan("BAG002", T0 + timedelta(hours=9), "sofa"),
                                 _scan("BAG003", T0 + timedelta(hours=1))])
        self.sessions.insert_many([store, room])
        client = self._client()
        for html in (client.get(self.URL).get_data(as_text=True),
                     client.get(f"/inventory-check/compare?store={store['_id']}&room={room['_id']}").get_data(as_text=True)):
            box = html.split('class="both-box"', 1)[1].split("</table>", 1)[0]
            self.assertIn("两处都扫到的包（2 件）", box)
            self.assertIn("不算丢失", box)
            rows = box.split("<tr>")[2:]
            # BAG001: room 11:00 → store 15:00, so the store scan wins.
            self.assertIn("2026-11-10 11:00 · 货架A", rows[0])
            self.assertIn("2026-11-10 15:00 · 直播墙\n            <span class=\"both-tag\">✓ 以此为准</span>", rows[0])
            self.assertIn("<b>店内陈列 · 直播墙</b>", rows[0])
            # BAG002: moved back into the room later.
            self.assertIn("<b>老板间 · 沙发</b>", rows[1])
            self.assertNotIn("BAG003", box)
        # Neither is listed as missing.
        self.assertEqual(self._skus(client.get(self.URL).get_data(as_text=True).split('id="both-box"')[1]
                                    .split("</table>", 1)[1]), ["BAG004", "BAG005", "BAG006", "BAG007", "BAG008", "BAG009"])

    def test_check_in_progress_counts_too(self):
        older_active = _session("store", [_scan("BAG005", T0)], status="active")
        active = _session("store", [_scan("BAG001", T0 + timedelta(hours=3), "island")],
                          status="active", started=T0 + timedelta(hours=2))
        room = _session("room", [_scan("BAG001", T0 + timedelta(hours=1), "shelf_b"), _scan("BAG002", T0)])
        self.sessions.insert_many([older_active, active, room])
        html = self._client().get(self.URL).get_data(as_text=True)
        missing = self._skus(html.split('id="both-box"')[1].split("</table>", 1)[1])
        self.assertNotIn("BAG001", missing)   # scanned in the store check in progress
        self.assertNotIn("BAG002", missing)   # scanned in the finished room check
        self.assertIn("BAG005", missing)      # only in an older in-progress check
        self.assertNotIn(str(older_active["_id"]), html)
        box = html.split('class="both-box"', 1)[1].split("</table>", 1)[0]
        self.assertIn("两处都扫到的包（1 件）", box)
        self.assertIn("<b>店内陈列 · 中岛柜</b>", box)

    def test_without_finished_checks_lists_all_and_says_so(self):
        html = self._client().get(self.URL).get_data(as_text=True)
        self.assertIn("还没有已结束的盘查", html)
        self.assertEqual(len(self._skus(html)), 9)
        self.sessions.insert_one(_session("room", [_scan("BAG001", T0)]))
        html = self._client().get(self.URL).get_data(as_text=True)
        self.assertIn("还没有店内陈列盘查", html)
        self.assertNotIn("BAG001", self._skus(html))

    def test_admin_only(self):
        html = self._client(ROLE_STAFF).get(self.URL).get_data(as_text=True)
        self.assertNotIn("NOT_IN_LAST_CHECK", html)
        self.assertNotIn('class="last-check-note"', html)


class DeleteCheckTests(Base):
    def test_delete_removes_the_check_and_everything_derived_from_it(self):
        store = _session("store", [_scan("BAG001", T0)])
        room = _session("room", [_scan("BAG001", T0 + timedelta(days=1), "shelf_a")],
                        started=T0 + timedelta(days=1))
        self.sessions.insert_many([store, room])
        audit_before = self.db.audit_logs.count_documents({})
        client = self._client()

        response = client.post(f"/inventory-check/sessions/{room['_id']}/delete", data={"page": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/inventory-check?page=1", response.headers["Location"])
        self.assertIn("已删除这次盘查记录。", client.get(response.headers["Location"]).get_data(as_text=True))
        self.assertIsNone(self.sessions.find_one({"_id": room["_id"]}))
        self.assertIsNotNone(self.sessions.find_one({"_id": store["_id"]}))

        # 盘查记录 on the item: only the store check remains.
        detail = client.get("/items/BAG001").get_data(as_text=True)
        section = detail.split("<h3>盘查记录</h3>", 1)[1].split("<h3>操作记录</h3>", 1)[0]
        self.assertNotIn("老板间", section)
        self.assertIn("店内陈列", section)
        # The deleted check's own page and any comparison with it are gone.
        self.assertEqual(client.get(f"/inventory-check/sessions/{room['_id']}").status_code, 404)
        self.assertEqual(client.get(
            f"/inventory-check/compare?store={store['_id']}&room={room['_id']}").status_code, 400)
        # It is no longer offered for comparison, and nothing is logged about it.
        history = client.get("/inventory-check").get_data(as_text=True)
        self.assertNotIn(str(room["_id"]), history)
        self.assertEqual(self.db.audit_logs.count_documents({}), audit_before)

    def test_deleting_the_only_check_leaves_no_trace(self):
        room = _session("room", [_scan("BAG001", T0, "sofa")], status="active")
        self.sessions.insert_one(room)
        client = self._client()
        client.post(f"/inventory-check/sessions/{room['_id']}/delete")
        self.assertEqual(self.sessions.count_documents({}), 0)
        row = client.get("/items").get_data(as_text=True).split("BAG001", 1)[1].split("</tr>", 1)[0]
        self.assertIn("无", row)
        # A scan arriving from a phone still on the deleted check is refused.
        late = client.post(f"/inventory-check/sessions/{room['_id']}/scan", json={"sku": "BAG002"})
        self.assertEqual(late.status_code, 404)
        self.assertEqual(self.sessions.count_documents({}), 0)

    def test_history_has_delete_button_with_confirmation(self):
        room = _session("room", [_scan("BAG001", T0)])
        self.sessions.insert_one(room)
        html = self._client().get("/inventory-check").get_data(as_text=True)
        form = html.split(f'action="/inventory-check/sessions/{room["_id"]}/delete"', 1)[1].split("</form>", 1)[0]
        self.assertIn('onsubmit="return confirm(this.dataset.confirm);"', form)
        self.assertIn("确认删除这次老板间盘查", form)
        self.assertIn("无法恢复", form)
        self.assertIn(">删除</button>", form)

    def test_only_admin_and_bad_ids(self):
        room = _session("room", [_scan("BAG001", T0)])
        self.sessions.insert_one(room)
        staff = self._client(ROLE_STAFF)
        self.assertEqual(staff.post(f"/inventory-check/sessions/{room['_id']}/delete").status_code, 403)
        self.assertEqual(self.sessions.count_documents({}), 1)
        client = self._client()
        for bad in ("nope", str(ObjectId())):
            response = client.post(f"/inventory-check/sessions/{bad}/delete", data={"page": "abc"})
            self.assertEqual(response.status_code, 302)
            self.assertIn("page=1", response.headers["Location"])
        self.assertEqual(self.sessions.count_documents({}), 1)
        self.assertIn("不存在或已被删除", client.get("/inventory-check").get_data(as_text=True))

    def test_page_past_the_end_after_deleting_shows_last_page(self):
        self.sessions.insert_many([
            _session("store", [], started=T0 + timedelta(minutes=i)) for i in range(21)
        ])
        html = self._client().get("/inventory-check?page=3").get_data(as_text=True)
        self.assertIn("第 2 / 2 页", html)


if __name__ == "__main__":
    unittest.main()
