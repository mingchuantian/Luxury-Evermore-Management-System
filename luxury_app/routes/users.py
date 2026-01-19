from bson import ObjectId
from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from models import now

from ..auth import ALL_ROLES, ROLE_ADMIN, require_roles


def register(app, users):
    @app.get("/users")
    @require_roles(ROLE_ADMIN)
    def users_list():
        docs = list(users.find({}).sort("created_at", -1))
        return render_template("users.html", users=docs, roles=ALL_ROLES)

    @app.get("/users/new")
    @require_roles(ROLE_ADMIN)
    def users_new_form():
        return render_template("user_new.html", roles=ALL_ROLES)

    @app.post("/users/new")
    @require_roles(ROLE_ADMIN)
    def users_new_create():
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        role = (request.form.get("role") or "").strip()
        if role not in ALL_ROLES:
            role = "Staff"

        if not username or not password:
            flash("用户名和密码不能为空", "error")
            return redirect(url_for("users_new_form"))

        try:
            users.insert_one({
                "username": username,
                "password_hash": generate_password_hash(password),
                "role": role,
                "created_at": now(),
                "updated_at": now(),
            })
        except Exception as e:
            flash(f"创建失败：{e}", "error")
            return redirect(url_for("users_new_form"))

        flash("已创建账号", "ok")
        return redirect(url_for("users_list"))

    @app.post("/users/<user_id>/reset_password")
    @require_roles(ROLE_ADMIN)
    def users_reset_password(user_id):
        pwd = (request.form.get("password") or "").strip()
        if not pwd:
            flash("新密码不能为空", "error")
            return redirect(url_for("users_list"))

        users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"password_hash": generate_password_hash(pwd), "updated_at": now()}}
        )
        flash("已重置密码", "ok")
        return redirect(url_for("users_list"))

    @app.post("/users/<user_id>/set_role")
    @require_roles(ROLE_ADMIN)
    def users_set_role(user_id):
        role = (request.form.get("role") or "").strip()
        if role not in ALL_ROLES:
            flash("无效角色", "error")
            return redirect(url_for("users_list"))

        users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"role": role, "updated_at": now()}}
        )
        flash("已更新角色", "ok")
        return redirect(url_for("users_list"))

    @app.post("/users/<user_id>/delete")
    @require_roles(ROLE_ADMIN)
    def users_delete(user_id):
        # prevent deleting yourself
        if session.get("user_id") == user_id:
            flash("不能删除当前登录账号", "error")
            return redirect(url_for("users_list"))

        users.delete_one({"_id": ObjectId(user_id)})
        flash("已删除账号", "ok")
        return redirect(url_for("users_list"))


