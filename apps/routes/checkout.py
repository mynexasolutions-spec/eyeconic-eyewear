import uuid
import json
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, abort
import db
from helpers import get_cached_store_settings, refresh_cart_prices
from extensions import csrf
from shipping import track_shipment as ithink_track_shipment, tracking_url_for as ithink_tracking_url_for

bp = Blueprint("checkout", __name__)

ALLOWED_PAYMENT_METHODS = {"cod", "razorpay"}


def _load_razorpay():
    """Import razorpay lazily so app startup does not fail when dependency is missing."""
    try:
        import razorpay  # type: ignore
        return razorpay, None
    except Exception as e:
        return None, e


def _calc_shipping(subtotal, settings=None):
    from helpers import calc_shipping
    return calc_shipping(subtotal, settings)


# ── Coupon validation ──────────────────────────────────────────────────────────

def _validate_coupon(code, user_id, subtotal):
    """Returns (coupon_dict, discount_amount, error_message)."""
    code = (code or "").strip().upper()
    if not code:
        return None, 0.0, "Please enter a coupon code."

    coupon = db.query_one(
        "SELECT * FROM coupons WHERE UPPER(code) = ? AND is_active = 1", [code]
    )
    if not coupon:
        return None, 0.0, "Invalid or inactive coupon code."

    if coupon.get("expires_at") and coupon["expires_at"] < datetime.now():
        return None, 0.0, "This coupon has expired."

    min_order = float(coupon.get("min_order_amount") or 0)
    if subtotal < min_order:
        return None, 0.0, f"Minimum order amount of ₹{min_order:.0f} required for this coupon."

    if coupon.get("usage_limit"):
        row = db.query_one(
            "SELECT COUNT(*) AS cnt FROM coupon_usages WHERE coupon_id = ?", [coupon["id"]]
        )
        if row and int(row["cnt"]) >= int(coupon["usage_limit"]):
            return None, 0.0, "This coupon has reached its usage limit."

    if user_id and coupon.get("usage_limit_per_user"):
        row = db.query_one(
            "SELECT COUNT(*) AS cnt FROM coupon_usages WHERE coupon_id = ? AND user_id = ?",
            [coupon["id"], user_id],
        )
        if row and int(row["cnt"]) >= int(coupon["usage_limit_per_user"]):
            return None, 0.0, "You have already used this coupon."

    value = float(coupon.get("value") or 0)
    if coupon["type"] == "percentage":
        discount = subtotal * (value / 100)
        if coupon.get("max_discount"):
            discount = min(discount, float(coupon["max_discount"]))
    else:
        discount = min(value, subtotal)

    return coupon, round(discount, 2), None


# ── Apply-coupon AJAX endpoint ─────────────────────────────────────────────────

@csrf.exempt
@bp.route("/apply_coupon", methods=["POST"])
def apply_coupon():
    if "user" not in session:
        return jsonify({"valid": False, "error": "Login required."}), 401

    data     = request.get_json(silent=True) or {}
    code     = (data.get("coupon_code") or data.get("code") or "").strip()
    subtotal = float(data.get("subtotal") or 0)
    uid      = session["user"]["id"]

    coupon, discount, error = _validate_coupon(code, uid, subtotal)
    if error:
        return jsonify({"valid": False, "error": error})

    shipping  = _calc_shipping(subtotal)
    new_total = round(max(0.0, subtotal + shipping - discount), 2)

    return jsonify({
        "valid":           True,
        "discount_amount": discount,
        "new_total":       new_total,
        "message":         f"Coupon applied! You save ₹{discount:.0f}.",
    })


# ── Razorpay order creation ────────────────────────────────────────────────────

