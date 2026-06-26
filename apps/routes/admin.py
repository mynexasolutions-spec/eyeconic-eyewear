import csv
import io
import uuid
import itertools
from functools import wraps
from flask import render_template, request, redirect, url_for, flash, abort, session
import db
from helpers import slugify, get_cached_store_settings, get_unique_slug, handle_upload
from queries import get_products, get_categories, get_brands, get_admin_stats, get_featured_categories, get_trending_shapes, PRODUCTS_SELECT, get_product_detail, get_homepage_products


def _sanitize_sku_prefix(prefix, fallback):
    cleaned = "".join(ch for ch in (prefix or "").upper() if ch.isalnum() or ch in ("-", "_"))
    cleaned = cleaned.strip("-_")
    return cleaned or fallback


def generate_unique_product_sku(name=None):
    base = _sanitize_sku_prefix(slugify(name or ""), "PRD")
    for _ in range(8):
        candidate = f"{base}-{uuid.uuid4().hex[:8].upper()}"
        if not db.query_one("SELECT id FROM products WHERE sku = ?", [candidate]):
            return candidate
    return f"{base}-{uuid.uuid4().hex[:12].upper()}"


def generate_unique_variation_sku(base_sku=None, exclude_id=None):
    base = _sanitize_sku_prefix(base_sku, "VAR")
    for _ in range(8):
        candidate = f"{base}-{uuid.uuid4().hex[:6].upper()}"
        params = [candidate]
        sql = "SELECT id FROM product_variations WHERE sku = ?"
        if exclude_id:
            sql += " AND id <> ?"
            params.append(exclude_id)
        if not db.query_one(sql, params):
            return candidate
    return f"{base}-{uuid.uuid4().hex[:10].upper()}"


def generate_variations(product_id):
    """
    For variable products, we no longer pre-generate all cartesian-product
    variation rows.  The storefront product page already loads the available
    attribute values directly from `product_attribute_values`, and the cart
    stores selected options as text — so thousands of combination rows in
    `product_variations` / `variation_attribute_values` are not required.

    Instead, we create ONE placeholder variation per product so that the
    admin 'Manage Variations' panel still works for editing, and the product
    is recognized as having variations.
    """
    existing = db.query_one(
        "SELECT id FROM product_variations WHERE product_id = ? LIMIT 1",
        [product_id],
    )
    if existing:
        return  # already has at least one variation row

    product = db.query_one(
        "SELECT price, sale_price, stock_quantity, sku FROM products WHERE id = ?",
        [product_id],
    )
    if not product:
        return

    base_price = float(product.get("sale_price") or product.get("price") or 0)
    base_stock = int(product.get("stock_quantity") or 0)
    base_sku   = product.get("sku") or "VAR"

    var_id  = str(uuid.uuid4())
    var_sku = generate_unique_variation_sku(base_sku)
    db.execute(
        "INSERT INTO product_variations (id, product_id, sku, price, stock_quantity) "
        "VALUES (?,?,?,?,?)",
        [var_id, product_id, var_sku, base_price, base_stock],
    )

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user")
        if not user:
            flash("Please log in to continue.", "error")
            return redirect(url_for("auth.login", next=request.url))
        if user.get("role") not in ("admin", "manager"):
            flash("You do not have permission to access this page.", "error")
            return redirect(url_for("public.index"))
        return f(*args, **kwargs)
    return decorated


