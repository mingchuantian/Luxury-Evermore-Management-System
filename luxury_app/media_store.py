"""Item photos and videos kept in Alibaba Cloud OSS, untouched (original bytes).

Layout in the bucket (nothing is stored in MongoDB; the folders are the record):

    <MEDIA_PREFIX><SKU>/<category>/<YYYYMMDD-HHMMSS>-<random>-<original name>

Uploads go straight from the phone to OSS with short-lived signed URLs, so
large files never pass through the app server. Big files (videos) use OSS
multipart upload, so a dropped connection only repeats one part.

Configuration (environment):
    OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET   RAM user with access to the bucket
    OSS_BUCKET                                 bucket name
    OSS_ENDPOINT                               e.g. https://oss-ap-southeast-1.aliyuncs.com
    OSS_REGION                                 e.g. ap-southeast-1 (signature V4)
    OSS_INTERNAL_ENDPOINT   optional, used by the server itself (list/delete/zip)
                            when the server is in the same region
    MEDIA_PREFIX            optional, default "item-media/"
"""

import os
import re
import secrets
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

# key, Chinese label, English label, kind
CATEGORIES = (
    ("main_raw", "首图（原始）", "Main photo (original)", "image"),
    ("main_edit", "首图（精修）", "Main photo (edited)", "image"),
    ("detail_raw", "细节图（原始）", "Detail photos (original)", "image"),
    ("detail_edit", "细节图（精修）", "Detail photos (edited)", "image"),
    ("model", "上身图", "On-body photos", "image"),
    ("video", "视频", "Videos", "video"),
)
CATEGORY_KIND = {key: kind for key, _, _, kind in CATEGORIES}
BULK_DOWNLOAD = ("detail_raw", "detail_edit")

MAX_IMAGE_BYTES = 200 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024 * 1024
MULTIPART_THRESHOLD = 24 * 1024 * 1024     # above this, upload in parts
PART_SIZE = 16 * 1024 * 1024               # OSS: 100 KB – 5 GB per part, ≤ 10 000 parts
UPLOAD_URL_SECONDS = 6 * 3600              # slow mobile networks + long videos
VIEW_URL_SECONDS = 2 * 3600
THUMB_PROCESS = "image/resize,m_lfit,w_480,h_480/auto-orient,1/quality,q_80/format,jpg"
VIDEO_THUMB_PROCESS = "video/snapshot,t_1000,f_jpg,w_480,m_fast"

SKU_RE = re.compile(r"^[A-Z0-9_-]{2,64}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic", "image/heif": ".heif",
    "image/webp": ".webp", "image/tiff": ".tif", "image/x-adobe-dng": ".dng",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/3gpp": ".3gp",
}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".dng"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".3gp", ".hevc"}


class MediaNotConfigured(RuntimeError):
    pass


def media_prefix() -> str:
    prefix = (os.getenv("MEDIA_PREFIX") or "item-media/").strip().lstrip("/")
    return prefix if prefix.endswith("/") else prefix + "/"


def sku_prefix(sku: str) -> str:
    return f"{media_prefix()}{sku}/"


def category_label(category: str, english: bool = False) -> str:
    for key, zh, en, _ in CATEGORIES:
        if key == category:
            return en if english else zh
    return category


def safe_filename(name: str, content_type: str = "") -> str:
    """Keep a readable, URL-safe version of the original file name."""
    stem, ext = os.path.splitext(os.path.basename(name or ""))
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    ext = _SAFE_NAME.sub("", ext.lower())[:10]
    if not ext:
        ext = _EXT_BY_TYPE.get((content_type or "").lower(), "")
    stem = _SAFE_NAME.sub("-", stem).strip("-.")[:60] or "file"
    return f"{stem}{ext}"


def check_upload(category: str, filename: str, content_type: str, size: int) -> Optional[str]:
    """Return an error message, or None when the upload is acceptable."""
    if category not in CATEGORY_KIND:
        return "未知的媒体类别"
    kind = CATEGORY_KIND[category]
    ext = os.path.splitext(filename or "")[1].lower()
    content_type = (content_type or "").lower()
    if kind == "image" and not (content_type.startswith("image/") or ext in _IMAGE_EXTS):
        return "这里只能上传图片"
    if kind == "video" and not (content_type.startswith("video/") or ext in _VIDEO_EXTS):
        return "这里只能上传视频"
    if not isinstance(size, int) or size <= 0:
        return "文件是空的"
    if size > (MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES):
        return "文件太大"
    return None