@csrf.exempt
@bp.route("/razorpay/create_order", methods=["POST"])
def rzp_create_order():
    if "user" not in session:
        return jsonify({"success": False, "message": "Login required."}), 401

    settings   = get_cached_store_settings()
    key_id     = settings.get("razorpay_key_id", "").strip()
    key_secret = settings.get("razorpay_key_secret", "").strip()

    if settings.get("online_payment_enabled", "false") != "true":
        return jsonify({"success": False, "message": "Online payment is not enabled."}), 403
    if not key_id or not key_secret:
        return jsonify({"success": False, "message": "Razorpay is not configured."}), 400

    cart = session.get("cart", {})
    if not cart:
        return jsonify({"success": False, "message": "Cart is empty."}), 400

    razorpay, import_error = _load_razorpay()
    if not razorpay:
        return jsonify({"success": False, "message": f"Online payment dependency error: {import_error}"}), 500

    try:
        data        = request.get_json(silent=True) or {}
        coupon_code = (data.get("coupon_code") or "").strip()

        cart, subtotal = refresh_cart_prices(cart)
        session["cart"] = cart

        discount = 0.0
        if coupon_code:
            uid = session["user"]["id"]
            _, discount, _ = _validate_coupon(coupon_code, uid, subtotal)

        shipping     = _calc_shipping(subtotal, settings)
        amount_total = max(0.0, subtotal + shipping - discount)
        amount_paisa = int(round(amount_total * 100))

        if amount_paisa == 0:
            return jsonify({"success": True, "free_order": True})

        client = razorpay.Client(auth=(key_id, key_secret))
        order  = client.order.create({
            "amount":          amount_paisa,
            "currency":        "INR",
            "payment_capture": 1,
        })
        return jsonify({"success": True, "order": order})
    except Exception:
        return jsonify({"success": False, "message": "Could not create payment order. Please try again."}), 500


# ── Main checkout ──────────────────────────────────────────────────────────────

