import uuid
import bcrypt
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
import db
from extensions import limiter

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if "user" in session:
        return redirect(url_for("public.index"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("login.html")
        try:
            user = db.query_one(
                "SELECT id, first_name, last_name, email, password_hash, role FROM users WHERE email=?",
                [email]
            )
        except Exception as e:
            flash(f"Database error: {e}", "error")
            return render_template("login.html")
        if not user or not bcrypt.checkpw(
            password[:72].encode("utf-8"),
            user.get("password_hash", "").encode("utf-8")
        ):
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        full_name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()
        session["user"] = {
            "id":    str(user["id"]),
            "name":  full_name,
            "email": user["email"],
            "role":  user.get("role", "customer"),
        }
        flash(f"Welcome back, {full_name}!", "success")
        next_url = request.args.get("next") or request.form.get("next")
        if next_url:
            return redirect(next_url)
        if user.get("role") in ("admin", "manager"):
            try:
                return redirect(url_for("admin_dashboard"))
            except Exception:
                return redirect("/admin")
        return redirect(url_for("public.index"))
    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if "user" in session:
        return redirect(url_for("public.index"))
    if request.method == "POST":
        fname    = request.form.get("first_name", "").strip()
        lname    = request.form.get("last_name", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        name     = f"{fname} {lname}".strip()
        if not all([fname, email, password]):
            flash("All fields are required.", "error")
            return render_template("register.html")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        try:
            if db.query_one("SELECT id FROM users WHERE email=?", [email]):
                flash("An account with that email already exists.", "error")
                return render_template("register.html")
            hashed   = bcrypt.hashpw(password[:72].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            new_user = db.execute_returning(
                "INSERT INTO users (id, first_name, last_name, email, password_hash, role) "
                "VALUES (?,?,?,?,?,'customer') RETURNING id, first_name, last_name, email, role",
                [str(uuid.uuid4()), fname, lname, email, hashed]
            )
            if new_user:
                session["user"] = {
                    "id":    str(new_user["id"]),
                    "name":  f"{new_user['first_name']} {new_user.get('last_name','')}".strip(),
                    "email": new_user["email"],
                    "role":  "customer",
                }
                flash(f"Welcome to EyeConic, {name}!", "success")
                return redirect(url_for("public.index"))
        except Exception as e:
            flash(f"Registration failed: {e}", "error")
    return render_template("register.html")


@bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("public.index"))


# ── Account & Addresses ────────────────────────────────────────────────────────

@bp.route("/account", methods=["GET", "POST"])
def account():
    if "user" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login", next=request.url))
    uid = session["user"]["id"]
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "update_profile":
            fname = request.form.get("first_name", "").strip()
            lname = request.form.get("last_name", "").strip()
            if not fname:
                flash("First name is required.", "error")
            else:
                try:
                    db.execute("UPDATE users SET first_name=?, last_name=? WHERE id=?", [fname, lname, uid])
                    session["user"]["name"] = f"{fname} {lname}".strip()
                    flash("Profile updated successfully.", "success")
                except Exception as e:
                    flash(f"Error updating profile: {e}", "error")
        elif action == "add_address":
            try:
                is_default = request.form.get("is_default") == "on"
                if is_default:
                    db.execute("UPDATE user_addresses SET is_default=FALSE WHERE user_id=?", [uid])
                db.execute(
                    """INSERT INTO user_addresses
                       (id, user_id, label, first_name, last_name, phone,
                        address_line1, address_line2, city, state, pincode, country, is_default)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        str(uuid.uuid4()), uid,
                        request.form.get("label", "Home"),
                        request.form.get("first_name", ""),
                        request.form.get("last_name", ""),
                        request.form.get("phone", ""),
                        request.form.get("address_line1", ""),
                        request.form.get("address_line2", ""),
                        request.form.get("city", ""),
                        request.form.get("state", ""),
                        request.form.get("pincode", ""),
                        request.form.get("country", "India"),
                        is_default,
                    ]
                )
                flash("Address added successfully.", "success")
            except Exception as e:
                flash(f"Error saving address: {e}", "error")
        return redirect(url_for("auth.account"))
    try:
        user      = db.query_one(
            "SELECT id, first_name, last_name, email, role, created_at FROM users WHERE id=?", [uid]
        )
        addresses = db.query(
            "SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, created_at DESC", [uid]
        )
        orders    = db.query(
            "SELECT id, created_at, total_amount, status, payment_method, payment_status "
            "FROM orders WHERE user_id=? ORDER BY created_at DESC",
            [uid]
        )
    except Exception as e:
        user = session["user"]
        addresses = []
        orders    = []
        flash(f"Error loading account data: {e}", "error")
    return render_template("account.html", user=user, addresses=addresses, orders=orders)


@bp.route("/account/address/<addr_id>/delete", methods=["POST"])
def account_address_delete(addr_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    db.execute(
        "DELETE FROM user_addresses WHERE id=? AND user_id=?",
        [addr_id, session["user"]["id"]]
    )
    flash("Address removed.", "success")
    return redirect(url_for("auth.account"))


@bp.route("/account/address/<addr_id>/default", methods=["POST"])
def account_address_default(addr_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    uid = session["user"]["id"]
    db.execute("UPDATE user_addresses SET is_default=FALSE WHERE user_id=?", [uid])
    db.execute("UPDATE user_addresses SET is_default=TRUE WHERE id=? AND user_id=?", [addr_id, uid])
    flash("Default address updated.", "success")
    return redirect(url_for("auth.account"))
