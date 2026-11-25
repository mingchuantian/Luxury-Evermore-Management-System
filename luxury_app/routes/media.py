"""Item media (photos / videos in OSS): scan page, per-item shooting page, JSON API.

All roles may use it. Management may only reach items it owns. Nothing is
written to MongoDB; see luxury_app/media_store.py for where files live.
"""

import logging

from flask import Response, abort, jsonify, redirect, render_template, request, session, stream_with_context, url_for

from ..auth import ALL_ROLES, ROLE_MANAGEMENT, require_roles
from ..constants import OWNERSHIP_MANAGEMENT
from ..media_store import (
    BULK_DOWNLOAD,
    CATEGORIES,
    CATEGORY_KIND,
    SKU_RE,
    MediaNotConfigured,
    get_store,
    key_belongs_to,
    list_media,
    plan_upload,
    zip_stream,
)

logger = logging.getLogger(__name__)


def register(app, items, management_items=None):
    real_items = management_items if management_items is not None else items

    def _english():
        return session.get("role") == ROLE_MANAGEMENT

    def _item_or_404(sku):
        """The item behind a SKU, if this user may see it."""
        sku = (sku or "").strip().upper()
        if not SKU_RE.fullmatch(sku):
            abort(404)
        projection = {"sku": 1, "name": 1, "brand": 1, "status": 1, "shopify_details": 1, "ownership": 1}
        if session.get("role") == ROLE_MANAGEMENT:
            item = real_items.find_one({"sku": sku, "ownership": OWNERSHIP_MANAGEMENT}, projection)
        else:
            item = items.find_one({"sku": sku}, projection)
        if not item:
            abort(404)
        return item

    def _store_or_error():
        store = get_store()
        error = store.configuration_error()
        return store, error

    def _json_error(message, status=400):
        return jsonify({"ok": False, "message": message}), status

    def _detail_url(sku):
        if session.get("role") == ROLE_MANAGEMENT:
            return url_for("management_item_detail", item_key=sku)
        return url_for("item_detail", item_key=sku)

    app.jinja_env.globals.setdefault("MEDIA_CATEGORIES", CATEGORIES)
    # For the media section on item detail pages.
    app.jinja_env.globals.setdefault("media_config_error", lambda: get_store().configuration_error())

    # ------------------------------------------------------------------ pages

    @app.get("/media")
    @require_roles(*ALL_ROLES)
    def media_scan_page():
        sku = (request.args.get("sku") or "").strip().upper()
        if sku:
            return redirect(url_for("media_item_page", sku=sku))
        return render_template("media_scan.html", english=_english())

    @app.get("/media/<sku>")
    @require_roles(*ALL_ROLES)
    def media_item_page(sku):
        item = _item_or_404(sku)
        _, error = _store_or_error()
        return render_template("media_item.html", item=item, english=_english(),
                               media_error=error, detail_url=_detail_url(item["sku"]))

    # ------------------------------------------------------------------ API

    @app.get("/media/<sku>/list")
    @require_roles(*ALL_ROLES)
    def media_list(sku):
        item = _item_or_404(sku)
        store, error = _store_or_error()
        if error:
            return _json_error(error, 503)
        try:
            return jsonify({"ok": True, **list_media(store, item["sku"], english=_english())})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Listing media failed for %s", item["sku"])
            return _json_error(f"读取媒体失败：{exc}", 502)

    @app.post("/media/<sku>/upload-url")
    @require_roles(*ALL_ROLES)
    def media_upload_url(sku):
        item = _item_or_404(sku)
        store, error = _store_or_error()
        if error:
            return _json_error(error, 503)
        body = request.get_json(silent=True) or {}
        try:
            size = int(body.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            plan = plan_upload(store, item["sku"], body.get("category") or "", body.get("filename") or "",
                               body.get("content_type") or "", size)
        except ValueError as exc:
            return _json_error(str(exc))
        except MediaNotConfigured as exc:
            return _json_error(str(exc), 503)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Preparing upload failed for %s", item["sku"])
            return _json_error(f"准备上传失败：{exc}", 502)
        return jsonify({"ok": True, **plan})

    @app.post("/media/<sku>/complete")
    @require_roles(*ALL_ROLES)
    def media_complete(sku):
        item = _item_or_404(sku)
        store, error = _store_or_error()
        if error:
            return _json_error(error, 503)
        body = request.get_json(silent=True) or {}
        key, upload_id = body.get("key") or "", body.get("upload_id") or ""
        if not key_belongs_to(key, item["sku"]):
            return _json_error("文件不属于这件商品", 403)
        try:
            if upload_id:
                store.complete_multipart(key, upload_id)
            elif not store.exists(key):
                return _json_error("上传没有完成，请重试")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Completing upload failed for %s", key)
            return _json_error(f"上传收尾失败：{exc}", 502)
        return jsonify({"ok": True, "key": key})

    @app.post("/media/<sku>/abort")
    @require_roles(*ALL_ROLES)
    def media_abort(sku):
        item = _item_or_404(sku)
        store, error = _store_or_error()
        body = request.get_json(silent=True) or {}
        key, upload_id = body.get("key") or "", body.get("upload_id") or ""
        if error or not upload_id or not key_belongs_to(key, item["sku"]):
            return jsonify({"ok": True})
        try:
            store.abort_multipart(key, upload_id)
        except Exception:  # noqa: BLE001 - cleanup only
            logger.warning("Aborting multipart upload failed for %s", key, exc_info=True)
        return jsonify({"ok": True})

    @app.post("/media/<sku>/delete")
    @require_roles(*ALL_ROLES)
    def media_delete(sku):
        item = _item_or_404(sku)
        store, error = _store_or_error()
        if error:
            return _json_error(error, 503)
        body = request.get_json(silent=True) or {}
        key = body.get("key") or ""
        if not key_belongs_to(key, item["sku"]):
            return _json_error("文件不属于这件商品", 403)
        try:
            store.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Deleting media failed for %s", key)
            return _json_error(f"删除失败：{exc}", 502)
        logger.info("Media deleted by %s: %s", session.get("username"), key)
        return jsonify({"ok": True})

    @app.get("/media/<sku>/zip/<category>")
    @require_roles(*ALL_ROLES)
    def media_zip(sku, category):
        item = _item_or_404(sku)
        if category not in CATEGORY_KIND or category not in BULK_DOWNLOAD:
            abort(404)
        store, error = _store_or_error()
        if error:
            return error, 503
        filename = f"{item['sku']}_{category}.zip"
        return Response(
            stream_with_context(zip_stream(store, item["sku"], category)),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )
