"""Item photos/videos in OSS: keys, uploads, listing, zip, and who may use it."""

import io
import unittest
import zipfile

from bson import ObjectId
from flask import Flask

from luxury_app import media_store as ms
from luxury_app.auth import ROLE_ADMIN, ROLE_MANAGEMENT, ROLE_STAFF


class FakeStore:
    """In-memory stand-in for OssMediaStore."""

    def __init__(self, error=None):
        self.objects = {}        # key -> bytes
        self.uploads = {}        # upload_id -> {"key", "parts": {n: bytes}}
        self.error = error
        self.deleted = []

    def configuration_error(self):
        return self.error

    def put_url(self, key, content_type):
        return f"https://oss.test/{key}?put&ct={content_type}"

    def part_url(self, key, upload_id, n):
        return f"https://oss.test/{key}?partNumber={n}&uploadId={upload_id}"

    def get_url(self, key, process="", attachment_name=""):
        return f"https://oss.test/{key}?p={process}&a={attachment_name}"

    def start_multipart(self, key, content_type):
        upload_id = f"up{len(self.uploads) + 1}"
        self.uploads[upload_id] = {"key": key, "parts": {}}
        return upload_id

    def complete_multipart(self, key, upload_id):
        upload = self.uploads.pop(upload_id)
        assert upload["key"] == key
        self.objects[key] = b"".join(upload["parts"][n] for n in sorted(upload["parts"]))
        return len(upload["parts"])

    def abort_multipart(self, key, upload_id):
        self.uploads.pop(upload_id, None)

    def list(self, prefix):
        return [{"key": k, "size": len(v), "last_modified": 1795000000}
                for k, v in self.objects.items() if k.startswith(prefix)]

    def exists(self, key):
        return key in self.objects

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    def read_chunks(self, key, chunk_size=1024 * 1024):
        data = self.objects[key]
        for i in range(0, len(data), 7):
            yield data[i:i + 7]


class Items:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return dict(doc)
        return None


ITEMS = [
    {"_id": ObjectId(), "sku": "ABC1234", "name": "Classic Flap", "brand": "Chanel", "status": "RECEIVED", "ownership": "admin"},
    {"_id": ObjectId(), "sku": "MGT7777", "name": "Kelly", "brand": "Hermes", "status": "RECEIVED", "ownership": "management"},
]


