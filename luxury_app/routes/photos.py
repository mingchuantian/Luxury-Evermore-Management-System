from pathlib import Path

from bson import ObjectId
from flask import flash, redirect, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from models import now

from ..audit import insert_audit_logs, make_change_detail, push_item_audits
from ..constants import ALLOWED_EXT, PHOTO_CATEGORIES
from ..photos import category_dir, safe_ext_ok, tmp_photo_dir
from ..utils import safe_segment


def register(app, items, photos_root, audit_logs=None):
    @app.post("/tmp/photos/seller/upload")
    def upload_tmp_seller_photos():
        temp_id = request.form.get("temp_id")
        if not temp_id:
            return "missing temp_id", 400

        files = request.files.getlist("files")
        if not files:
            return "", 204

        d = tmp_photo_dir(photos_root, temp_id)

        for f in files:
            if not f or not f.filename:
                continue
            fn = secure_filename(f.filename)
            if not safe_ext_ok(fn):
                continue
            dst = d / fn
            if not dst.exists():
                f.save(dst)

        return "", 204

    @app.post("/items/<item_id>/photos/copy_taken_to_product")
    def copy_taken_to_product(item_id):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404

        sku = it.get("sku")
        if not sku:
            return "no sku", 400

        src_dir = category_dir(photos_root, sku, "taken", ensure=False)
        dst_dir = category_dir(photos_root, sku, "product", ensure=True)

        copied = 0
        for p in src_dir.iterdir():
            if p.is_file() and p.suffix.lower() in ALLOWED_EXT:
                dst = dst_dir / p.name
                if not dst.exists():
                    dst.write_bytes(p.read_bytes())
                    copied += 1

        entry = make_change_detail(sku=sku, target="photos.product", from_value="", to_value={"op": "copy_taken_to_product", "count": copied})
        push_item_audits(items, ObjectId(item_id), [entry])
        insert_audit_logs(audit_logs, [entry])

        return "", 204

    @app.get("/photos/<sku>/<cat_key>/<filename>")
    def serve_photo(sku, cat_key, filename):
        if not safe_segment(sku):
            return "invalid sku", 400
        if not safe_segment(filename):
            return "invalid filename", 400
        # read-only: do NOT create folders on request
        d = category_dir(photos_root, sku, cat_key, ensure=False)
        return send_from_directory(d, filename)

    @app.post("/items/<item_id>/photos/<cat_key>/upload")
    def upload_photos(item_id, cat_key):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404

        sku = it.get("sku")
        if not sku:
            return "no sku", 400

        if cat_key not in PHOTO_CATEGORIES:
            return "invalid category", 400

        files = request.files.getlist("files")
        if not files:
            flash("未选择图片", "error")
            return redirect(url_for("item_detail", item_key=item_id))

        saved = 0
        target_dir = category_dir(photos_root, sku, cat_key, ensure=True)

        for f in files:
            if not f or not f.filename:
                continue

            filename = secure_filename(f.filename)
            if not filename:
                continue

            if not safe_ext_ok(filename):
                continue

            dst = target_dir / filename
            if dst.exists():
                stem = dst.stem
                ext = dst.suffix
                i = 2
                while True:
                    cand = target_dir / f"{stem}_{i}{ext}"
                    if not cand.exists():
                        dst = cand
                        break
                    i += 1

            f.save(dst)
            saved += 1

        entry = make_change_detail(sku=sku, target=f"photos.{cat_key}", from_value="", to_value={"op": "upload", "count": saved})
        items.update_one({"_id": ObjectId(item_id)}, {"$set": {"updated_at": now()}})
        push_item_audits(items, ObjectId(item_id), [entry])
        insert_audit_logs(audit_logs, [entry])

        flash(f"已上传 {saved} 张图片", "ok")
        return redirect(url_for("item_detail", item_key=item_id))

    @app.post("/items/<item_id>/photos/<cat_key>/delete")
    def delete_photo(item_id, cat_key):
        it = items.find_one({"_id": ObjectId(item_id)})
        if not it:
            return "not found", 404

        sku = it.get("sku")
        if not sku:
            return "no sku", 400

        filename = request.form.get("filename", "").strip()
        if not filename:
            return "missing filename", 400

        d = category_dir(photos_root, sku, cat_key, ensure=False)
        fp = d / filename

        try:
            fp.resolve().relative_to(d.resolve())
        except Exception:
            return "invalid path", 400

        if fp.exists() and fp.is_file():
            fp.unlink()

        entry = make_change_detail(sku=sku, target=f"photos.{cat_key}", from_value=filename, to_value={"op": "delete"})
        items.update_one({"_id": ObjectId(item_id)}, {"$set": {"updated_at": now()}})
        push_item_audits(items, ObjectId(item_id), [entry])
        insert_audit_logs(audit_logs, [entry])

        flash("已删除图片", "ok")
        return redirect(url_for("item_detail", item_key=item_id))


