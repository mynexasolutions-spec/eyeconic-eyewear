import csv
import io
import uuid
import itertools
from functools import wraps
from flask import render_template, request, redirect, url_for, flash, abort, session
import db
from helpers import slugify, get_cached_store_settings, get_unique_slug, handle_upload
from queries import get_products, get_categories, get_brands, get_admin_stats, get_featured_categories, get_trending_shapes, PRODUCTS_SELECT, get_product_detail, get_homepage_products, get_home_sections, get_home_sections_admin, get_home_section, get_home_product_picks, get_home_product_picks_admin, ensure_builtin_home_sections, HOME_BUILTIN_CAROUSEL_DEFAULTS
import shipping


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


def generate_variations(product_id, executor=None):
    """
    For variable products, we no longer pre-generate all cartesian-product
    variation rows.  The storefront product page already loads the available
    attribute values directly from `product_attribute_values`, and the cart
    stores selected options as text — so thousands of combination rows in
    `product_variations` / `variation_attribute_values` are not required.

    Instead, we create ONE placeholder variation per product so that the
    admin 'Manage Variations' panel still works for editing, and the product
    is recognized as having variations.

    `executor` may be the `db` module or an open `db.transaction()` handle
    (`tx`) so this can participate in a caller's transaction.
    """
    ex = executor or db
    existing = ex.query_one(
        "SELECT id FROM product_variations WHERE product_id = ? LIMIT 1",
        [product_id],
    )
    if existing:
        return  # already has at least one variation row

    product = ex.query_one(
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
    ex.execute(
        "INSERT INTO product_variations (id, product_id, sku, price, stock_quantity) "
        "VALUES (?,?,?,?,?)",
        [var_id, product_id, var_sku, base_price, base_stock],
    )

def get_attributes_with_options():
    """All attributes plus their values, loaded in 2 queries instead of 1 + N."""
    attributes = db.query("SELECT * FROM attributes ORDER BY name ASC")
    if not attributes:
        return attributes
    attr_ids     = [a["id"] for a in attributes]
    placeholders = ",".join(["?"] * len(attr_ids))
    all_values   = db.query(
        f"SELECT * FROM attribute_values WHERE attribute_id IN ({placeholders}) ORDER BY value ASC",
        attr_ids,
    )
    values_by_attr = {}
    for v in all_values:
        values_by_attr.setdefault(str(v["attribute_id"]), []).append(v)
    for attr in attributes:
        attr["options"] = values_by_attr.get(str(attr["id"]), [])
    return attributes


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
        all_attributes = get_attributes_with_options()
        
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

                # Uploads happen outside the transaction (they're network calls to Cloudinary)
                primary_file = request.files.get("primary_image")
                primary_url = handle_upload(primary_file) if primary_file and primary_file.filename else None

                gallery_files = request.files.getlist("gallery_images")
                gallery_urls  = [handle_upload(gfile) for gfile in gallery_files if gfile and gfile.filename]

                attr_ids     = request.form.getlist("attribute_ids")
                val_ids      = request.form.getlist("attribute_value_ids")
                is_variable  = f.get("type") == "variable"

                with db.transaction() as tx:
                    product_id = tx.execute_returning(
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

                    # Primary Image
                    if primary_url:
                        mid = str(uuid.uuid4())
                        tx.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, primary_url])
                        tx.execute("INSERT INTO product_images (id, product_id, media_id, is_primary, display_order) VALUES (?,?,?,1,0)", [str(uuid.uuid4()), product_id, mid])

                    # Gallery Images
                    for i, url in enumerate(gallery_urls):
                        mid = str(uuid.uuid4())
                        tx.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, url])
                        tx.execute("INSERT INTO product_images (id, product_id, media_id, is_primary, display_order) VALUES (?,?,?,0,?)", [str(uuid.uuid4()), product_id, mid, i+1])

                    if attr_ids:
                        values_sql = ",".join(["(?,?,?)"] * len(attr_ids))
                        params = []
                        for attr_id in attr_ids:
                            params.extend([str(uuid.uuid4()), product_id, attr_id])
                        tx.execute(
                            f"INSERT INTO product_attributes (id, product_id, attribute_id) VALUES {values_sql}",
                            params
                        )

                    if val_ids:
                        values_sql = ",".join(["(?,?,?)"] * len(val_ids))
                        params = []
                        for val_id in val_ids:
                            params.extend([str(uuid.uuid4()), product_id, val_id])
                        tx.execute(
                            f"INSERT INTO product_attribute_values (id, product_id, attribute_value_id) VALUES {values_sql}",
                            params
                        )
                    if is_variable:
                        generate_variations(product_id, executor=tx)

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
        all_attributes = get_attributes_with_options()
        
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

                # Uploads happen outside the transaction (they're network calls to Cloudinary)
                primary_file = request.files.get("primary_image")
                primary_url = handle_upload(primary_file) if primary_file and primary_file.filename else None

                gallery_files = request.files.getlist("gallery_images")
                gallery_urls  = [handle_upload(gfile) for gfile in gallery_files if gfile and gfile.filename]

                attr_ids = request.form.getlist("attribute_ids")
                val_ids  = request.form.getlist("attribute_value_ids")

                with db.transaction() as tx:
                    tx.execute(
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

                    # Primary Image
                    if primary_url:
                        mid = str(uuid.uuid4())
                        tx.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, primary_url])
                        tx.execute("DELETE FROM product_images WHERE product_id=? AND is_primary=1", [product_id])
                        tx.execute("INSERT INTO product_images (id, product_id, media_id, is_primary, display_order) VALUES (?,?,?,1,0)", [str(uuid.uuid4()), product_id, mid])

                    # New Gallery Images
                    for url in gallery_urls:
                        mid = str(uuid.uuid4())
                        tx.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, url])
                        tx.execute("INSERT INTO product_images (id, product_id, media_id, is_primary) VALUES (?,?,?,0)", [str(uuid.uuid4()), product_id, mid])

                    tx.execute("DELETE FROM product_attributes WHERE product_id=?", [product_id])
                    if attr_ids:
                        values_sql = ",".join(["(?,?,?)"] * len(attr_ids))
                        params = []
                        for aid in attr_ids:
                            params.extend([str(uuid.uuid4()), product_id, aid])
                        tx.execute(f"INSERT INTO product_attributes (id, product_id, attribute_id) VALUES {values_sql}", params)

                    tx.execute("DELETE FROM product_attribute_values WHERE product_id=?", [product_id])
                    if val_ids:
                        values_sql = ",".join(["(?,?,?)"] * len(val_ids))
                        params = []
                        for vid in val_ids:
                            params.extend([str(uuid.uuid4()), product_id, vid])
                        tx.execute(f"INSERT INTO product_attribute_values (id, product_id, attribute_value_id) VALUES {values_sql}", params)

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

    @app.route("/admin/products/<product_id>/images/<image_id>/delete", methods=["POST"])
    @require_admin
    def admin_product_image_delete(product_id, image_id):
        try:
            with db.transaction() as tx:
                deleted = tx.query_one(
                    "SELECT is_primary FROM product_images WHERE id=? AND product_id=?",
                    [image_id, product_id]
                )
                tx.execute(
                    "DELETE FROM product_images WHERE id=? AND product_id=?",
                    [image_id, product_id]
                )
                # If the primary image was removed, promote another remaining
                # image so the product always has a primary while any exist.
                if deleted and deleted.get("is_primary"):
                    next_image = tx.query_one(
                        "SELECT id FROM product_images WHERE product_id=? ORDER BY display_order ASC LIMIT 1",
                        [product_id]
                    )
                    if next_image:
                        tx.execute("UPDATE product_images SET is_primary=1 WHERE id=?", [next_image["id"]])
            get_products.cache_clear()
            get_homepage_products.cache_clear()
            get_product_detail.cache_clear()
            flash("Image deleted.", "success")
        except Exception as e:
            flash(f"Error deleting image: {e}", "error")
        return redirect(url_for("admin_product_edit", product_id=product_id))

    @app.route("/admin/products/<product_id>/delete", methods=["POST"])
    @require_admin
    def admin_product_delete(product_id):
        try:
            with db.transaction() as tx:
                # Preserve order history: unlink rather than delete order_items
                tx.execute("UPDATE order_items SET product_id = NULL WHERE product_id = ?", [product_id])
                tx.execute("DELETE FROM product_reviews WHERE product_id = ?", [product_id])
                tx.execute("DELETE FROM product_attribute_values WHERE product_id = ?", [product_id])
                tx.execute("DELETE FROM product_attributes WHERE product_id = ?", [product_id])
                tx.execute(
                    "DELETE FROM variation_attribute_values WHERE variation_id IN "
                    "(SELECT id FROM product_variations WHERE product_id = ?)", [product_id]
                )
                tx.execute("DELETE FROM product_variations WHERE product_id = ?", [product_id])
                tx.execute("DELETE FROM product_images WHERE product_id = ?", [product_id])
                tx.execute("DELETE FROM products WHERE id = ?", [product_id])
            get_products.cache_clear()
            get_homepage_products.cache_clear()
            get_product_detail.cache_clear()
            flash("Product deleted permanently.", "success")
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
            with db.transaction() as tx:
                tx.execute("UPDATE products SET category_id = NULL WHERE category_id = ?", [cat_id])
                tx.execute("UPDATE categories SET parent_id = NULL WHERE parent_id = ?", [cat_id])
                tx.execute("DELETE FROM categories WHERE id=?", [cat_id])
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
        import math
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        per_page = 20
        offset   = (page - 1) * per_page
        try:
            brands = db.query(
                """
                SELECT b.*, COUNT(p.id) AS product_count
                FROM brands b
                LEFT JOIN products p ON p.brand_id = b.id
                GROUP BY b.id
                ORDER BY b.name ASC
                LIMIT ? OFFSET ?
                """,
                [per_page, offset]
            )
            total       = (db.query_one("SELECT COUNT(*) AS cnt FROM brands") or {}).get("cnt", 0)
            total_pages = max(1, math.ceil(total / per_page))
        except Exception as e:
            brands, total, total_pages = [], 0, 1
            flash(f"Error: {e}", "error")
        return render_template(
            "admin/brands.html", brands=brands, total=total, total_pages=total_pages, page=page
        )

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
            with db.transaction() as tx:
                tx.execute("UPDATE products SET brand_id = NULL WHERE brand_id = ?", [brand_id])
                tx.execute("DELETE FROM brands WHERE id=?", [brand_id])
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
                """SELECT oi.*, p.name AS product_name, p.sku, m.file_url AS image_url
                   FROM order_items oi
                   LEFT JOIN products p ON p.id = oi.product_id
                   LEFT JOIN product_images pi ON pi.product_id = oi.product_id AND pi.is_primary = 1
                   LEFT JOIN media m ON m.id = pi.media_id
                   WHERE oi.order_id=?""",
                [order_id]
            )
            shipping_address = {}
            if order.get("shipping_address_json"):
                try:
                    import json
                    shipping_address = json.loads(order["shipping_address_json"])
                except Exception:
                    pass
        except Exception as e:
            flash(f"Error: {e}", "error")
            return redirect(url_for("admin_orders"))

        tracking = None
        if order.get("awb_number"):
            if not order.get("shipment_tracking_url"):
                fallback_url = shipping.tracking_url_for(order["awb_number"])
                try:
                    db.execute("UPDATE orders SET shipment_tracking_url=? WHERE id=?", [fallback_url, order_id])
                    order["shipment_tracking_url"] = fallback_url
                except Exception:
                    pass
            try:
                ok, _msg, tracking = shipping.track_shipment(order["awb_number"])
                if not ok:
                    tracking = None
            except Exception:
                tracking = None

            if tracking and order.get("status") not in ("delivered", "cancelled", "refunded"):
                inferred = shipping.infer_order_status(tracking.get("current_status"))
                status_rank = {"pending": 0, "processing": 1, "shipped": 2, "delivered": 3}
                if inferred and status_rank.get(inferred, 0) > status_rank.get(order.get("status"), 0):
                    try:
                        db.execute("UPDATE orders SET status=? WHERE id=?", [inferred, order_id])
                        order["status"] = inferred
                        flash(f"Order status auto-updated to '{inferred}' based on courier tracking.", "success")
                    except Exception:
                        pass

        default_weight = get_cached_store_settings().get("ithink_default_weight_kg", "").strip() or "0.5"
        return render_template(
            "admin/order_detail.html", order=order, items=items, shipping_address=shipping_address,
            tracking=tracking, shipping_configured=shipping.is_configured(), default_weight=default_weight
        )

    @app.route("/admin/orders/<order_id>/ship", methods=["POST"])
    @require_admin
    def admin_order_create_shipment(order_id):
        order = db.query_one(
            """SELECT o.*, (u.first_name || ' ' || u.last_name) AS customer_name, u.email AS customer_email
               FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE o.id=?""",
            [order_id]
        )
        if not order:
            abort(404)
        items = db.query(
            "SELECT oi.*, p.sku FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id WHERE oi.order_id=?",
            [order_id]
        )
        shipping_address = {}
        if order.get("shipping_address_json"):
            try:
                import json
                shipping_address = json.loads(order["shipping_address_json"])
            except Exception:
                pass
        weight_raw = request.form.get("weight", "").strip()
        try:
            weight = weight_raw if weight_raw and float(weight_raw) > 0 else None
        except ValueError:
            weight = None
        ok, message, info = shipping.create_shipment(order, items, shipping_address, weight=weight)
        if ok:
            db.execute(
                "UPDATE orders SET awb_number=?, shipment_courier=?, shipment_tracking_url=? WHERE id=?",
                [info["awb_number"], info["courier"], info["tracking_url"], order_id]
            )
            flash(f"Shipment created — AWB {info['awb_number']} ({info['courier']}).", "success")
        else:
            flash(f"Could not create shipment: {message}", "error")
        return redirect(url_for("admin_order_detail", order_id=order_id))

    @app.route("/admin/orders/<order_id>/cancel-shipment", methods=["POST"])
    @require_admin
    def admin_order_cancel_shipment(order_id):
        order = db.query_one("SELECT awb_number FROM orders WHERE id=?", [order_id])
        if not order or not order.get("awb_number"):
            flash("This order has no shipment to cancel.", "error")
            return redirect(url_for("admin_order_detail", order_id=order_id))
        ok, message, _info = shipping.cancel_shipment(order["awb_number"])
        # Always clear the local record on an explicit cancel — even if iThink couldn't
        # confirm it (e.g. a stale/test AWB that no longer exists on their side), the
        # admin should still be able to detach it here and create a fresh shipment.
        db.execute(
            "UPDATE orders SET awb_number='', shipment_courier='', shipment_tracking_url='' WHERE id=?",
            [order_id]
        )
        if ok:
            flash("Shipment cancelled.", "success")
        else:
            flash(f"Shipment removed from this order, but iThink Logistics did not confirm cancellation: {message}", "error")
        return redirect(url_for("admin_order_detail", order_id=order_id))

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
        import math
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        per_page = 20
        offset   = (page - 1) * per_page
        try:
            customers = db.query(
                "SELECT * FROM users WHERE role='customer' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [per_page, offset]
            )
            total       = (db.query_one("SELECT COUNT(*) AS cnt FROM users WHERE role='customer'") or {}).get("cnt", 0)
            total_pages = max(1, math.ceil(total / per_page))
        except Exception as e:
            customers, total, total_pages = [], 0, 1
            flash(f"Error: {e}", "error")
        return render_template(
            "admin/customers.html", customers=customers, total=total, total_pages=total_pages, page=page
        )

    @app.route("/admin/subscribers")
    @require_admin
    def admin_subscribers():
        import math
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        per_page = 20
        offset   = (page - 1) * per_page
        try:
            subscribers = db.query(
                "SELECT * FROM newsletter_subscribers ORDER BY subscribed_at DESC LIMIT ? OFFSET ?",
                [per_page, offset]
            )
            total       = (db.query_one("SELECT COUNT(*) AS cnt FROM newsletter_subscribers") or {}).get("cnt", 0)
            total_pages = max(1, math.ceil(total / per_page))
        except Exception as e:
            subscribers, total, total_pages = [], 0, 1
            flash(f"Error loading subscribers: {e}", "error")
        return render_template(
            "admin/subscribers.html", subscribers=subscribers, total=total, total_pages=total_pages, page=page
        )

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
            with db.transaction() as tx:
                tx.execute(
                    "DELETE FROM product_attribute_values WHERE attribute_value_id IN "
                    "(SELECT id FROM attribute_values WHERE attribute_id = ?)", [attr_id]
                )
                tx.execute(
                    "DELETE FROM variation_attribute_values WHERE attribute_value_id IN "
                    "(SELECT id FROM attribute_values WHERE attribute_id = ?)", [attr_id]
                )
                tx.execute("DELETE FROM attribute_values WHERE attribute_id = ?", [attr_id])
                tx.execute("DELETE FROM product_attributes WHERE attribute_id = ?", [attr_id])
                tx.execute("DELETE FROM attributes WHERE id = ?", [attr_id])
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
            updates = []
            for v in values:
                v_id = v["id"]
                new_value = (f.get(f"value_{v_id}") or "").strip()
                if new_value:
                    updates.append((v_id, new_value))
            if updates:
                case_sql = " ".join(["WHEN ? THEN ?"] * len(updates))
                ids = [v_id for v_id, _ in updates]
                case_params = [part for pair in updates for part in pair]
                placeholders = ",".join(["?"] * len(ids))
                db.execute(
                    f"UPDATE attribute_values SET value = CASE id {case_sql} END WHERE id IN ({placeholders})",
                    case_params + ids
                )
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
            with db.transaction() as tx:
                tx.execute("DELETE FROM product_attribute_values WHERE attribute_value_id = ?", [val_id])
                tx.execute("DELETE FROM variation_attribute_values WHERE attribute_value_id = ?", [val_id])
                tx.execute("DELETE FROM attribute_values WHERE id = ?", [val_id])
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
        if linked_attributes:
            attr_ids     = [a["id"] for a in linked_attributes]
            placeholders = ",".join(["?"] * len(attr_ids))

            selected_rows = db.query(
                f"""SELECT av.* FROM attribute_values av
                    JOIN product_attribute_values pav ON pav.attribute_value_id = av.id
                    WHERE av.attribute_id IN ({placeholders}) AND pav.product_id = ? ORDER BY av.value ASC""",
                attr_ids + [product_id]
            )
            selected_by_attr = {}
            for row in selected_rows:
                selected_by_attr.setdefault(str(row["attribute_id"]), []).append(row)

            fallback_ids = [a["id"] for a in linked_attributes if not selected_by_attr.get(str(a["id"]))]
            fallback_by_attr = {}
            if fallback_ids:
                fb_placeholders = ",".join(["?"] * len(fallback_ids))
                fallback_rows = db.query(
                    f"SELECT * FROM attribute_values WHERE attribute_id IN ({fb_placeholders}) ORDER BY value ASC",
                    fallback_ids
                )
                for row in fallback_rows:
                    fallback_by_attr.setdefault(str(row["attribute_id"]), []).append(row)

            for attr in linked_attributes:
                aid = str(attr["id"])
                attr["options"] = selected_by_attr.get(aid) or fallback_by_attr.get(aid, [])
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
            attr_value_ids = [val_id for key, val_id in f.items() if key.startswith("attr_") and val_id]
            with db.transaction() as tx:
                tx.execute(
                    "INSERT INTO product_variations (id, product_id, sku, price, sale_price, stock_quantity) "
                    "VALUES (?,?,?,?,?,?)",
                    [var_id, product_id, variation_sku,
                     float(product.get("sale_price") or product.get("price") or 0), None,
                     int(product.get("stock_quantity") or 0)]
                )
                for val_id in attr_value_ids:
                    tx.execute(
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
            with db.transaction() as tx:
                tx.execute("DELETE FROM variation_attribute_values WHERE variation_id = ?", [var_id])
                tx.execute("DELETE FROM product_variations WHERE id = ?", [var_id])
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

            if var_ids:
                final_skus = {}
                for vid in var_ids:
                    sku = (request.form.get(f"sku_{vid}") or "").strip()
                    final_skus[vid] = sku or generate_unique_variation_sku(base_sku, exclude_id=vid)

                with db.transaction() as tx:
                    # price/sale_price/stock are identical for every variation of this
                    # product, so a single UPDATE covers all rows at once.
                    tx.execute(
                        "UPDATE product_variations SET price=?, sale_price=?, stock_quantity=? WHERE product_id=?",
                        [base_price, None, base_stock, product_id]
                    )
                    case_sql = " ".join(["WHEN ? THEN ?"] * len(var_ids))
                    case_params = [part for vid in var_ids for part in (vid, final_skus[vid])]
                    placeholders = ",".join(["?"] * len(var_ids))
                    tx.execute(
                        f"UPDATE product_variations SET sku = CASE id {case_sql} END WHERE id IN ({placeholders})",
                        case_params + var_ids
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
            if types:
                type_ids     = [t["id"] for t in types]
                placeholders = ",".join(["?"] * len(type_ids))
                all_options  = db.query(
                    f"SELECT * FROM lens_options WHERE lens_type_id IN ({placeholders}) "
                    f"ORDER BY display_order ASC, name ASC",
                    type_ids,
                )
                options_by_type = {}
                for opt in all_options:
                    options_by_type.setdefault(str(opt["lens_type_id"]), []).append(opt)
                for t in types:
                    t["options"] = options_by_type.get(str(t["id"]), [])
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
        import math
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        per_page = 20
        offset   = (page - 1) * per_page
        try:
            reviews = db.query("""
                SELECT r.*, r.body AS comment, p.name AS product_name, p.id AS product_id,
                       (u.first_name || ' ' || u.last_name) AS reviewer_name
                FROM product_reviews r
                LEFT JOIN products p ON p.id = r.product_id
                LEFT JOIN users u ON u.id = r.user_id
                ORDER BY r.created_at DESC
                LIMIT ? OFFSET ?
            """, [per_page, offset])
            total       = (db.query_one("SELECT COUNT(*) AS cnt FROM product_reviews") or {}).get("cnt", 0)
            total_pages = max(1, math.ceil(total / per_page))
        except Exception as e:
            reviews, total, total_pages = [], 0, 1
            flash(f"Error loading reviews: {e}", "error")
        return render_template(
            "admin/reviews.html", reviews=reviews, total=total, total_pages=total_pages, page=page
        )

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

    # ── Home Page Customization ───────────────────────────────────────────────

    HOME_SECTION_TYPES = ["hero", "category", "carousel", "policy", "banner", "stat", "testimonial", "instagram"]

    @app.route("/admin/homepage")
    @require_admin
    def admin_homepage():
        ensure_builtin_home_sections()
        sections = {t: get_home_sections_admin(t) for t in HOME_SECTION_TYPES}
        shape_section = get_home_section("shape")
        settings = get_cached_store_settings()
        section_visible = {t: settings.get(f"home_visible_{t}", "true") != "false" for t in HOME_SECTION_TYPES}
        for c in sections["carousel"]:
            row = db.query_one("SELECT COUNT(*) AS cnt FROM home_product_picks WHERE section_key=?", [c["id"]])
            c["product_count"] = row.get("cnt", 0) if row else 0
            c["is_builtin"] = c["id"] in HOME_BUILTIN_CAROUSEL_DEFAULTS
        for p in sections["policy"]:
            p["is_builtin"] = True
        return render_template(
            "admin/homepage.html", sections=sections, shape_section=shape_section,
            section_visible=section_visible,
        )

    @app.route("/admin/homepage/section/<stype>/toggle", methods=["POST"])
    @require_admin
    def admin_homepage_section_toggle(stype):
        if stype not in HOME_SECTION_TYPES:
            abort(404)
        try:
            settings = get_cached_store_settings()
            currently_visible = settings.get(f"home_visible_{stype}", "true") != "false"
            new_value = "false" if currently_visible else "true"
            db.execute(
                "INSERT INTO store_settings (key, value) VALUES (?,?) "
                "ON CONFLICT (key) DO UPDATE SET value=?, updated_at=NOW()",
                [f"home_visible_{stype}", new_value, new_value]
            )
            get_cached_store_settings.cache_clear()
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage"))

    @app.route("/admin/homepage/<section_type>/new", methods=["GET", "POST"])
    @require_admin
    def admin_homepage_new(section_type):
        if section_type not in HOME_SECTION_TYPES:
            abort(404)
        if request.method == "POST":
            f = request.form
            image_url = handle_upload(request.files.get("image_file")) or f.get("image_url", "").strip() or None
            try:
                row = db.query_one(
                    "SELECT COALESCE(MAX(sort_order), -1) AS m FROM home_sections WHERE section_type=?",
                    [section_type]
                )
                next_order = (row["m"] if row else -1) + 1
                db.execute(
                    """INSERT INTO home_sections
                       (id, section_type, title, subtitle, body, badge_text, image_url, link_url,
                        cta_text, cta_link, cta2_text, cta2_link, rating, sort_order, is_active)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [str(uuid.uuid4()), section_type,
                     f.get("title", "").strip(), f.get("subtitle", "").strip(), f.get("body", "").strip(),
                     f.get("badge_text", "").strip(), image_url, f.get("link_url", "").strip(),
                     f.get("cta_text", "").strip(), f.get("cta_link", "").strip(),
                     f.get("cta2_text", "").strip(), f.get("cta2_link", "").strip(),
                     int(f.get("rating") or 5), next_order,
                     1 if f.get("is_active") == "on" else 0]
                )
                get_home_sections.cache_clear()
                flash("Homepage item created.", "success")
                return redirect(url_for("admin_homepage"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/homepage_form.html", item=None, section_type=section_type)

    @app.route("/admin/homepage/<item_id>/edit", methods=["GET", "POST"])
    @require_admin
    def admin_homepage_edit(item_id):
        item = get_home_section(item_id)
        if not item:
            abort(404)
        if request.method == "POST":
            f = request.form
            image_url = handle_upload(request.files.get("image_file")) or f.get("image_url", "").strip() or item["image_url"]
            try:
                db.execute(
                    """UPDATE home_sections SET title=?, subtitle=?, body=?, badge_text=?, image_url=?, link_url=?,
                       cta_text=?, cta_link=?, cta2_text=?, cta2_link=?, rating=?, is_active=? WHERE id=?""",
                    [f.get("title", "").strip(), f.get("subtitle", "").strip(), f.get("body", "").strip(),
                     f.get("badge_text", "").strip(), image_url, f.get("link_url", "").strip(),
                     f.get("cta_text", "").strip(), f.get("cta_link", "").strip(),
                     f.get("cta2_text", "").strip(), f.get("cta2_link", "").strip(),
                     int(f.get("rating") or 5), 1 if f.get("is_active") == "on" else 0, item_id]
                )
                get_home_sections.cache_clear()
                flash("Homepage item updated.", "success")
                return redirect(url_for("admin_homepage"))
            except Exception as e:
                flash(f"Error: {e}", "error")
        return render_template("admin/homepage_form.html", item=item, section_type=item["section_type"])

    @app.route("/admin/homepage/<item_id>/delete", methods=["POST"])
    @require_admin
    def admin_homepage_delete(item_id):
        try:
            with db.transaction() as tx:
                tx.execute("DELETE FROM home_sections WHERE id=?", [item_id])
                tx.execute("DELETE FROM home_product_picks WHERE section_key=?", [item_id])
            get_home_sections.cache_clear()
            get_home_product_picks.cache_clear()
            flash("Homepage item deleted.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage"))

    @app.route("/admin/homepage/<item_id>/toggle", methods=["POST"])
    @require_admin
    def admin_homepage_toggle(item_id):
        try:
            db.execute(
                "UPDATE home_sections SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
                [item_id]
            )
            get_home_sections.cache_clear()
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage"))

    @app.route("/admin/homepage/<item_id>/move/<direction>", methods=["POST"])
    @require_admin
    def admin_homepage_move(item_id, direction):
        if direction not in ("up", "down"):
            abort(404)
        item = get_home_section(item_id)
        if not item:
            abort(404)
        try:
            if direction == "up":
                neighbor = db.query_one(
                    "SELECT * FROM home_sections WHERE section_type=? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
                    [item["section_type"], item["sort_order"]]
                )
            else:
                neighbor = db.query_one(
                    "SELECT * FROM home_sections WHERE section_type=? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                    [item["section_type"], item["sort_order"]]
                )
            if neighbor:
                with db.transaction() as tx:
                    tx.execute("UPDATE home_sections SET sort_order=? WHERE id=?", [neighbor["sort_order"], item["id"]])
                    tx.execute("UPDATE home_sections SET sort_order=? WHERE id=?", [item["sort_order"], neighbor["id"]])
                get_home_sections.cache_clear()
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage"))

    # ── Home Page: section visibility + curated product picks ──────────────────

    HOME_PRODUCT_SECTIONS = {
        "bestsellers": "Best Sellers",
        "men":         "Men's Eyewear",
        "women":       "Women's Eyewear",
        "kids":        "Kids' Eyewear",
        "accessories": "Premium Accessories",
        "sunglasses":  "Sunglasses",
        "eyeglasses":  "Eyeglasses",
    }
    # "shape" used to live in a separate visibility checklist. It's now a real
    # row in home_sections (id="shape") like everything else, managed straight
    # from its own card on the Home Page screen (Edit + Live/Hidden toggle).

    def _resolve_product_section(section_key):
        """A product carousel is either one of the 7 built-in sections, or a custom
        carousel (a home_sections row of type 'carousel', keyed by its own id).
        Returns (label, is_builtin) or (None, None) if section_key is invalid."""
        if section_key in HOME_PRODUCT_SECTIONS:
            return HOME_PRODUCT_SECTIONS[section_key], True
        row = db.query_one(
            "SELECT title FROM home_sections WHERE id=? AND section_type='carousel'",
            [section_key]
        )
        if row:
            return row["title"], False
        return None, None

    @app.route("/admin/homepage/products/<section_key>")
    @require_admin
    def admin_homepage_products(section_key):
        label, is_builtin = _resolve_product_section(section_key)
        if label is None:
            abort(404)
        q = request.args.get("q", "").strip()
        picks = get_home_product_picks_admin(section_key)
        picked_ids = {p["product_id"] for p in picks}
        results = []
        if q:
            results = [r for r in get_products(search=q, limit=20) if r["id"] not in picked_ids]
        return render_template(
            "admin/homepage_products.html",
            section_key=section_key, section_label=label, is_builtin=is_builtin,
            picks=picks, results=results, q=q
        )

    @app.route("/admin/homepage/products/<section_key>/add", methods=["POST"])
    @require_admin
    def admin_homepage_products_add(section_key):
        label, _ = _resolve_product_section(section_key)
        if label is None:
            abort(404)
        product_id = request.form.get("product_id")
        q = request.form.get("q", "")
        try:
            if product_id:
                existing = db.query_one(
                    "SELECT id FROM home_product_picks WHERE section_key=? AND product_id=?",
                    [section_key, product_id]
                )
                if not existing:
                    row = db.query_one(
                        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM home_product_picks WHERE section_key=?",
                        [section_key]
                    )
                    next_order = (row["m"] if row else -1) + 1
                    db.execute(
                        "INSERT INTO home_product_picks (id, section_key, product_id, sort_order) VALUES (?,?,?,?)",
                        [str(uuid.uuid4()), section_key, product_id, next_order]
                    )
                    get_home_product_picks.cache_clear()
                    flash("Product added.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage_products", section_key=section_key, q=q))

    @app.route("/admin/homepage/products/pick/<pick_id>/remove", methods=["POST"])
    @require_admin
    def admin_homepage_products_remove(pick_id):
        pick = db.query_one("SELECT * FROM home_product_picks WHERE id=?", [pick_id])
        if not pick:
            abort(404)
        try:
            db.execute("DELETE FROM home_product_picks WHERE id=?", [pick_id])
            get_home_product_picks.cache_clear()
            flash("Product removed.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage_products", section_key=pick["section_key"]))

    @app.route("/admin/homepage/products/pick/<pick_id>/move/<direction>", methods=["POST"])
    @require_admin
    def admin_homepage_products_move(pick_id, direction):
        if direction not in ("up", "down"):
            abort(404)
        pick = db.query_one("SELECT * FROM home_product_picks WHERE id=?", [pick_id])
        if not pick:
            abort(404)
        try:
            if direction == "up":
                neighbor = db.query_one(
                    "SELECT * FROM home_product_picks WHERE section_key=? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
                    [pick["section_key"], pick["sort_order"]]
                )
            else:
                neighbor = db.query_one(
                    "SELECT * FROM home_product_picks WHERE section_key=? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                    [pick["section_key"], pick["sort_order"]]
                )
            if neighbor:
                with db.transaction() as tx:
                    tx.execute("UPDATE home_product_picks SET sort_order=? WHERE id=?", [neighbor["sort_order"], pick["id"]])
                    tx.execute("UPDATE home_product_picks SET sort_order=? WHERE id=?", [pick["sort_order"], neighbor["id"]])
                get_home_product_picks.cache_clear()
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage_products", section_key=pick["section_key"]))

    @app.route("/admin/homepage/products/<section_key>/reset", methods=["POST"])
    @require_admin
    def admin_homepage_products_reset(section_key):
        label, _ = _resolve_product_section(section_key)
        if label is None:
            abort(404)
        try:
            db.execute("DELETE FROM home_product_picks WHERE section_key=?", [section_key])
            get_home_product_picks.cache_clear()
            flash("Products cleared.", "success")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("admin_homepage_products", section_key=section_key))

    # ── Settings ───────────────────────────────────────────────────────────────

    @app.route("/admin/settings", methods=["GET", "POST"])
    @require_admin
    def admin_settings():
        if request.method == "POST":
            toggle_keys  = ["cod_enabled", "online_payment_enabled", "free_shipping_enabled", "free_shipping_all"]
            text_keys    = [
                "razorpay_key_id", "razorpay_key_secret",
                "ithink_access_token", "ithink_secret_key", "ithink_pickup_address_id",
                "ithink_return_address_id", "ithink_logistics_partner", "ithink_default_weight_kg",
                "ithink_service_type", "social_instagram_url",
            ]
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
        import math
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        per_page = 20
        offset   = (page - 1) * per_page
        try:
            coupons = db.query("""
                SELECT c.*, (SELECT COUNT(*) FROM coupon_usages WHERE coupon_id = c.id) as used_count
                FROM coupons c
                ORDER BY c.created_at DESC
                LIMIT ? OFFSET ?
            """, [per_page, offset])
            total       = (db.query_one("SELECT COUNT(*) AS cnt FROM coupons") or {}).get("cnt", 0)
            total_pages = max(1, math.ceil(total / per_page))
        except Exception as e:
            coupons, total, total_pages = [], 0, 1
            flash(f"Error loading coupons: {e}", "error")
        return render_template(
            "admin/coupons.html", coupons=coupons, total=total, total_pages=total_pages, page=page
        )

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
            with db.transaction() as tx:
                tx.execute("DELETE FROM coupon_usages WHERE coupon_id=?", [coupon_id])
                tx.execute("DELETE FROM coupons WHERE id=?", [coupon_id])
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

                # Pass 1: parse & validate every row up front, collecting the
                # sku/slug candidates so duplicates can be checked in ONE query
                # instead of one SELECT per row.
                parsed_rows = []
                candidate_skus, candidate_slugs = [], []
                for i, row in enumerate(reader, 1):
                    try:
                        name = (row.get("post_title") or row.get("name") or "").strip()
                        if not name:
                            skipped += 1
                            continue
                        sku_input = (row.get("sku") or "").strip()
                        price = float(row.get("regular_price") or row.get("price") or 0)
                        sale  = float(row.get("sale_price") or 0) or None
                        stock = int(row.get("stock") or row.get("stock_quantity") or 0)
                        desc  = (row.get("description") or row.get("post_content") or "").strip()
                        short = (row.get("short_description") or row.get("post_excerpt") or "").strip()
                        img   = (row.get("images") or row.get("image") or "").strip().split("|")[0].strip()
                        slug  = (row.get("post_name") or row.get("slug") or name.lower().replace(" ", "-")).strip()
                        parsed_rows.append({
                            "i": i, "name": name, "sku_input": sku_input, "price": price,
                            "sale": sale, "stock": stock, "desc": desc, "short": short,
                            "img": img, "slug": slug,
                        })
                        if sku_input:
                            candidate_skus.append(sku_input)
                        candidate_slugs.append(slug)
                    except Exception as row_err:
                        errors.append(f"Row {i}: {row_err}")

                existing_skus, existing_slugs = set(), set()
                if candidate_skus or candidate_slugs:
                    dupe_rows = db.query(
                        "SELECT sku, slug FROM products WHERE sku = ANY(?) OR slug = ANY(?)",
                        [candidate_skus or [""], candidate_slugs or [""]]
                    )
                    existing_skus  = {r["sku"] for r in dupe_rows if r["sku"]}
                    existing_slugs = {r["slug"] for r in dupe_rows if r["slug"]}

                # Pass 2: insert. Each row's writes are wrapped in their own
                # transaction so a mid-row failure can't leave an orphaned
                # product without its image, while keeping row failures
                # isolated from each other (one bad row doesn't sink the batch).
                seen_skus, seen_slugs = set(), set()
                for r in parsed_rows:
                    try:
                        sku  = r["sku_input"] or generate_unique_product_sku(r["name"])
                        slug = r["slug"]
                        if (sku in existing_skus or sku in seen_skus or
                                slug in existing_slugs or slug in seen_slugs):
                            skipped += 1
                            continue

                        pid = str(uuid.uuid4())
                        with db.transaction() as tx:
                            tx.execute(
                                """INSERT INTO products (id, name, slug, sku, price, sale_price,
                                   stock_quantity, stock_status, description, short_description, is_active)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                                [pid, r["name"], slug, sku, r["price"], r["sale"], r["stock"],
                                 "in_stock" if r["stock"] > 0 else "out_of_stock", r["desc"], r["short"]]
                            )
                            if r["img"]:
                                mid = str(uuid.uuid4())
                                tx.execute("INSERT INTO media (id, file_url) VALUES (?,?)", [mid, r["img"]])
                                tx.execute(
                                    "INSERT INTO product_images (id, product_id, media_id, is_primary) VALUES (?,?,?,1)",
                                    [str(uuid.uuid4()), pid, mid]
                                )
                        seen_skus.add(sku)
                        seen_slugs.add(slug)
                        imported += 1
                    except Exception as row_err:
                        errors.append(f"Row {r['i']}: {row_err}")

                results = {"imported": imported, "skipped": skipped, "errors": errors}
                if imported > 0:
                    get_products.cache_clear()
                    get_homepage_products.cache_clear()
                    get_product_detail.cache_clear()
                flash(f"Import complete: {imported} imported, {skipped} skipped.", "success")
            except Exception as e:
                flash(f"CSV parse error: {e}", "error")
        return render_template("admin/import.html", results=results)