class HelperTests(unittest.TestCase):
    def test_keys(self):
        key = ms.new_key("ABC1234", "detail_raw", "IMG 0001 (1).HEIC", "image/heic")
        self.assertTrue(key.startswith("item-media/ABC1234/detail_raw/"))
        self.assertTrue(key.endswith("-IMG-0001-1.heic"))
        self.assertTrue(ms.key_belongs_to(key, "ABC1234"))
        self.assertFalse(ms.key_belongs_to(key, "XYZ9999"))
        self.assertFalse(ms.key_belongs_to("item-media/ABC1234/other/x.jpg", "ABC1234"))
        self.assertFalse(ms.key_belongs_to("item-media/ABC1234/detail_raw/../../x", "ABC1234"))
        self.assertFalse(ms.key_belongs_to("item-media/ABC1234/detail_raw/a/b.jpg", "ABC1234"))
        self.assertEqual(ms.safe_filename("照片.jpg"), "file.jpg")
        self.assertEqual(ms.safe_filename("", "video/quicktime"), "file.mov")

    def test_check_upload(self):
        self.assertIsNone(ms.check_upload("main_raw", "a.jpg", "image/jpeg", 10))
        self.assertIsNone(ms.check_upload("detail_raw", "a.HEIC", "", 10))          # iOS sometimes sends no type
        self.assertIsNone(ms.check_upload("video", "clip.mov", "video/quicktime", 10))
        self.assertEqual(ms.check_upload("video", "a.jpg", "image/jpeg", 10), "这里只能上传视频")
        self.assertEqual(ms.check_upload("main_raw", "a.mov", "video/quicktime", 10), "这里只能上传图片")
        self.assertEqual(ms.check_upload("nope", "a.jpg", "image/jpeg", 10), "未知的媒体类别")
        self.assertEqual(ms.check_upload("main_raw", "a.jpg", "image/jpeg", 0), "文件是空的")
        self.assertEqual(ms.check_upload("main_raw", "a.jpg", "image/jpeg", ms.MAX_IMAGE_BYTES + 1), "文件太大")

    def test_plan_single_and_multipart(self):
        store = FakeStore()
        single = ms.plan_upload(store, "ABC1234", "main_raw", "a.jpg", "image/jpeg", 5_000_000)
        self.assertEqual(single["mode"], "single")
        self.assertEqual(single["headers"], {"Content-Type": "image/jpeg"})
        big = 3 * ms.PART_SIZE + 5
        multi = ms.plan_upload(store, "ABC1234", "video", "v.mov", "video/quicktime", big)
        self.assertEqual(multi["mode"], "multipart")
        self.assertEqual([p["number"] for p in multi["parts"]], [1, 2, 3, 4])
        self.assertEqual(multi["part_size"], ms.PART_SIZE)
        with self.assertRaises(ValueError):
            ms.plan_upload(store, "ABC1234", "video", "a.jpg", "image/jpeg", 10)

    def test_list_groups_and_names(self):
        store = FakeStore()
        store.objects = {
            "item-media/ABC1234/detail_raw/20261125-100000-aaaaaa-IMG_1.jpg": b"1",
            "item-media/ABC1234/detail_raw/20261125-100100-bbbbbb-IMG_2.jpg": b"22",
            "item-media/ABC1234/video/20261125-100200-cccccc-clip.mov": b"333",
            "item-media/ABC1234/stray.txt": b"x",                  # not in a category: ignored
            "item-media/ABC12345/main_raw/20261125-1-a-x.jpg": b"x",  # another SKU with the same start
        }
        data = ms.list_media(store, "ABC1234")
        cats = {c["category"]: c for c in data["categories"]}
        self.assertEqual([c["category"] for c in data["categories"]],
                         ["main_raw", "main_edit", "detail_raw", "detail_edit", "model", "video"])
        self.assertEqual([f["name"] for f in cats["detail_raw"]["files"]], ["IMG_1.jpg", "IMG_2.jpg"])
        self.assertEqual(cats["detail_raw"]["files"][1]["download_name"], "ABC1234_detail_raw_02.jpg")
        self.assertIn(ms.THUMB_PROCESS, cats["detail_raw"]["files"][0]["thumb"])
        self.assertIn(ms.VIDEO_THUMB_PROCESS, cats["video"]["files"][0]["thumb"])
        self.assertEqual(cats["detail_raw"]["total_bytes"], 3)
        self.assertTrue(cats["detail_raw"]["bulk"])
        self.assertFalse(cats["video"]["bulk"])
        self.assertEqual(cats["main_raw"]["files"], [])
        self.assertEqual(ms.list_media(store, "ABC1234", english=True)["categories"][0]["label"], "Main photo (original)")

    def test_zip_keeps_original_bytes(self):
        store = FakeStore()
        store.objects = {
            "item-media/ABC1234/detail_raw/20261125-100000-a-1.jpg": bytes(range(256)) * 50,
            "item-media/ABC1234/detail_raw/20261125-100100-b-2.heic": b"heic-bytes",
            "item-media/ABC1234/detail_edit/20261125-1-c-x.jpg": b"not this one",
        }
        data = b"".join(ms.zip_stream(store, "ABC1234", "detail_raw"))
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(archive.namelist(), ["ABC1234_detail_raw_01.jpg", "ABC1234_detail_raw_02.heic"])
        self.assertEqual(archive.read("ABC1234_detail_raw_01.jpg"), bytes(range(256)) * 50)
        self.assertEqual(archive.read("ABC1234_detail_raw_02.heic"), b"heic-bytes")
        self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in archive.infolist()))

    def test_real_signing_when_oss2_installed(self):
        try:
            import oss2  # noqa: F401
        except ImportError:
            self.skipTest("oss2 not installed")
        store = ms.OssMediaStore({"OSS_ACCESS_KEY_ID": "id", "OSS_ACCESS_KEY_SECRET": "secret",
                                  "OSS_BUCKET": "le-media", "OSS_ENDPOINT": "https://oss-ap-southeast-1.aliyuncs.com",
                                  "OSS_REGION": "ap-southeast-1"})
        self.assertIsNone(store.configuration_error())
        url = store.put_url("item-media/ABC1234/main_raw/x.jpg", "image/jpeg")
        self.assertTrue(url.startswith("https://le-media.oss-ap-southeast-1.aliyuncs.com/item-media/ABC1234/main_raw/x.jpg?"))
        self.assertIn("x-oss-signature-version=OSS4-HMAC-SHA256", url)
        part = store.part_url("item-media/ABC1234/video/v.mov", "UP1", 3)
        self.assertIn("partNumber=3", part)
        self.assertIn("uploadId=UP1", part)
        download = store.get_url("item-media/ABC1234/main_raw/x.jpg", attachment_name="ABC1234_main_raw_01.jpg")
        self.assertIn("response-content-disposition=attachment", download)
        self.assertIn("OSS_REGION", ms.OssMediaStore({}).configuration_error())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        ms._set_store_for_tests(self.store)
        self.addCleanup(ms._set_store_for_tests, None)

    def _client(self, role=ROLE_ADMIN):
        from luxury_app.routes.media import register
        app = Flask(__name__, template_folder="../templates")
        app.secret_key = "test-secret-that-is-long-enough-for-tests"
        app.jinja_env.filters["dt8"] = lambda v: str(v or "")
        for endpoint in ("index", "logout", "login", "list_items", "item_new_buyin_form",
                         "item_new_consignment_form", "analytics", "audit_history", "notes_list",
                         "users_list", "admin_management_payments", "management_index",
                         "management_list_items", "management_item_new_buyin_form",
                         "management_item_new_consignment_form", "sale_new_form"):
            app.add_url_rule(f"/_{endpoint}", endpoint, lambda: "x")
        app.add_url_rule("/items/<item_key>", "item_detail", lambda item_key: "x")
        app.add_url_rule("/management/items/<item_key>", "management_item_detail", lambda item_key: "x")
        register(app, Items(ITEMS), management_items=Items(ITEMS))
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, user_id=str(ObjectId()), username="boss")
        return client

    def test_pages_for_every_role(self):
        for role in (ROLE_ADMIN, ROLE_STAFF):
            client = self._client(role)
            html = client.get("/media/abc1234").get_data(as_text=True)
            self.assertIn("拍首图", html)
            self.assertIn('capture="environment"', html)
            self.assertIn('accept="video/*" capture="environment"', html)
            self.assertIn("管理媒体", html)
            self.assertIn("扫码拍照", client.get("/media").get_data(as_text=True))
        management = self._client(ROLE_MANAGEMENT)
        html = management.get("/media/MGT7777").get_data(as_text=True)
        self.assertIn("Detail photos", html)
        self.assertIn("/management/items/MGT7777", html)
        self.assertEqual(management.get("/media/ABC1234").status_code, 404)   # not Management's item
        self.assertEqual(self._client().get("/media/NOPE999").status_code, 404)
        self.assertEqual(self._client().get("/media?sku=abc1234").headers["Location"], "/media/ABC1234")

    def test_not_configured(self):
        self.store.error = "媒体存储（阿里云 OSS）未配置：缺少 OSS_BUCKET"
        client = self._client()
        self.assertIn("未配置", client.get("/media/ABC1234").get_data(as_text=True))
        self.assertEqual(client.get("/media/ABC1234/list").status_code, 503)

    def test_single_upload_flow(self):
        client = self._client(ROLE_STAFF)
        plan = client.post("/media/ABC1234/upload-url", json={
            "category": "detail_raw", "filename": "IMG_1.jpg", "content_type": "image/jpeg", "size": 1000}).json
        self.assertEqual(plan["mode"], "single")
        # Browser PUTs to OSS; then the server checks it arrived.
        self.assertEqual(client.post("/media/ABC1234/complete", json={"key": plan["key"]}).status_code, 400)
        self.store.objects[plan["key"]] = b"x" * 1000
        self.assertTrue(client.post("/media/ABC1234/complete", json={"key": plan["key"]}).json["ok"])
        listed = client.get("/media/ABC1234/list").json
        detail = next(c for c in listed["categories"] if c["category"] == "detail_raw")
        self.assertEqual(detail["files"][0]["name"], "IMG_1.jpg")

    def test_multipart_flow_and_abort(self):
        client = self._client()
        size = ms.MULTIPART_THRESHOLD + 10
        plan = client.post("/media/ABC1234/upload-url", json={
            "category": "video", "filename": "clip.mov", "content_type": "video/quicktime", "size": size}).json
        self.assertEqual(plan["mode"], "multipart")
        self.store.uploads[plan["upload_id"]]["parts"] = {1: b"a" * 5, 2: b"b" * 5}
        self.assertTrue(client.post("/media/ABC1234/complete",
                                    json={"key": plan["key"], "upload_id": plan["upload_id"]}).json["ok"])
        self.assertEqual(self.store.objects[plan["key"]], b"aaaaabbbbb")
        second = client.post("/media/ABC1234/upload-url", json={
            "category": "video", "filename": "clip2.mov", "content_type": "video/quicktime", "size": size}).json
        client.post("/media/ABC1234/abort", json={"key": second["key"], "upload_id": second["upload_id"]})
        self.assertNotIn(second["upload_id"], self.store.uploads)

    def test_validation_and_other_items_keys(self):
        client = self._client()
        bad = client.post("/media/ABC1234/upload-url", json={
            "category": "video", "filename": "a.jpg", "content_type": "image/jpeg", "size": 10})
        self.assertEqual((bad.status_code, bad.json["message"]), (400, "这里只能上传视频"))
        other = "item-media/MGT7777/main_raw/20261125-1-a-x.jpg"
        self.store.objects[other] = b"x"
        self.assertEqual(client.post("/media/ABC1234/delete", json={"key": other}).status_code, 403)
        self.assertEqual(client.post("/media/ABC1234/complete", json={"key": other}).status_code, 403)
        self.assertIn(other, self.store.objects)

    def test_delete(self):
        key = "item-media/ABC1234/model/20261125-1-a-x.jpg"
        self.store.objects[key] = b"x"
        self.assertTrue(self._client(ROLE_STAFF).post("/media/ABC1234/delete", json={"key": key}).json["ok"])
        self.assertEqual(self.store.deleted, [key])
        # Management cannot touch an item it does not own.
        self.store.objects[key] = b"x"
        self.assertEqual(self._client(ROLE_MANAGEMENT).post("/media/ABC1234/delete", json={"key": key}).status_code, 404)

    def test_zip_route(self):
        self.store.objects = {"item-media/ABC1234/detail_edit/20261125-1-a-x.jpg": b"edited"}
        response = self._client().get("/media/ABC1234/zip/detail_edit")
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="ABC1234_detail_edit.zip"')
        archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
        self.assertEqual(archive.read("ABC1234_detail_edit_01.jpg"), b"edited")
        self.assertEqual(self._client().get("/media/ABC1234/zip/video").status_code, 404)


if __name__ == "__main__":
    unittest.main()