@bp.route("/checkout", methods=["GET", "POST"])
def checkout():
    if "user" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login", next=request.url))
    cart = session.get("cart", {})
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for("cart.view_cart"))

    uid            = session["user"]["id"]
    cart, subtotal = refresh_cart_prices(cart)
    session["cart"] = cart

    settings       = get_cached_store_settings()
    shipping       = _calc_shipping(subtotal, settings)
    cod_enabled    = settings.get("cod_enabled", "true") == "true"
    online_enabled = settings.get("online_payment_enabled", "false") == "true"

    try:
        addresses = db.query(
            "SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, created_at DESC",
            [uid],
        )
    except Exception:
        addresses = []

    if request.method == "POST":
        payment_method = request.form.get("payment_method", "").strip()
        coupon_code    = request.form.get("coupon_code", "").strip().upper()
        notes          = request.form.get("notes", "").strip()
        save_address   = request.form.get("save_address") == "on"
        saved_address_id = request.form.get("saved_address_id", "").strip()
        addr_first     = request.form.get("addr_first_name", "").strip()
        addr_last      = request.form.get("addr_last_name", "").strip()
        addr_phone     = request.form.get("addr_phone", "").strip()
        addr_line1     = request.form.get("addr_line1", "").strip()
        addr_line2     = request.form.get("addr_line2", "").strip()
        addr_city      = request.form.get("addr_city", "").strip()
        addr_state     = request.form.get("addr_state", "").strip()
        addr_pin       = request.form.get("addr_pincode", "").strip()
        addr_country   = request.form.get("addr_country", "India").strip()

        # Validate payment method
        if payment_method not in ALLOWED_PAYMENT_METHODS:
            flash("Invalid payment method.", "error")
            return redirect(url_for("checkout.checkout"))

        razorpay_key_id     = settings.get("razorpay_key_id", "").strip()
        razorpay_key_secret = settings.get("razorpay_key_secret", "").strip()
        razorpay_ready      = online_enabled and bool(razorpay_key_id) and bool(razorpay_key_secret)

        if payment_method == "cod" and not cod_enabled:
            flash("Cash on Delivery is not available.", "error")
            return redirect(url_for("checkout.checkout"))
        if payment_method == "razorpay" and not razorpay_ready:
            flash("Online payment is not available at this time.", "error")
            return redirect(url_for("checkout.checkout"))

        shipping_addr = None
        if saved_address_id:
            shipping_addr = db.query_one(
                "SELECT first_name, last_name, phone, address_line1, address_line2, city, state, pincode, country FROM user_addresses WHERE id=? AND user_id=?",
                [saved_address_id, uid],
            )
            if not shipping_addr:
                flash("Selected address could not be found. Please choose it again.", "error")
                return redirect(url_for("checkout.checkout"))
            addr_first   = shipping_addr.get("first_name", "").strip()
            addr_last    = shipping_addr.get("last_name", "").strip()
            addr_phone   = shipping_addr.get("phone", "").strip()
            addr_line1   = shipping_addr.get("address_line1", "").strip()
            addr_line2   = shipping_addr.get("address_line2", "").strip()
            addr_city    = shipping_addr.get("city", "").strip()
            addr_state   = shipping_addr.get("state", "").strip()
            addr_pin     = shipping_addr.get("pincode", "").strip()
            addr_country = shipping_addr.get("country", "India").strip()

        if not addr_line1 or not addr_city or not addr_pin or not addr_phone:
            flash("Please fill in all required address fields.", "error")
            return render_template(
                "checkout.html",
                cart=cart, subtotal=subtotal, shipping=shipping, total=subtotal + shipping,
                settings=settings, cod_enabled=cod_enabled, online_enabled=online_enabled,
                addresses=addresses,
                free_shipping_threshold=float(settings.get("free_shipping_threshold") or 599),
                free_shipping_enabled=settings.get("free_shipping_enabled", "true") == "true",
                free_shipping_all=settings.get("free_shipping_all") == "true",
            )

        # Validate coupon server-side (re-validate even if client showed it valid)
        coupon          = None
        discount_amount = 0.0
        if coupon_code:
            coupon, discount_amount, coupon_error = _validate_coupon(coupon_code, uid, subtotal)
            if coupon_error:
                flash(f"Coupon: {coupon_error}", "error")
                coupon_code     = ""
                discount_amount = 0.0

        total = max(0.0, subtotal + shipping - discount_amount)

        payment_status = "pending"
        if payment_method == "razorpay":
            if total > 0:
                razorpay, import_error = _load_razorpay()
                if not razorpay:
                    flash(f"Online payment dependency error: {import_error}", "error")
                    return redirect(url_for("checkout.checkout"))

                rzp_payment_id = request.form.get("razorpay_payment_id", "").strip()
                rzp_order_id   = request.form.get("razorpay_order_id", "").strip()
                rzp_signature  = request.form.get("razorpay_signature", "").strip()
                if not rzp_payment_id or not rzp_order_id or not rzp_signature:
                    flash("Payment information is incomplete. Please try again.", "error")
                    return redirect(url_for("checkout.checkout"))
                try:
                    client = razorpay.Client(auth=(razorpay_key_id, razorpay_key_secret))
                    client.utility.verify_payment_signature({
                        "razorpay_order_id":   rzp_order_id,
                        "razorpay_payment_id": rzp_payment_id,
                        "razorpay_signature":  rzp_signature,
                    })
                    payment_status = "paid"
                except razorpay.errors.SignatureVerificationError:
                    flash("Payment signature verification failed. Please contact support if your money was deducted.", "error")
                    return redirect(url_for("checkout.checkout"))
                except Exception:
                    flash("Payment verification failed. Please contact support if your money was deducted.", "error")
                    return redirect(url_for("checkout.checkout"))
            else:
                payment_status = "paid"

        shipping_addr = shipping_addr or {
            "first_name": addr_first, "last_name": addr_last, "phone": addr_phone,
            "address_line1": addr_line1, "address_line2": addr_line2,
            "city": addr_city, "state": addr_state, "pincode": addr_pin, "country": addr_country,
        }
        customer_name  = session["user"].get("name", f"{addr_first} {addr_last}").strip()
        customer_email = session["user"].get("email", "")

        try:
            order_id     = str(uuid.uuid4())
            order_number = f"ORD-{uuid.uuid4().hex[:12].upper()}"

            with db.transaction() as tx:
                tx.execute(
                    """INSERT INTO orders
                       (id, order_number, user_id, subtotal, shipping_amount, total_amount, status,
                        payment_method, payment_status, shipping_address_json, customer_name,
                        customer_email, customer_phone, notes, coupon_code, discount_amount)
                       VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?)""",
                    [order_id, order_number, uid, subtotal, shipping, total,
                     payment_method, payment_status, json.dumps(shipping_addr),
                     customer_name, customer_email, addr_phone, notes,
                     coupon_code or "", discount_amount],
                )

                for item_key, item in cart.items():
                    unit_price = float(item.get("price", 0))
                    qty        = int(item.get("qty", 1))
                    pid        = item.get("product_id")
                    vid        = item.get("variation_id")

                    # Only track stock if product has manage_stock enabled
                    p_row = tx.query_one("SELECT stock_quantity, manage_stock FROM products WHERE id = ?", [pid])
                    if p_row and p_row.get("manage_stock"):
                        tx.execute(
                            "UPDATE products SET stock_quantity = stock_quantity - ? WHERE id = ?", [qty, pid]
                        )
                        # Re-fetch after decrement to check if now out of stock
                        updated = tx.query_one("SELECT stock_quantity FROM products WHERE id = ?", [pid])
                        if updated and updated["stock_quantity"] <= 0:
                            tx.execute(
                                "UPDATE products SET stock_quantity = 0, stock_status = 'out_of_stock' WHERE id = ?", [pid]
                            )

                    pres_url = item.get("prescription_url", "")
                    tx.execute(
                        """INSERT INTO order_items
                           (id, order_id, product_id, variation_id, quantity,
                            unit_price, total_price, product_name_snapshot, prescription_url)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        [str(uuid.uuid4()), order_id, pid, vid or None, qty,
                         unit_price, unit_price * qty, item.get("name", ""), pres_url],
                    )

                # Record coupon usage
                if coupon:
                    tx.execute(
                        "INSERT INTO coupon_usages (id, coupon_id, user_id, order_id) VALUES (?,?,?,?)",
                        [str(uuid.uuid4()), coupon["id"], uid, order_id],
                    )

            # Save address separately (not critical — failure shouldn't roll back the order)
            if save_address:
                try:
                    is_default = len(addresses) == 0
                    if is_default:
                        db.execute("UPDATE user_addresses SET is_default=FALSE WHERE user_id=?", [uid])
                    db.execute(
                        """INSERT INTO user_addresses
                           (id, user_id, label, first_name, last_name, phone,
                            address_line1, address_line2, city, state, pincode, country, is_default)
                           VALUES (?,?,'Home',?,?,?,?,?,?,?,?,?,?)""",
                        [str(uuid.uuid4()), uid, addr_first, addr_last, addr_phone,
                         addr_line1, addr_line2, addr_city, addr_state, addr_pin,
                         addr_country, is_default],
                    )
                except Exception:
                    pass

            session.pop("cart", None)
            flash("Order placed successfully!", "success")
            return redirect(url_for("checkout.order_success", order_id=order_id))
        except Exception as e:
            flash(f"Error placing order: {e}", "error")

    total = subtotal + shipping
    return render_template(
        "checkout.html",
        cart=cart, subtotal=subtotal, shipping=shipping, total=total,
        settings=settings, cod_enabled=cod_enabled, online_enabled=online_enabled,
        addresses=addresses,
        free_shipping_threshold=float(settings.get("free_shipping_threshold") or 599),
        free_shipping_enabled=settings.get("free_shipping_enabled", "true") == "true",
        free_shipping_all=settings.get("free_shipping_all") == "true",
    )


# ── Order success ──────────────────────────────────────────────────────────────

@bp.route("/order/<order_id>/success")
def order_success(order_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    uid = session["user"]["id"]
    try:
        order = db.query_one("SELECT * FROM orders WHERE id=? AND user_id=?", [order_id, uid])
        if not order:
            abort(404)
        items = db.query(
            """SELECT oi.*, p.name AS product_name, m.file_url AS image_url
               FROM order_items oi
               LEFT JOIN products p ON p.id = oi.product_id
               LEFT JOIN product_images pi ON pi.product_id = oi.product_id AND pi.is_primary=1
               LEFT JOIN media m ON m.id = pi.media_id
               WHERE oi.order_id=?""",
            [order_id],
        )
        shipping_address = {}
        if order.get("shipping_address_json"):
            try:
                shipping_address = json.loads(order["shipping_address_json"])
            except Exception:
                pass
    except Exception as e:
        flash(f"Error loading order: {e}", "error")
        return redirect(url_for("auth.account"))
    return render_template("order_success.html", order=order, items=items, shipping_address=shipping_address)


@bp.route("/order/<order_id>")
def order_detail(order_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    uid = session["user"]["id"]
    try:
        order = db.query_one("SELECT * FROM orders WHERE id=? AND user_id=?", [order_id, uid])
        if not order:
            abort(404)
        items = db.query(
            """SELECT oi.*, p.name AS product_name, m.file_url AS image_url
               FROM order_items oi
               LEFT JOIN products p ON p.id = oi.product_id
               LEFT JOIN product_images pi ON pi.product_id = oi.product_id AND pi.is_primary=1
               LEFT JOIN media m ON m.id = pi.media_id
               WHERE oi.order_id=?""",
            [order_id],
        )
        shipping_address = {}
        if order.get("shipping_address_json"):
            try:
                import json
                shipping_address = json.loads(order["shipping_address_json"])
            except Exception:
                pass
    except Exception as e:
        flash(f"Error fetching order: {e}", "error")
        return redirect(url_for("auth.account"))

    tracking = None
    if order.get("awb_number"):
        if not order.get("shipment_tracking_url"):
            fallback_url = ithink_tracking_url_for(order["awb_number"])
            try:
                db.execute("UPDATE orders SET shipment_tracking_url=? WHERE id=?", [fallback_url, order_id])
                order["shipment_tracking_url"] = fallback_url
            except Exception:
                pass
        try:
            ok, _msg, tracking = ithink_track_shipment(order["awb_number"])
            if not ok:
                tracking = None
        except Exception:
            tracking = None

    return render_template(
        "order_detail.html", order=order, items=items, shipping_address=shipping_address, tracking=tracking
    )


@bp.route("/order/<order_id>/cancel", methods=["POST"])
def order_cancel(order_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    uid = session["user"]["id"]
    try:
        order = db.query_one(
            "SELECT id, status, payment_method, payment_status FROM orders WHERE id=? AND user_id=?",
            [order_id, uid],
        )
        if not order:
            abort(404)
        if order["status"] in ("shipped", "delivered", "cancelled", "refunded"):
            flash("This order can no longer be cancelled.", "error")
            return redirect(url_for("checkout.order_detail", order_id=order_id))

        cancel_reason       = (request.form.get("cancel_reason") or "").strip()
        cancel_reason_other = (request.form.get("cancel_reason_other") or "").strip()
        if not cancel_reason:
            flash("Please select a cancellation reason.", "error")
            return redirect(url_for("checkout.order_detail", order_id=order_id))
        if cancel_reason == "Other":
            if not cancel_reason_other:
                flash("Please enter the cancellation reason details.", "error")
                return redirect(url_for("checkout.order_detail", order_id=order_id))
            cancel_reason = f"Other: {cancel_reason_other}"

        items        = db.query("SELECT product_id, quantity FROM order_items WHERE order_id=?", [order_id])
        stock_changes = {}
        for item in items:
            pid = item.get("product_id")
            qty = int(item.get("quantity") or 0)
            if pid and qty > 0:
                stock_changes[pid] = stock_changes.get(pid, 0) + qty
        for pid, qty in stock_changes.items():
            p_row = db.query_one("SELECT manage_stock FROM products WHERE id = ?", [pid])
            if p_row and p_row.get("manage_stock"):
                db.execute(
                    "UPDATE products SET stock_quantity = stock_quantity + ?, stock_status = 'in_stock' WHERE id = ?",
                    [qty, pid],
                )
        db.execute(
            "UPDATE orders SET status='cancelled', payment_status='cancelled', cancelled_at=NOW(), cancel_reason=? WHERE id=? AND user_id=?",
            [cancel_reason, order_id, uid],
        )
        flash("Order cancelled successfully.", "success")
    except Exception as e:
        flash(f"Error cancelling order: {e}", "error")
    return redirect(url_for("checkout.order_detail", order_id=order_id))


@bp.route("/product/<product_id>/review", methods=["POST"])
def submit_review(product_id):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    uid = session["user"]["id"]
    try:
        rating_str = request.form.get("rating", "").strip()
        comment    = request.form.get("comment", "").strip()
        if not rating_str or not rating_str.isdigit():
            flash("Please select a star rating.", "error")
            return redirect(url_for("public.product_detail", product_id=product_id))
        rating = int(rating_str)
        if not (1 <= rating <= 5):
            flash("Rating must be between 1 and 5.", "error")
            return redirect(url_for("public.product_detail", product_id=product_id))
        if len(comment) < 10:
            flash("Review must be at least 10 characters long.", "error")
            return redirect(url_for("public.product_detail", product_id=product_id))
        if db.query_one(
            "SELECT id FROM product_reviews WHERE product_id=? AND user_id=?", [product_id, uid]
        ):
            flash("You have already submitted a review for this product.", "info")
            return redirect(url_for("public.product_detail", product_id=product_id))
        db.execute(
            "INSERT INTO product_reviews (product_id, user_id, rating, body, is_approved) VALUES (?,?,?,?,0)",
            [product_id, uid, rating, comment],
        )
        try:
            from queries import get_product_detail
            get_product_detail.cache_clear()
        except Exception:
            pass
        flash("Thank you! Your review has been submitted and is pending approval.", "success")
    except Exception as e:
        flash(f"Error submitting review: {e}", "error")
    return redirect(url_for("public.product_detail", product_id=product_id))