def new_key(sku: str, category: str, filename: str, content_type: str = "", now=None) -> str:
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return f"{sku_prefix(sku)}{category}/{stamp}-{secrets.token_hex(3)}-{safe_filename(filename, content_type)}"


def key_belongs_to(key: str, sku: str) -> bool:
    """A key the browser sends back must be inside this SKU's folder, in a known category."""
    prefix = sku_prefix(sku)
    if not key or not key.startswith(prefix) or ".." in key:
        return False
    rest = key[len(prefix):].split("/")
    return len(rest) == 2 and rest[0] in CATEGORY_KIND and bool(rest[1])


def download_name(sku: str, category: str, index: int, key: str) -> str:
    ext = os.path.splitext(key)[1].lower() or ".bin"
    return f"{sku}_{category}_{index:02d}{ext}"


class OssMediaStore:
    """Thin wrapper over oss2 (imported lazily so the app runs without it)."""

    def __init__(self, env=os.environ):
        self.access_key_id = (env.get("OSS_ACCESS_KEY_ID") or "").strip()
        self.access_key_secret = (env.get("OSS_ACCESS_KEY_SECRET") or "").strip()
        self.bucket_name = (env.get("OSS_BUCKET") or "").strip()
        self.endpoint = (env.get("OSS_ENDPOINT") or "").strip()
        self.region = (env.get("OSS_REGION") or "").strip()
        self.internal_endpoint = (env.get("OSS_INTERNAL_ENDPOINT") or "").strip()
        self._public = None
        self._internal = None

    def configuration_error(self) -> Optional[str]:
        missing = [name for name, value in (
            ("OSS_ACCESS_KEY_ID", self.access_key_id), ("OSS_ACCESS_KEY_SECRET", self.access_key_secret),
            ("OSS_BUCKET", self.bucket_name), ("OSS_ENDPOINT", self.endpoint), ("OSS_REGION", self.region),
        ) if not value]
        if missing:
            return "媒体存储（阿里云 OSS）未配置：缺少 " + ", ".join(missing)
        try:
            import oss2  # noqa: F401
        except ImportError:
            return "服务器缺少 oss2 组件，请运行 pip install -r requirements.txt"
        return None

    def _bucket(self, internal: bool = False):
        error = self.configuration_error()
        if error:
            raise MediaNotConfigured(error)
        import oss2

        attr = "_internal" if internal and self.internal_endpoint else "_public"
        if getattr(self, attr) is None:
            endpoint = self.internal_endpoint if attr == "_internal" else self.endpoint
            auth = oss2.AuthV4(self.access_key_id, self.access_key_secret)
            setattr(self, attr, oss2.Bucket(auth, endpoint, self.bucket_name, region=self.region,
                                             connect_timeout=30))
        return getattr(self, attr)

    # -- signed URLs (no network) -------------------------------------------------
    def put_url(self, key: str, content_type: str) -> str:
        return self._bucket().sign_url("PUT", key, UPLOAD_URL_SECONDS,
                                       headers={"Content-Type": content_type}, slash_safe=True)

    def part_url(self, key: str, upload_id: str, part_number: int) -> str:
        return self._bucket().sign_url("PUT", key, UPLOAD_URL_SECONDS, slash_safe=True,
                                       params={"partNumber": str(part_number), "uploadId": upload_id})

    def get_url(self, key: str, process: str = "", attachment_name: str = "") -> str:
        params = {}
        if process:
            params["x-oss-process"] = process
        if attachment_name:
            params["response-content-disposition"] = (
                f"attachment; filename=\"{attachment_name}\"; filename*=UTF-8''{quote(attachment_name)}")
        return self._bucket().sign_url("GET", key, VIEW_URL_SECONDS, params=params or None, slash_safe=True)

    # -- calls to OSS -----------------------------------------------------------
    def start_multipart(self, key: str, content_type: str) -> str:
        return self._bucket().init_multipart_upload(key, headers={"Content-Type": content_type}).upload_id

    def complete_multipart(self, key: str, upload_id: str) -> int:
        import oss2

        bucket = self._bucket(internal=True)
        parts, marker = [], ""
        while True:
            result = bucket.list_parts(key, upload_id, marker=marker)
            parts.extend(oss2.models.PartInfo(p.part_number, p.etag) for p in result.parts)
            if not result.is_truncated:
                break
            marker = result.next_marker
        if not parts:
            raise RuntimeError("没有收到任何分片")
        bucket.complete_multipart_upload(key, upload_id, sorted(parts, key=lambda p: p.part_number))
        return len(parts)

    def abort_multipart(self, key: str, upload_id: str) -> None:
        self._bucket(internal=True).abort_multipart_upload(key, upload_id)

    def list(self, prefix: str) -> List[Dict[str, Any]]:
        import oss2

        found = []
        for obj in oss2.ObjectIteratorV2(self._bucket(internal=True), prefix=prefix):
            found.append({"key": obj.key, "size": obj.size, "last_modified": obj.last_modified})
        return found

    def exists(self, key: str) -> bool:
        return self._bucket(internal=True).object_exists(key)

    def delete(self, key: str) -> None:
        self._bucket(internal=True).delete_object(key)

    def read_chunks(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        stream = self._bucket(internal=True).get_object(key)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk


# ------------------------------------------------------------------ listing


def list_media(store, sku: str, english: bool = False) -> Dict[str, Any]:
    """All media of one SKU, grouped by category, with signed view/download URLs."""
    prefix = sku_prefix(sku)
    groups = {key: [] for key in CATEGORY_KIND}
    for obj in sorted(store.list(prefix), key=lambda o: o["key"]):
        rest = obj["key"][len(prefix):].split("/")
        if len(rest) != 2 or rest[0] not in groups or not rest[1]:
            continue
        groups[rest[0]].append(obj)
    result = []
    for key, zh, en, kind in CATEGORIES:
        files = []
        for index, obj in enumerate(groups[key], start=1):
            name = obj["key"].rsplit("/", 1)[-1]
            files.append({
                "key": obj["key"],
                "name": name.split("-", 3)[-1] if name.count("-") >= 3 else name,
                "size": obj["size"],
                "uploaded_at": datetime.fromtimestamp(obj["last_modified"], timezone.utc).isoformat()
                if obj.get("last_modified") else "",
                "url": store.get_url(obj["key"]),
                "thumb": store.get_url(obj["key"], THUMB_PROCESS if kind == "image" else VIDEO_THUMB_PROCESS),
                "download": store.get_url(obj["key"], attachment_name=download_name(sku, key, index, obj["key"])),
                "download_name": download_name(sku, key, index, obj["key"]),
            })
        result.append({"category": key, "label": en if english else zh, "kind": kind,
                       "bulk": key in BULK_DOWNLOAD, "files": files,
                       "total_bytes": sum(f["size"] for f in files)})
    return {"sku": sku, "categories": result}


# ------------------------------------------------------------------ uploads


def plan_upload(store, sku: str, category: str, filename: str, content_type: str, size: int) -> Dict[str, Any]:
    """Signed URL(s) for one upload. Raises ValueError with a message for bad input."""
    content_type = (content_type or "").strip() or "application/octet-stream"
    error = check_upload(category, filename, content_type, size)
    if error:
        raise ValueError(error)
    key = new_key(sku, category, filename, content_type)
    if size <= MULTIPART_THRESHOLD:
        return {"mode": "single", "key": key, "url": store.put_url(key, content_type),
                "headers": {"Content-Type": content_type}}
    upload_id = store.start_multipart(key, content_type)
    count = (size + PART_SIZE - 1) // PART_SIZE
    return {"mode": "multipart", "key": key, "upload_id": upload_id, "part_size": PART_SIZE,
            "parts": [{"number": n, "url": store.part_url(key, upload_id, n)} for n in range(1, count + 1)]}


# ------------------------------------------------------------------ zip


class _ZipSink:
    """A write-only file object that hands written bytes to a generator."""

    def __init__(self):
        self.buffer = bytearray()
        self.position = 0

    def write(self, data):
        self.buffer.extend(data)
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def zip_stream(store, sku: str, category: str) -> Iterator[bytes]:
    """Stream a .zip of one category's originals (stored, not recompressed)."""
    prefix = f"{sku_prefix(sku)}{category}/"
    objects = sorted(store.list(prefix), key=lambda o: o["key"])
    sink = _ZipSink()
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for index, obj in enumerate(objects, start=1):
            info = zipfile.ZipInfo(download_name(sku, category, index, obj["key"]),
                                   date_time=time.localtime(obj.get("last_modified") or time.time())[:6])
            info.compress_type = zipfile.ZIP_STORED
            with archive.open(info, mode="w", force_zip64=True) as entry:
                for chunk in store.read_chunks(obj["key"]):
                    entry.write(chunk)
                    if len(sink.buffer) >= 1024 * 1024:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


_store = None


def get_store():
    global _store
    if _store is None:
        _store = OssMediaStore()
    return _store


def _set_store_for_tests(store):
    global _store
    _store = store
