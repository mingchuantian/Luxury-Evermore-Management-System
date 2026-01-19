from bson import ObjectId
from flask import flash, redirect, render_template, request, session, url_for

from models import now


def register(app, notes):
    @app.get("/notes")
    def notes_list():
        """显示所有随记列表"""
        docs = list(notes.find({}).sort("created_at", -1))
        return render_template("notes.html", notes=docs)

    @app.post("/notes/new")
    def notes_create():
        """创建新随记"""
        content = (request.form.get("content") or "").strip()
        if not content:
            flash("随记内容不能为空", "error")
            return redirect(url_for("notes_list"))

        username = session.get("username", "Unknown")
        notes.insert_one({
            "content": content,
            "username": username,
            "created_at": now(),
            "updated_at": now(),
        })
        flash("已保存随记", "ok")
        return redirect(url_for("notes_list"))

    @app.post("/notes/<note_id>/update")
    def notes_update(note_id):
        """更新随记"""
        content = (request.form.get("content") or "").strip()
        if not content:
            flash("随记内容不能为空", "error")
            return redirect(url_for("notes_list"))

        result = notes.update_one(
            {"_id": ObjectId(note_id)},
            {"$set": {"content": content, "updated_at": now()}}
        )
        if result.matched_count == 0:
            flash("随记不存在", "error")
        else:
            flash("已更新随记", "ok")
        return redirect(url_for("notes_list"))

    @app.post("/notes/<note_id>/delete")
    def notes_delete(note_id):
        """删除随记"""
        result = notes.delete_one({"_id": ObjectId(note_id)})
        if result.deleted_count == 0:
            flash("随记不存在", "error")
        else:
            flash("已删除随记", "ok")
        return redirect(url_for("notes_list"))

