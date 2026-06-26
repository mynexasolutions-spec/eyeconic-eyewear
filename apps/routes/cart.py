from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
import db
from helpers import refresh_cart_prices, calc_shipping
from queries import PRODUCTS_SELECT

bp = Blueprint("cart", __name__)


@bp.route("/cart")
def view_cart():
    cart_items = session.get("cart", {})
    cart_items, subtotal = refresh_cart_prices(cart_items)
    session["cart"] = cart_items
    
    from helpers import get_cached_store_settings
    settings = get_cached_store_settings()
    shipping = calc_shipping(subtotal, settings)
    
    fee       = float(settings.get("shipping_fee") or 49)
    threshold = float(settings.get("free_shipping_threshold") or 599)
    
    return render_template(
        "cart.html",
        cart_items=cart_items,
        subtotal=subtotal,
        shipping=shipping,
        total=subtotal + shipping,
        shipping_fee=fee,
        free_shipping_threshold=threshold,
    )


@bp.route("/cart/add", methods=["POST"])
def cart_add():
    product_id       = str(request.form.get("product_id", "")).strip()
    variation_id     = str(request.form.get("variation_id", "")).strip()
    selected_options = str(request.form.get("selected_options", "")).strip()
    try:
        qty = max(1, int(request.form.get("qty", 1)))
    except (ValueError, TypeError):
        qty = 1

    if not product_id:
        flash("Invalid product.", "error")
        return redirect(request.referrer or url_for("public.shop"))

    try:
        product = db.query_one(f"{PRODUCTS_SELECT} WHERE p.id = ?", [product_id])
        if not product:
            flash("Product not found.", "error")
            return redirect(request.referrer or url_for("public.shop"))

        display_name = product["name"]
        price        = float(product.get("sale_price") or product.get("price") or 0)
        sku          = product.get("sku", "")
        img          = product.get("image_url", "")

        if variation_id:
            # Traditional variation — look up the combo label from the DB
            var = db.query_one("SELECT * FROM product_variations WHERE id = ?", [variation_id])
            if var:
                sku  = var.get("sku", sku)
                opts = db.query("""
                    SELECT av.value FROM attribute_values av
                    JOIN variation_attribute_values vav ON vav.attribute_value_id = av.id
                    WHERE vav.variation_id = ?
                """, [variation_id])
                if opts:
                    display_name += f" ({' / '.join(o['value'] for o in opts)})"
            item_key = variation_id

        is_contacts = product.get("category_slug") in ("contacts", "contact-lenses")
        cart = session.get("cart", {})

        # Handle Lenses Customization
        purchase_mode   = str(request.form.get("purchase_mode", "frame_only")).strip()
        lens_option_id  = str(request.form.get("lens_option_id", "")).strip()
        
        lens_price_modifier = 0.0
        lens_details = ""
        prescription_url = ""

        if purchase_mode == "with_lenses" and lens_option_id:
            lens_opt = db.query_one(
                """SELECT lo.name as option_name, lo.price_modifier, lt.name as type_name
                   FROM lens_options lo 
                   JOIN lens_types lt ON lt.id = lo.lens_type_id
                   WHERE lo.id = ?""",
                [lens_option_id]
            )
            if lens_opt:
                lens_price_modifier = float(lens_opt["price_modifier"] or 0.0)
                lens_details = f"{lens_opt['type_name']} - {lens_opt['option_name']}"
                
                if lens_opt["type_name"] != "Zero Power":
                    prescription_file = request.files.get("prescription")
                    if prescription_file and prescription_file.filename:
                        from helpers import handle_upload
                        prescription_url = handle_upload(prescription_file)
                        
                price += lens_price_modifier
                display_name += f" ({lens_details})"
                
                # Determine custom item key so unique prescription files/lenses don't merge
                item_key = f"{product_id}"
                if variation_id:
                    item_key += f"|{variation_id}"
                if selected_options:
                    item_key += f"|{selected_options}"
                item_key += f"|{lens_option_id}"
                if prescription_url:
                    import hashlib
                    url_hash = hashlib.md5(prescription_url.encode('utf-8')).hexdigest()[:6]
                    item_key += f"|{url_hash}"
                
                if item_key in cart:
                    cart[item_key]["qty"] += qty
                else:
                    cart[item_key] = {
                        "product_id": product_id,
                        "variation_id": variation_id or None,
                        "lens_option_id": lens_option_id,
                        "name": display_name,
                        "price": price,
                        "qty": qty,
                        "image": img,
                        "sku": sku,
                        "lens_details": lens_details,
                        "prescription_url": prescription_url
                    }
        elif is_contacts and selected_options and "||" in selected_options:
            # Split into separate entities for each eye (Left / Right boxes)
            boxes = [b.strip() for b in selected_options.split("||")]
            unit_qty = qty // len(boxes) if len(boxes) > 0 else qty
            for box in boxes:
                item_key = f"{product_id}|{box}"
                if item_key in cart:
                    cart[item_key]["qty"] += unit_qty
                else:
                    cart[item_key] = {
                        "product_id": product_id,
                        "variation_id": None,
                        "name": f"{product['name']} ({box})",
                        "price": price,
                        "qty": unit_qty,
                        "image": img,
                        "sku": sku,
                    }
        elif selected_options:
            # Independent-attribute mode (e.g. contacts: Left Eye + Right Eye).
            # qty already reflects how many attributes were selected (sent from JS).
            display_name += f" ({selected_options})"
            # Use a stable key so the same eye combination stacks, different ones don't.
            item_key = f"{product_id}|{selected_options}"
            if item_key in cart:
                cart[item_key]["qty"] += qty
            else:
                cart[item_key] = {
                    "product_id": product_id,
                    "variation_id": variation_id or None,
                    "name": display_name,
                    "price": price,
                    "qty": qty,
                    "image": img,
                    "sku": sku,
                }
        else:
            item_key = product_id
            if item_key in cart:
                cart[item_key]["qty"] += qty
            else:
                cart[item_key] = {
                    "product_id": product_id,
                    "variation_id": variation_id or None,
                    "name": display_name,
                    "price": price,
                    "qty": qty,
                    "image": img,
                    "sku": sku,
                }
        session["cart"] = cart
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            flash(f"'{display_name}' added to cart!", "success")
    except Exception as e:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            flash(f"Error adding to cart: {e}", "error")
        else:
            return jsonify({"success": False, "message": str(e)})

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        count = sum(i["qty"] for i in session.get("cart", {}).values())
        return jsonify({"success": True, "cart_count": count})
    return redirect(request.referrer or url_for("public.shop"))


@bp.route("/cart/remove", methods=["POST"])
def cart_remove():
    cart = session.get("cart", {})
    item_key = str(request.form.get("product_id", "")).strip()
    if item_key in cart:
        cart.pop(item_key)
        session["cart"] = cart
        flash("Item removed from cart.", "info")
    else:
        # Fallback for complex keys if they were somehow mutated
        removed = False
        for key in list(cart.keys()):
            if str(key).strip() == item_key:
                cart.pop(key)
                removed = True
                break
        if removed:
            session["cart"] = cart
            flash("Item removed from cart.", "info")
        else:
            flash("Item not found in cart.", "error")
    return redirect(url_for("cart.view_cart"))


@bp.route("/cart/update", methods=["POST"])
def cart_update():
    cart = session.get("cart", {})
    for key in list(cart.keys()):
        current_qty = int(cart[key].get("qty", 1) or 1)
        raw_qty = request.form.get(f"qty_{key}", current_qty)
        try:
            new_qty = int(raw_qty)
        except (TypeError, ValueError):
            new_qty = current_qty
        cart[key]["qty"] = max(1, new_qty)
    session["cart"] = cart
    
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})
        
    flash("Cart updated.", "success")
    return redirect(url_for("cart.view_cart"))
