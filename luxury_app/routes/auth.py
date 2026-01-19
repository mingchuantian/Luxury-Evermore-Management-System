from werkzeug.security import check_password_hash, generate_password_hash

from flask import flash, redirect, render_template, request, url_for

from models import now

from ..auth import ALL_ROLES, ROLE_ADMIN, login_user, logout_user


def register(app, users):
    @app.get("/login")
    def login():
        return render_template("login.html", next=request.args.get("next", "/"))

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        next_url = (request.form.get("next") or "/").strip() or "/"

        u = users.find_one({"username": username})
        if not u or not check_password_hash(u.get("password_hash", ""), password):
            flash("用户名或密码错误", "error")
            return redirect(url_for("login", next=next_url))

        login_user(u)
        users.update_one({"_id": u["_id"]}, {"$set": {"last_login_at": now()}})
        return redirect(next_url)

    @app.get("/logout")
    def logout():
        logout_user()
        flash("已登出", "ok")
        return redirect(url_for("login"))

    @app.post("/logout")
    def logout_post():
        logout_user()
        flash("已登出", "ok")
        return redirect(url_for("login"))

    # First-run setup: create initial Admin if no users exist.
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if users.count_documents({}, limit=1) > 0:
            return "not found", 404

        if request.method == "GET":
            return render_template("setup.html", roles=ALL_ROLES, default_role=ROLE_ADMIN)

        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not username or not password:
            flash("用户名和密码不能为空", "error")
            return redirect(url_for("setup"))

        users.insert_one({
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": ROLE_ADMIN,
            "created_at": now(),
            "updated_at": now(),
        })
        flash("已创建初始 Admin，请登录", "ok")
        return redirect(url_for("login"))