def register(app):

    # ── Dashboard ──────────────────────────────────────────────────────────────

    @app.route("/admin/")
    @app.route("/admin")
    @require_admin
    def admin_dashboard():
        _empty_stats = {
            "total_products": 0, "total_orders": 0, "total_revenue": 0.0,
            "total_customers": 0, "pending_orders": 0, "low_stock": 0,
        }
        try:
            stats = get_admin_stats()
        except Exception as e:
            stats = _empty_stats
            flash(f"Stats error: {e}", "error")
        # Ensure all keys exist even if get_admin_stats returns a partial dict
        for k, v in _empty_stats.items():
            stats.setdefault(k, v)

        try:
            recent_orders = db.query(
                """SELECT o.id, o.created_at, o.total_amount, o.status,
                          (u.first_name || ' ' || u.last_name) AS customer_name, u.email AS customer_email
                   FROM orders o LEFT JOIN users u ON u.id = o.user_id
                   ORDER BY o.created_at DESC LIMIT 10"""
            )
        except Exception:
            recent_orders = []

        try:
            recent_products = db.query(
                f"{PRODUCTS_SELECT} WHERE p.is_active=1 ORDER BY p.created_at DESC LIMIT 8"
            )
        except Exception:
            recent_products = []

        try:
            chart_rows = db.query("""
                SELECT TO_CHAR(created_at, 'DD Mon') AS day,
                       SUM(total_amount) AS amount
                FROM orders
                WHERE created_at >= NOW() - INTERVAL '7 days'
                  AND status != 'cancelled'
                GROUP BY TO_CHAR(created_at, 'YYYY-MM-DD')
                ORDER BY TO_CHAR(created_at, 'YYYY-MM-DD')
            """)
            chart_data = {
                "labels": [r["day"] for r in chart_rows],
                "values": [float(r["amount"]) for r in chart_rows],
            }
        except Exception:
            chart_data = {"labels": [], "values": []}
        return render_template(
            "admin/dashboard.html",
            stats=stats, recent_orders=recent_orders,
            recent_products=recent_products, chart_data=chart_data,
        )

    # ── Products ───────────────────────────────────────────────────────────────

    @app.route("/admin/products")
    @require_admin
    def admin_products():
        search   = request.args.get("search", "").strip()
        category = request.args.get("category", "").strip()
        brand    = request.args.get("brand", "").strip()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        try:
            products, total, total_pages = get_products(
                search=search, category=category, brand=brand, page=page, per_page=20
            )
            categories = get_categories()
            brands     = get_brands()
        except Exception as e:
            products, total, total_pages = [], 0, 1
            categories = brands = []
            flash(f"Error: {e}", "error")
        return render_template(
            "admin/products.html",
            products=products, total=total, total_pages=total_pages, page=page,
            categories=categories, brands=brands,
            search=search, selected_category=category, selected_brand=brand,
        )

    @app.route("/admin/products/new", methods=["GET", "POST"])
    @require_admin
    def admin_product_new():
        categories     = get_categories()
        brands         = get_brands()
        all_attributes = db.query("SELECT * FROM attributes ORDER BY name ASC")
        for attr in all_attributes:
            attr["options"] = db.query(
                "SELECT * FROM attribute_values WHERE attribute_id = ? ORDER BY value ASC", [attr["id"]]
            )
        
        if request.method == "POST":
            f = request.form
            try:
                name = (f.get("name") or "").strip()
                if not name:
                    flash("Product name is required.", "error")
                    return render_template("admin/product_form.html", product=None, categories=categories, brands=brands, all_attributes=all_attributes, action="new")
                stock_qty = int(f.get("stock_quantity") or 0)
                stock_status = f.get("stock_status", "in_stock")
                slug = get_unique_slug("products", f.get("slug") or slugify(name))
                sku_input = f.get("sku", "").strip()
                sku = sku_input or generate_unique_product_sku(name)

                product_id = db.execute_returning(
                    """INSERT INTO products
                       (id, name, slug, sku, type, description, short_description,
                        price, sale_price, stock_quantity, stock_status, manage_stock,
                        category_id, brand_id, is_featured, is_active, is_lens_compatible)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                    [
                        str(uuid.uuid4()), name, slug, sku,
                        f.get("type", "simple"), f.get("description"), f.get("short_description"),
                        float(f.get("price") or 0), float(f.get("sale_price") or 0) or None,
                        stock_qty, stock_status, True,
                        f.get("category_id") or None, f.get("brand_id") or None,
                        1 if f.get("is_featured") == "on" else 0, 1 if f.get("is_active", "on") == "on" else 0,
                        True if f.get("is_lens_compatible") == "on" else False,
                    ]
                )["id"]

                # Handle Primary Image
                primary_file = request.files.get("primary_image")
                if primary_file and primary_file.filename:
                    url = handle_upload(primary_file)
                    mid = str(uuid.uuid4())
                    db.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, url])
                    db.execute("INSERT INTO product_images (id, product_id, media_id, is_primary, display_order) VALUES (?,?,?,1,0)", [str(uuid.uuid4()), product_id, mid])

                # Handle Gallery Images
                gallery_files = request.files.getlist("gallery_images")
                if gallery_files and any(g.filename for g in gallery_files):
                    for i, gfile in enumerate(gallery_files):
                        if gfile and gfile.filename:
                            url = handle_upload(gfile)
                            mid = str(uuid.uuid4())
                            db.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, url])
                            db.execute("INSERT INTO product_images (id, product_id, media_id, is_primary, display_order) VALUES (?,?,?,0,?)", [str(uuid.uuid4()), product_id, mid, i+1])

                attr_ids = request.form.getlist("attribute_ids")
                if attr_ids:
                    values_sql = ",".join(["(?,?,?)"] * len(attr_ids))
                    params = []
                    for attr_id in attr_ids:
                        params.extend([str(uuid.uuid4()), product_id, attr_id])
                    db.execute(
                        f"INSERT INTO product_attributes (id, product_id, attribute_id) VALUES {values_sql}",
                        params
                    )

                val_ids = request.form.getlist("attribute_value_ids")
                if val_ids:
                    values_sql = ",".join(["(?,?,?)"] * len(val_ids))
                    params = []
                    for val_id in val_ids:
                        params.extend([str(uuid.uuid4()), product_id, val_id])
                    db.execute(
                        f"INSERT INTO product_attribute_values (id, product_id, attribute_value_id) VALUES {values_sql}",
                        params
                    )
                if f.get("type") == "variable":
                    generate_variations(product_id)

                get_products.cache_clear()
                get_homepage_products.cache_clear()
                get_product_detail.cache_clear()
                flash("Product created successfully.", "success")
                return redirect(url_for("admin_products"))
            except Exception as e:
                flash(f"Error creating product: {e}", "error")

        return render_template("admin/product_form.html", product=None, categories=categories, brands=brands, all_attributes=all_attributes, action="new")

    @app.route("/admin/products/<product_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_product_edit(product_id):
        product = db.query_one("SELECT * FROM products WHERE id=?", [product_id])
        if not product: abort(404)
        
        categories = get_categories()
        brands = get_brands()
        all_attributes = db.query("SELECT * FROM attributes ORDER BY name ASC")
        for attr in all_attributes:
            attr["options"] = db.query("SELECT * FROM attribute_values WHERE attribute_id = ? ORDER BY value ASC", [attr["id"]])
        
        if request.method == "POST":
            f = request.form
            try:
                name = (f.get("name") or "").strip()
                if not name:
                    flash("Product name is required.", "error")
                    return render_template("admin/product_form.html", product=product, categories=categories, brands=brands, all_attributes=all_attributes, action="edit")
                slug = get_unique_slug("products", f.get("slug") or slugify(name), exclude_id=product_id)
                sku_input = (f.get("sku") or "").strip()
                updated_sku = sku_input or product.get("sku") or generate_unique_product_sku(name)
                db.execute(
                     """UPDATE products SET name=?, slug=?, sku=?, type=?, description=?,
                        short_description=?, price=?, sale_price=?, stock_quantity=?, stock_status=?,
                        category_id=?, brand_id=?, is_featured=?, is_active=?, is_lens_compatible=? WHERE id=?""",
                    [
                        name, slug, updated_sku, f.get("type"), f.get("description"),
                        f.get("short_description"), float(f.get("price") or 0), float(f.get("sale_price") or 0) or None,
                        int(f.get("stock_quantity") or 0), f.get("stock_status"),
                        f.get("category_id") or None, f.get("brand_id") or None,
                        1 if f.get("is_featured") == "on" else 0, 1 if f.get("is_active") == "on" else 0,
                        True if f.get("is_lens_compatible") == "on" else False,
                        product_id
                    ]
                )

                # Handle Primary Image
                primary_file = request.files.get("primary_image")
                if primary_file and primary_file.filename:
                    url = handle_upload(primary_file)
                    mid = str(uuid.uuid4())
                    db.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, url])
                    db.execute("DELETE FROM product_images WHERE product_id=? AND is_primary=1", [product_id])
                    db.execute("INSERT INTO product_images (id, product_id, media_id, is_primary, display_order) VALUES (?,?,?,1,0)", [str(uuid.uuid4()), product_id, mid])

                # Handle New Gallery Images
                gallery_files = request.files.getlist("gallery_images")
                for gfile in gallery_files:
                    if gfile and gfile.filename:
                        url = handle_upload(gfile)
                        mid = str(uuid.uuid4())
                        db.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, url])
                        db.execute("INSERT INTO product_images (id, product_id, media_id, is_primary) VALUES (?,?,?,0)", [str(uuid.uuid4()), product_id, mid])

                # Handle Deletions
                for key in request.form:
                    if key.startswith("delete_image_"):
                        img_id = key.replace("delete_image_", "")
                        db.execute("DELETE FROM product_images WHERE id=?", [img_id])

                db.execute("DELETE FROM product_attributes WHERE product_id=?", [product_id])
                for aid in request.form.getlist("attribute_ids"):
                    db.execute("INSERT INTO product_attributes (id, product_id, attribute_id) VALUES (?,?,?)", [str(uuid.uuid4()), product_id, aid])
                
                db.execute("DELETE FROM product_attribute_values WHERE product_id=?", [product_id])
                for vid in request.form.getlist("attribute_value_ids"):
                    db.execute("INSERT INTO product_attribute_values (id, product_id, attribute_value_id) VALUES (?,?,?)", [str(uuid.uuid4()), product_id, vid])

                get_products.cache_clear()
                get_homepage_products.cache_clear()
                get_product_detail.cache_clear()
                flash("Product updated successfully.", "success")
                return redirect(url_for("admin_products"))
            except Exception as e:
                flash(f"Error: {e}", "error")

        # Fetch images for display
        product_images = db.query("""
            SELECT pi.id, pi.is_primary, m.file_url as image_url 
            FROM product_images pi 
            JOIN media m ON m.id = pi.media_id 
            WHERE pi.product_id=? ORDER BY pi.is_primary DESC, pi.display_order ASC
        """, [product_id])
        
        product_attribute_ids = [r["attribute_id"] for r in db.query("SELECT attribute_id FROM product_attributes WHERE product_id=?", [product_id])]
        product_value_ids = [r["attribute_value_id"] for r in db.query("SELECT attribute_value_id FROM product_attribute_values WHERE product_id=?", [product_id])]
        
        return render_template(
            "admin/product_form.html",
            product=product, product_images=product_images,
            categories=categories, brands=brands, all_attributes=all_attributes,
            product_attribute_ids=product_attribute_ids, product_value_ids=product_value_ids,
            action="edit"
        )

    @app.route("/admin/products/<product_id>/delete", methods=["POST"])
    @require_admin
    def admin_product_delete(product_id):
        try:
            db.execute("UPDATE products SET is_active=0 WHERE id=?", [product_id])
            get_products.cache_clear()
            get_homepage_products.cache_clear()
            get_product_detail.cache_clear()
            flash("Product deleted (deactivated).", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_products"))

    # ── Categories ─────────────────────────────────────────────────────────────

    @app.route("/admin/categories")
    @require_admin
    def admin_categories():
        try:
            categories = get_categories()
        except Exception as e:
            categories = []
            flash(f"Error: {e}", "error")
        return render_template("admin/categories.html", categories=categories)

    @app.route("/admin/categories/new", methods=["GET", "POST"])
    @require_admin
    def admin_category_new():
        if request.method == "POST":
            name       = request.form.get("name", "").strip()
            if not name:
                flash("Category name is required.", "error")
                return render_template("admin/category_form.html", category=None, categories=get_categories())
            slug       = request.form.get("slug") or slugify(name)
            parent_id  = request.form.get("parent_id") or None
            is_featured = 1 if request.form.get("is_featured") == "on" else 0
            
            # Handle Upload
            image_url = handle_upload(request.files.get("image_file")) or request.form.get("image_url") or None
            
            try:
                db.execute(
                    "INSERT INTO categories (id, name, slug, parent_id, image_url, is_featured) VALUES (?,?,?,?,?,?)",
                    [str(uuid.uuid4()), name, slug, parent_id, image_url, is_featured]
                )
                get_categories.cache_clear()
                get_featured_categories.cache_clear()
                flash("Category created", "success")
                return redirect(url_for("admin_categories"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/category_form.html", category=None, categories=get_categories())

    @app.route("/admin/categories/<cat_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_category_edit(cat_id):
        category = db.query_one("SELECT * FROM categories WHERE id = ?", [cat_id])
        if not category:
            abort(404)
        if request.method == "POST":
            # Handle Upload
            image_url = handle_upload(request.files.get("image_file")) or request.form.get("image_url") or category["image_url"]
            
            try:
                db.execute(
                    "UPDATE categories SET name=?, slug=?, parent_id=?, image_url=?, is_featured=? WHERE id=?",
                    [request.form.get("name"), request.form.get("slug"),
                     request.form.get("parent_id") or None, image_url,
                     1 if request.form.get("is_featured") == "on" else 0, cat_id]
                )
                get_categories.cache_clear()
                get_featured_categories.cache_clear()
                flash("Category updated", "success")
                return redirect(url_for("admin_categories"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/category_form.html", category=category, categories=get_categories())

    @app.route("/admin/categories/<cat_id>/delete", methods=["POST"])
    @require_admin
    def admin_category_delete(cat_id):
        try:
            # Manually decouple products and subcategories before deletion
            db.execute("UPDATE products SET category_id = NULL WHERE category_id = ?", [cat_id])
            db.execute("UPDATE categories SET parent_id = NULL WHERE parent_id = ?", [cat_id])
            
            db.execute("DELETE FROM categories WHERE id=?", [cat_id])
            get_categories.cache_clear()
            get_featured_categories.cache_clear()
            flash("Category deleted.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_categories"))

    # ── Brands ─────────────────────────────────────────────────────────────────

    @app.route("/admin/brands")
    @require_admin
    def admin_brands():
        try:
            brands = db.query(
                """
                SELECT b.*, COUNT(p.id) AS product_count
                FROM brands b
                LEFT JOIN products p ON p.brand_id = b.id
                GROUP BY b.id
                ORDER BY b.name ASC
                """
            )
        except Exception as e:
            brands = []
            flash(f"Error: {e}", "error")
        return render_template("admin/brands.html", brands=brands)

    @app.route("/admin/brands/new", methods=["GET", "POST"])
    @require_admin
    def admin_brand_new():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Brand name is required.", "error")
                return render_template("admin/brand_form.html", brand=None)
            slug = request.form.get("slug") or slugify(name)
            image_url = handle_upload(request.files.get("image_file")) or None
            try:
                db.execute(
                    "INSERT INTO brands (id, name, slug, image_url) VALUES (?,?,?,?)",
                    [str(uuid.uuid4()), name, slug, image_url]
                )
                flash("Brand created.", "success")
                return redirect(url_for("admin_brands"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/brand_form.html", brand=None)

    @app.route("/admin/brands/<brand_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_brand_edit(brand_id):
        brand = db.query_one("SELECT * FROM brands WHERE id = ?", [brand_id])
        if not brand: abort(404)
        if request.method == "POST":
            name = request.form.get("name")
            slug = request.form.get("slug") or slugify(name)
            image_url = handle_upload(request.files.get("image_file")) or brand["image_url"]
            try:
                db.execute(
                    "UPDATE brands SET name=?, slug=?, image_url=? WHERE id=?",
                    [name, slug, image_url, brand_id]
                )
                flash("Brand updated.", "success")
                return redirect(url_for("admin_brands"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/brand_form.html", brand=brand)

    @app.route("/admin/brands/<brand_id>/delete", methods=["POST"])
    @require_admin
    def admin_brand_delete(brand_id):
        try:
            # Manually decouple products before deletion
            db.execute("UPDATE products SET brand_id = NULL WHERE brand_id = ?", [brand_id])
            
            db.execute("DELETE FROM brands WHERE id=?", [brand_id])
            flash("Brand deleted.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_brands"))

    # ── Orders ─────────────────────────────────────────────────────────────────

    @app.route("/admin/orders")
    @require_admin
    def admin_orders():
        import math
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        per_page = 20
        offset   = (page - 1) * per_page
        try:
            orders = db.query(
                """SELECT o.id, o.created_at, o.total_amount, o.status,
                          (u.first_name || ' ' || u.last_name) AS customer_name,
                          u.email AS customer_email, COUNT(oi.id) AS item_count
                   FROM orders o
                   LEFT JOIN users u  ON u.id = o.user_id
                   LEFT JOIN order_items oi ON oi.order_id = o.id
                   GROUP BY o.id, o.created_at, o.total_amount, o.status, u.first_name, u.last_name, u.email
                   ORDER BY o.created_at DESC LIMIT ? OFFSET ?""",
                [per_page, offset]
            )
            total       = (db.query_one("SELECT COUNT(*) AS cnt FROM orders") or {}).get("cnt", 0)
            total_pages = max(1, math.ceil(total / per_page))
        except Exception as e:
            orders, total, total_pages = [], 0, 1
            flash(f"Error: {e}", "error")
        return render_template(
            "admin/orders.html", orders=orders, total=total, total_pages=total_pages, page=page
        )

    @app.route("/admin/orders/<order_id>")
    @require_admin
    def admin_order_detail(order_id):
        try:
            order = db.query_one(
                """SELECT o.*, (u.first_name || ' ' || u.last_name) AS customer_name, u.email AS customer_email
                   FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE o.id=?""",
                [order_id]
            )
            if not order:
                abort(404)
            items = db.query(
                "SELECT oi.*, p.name AS product_name, p.sku FROM order_items oi "
                "LEFT JOIN products p ON p.id = oi.product_id WHERE oi.order_id=?",
                [order_id]
            )
        except Exception as e:
            flash(f"Error: {e}", "error")
            return redirect(url_for("admin_orders"))
        return render_template("admin/order_detail.html", order=order, items=items)

    @app.route("/admin/orders/<order_id>/status", methods=["POST"])
    @require_admin
    def admin_order_status(order_id):
        status = request.form.get("status")
        valid  = ("pending", "processing", "shipped", "delivered", "cancelled", "refunded")
        if status not in valid:
            flash("Invalid status.", "error")
            return redirect(url_for("admin_order_detail", order_id=order_id))
        try:
            # Sync payment_status for terminal states
            payment_status_map = {
                "cancelled": "cancelled",
                "refunded":  "refunded",
            }
            payment_status = payment_status_map.get(status)
            if payment_status:
                db.execute(
                    "UPDATE orders SET status=?, payment_status=? WHERE id=?",
                    [status, payment_status, order_id]
                )
            else:
                db.execute("UPDATE orders SET status=? WHERE id=?", [status, order_id])
            flash(f"Order status updated to '{status}'.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_order_detail", order_id=order_id))

    # ── Customers ──────────────────────────────────────────────────────────────

    @app.route("/admin/customers")
    @require_admin
    def admin_customers():
        try:
            customers = db.query("SELECT * FROM users WHERE role='customer' ORDER BY created_at DESC")
        except Exception as e:
            customers = []
            flash(f"Error: {e}", "error")
        return render_template("admin/customers.html", customers=customers)

    @app.route("/admin/subscribers")
    @require_admin
    def admin_subscribers():
        try:
            subscribers = db.query("SELECT * FROM newsletter_subscribers ORDER BY subscribed_at DESC")
        except Exception as e:
            subscribers = []
            flash(f"Error loading subscribers: {e}", "error")
        return render_template("admin/subscribers.html", subscribers=subscribers)

    # ── Attributes ─────────────────────────────────────────────────────────────

    @app.route("/admin/attributes")
    @require_admin
    def admin_attributes():
        try:
            attributes = db.query("""
                SELECT a.*, (SELECT COUNT(*) FROM attribute_values v WHERE v.attribute_id = a.id) as value_count
                FROM attributes a ORDER BY a.name ASC
            """)
        except Exception as e:
            attributes = []
            flash(f"Error: {e}", "error")
        return render_template("admin/attributes.html", attributes=attributes)

    @app.route("/admin/attributes/new", methods=["GET", "POST"])
    @require_admin
    def admin_attribute_new():
        if request.method == "POST":
            name       = request.form.get("name")
            slug       = request.form.get("slug") or slugify(name)
            is_featured = 1 if request.form.get("is_featured") == "on" else 0
            image_url = handle_upload(request.files.get("image_file")) or None
            
            if not name:
                flash("Name is required", "error")
            else:
                try:
                    db.execute(
                        "INSERT INTO attributes (id, name, slug, display_order, image_url, is_featured) VALUES (?,?,?,0,?,?)",
                        [str(uuid.uuid4()), name, slug, image_url, is_featured]
                    )
                    flash("Attribute created", "success")
                    return redirect(url_for("admin_attributes"))
                except Exception as e:
                    flash(f"Error: {e}", "error")
        return render_template("admin/attribute_form.html", attribute=None)

    @app.route("/admin/attributes/<attr_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_attribute_edit(attr_id):
        attribute = db.query_one("SELECT * FROM attributes WHERE id = ?", [attr_id])
        if not attribute:
            abort(404)
        if request.method == "POST":
            image_url = handle_upload(request.files.get("image_file")) or attribute["image_url"]
            try:
                db.execute(
                    "UPDATE attributes SET name=?, slug=?, image_url=?, is_featured=? WHERE id=?",
                    [request.form.get("name"), request.form.get("slug"),
                     image_url,
                     1 if request.form.get("is_featured") == "on" else 0, attr_id]
                )
                flash("Attribute updated", "success")
                return redirect(url_for("admin_attributes"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/attribute_form.html", attribute=attribute)

    @app.route("/admin/attributes/<attr_id>/delete", methods=["POST"])
    @require_admin
    def admin_attribute_delete(attr_id):
        try:
            # Cascade: remove all references before deleting the attribute
            db.execute(
                "DELETE FROM product_attribute_values WHERE attribute_value_id IN "
                "(SELECT id FROM attribute_values WHERE attribute_id = ?)", [attr_id]
            )
            db.execute(
                "DELETE FROM variation_attribute_values WHERE attribute_value_id IN "
                "(SELECT id FROM attribute_values WHERE attribute_id = ?)", [attr_id]
            )
            db.execute("DELETE FROM attribute_values WHERE attribute_id = ?", [attr_id])
            db.execute("DELETE FROM product_attributes WHERE attribute_id = ?", [attr_id])
            db.execute("DELETE FROM attributes WHERE id = ?", [attr_id])
            flash("Attribute deleted", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_attributes"))

    @app.route("/admin/attributes/<attr_id>/values", methods=["GET", "POST"])
    @require_admin
    def admin_attribute_values(attr_id):
        attribute = db.query_one("SELECT * FROM attributes WHERE id = ?", [attr_id])
        if not attribute:
            flash("Attribute not found", "error")
            return redirect(url_for("admin_attributes"))
        if request.method == "POST":
            value     = request.form.get("value")
            image_url = handle_upload(request.files.get("image_file")) or None
            if value:
                try:
                    db.execute(
                        "INSERT INTO attribute_values (id, attribute_id, value, image_url) VALUES (?,?,?,?)",
                        [str(uuid.uuid4()), attr_id, value, image_url]
                    )
                    get_trending_shapes.cache_clear()
                    flash("Value added", "success")
                except Exception as e:
                    flash(f"Error: {e}", "error")
        values = db.query(
            "SELECT * FROM attribute_values WHERE attribute_id = ? ORDER BY value ASC", [attr_id]
        )
        return render_template("admin/attribute_values.html", attribute=attribute, values=values)

    @app.route("/admin/attributes/<attr_id>/values/bulk_update", methods=["POST"])
    @require_admin
    def admin_attribute_values_bulk_update(attr_id):
        f = request.form
        try:
            values = db.query("SELECT id FROM attribute_values WHERE attribute_id = ?", [attr_id])
            for v in values:
                v_id = v["id"]
                new_value = (f.get(f"value_{v_id}") or "").strip()
                if new_value:
                    db.execute("UPDATE attribute_values SET value=? WHERE id=?", [new_value, v_id])
            get_trending_shapes.cache_clear()
            flash("Attribute values updated.", "success")
        except Exception as e:
            flash(f"Error updating values: {e}", "error")
        return redirect(url_for("admin_attribute_values", attr_id=attr_id))

    @app.route("/admin/attributes/values/<val_id>/delete", methods=["POST"])
    @require_admin
    def admin_attribute_value_delete(val_id):
        attr_id = request.form.get("attribute_id")
        try:
            # Cascade: remove all product & variation references first
            db.execute("DELETE FROM product_attribute_values WHERE attribute_value_id = ?", [val_id])
            db.execute("DELETE FROM variation_attribute_values WHERE attribute_value_id = ?", [val_id])
            db.execute("DELETE FROM attribute_values WHERE id = ?", [val_id])
            get_trending_shapes.cache_clear()
            flash("Value deleted", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_attribute_values", attr_id=attr_id))

    @app.route("/admin/attributes/values/<val_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_attribute_value_edit(val_id):
        value = db.query_one("SELECT * FROM attribute_values WHERE id = ?", [val_id])
        if not value:
            abort(404)
        
        attribute = db.query_one("SELECT * FROM attributes WHERE id = ?", [value["attribute_id"]])
        
        if request.method == "POST":
            new_value = request.form.get("value")
            image_url = handle_upload(request.files.get("image_file")) or value.get("image_url")
            
            try:
                db.execute(
                    "UPDATE attribute_values SET value=?, image_url=? WHERE id=?",
                    [new_value, image_url, val_id]
                )
                get_trending_shapes.cache_clear()
                flash("Value updated", "success")
                return redirect(url_for("admin_attribute_values", attr_id=value["attribute_id"]))
            except Exception as e:
                flash(f"Error: {e}", "error")
                
        return render_template("admin/attribute_value_form.html", attribute=attribute, value=value)

    # ── Variations ─────────────────────────────────────────────────────────────

    @app.route("/admin/products/<product_id>/variations")
    @require_admin
    def admin_product_variations(product_id):
        product = db.query_one("SELECT id, name, type FROM products WHERE id = ?", [product_id])
        if not product:
            abort(404)
        variations = db.query("""
            SELECT v.*,
                   (SELECT string_agg(av.value, ' / ')
                    FROM variation_attribute_values vav
                    JOIN attribute_values av ON av.id = vav.attribute_value_id
                    WHERE vav.variation_id = v.id) as option_names
            FROM product_variations v WHERE v.product_id = ? ORDER BY v.sku ASC
        """, [product_id])
        linked_attributes = db.query("""
            SELECT a.id, a.name FROM attributes a
            JOIN product_attributes pa ON pa.attribute_id = a.id
            WHERE pa.product_id = ? ORDER BY pa.display_order ASC
        """, [product_id])
        for attr in linked_attributes:
            attr["options"] = db.query("""
                SELECT av.* FROM attribute_values av
                JOIN product_attribute_values pav ON pav.attribute_value_id = av.id
                WHERE av.attribute_id = ? AND pav.product_id = ? ORDER BY av.value ASC
            """, [attr["id"], product_id])
            if not attr["options"]:
                attr["options"] = db.query(
                    "SELECT * FROM attribute_values WHERE attribute_id = ? ORDER BY value ASC", [attr["id"]]
                )
        return render_template(
            "admin/variations.html", product=product, variations=variations, attributes=linked_attributes
        )

    @app.route("/admin/products/<product_id>/variations/new", methods=["POST"])
    @require_admin
    def admin_variation_new(product_id):
        f = request.form
        try:
            product = db.query_one("SELECT price, sale_price, stock_quantity, sku FROM products WHERE id = ?", [product_id]) or {}
            variation_sku = (f.get("sku") or "").strip() or generate_unique_variation_sku(
                (product or {}).get("sku")
            )
            var_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO product_variations (id, product_id, sku, price, sale_price, stock_quantity) "
                "VALUES (?,?,?,?,?,?)",
                [var_id, product_id, variation_sku,
                 float(product.get("sale_price") or product.get("price") or 0), None,
                 int(product.get("stock_quantity") or 0)]
            )
            for key, val_id in f.items():
                if key.startswith("attr_") and val_id:
                    db.execute(
                        "INSERT INTO variation_attribute_values (id, variation_id, attribute_value_id) VALUES (?,?,?)",
                        [str(uuid.uuid4()), var_id, val_id]
                    )
            flash("Variation created", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_product_variations", product_id=product_id))

    @app.route("/admin/variations/<var_id>/delete", methods=["POST"])
    @require_admin
    def admin_variation_delete(var_id):
        product_id = request.form.get("product_id")
        try:
            # Cascade: remove attribute value links first to avoid orphaned rows
            db.execute("DELETE FROM variation_attribute_values WHERE variation_id = ?", [var_id])
            db.execute("DELETE FROM product_variations WHERE id = ?", [var_id])
            flash("Variation deleted", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_product_variations", product_id=product_id))

    @app.route("/admin/products/<product_id>/variations/bulk_update", methods=["POST"])
    @require_admin
    def admin_variations_bulk_update(product_id):
        try:
            product = db.query_one("SELECT sku, price, sale_price, stock_quantity FROM products WHERE id = ?", [product_id]) or {}
            base_sku = product.get("sku")
            base_price = float(product.get("sale_price") or product.get("price") or 0)
            base_stock = int(product.get("stock_quantity") or 0)
            # First, fetch all variation IDs for this product to validate
            rows = db.query("SELECT id FROM product_variations WHERE product_id = ?", [product_id])
            var_ids = [r["id"] for r in rows]
            
            for vid in var_ids:
                sku = (request.form.get(f"sku_{vid}") or "").strip()
                final_sku = sku or generate_unique_variation_sku(base_sku, exclude_id=vid)
                
                db.execute(
                    "UPDATE product_variations SET sku=?, price=?, sale_price=?, stock_quantity=? WHERE id=?",
                    [final_sku, base_price, None, base_stock, vid]
                )
            flash("All variations updated successfully.", "success")
        except Exception as e:
            flash(f"Error during bulk update: {e}", "error")
        return redirect(url_for("admin_product_variations", product_id=product_id))

    # ── Lenses Management ──────────────────────────────────────────────────────

    @app.route("/admin/lenses")
    @require_admin
    def admin_lenses():
        try:
            types = db.query("SELECT * FROM lens_types ORDER BY display_order ASC, name ASC")
            for t in types:
                t["options"] = db.query(
                    "SELECT * FROM lens_options WHERE lens_type_id = ? ORDER BY display_order ASC, name ASC",
                    [t["id"]]
                )
        except Exception as e:
            types = []
            flash(f"Error: {e}", "error")
        return render_template("admin/lenses.html", lens_types=types)

    @app.route("/admin/lenses/new", methods=["GET", "POST"])
    @require_admin
    def admin_lens_new():
        if request.method == "POST":
            name = request.form.get("name")
            desc = request.form.get("description", "")
            order = int(request.form.get("display_order") or 0)
            is_active = 1 if request.form.get("is_active", "on") == "on" else 0
            
            image_url = ""
            img_file = request.files.get("image_file")
            if img_file and img_file.filename:
                try:
                    image_url = handle_upload(img_file)
                except Exception as e:
                    flash(f"Image upload failed: {e}", "error")

            if not name:
                flash("Name is required", "error")
            else:
                try:
                    db.execute(
                        """INSERT INTO lens_types (id, name, description, image_url, display_order, is_active) 
                           VALUES (?,?,?,?,?,?)""",
                        [str(uuid.uuid4()), name, desc, image_url, order, is_active]
                    )
                    flash("Lens type created.", "success")
                    return redirect(url_for("admin_lenses"))
                except Exception as e:
                    flash(f"Error: {e}", "error")
        return render_template("admin/lens_form.html", lens_type=None, action="new")

    @app.route("/admin/lenses/<lens_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_lens_edit(lens_id):
        lens_type = db.query_one("SELECT * FROM lens_types WHERE id = ?", [lens_id])
        if not lens_type:
            abort(404)
            
        if request.method == "POST":
            name = request.form.get("name")
            desc = request.form.get("description", "")
            order = int(request.form.get("display_order") or 0)
            is_active = 1 if request.form.get("is_active") == "on" else 0
            
            image_url = lens_type.get("image_url") or ""
            img_file = request.files.get("image_file")
            if img_file and img_file.filename:
                try:
                    image_url = handle_upload(img_file)
                except Exception as e:
                    flash(f"Image upload failed: {e}", "error")

            if not name:
                flash("Name is required", "error")
            else:
                try:
                    db.execute(
                        """UPDATE lens_types SET name=?, description=?, image_url=?, display_order=?, is_active=? 
                           WHERE id=?""",
                        [name, desc, image_url, order, is_active, lens_id]
                    )
                    flash("Lens type updated.", "success")
                    return redirect(url_for("admin_lenses"))
                except Exception as e:
                    flash(f"Error: {e}", "error")
        return render_template("admin/lens_form.html", lens_type=lens_type, action="edit")

    @app.route("/admin/lenses/<lens_id>/delete", methods=["POST"])
    @require_admin
    def admin_lens_delete(lens_id):
        try:
            db.execute("DELETE FROM lens_types WHERE id = ?", [lens_id])
            flash("Lens type deleted.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_lenses"))

    @app.route("/admin/lenses/<lens_id>/options/new", methods=["GET", "POST"])
    @require_admin
    def admin_lens_option_new(lens_id):
        lens_type = db.query_one("SELECT * FROM lens_types WHERE id = ?", [lens_id])
        if not lens_type:
            abort(404)
            
        if request.method == "POST":
            name = request.form.get("name")
            price = float(request.form.get("price_modifier") or 0.00)
            order = int(request.form.get("display_order") or 0)
            description = request.form.get("description", "").strip()
            is_active = 1 if request.form.get("is_active", "on") == "on" else 0
            
            if not name:
                flash("Name is required", "error")
            else:
                try:
                    db.execute(
                        """INSERT INTO lens_options (id, lens_type_id, name, price_modifier, display_order, is_active, description) 
                           VALUES (?,?,?,?,?,?,?)""",
                        [str(uuid.uuid4()), lens_id, name, price, order, is_active, description]
                    )
                    flash("Lens option created.", "success")
                    return redirect(url_for("admin_lenses"))
                except Exception as e:
                    flash(f"Error: {e}", "error")
        return render_template("admin/lens_option_form.html", option=None, lens_type=lens_type, action="new")

    @app.route("/admin/lenses/options/<option_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_lens_option_edit(option_id):
        option = db.query_one("SELECT * FROM lens_options WHERE id = ?", [option_id])
        if not option:
            abort(404)
        lens_type = db.query_one("SELECT * FROM lens_types WHERE id = ?", [option["lens_type_id"]])
        
        if request.method == "POST":
            name = request.form.get("name")
            price = float(request.form.get("price_modifier") or 0.00)
            order = int(request.form.get("display_order") or 0)
            description = request.form.get("description", "").strip()
            is_active = 1 if request.form.get("is_active") == "on" else 0
            
            if not name:
                flash("Name is required", "error")
            else:
                try:
                    db.execute(
                        """UPDATE lens_options SET name=?, price_modifier=?, display_order=?, is_active=?, description=? 
                           WHERE id=?""",
                        [name, price, order, is_active, description, option_id]
                    )
                    flash("Lens option updated.", "success")
                    return redirect(url_for("admin_lenses"))
                except Exception as e:
                    flash(f"Error: {e}", "error")
        return render_template("admin/lens_option_form.html", option=option, lens_type=lens_type, action="edit")

    @app.route("/admin/lenses/options/<option_id>/delete", methods=["POST"])
    @require_admin
    def admin_lens_option_delete(option_id):
        try:
            db.execute("DELETE FROM lens_options WHERE id = ?", [option_id])
            flash("Lens option deleted.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_lenses"))

    # ── Reviews ────────────────────────────────────────────────────────────────

    @app.route("/admin/reviews")
    @require_admin
    def admin_reviews():
        try:
            reviews = db.query("""
                SELECT r.*, r.body AS comment, p.name AS product_name, p.id AS product_id,
                       (u.first_name || ' ' || u.last_name) AS reviewer_name
                FROM product_reviews r 
                LEFT JOIN products p ON p.id = r.product_id
                LEFT JOIN users u ON u.id = r.user_id
                ORDER BY r.created_at DESC
            """)
        except Exception as e:
            reviews = []
            flash(f"Error loading reviews: {e}", "error")
        return render_template("admin/reviews.html", reviews=reviews)

    @app.route("/admin/reviews/<review_id>/approve", methods=["POST"])
    @require_admin
    def admin_review_approve(review_id):
        action = request.form.get("action", "approve")
        try:
            approved = 1 if action == "approve" else 0
            db.execute("UPDATE product_reviews SET is_approved=? WHERE id=?", [approved, review_id])
            try:
                from queries import get_product_detail
                get_product_detail.cache_clear()
            except Exception:
                pass
            flash("Review " + ("approved." if approved else "rejected."), "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_reviews"))

    @app.route("/admin/reviews/<review_id>/delete", methods=["POST"])
    @require_admin
    def admin_review_delete(review_id):
        try:
            db.execute("DELETE FROM product_reviews WHERE id=?", [review_id])
            try:
                from queries import get_product_detail
                get_product_detail.cache_clear()
            except Exception:
                pass
            flash("Review deleted.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_reviews"))

    # ── Settings ───────────────────────────────────────────────────────────────

    @app.route("/admin/settings", methods=["GET", "POST"])
    @require_admin
    def admin_settings():
        if request.method == "POST":
            toggle_keys  = ["cod_enabled", "online_payment_enabled", "free_shipping_enabled", "free_shipping_all"]
            text_keys    = ["razorpay_key_id", "razorpay_key_secret"]
            numeric_keys = ["shipping_fee", "free_shipping_threshold"]
            try:
                for key in toggle_keys:
                    value = "true" if request.form.get(key) == "on" else "false"
                    db.execute(
                        "INSERT INTO store_settings (key, value) VALUES (?,?) "
                        "ON CONFLICT (key) DO UPDATE SET value=?, updated_at=NOW()",
                        [key, value, value]
                    )
                for key in text_keys:
                    value = request.form.get(key, "").strip()
                    db.execute(
                        "INSERT INTO store_settings (key, value) VALUES (?,?) "
                        "ON CONFLICT (key) DO UPDATE SET value=?, updated_at=NOW()",
                        [key, value, value]
                    )
                for key in numeric_keys:
                    raw = request.form.get(key, "").strip()
                    try:
                        value = str(max(0, float(raw))) if raw else ("49" if key == "shipping_fee" else "599")
                    except ValueError:
                        value = "49" if key == "shipping_fee" else "599"
                    db.execute(
                        "INSERT INTO store_settings (key, value) VALUES (?,?) "
                        "ON CONFLICT (key) DO UPDATE SET value=?, updated_at=NOW()",
                        [key, value, value]
                    )
                get_cached_store_settings.cache_clear()
                flash("Settings saved successfully.", "success")
            except Exception as e:
                flash(f"Error saving settings: {e}", "error")
            return redirect(url_for("admin_settings"))
        return render_template("admin/settings.html", settings=get_cached_store_settings())

    # ── Coupons ────────────────────────────────────────────────────────────────
    @app.route("/admin/coupons")
    @require_admin
    def admin_coupons():
        try:
            coupons = db.query("""
                SELECT c.*, (SELECT COUNT(*) FROM coupon_usages WHERE coupon_id = c.id) as used_count
                FROM coupons c 
                ORDER BY c.created_at DESC
            """)
        except Exception as e:
            coupons = []
            flash(f"Error loading coupons: {e}", "error")
        return render_template("admin/coupons.html", coupons=coupons)

    @app.route("/admin/coupons/new", methods=["GET", "POST"])
    @require_admin
    def admin_coupon_new():
        if request.method == "POST":
            f = request.form
            try:
                db.execute(
                    """INSERT INTO coupons 
                       (id, code, type, value, min_order_amount, usage_limit, 
                        usage_limit_per_user, max_discount, expires_at, is_active)
                       VALUES (?,?,?,?,?,?,?,?,?,CAST(? AS INTEGER))""",
                    [
                        str(uuid.uuid4()), f.get("code").upper(), f.get("type"),
                        float(f.get("value") or 0), float(f.get("min_order_amount") or 0),
                        int(f.get("usage_limit")) if f.get("usage_limit") else None,
                        int(f.get("usage_limit_per_user") or 1),
                        float(f.get("max_discount")) if f.get("max_discount") else None,
                        f.get("expires_at") or None,
                        1 if f.get("is_active") == "on" else 0
                    ]
                )
                flash("Coupon created successfully.", "success")
                return redirect(url_for("admin_coupons"))
            except Exception as e:
                flash(f"Error creating coupon: {e}", "error")
        return render_template("admin/coupon_form.html", coupon=None)

    @app.route("/admin/coupons/<coupon_id>/delete", methods=["POST"])
    @require_admin
    def admin_coupon_delete(coupon_id):
        try:
            # Remove usage records first to avoid orphaned rows
            db.execute("DELETE FROM coupon_usages WHERE coupon_id=?", [coupon_id])
            db.execute("DELETE FROM coupons WHERE id=?", [coupon_id])
            flash("Coupon deleted successfully.", "success")
        except Exception as e:
            flash(f"Error deleting coupon: {e}", "error")
        return redirect(url_for("admin_coupons"))

    # ── CSV Import ─────────────────────────────────────────────────────────────

    @app.route("/admin/import", methods=["GET", "POST"])
    @require_admin
    def admin_import():
        results = None
        if request.method == "POST":
            csv_file = request.files.get("csv_file")
            if not csv_file or csv_file.filename == "":
                flash("Please select a CSV file.", "error")
                return render_template("admin/import.html", results=None)
            try:
                content  = csv_file.read().decode("utf-8-sig")
                reader   = csv.DictReader(io.StringIO(content))
                imported = skipped = 0
                errors   = []
                for i, row in enumerate(reader, 1):
                    try:
                        name  = (row.get("post_title") or row.get("name") or "").strip()
                        sku   = (row.get("sku") or "").strip() or generate_unique_product_sku(name)
                        price = float(row.get("regular_price") or row.get("price") or 0)
                        sale  = float(row.get("sale_price") or 0) or None
                        stock = int(row.get("stock") or row.get("stock_quantity") or 0)
                        desc  = (row.get("description") or row.get("post_content") or "").strip()
                        short = (row.get("short_description") or row.get("post_excerpt") or "").strip()
                        img   = (row.get("images") or row.get("image") or "").strip().split("|")[0].strip()
                        slug  = (row.get("post_name") or row.get("slug") or name.lower().replace(" ", "-")).strip()
                        if not name:
                            skipped += 1
                            continue
                        if db.query_one("SELECT id FROM products WHERE sku=? OR slug=?", [sku, slug]):
                            skipped += 1
                            continue
                        pid = str(uuid.uuid4())
                        db.execute(
                            """INSERT INTO products (id, name, slug, sku, price, sale_price,
                               stock_quantity, stock_status, description, short_description, is_active)
                               VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                            [pid, name, slug, sku, price, sale, stock,
                             "in_stock" if stock > 0 else "out_of_stock", desc, short]
                        )
                        result = {"id": pid}
                        if result and img:
                            mid = str(uuid.uuid4())
                            db.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, img])
                            db.execute(
                                "INSERT INTO product_images (id, product_id, media_id, is_primary) VALUES (?,?,?,1)",
                                [str(uuid.uuid4()), result["id"], mid]
                            )
                        imported += 1
                    except Exception as row_err:
                        errors.append(f"Row {i}: {row_err}")
                results = {"imported": imported, "skipped": skipped, "errors": errors}
                if imported > 0:
                    get_products.cache_clear()
                    get_homepage_products.cache_clear()
                    get_product_detail.cache_clear()
                flash(f"Import complete: {imported} imported, {skipped} skipped.", "success")
            except Exception as e:
                flash(f"CSV parse error: {e}", "error")
        return render_template("admin/import.html", results=results)
