import math
import datetime as _dt
import db
from helpers import ttl_cache, get_cached_store_settings

_EPOCH = _dt.datetime.min

# SQLite-compatible correlated subquery for variable product min price
PRODUCTS_SELECT = """
    SELECT
        p.id, p.name, p.slug, p.sku, p.type, p.short_description, p.description,
        COALESCE(
            CASE WHEN p.type = 'variable' THEN (
                SELECT MIN(pv.price) FROM product_variations pv
                WHERE pv.product_id = p.id AND pv.price > 0
            ) END,
            p.price
        ) AS price,
        p.sale_price, p.stock_quantity, p.stock_status,
        p.is_featured, p.is_active, p.created_at, p.is_lens_compatible,
        c.name  AS category_name, c.slug AS category_slug,
        b.name  AS brand_name,    b.slug AS brand_slug,
        m.file_url AS image_url
    FROM products p
    LEFT JOIN categories c ON c.id = p.category_id
    LEFT JOIN brands b      ON b.id = p.brand_id
    LEFT JOIN product_images pi ON pi.product_id = p.id AND pi.is_primary = 1
    LEFT JOIN media m ON m.id = pi.media_id
"""

PRODUCTS_MINIMAL_SELECT = """
    SELECT
        p.id, p.name, p.slug, p.sku, p.type,
        COALESCE(
            CASE WHEN p.type = 'variable' THEN (
                SELECT MIN(pv.price) FROM product_variations pv
                WHERE pv.product_id = p.id AND pv.price > 0
            ) END,
            p.price
        ) AS price,
        p.sale_price, p.stock_status, p.is_featured, p.created_at,
        c.name AS category_name, c.slug AS category_slug,
        m.file_url AS image_url
    FROM products p
    LEFT JOIN categories c ON c.id = p.category_id
    LEFT JOIN product_images pi ON pi.product_id = p.id AND pi.is_primary = 1
    LEFT JOIN media m ON m.id = pi.media_id
"""


@ttl_cache(ttl_seconds=60)
def get_products(search=None, categories=(), brands=(), shape=None,
                 sort="created_at_desc", page=1, per_page=16,
                 featured=False, limit=None, on_sale=False,
                 min_price=None, max_price=None,
                 # legacy single-value aliases kept for admin callers
                 category=None, brand=None):
    # Normalise: merge legacy single values into the multi-select tuples
    cats_list = list(c for c in (list(categories or []) + ([category] if category else [])) if c)
    if len(cats_list) > 1:
        all_cats = get_categories()
        sel_cats = [c for c in all_cats if c["slug"] in cats_list]
        parent_ids = {c["parent_id"] for c in sel_cats if c.get("parent_id")}
        cats = tuple(c["slug"] for c in sel_cats if c["id"] not in parent_ids)
    else:
        cats = tuple(cats_list)
        
    brnds = tuple(b for b in (list(brands or []) + ([brand] if brand else [])) if b)

    conditions = ["p.is_active = 1"]
    params     = []

    if search:
        conditions.append("(p.name ILIKE ? OR p.sku ILIKE ? OR p.description ILIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if cats:
        ph = ",".join(["?"] * len(cats))
        # Include products from the selected category AND any of its child categories
        conditions.append(f"""p.category_id IN (
            SELECT id FROM categories
            WHERE slug IN ({ph})
               OR parent_id IN (SELECT id FROM categories WHERE slug IN ({ph}))
        )""")
        params += list(cats) + list(cats)
    if brnds:
        ph = ",".join(["?"] * len(brnds))
        conditions.append(f"b.slug IN ({ph})")
        params += list(brnds)
    if featured:
        conditions.append("p.is_featured = 1")
    if on_sale:
        conditions.append("p.sale_price IS NOT NULL AND p.sale_price > 0 AND p.sale_price < p.price")
    if min_price is not None:
        conditions.append("COALESCE(p.sale_price, p.price) >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("COALESCE(p.sale_price, p.price) <= ?")
        params.append(max_price)
    if shape:
        conditions.append("""
            (EXISTS (
                SELECT 1 FROM product_variations pv2
                JOIN variation_attribute_values vav ON vav.variation_id = pv2.id
                JOIN attribute_values av ON av.id = vav.attribute_value_id
                WHERE pv2.product_id = p.id AND av.value LIKE ?
            ) OR p.name LIKE ? OR p.short_description LIKE ?)
        """)
        params += [f"%{shape}%", f"%{shape}%", f"%{shape}%"]

    where     = "WHERE " + " AND ".join(conditions)
    order_map = {
        "created_at_desc": "p.created_at DESC",
        "created_at_asc":  "p.created_at ASC",
        "price_asc":       "p.price ASC",
        "price_desc":      "p.price DESC",
        "name_asc":        "p.name ASC",
    }
    glasses_priority = "CASE WHEN c.slug LIKE 'eyeglasses%%' OR c.slug LIKE 'sunglasses%%' THEN 0 ELSE 1 END"
    order            = f"{glasses_priority}, {order_map.get(sort, 'p.created_at DESC')}"

    if limit:
        return db.query(
            f"{PRODUCTS_MINIMAL_SELECT} {where} ORDER BY {order} LIMIT ?",
            params + [limit],
        )

    count_sql = (
        "SELECT COUNT(*) AS cnt FROM products p "
        "LEFT JOIN categories c ON c.id = p.category_id "
        "LEFT JOIN brands b ON b.id = p.brand_id "
        f"{where}"
    )
    total_row   = db.query_one(count_sql, params)
    total       = total_row["cnt"] if total_row else 0
    total_pages = max(1, math.ceil(total / per_page))
    offset      = (page - 1) * per_page
    products    = db.query(
        f"{PRODUCTS_SELECT} {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )
    return products, total, total_pages


@ttl_cache(ttl_seconds=120)
def get_homepage_products():
    """Single query for all homepage product sections; partitioned in Python."""
    rows = db.query(
        f"{PRODUCTS_MINIMAL_SELECT} WHERE p.is_active = 1 ORDER BY p.is_featured DESC, p.created_at DESC LIMIT 100"
    )
    featured   = [r for r in rows if r.get("is_featured")][:10]
    if not featured:
        featured = rows[:10]
    latest     = sorted(rows, key=lambda r: r.get("created_at") or _EPOCH, reverse=True)[:10]
    popular    = sorted(rows, key=lambda r: (r.get("name") or "").lower())[:10]
    
    # Category-specific slices using new sub-category slugs
    men_products    = [r for r in rows if r.get("category_slug") in ("eyeglasses-men", "sunglasses-men")][:8]
    women_products  = [r for r in rows if r.get("category_slug") in ("eyeglasses-women", "sunglasses-women")][:8]
    kids_products   = [r for r in rows if r.get("category_slug") in ("eyeglasses-kids", "sunglasses-kids")][:8]
    sun_products    = [r for r in rows if r.get("category_slug") in ("sunglasses", "sunglasses-men", "sunglasses-women", "sunglasses-kids")][:8]
    blue_products   = [r for r in rows if r.get("category_slug") == "blue-light"][:8]
    accessories_products = [r for r in rows if r.get("category_slug") in ("accessories", "accessories-contacts-solutions", "accessories-lens-cleaners")][:8]
    optical_products= [r for r in rows if r.get("category_slug") in ("eyeglasses", "eyeglasses-men", "eyeglasses-women", "eyeglasses-kids")][:8]

    price_asc  = sorted(rows, key=lambda r: float(r.get("price") or 0))
    promo1     = rows[:2]
    promo2     = price_asc[:2]
    
    return {
        "featured": featured, "latest": latest, "popular": popular,
        "promo1": promo1, "promo2": promo2,
        "men": men_products, "women": women_products, "kids": kids_products,
        "sunglasses": sun_products, "blue_light": blue_products, "accessories": accessories_products, "optical": optical_products
    }


@ttl_cache(ttl_seconds=60)
def get_trending_shapes():
    return db.query("""
        SELECT av.value AS label, av.image_url AS img, av.id
        FROM attribute_values av
        JOIN attributes a ON a.id = av.attribute_id
        WHERE a.slug = 'frame-shape' AND av.image_url IS NOT NULL
        LIMIT 9
    """) or []


@ttl_cache(ttl_seconds=600)
def get_featured_categories():
    return db.query("""
        SELECT name AS label, image_url AS img, slug
        FROM categories
        WHERE parent_id IS NULL
        ORDER BY name ASC
    """) or []


@ttl_cache(ttl_seconds=120)
def get_home_sections(section_type):
    """Active homepage content blocks of one type, in display order. Public-facing (cached)."""
    return db.query(
        "SELECT * FROM home_sections WHERE section_type=? AND is_active=1 "
        "ORDER BY sort_order ASC, created_at ASC",
        [section_type]
    ) or []


# The 7 product carousels that used to be hardcoded, fixed home page sections.
# They're seeded as real home_sections rows (id = their section key) so admins
# manage them from the same Product Carousels table as any custom carousel —
# same rename/reorder-within-picks/hide/products flow, no separate concept.
HOME_BUILTIN_CAROUSEL_DEFAULTS = {
    "bestsellers": {"title": "Best Sellers", "badge_text": "Top Picks",
                     "cta_text": "View All Best Sellers", "cta_link": "/shop?featured=1", "sort_order": 0},
    "men":         {"title": "Men's Eyewear", "badge_text": "For Him",
                     "cta_text": "View All Men's", "cta_link": "/shop?category=eyeglasses-men&category=sunglasses-men", "sort_order": 1},
    "women":       {"title": "Women's Eyewear", "badge_text": "For Her",
                     "cta_text": "View All Women's", "cta_link": "/shop?category=eyeglasses-women&category=sunglasses-women", "sort_order": 2},
    "kids":        {"title": "Kids' Eyewear", "badge_text": "For Little Ones",
                     "cta_text": "View All Kids'", "cta_link": "/shop?category=eyeglasses-kids&category=sunglasses-kids", "sort_order": 3},
    "accessories": {"title": "Premium Accessories", "badge_text": "Essential Care",
                     "cta_text": "View All", "cta_link": "/shop?category=accessories", "sort_order": 4},
    "sunglasses":  {"title": "Sunglasses", "badge_text": "Sun Protection",
                     "cta_text": "View All Sunglasses", "cta_link": "/shop?category=sunglasses", "sort_order": 5},
    "eyeglasses":  {"title": "Eyeglasses", "badge_text": "Prescription Ready",
                     "cta_text": "View All Eyeglasses", "cta_link": "/shop?category=eyeglasses", "sort_order": 6},
}

# The shape-browsing section ("Shop by Shape" / "Frames That Suit You"). Its
# tiles still come from Admin > Attributes > Frame Shape images, but the
# section's own heading + visibility is now a real, editable row too — id
# fixed at "shape" so it's a single, non-duplicable section like the others.
HOME_SHAPE_SECTION_DEFAULTS = {
    "title": "Frames That Suit You", "badge_text": "Shop by Shape",
    "cta_text": "Explore Collection", "cta_link": "/shop",
}


def ensure_builtin_home_sections():
    """Idempotent, self-healing seed. The 7 carousels are gated behind a
    store_settings flag (cheap: skip 7 lookups once seeded); the shape row is
    checked unconditionally on its own — a single indexed lookup — so seeding
    something new later (like this one was) can't get silently skipped by an
    already-true flag from before it existed."""
    settings = get_cached_store_settings()
    changed = False

    if settings.get("home_builtin_carousels_seeded") != "true":
        for key, d in HOME_BUILTIN_CAROUSEL_DEFAULTS.items():
            if db.query_one("SELECT id FROM home_sections WHERE id=?", [key]):
                continue
            is_active = 1 if settings.get(f"home_visible_{key}", "true") != "false" else 0
            db.execute(
                """INSERT INTO home_sections
                   (id, section_type, title, badge_text, cta_text, cta_link, sort_order, is_active)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [key, "carousel", d["title"], d["badge_text"], d["cta_text"], d["cta_link"], d["sort_order"], is_active]
            )
        db.execute(
            "INSERT INTO store_settings (key, value) VALUES ('home_builtin_carousels_seeded','true') "
            "ON CONFLICT (key) DO UPDATE SET value='true'"
        )
        changed = True

    if not db.query_one("SELECT id FROM home_sections WHERE id='shape'"):
        d = HOME_SHAPE_SECTION_DEFAULTS
        is_active = 1 if settings.get("home_visible_shape", "true") != "false" else 0
        db.execute(
            """INSERT INTO home_sections
               (id, section_type, title, badge_text, cta_text, cta_link, sort_order, is_active)
               VALUES (?,?,?,?,?,?,?,?)""",
            ["shape", "shape", d["title"], d["badge_text"], d["cta_text"], d["cta_link"], 0, is_active]
        )
        changed = True

    if changed:
        get_home_sections.cache_clear()
        get_cached_store_settings.cache_clear()


def get_home_sections_admin(section_type):
    """All homepage content blocks of one type (including hidden ones), for the admin list."""
    return db.query(
        "SELECT * FROM home_sections WHERE section_type=? ORDER BY sort_order ASC, created_at ASC",
        [section_type]
    ) or []


def get_home_section(item_id):
    return db.query_one("SELECT * FROM home_sections WHERE id=?", [item_id])


@ttl_cache(ttl_seconds=120)
def get_home_product_picks(section_key):
    """Ordered product IDs an admin has manually curated for a homepage carousel.
    Empty list means that section should fall back to its automatic selection."""
    rows = db.query(
        "SELECT product_id FROM home_product_picks WHERE section_key=? ORDER BY sort_order ASC",
        [section_key]
    )
    return [r["product_id"] for r in rows] if rows else []


def get_home_product_picks_admin(section_key):
    """Curated products for one section with display details, in admin-set order."""
    return db.query(
        """
        SELECT hpp.id AS pick_id, hpp.sort_order, hpp.product_id,
               p.name, p.sku, p.price, p.sale_price,
               m.file_url AS image_url
        FROM home_product_picks hpp
        JOIN products p ON p.id = hpp.product_id
        LEFT JOIN product_images pi ON pi.product_id = p.id AND pi.is_primary = 1
        LEFT JOIN media m ON m.id = pi.media_id
        WHERE hpp.section_key = ?
        ORDER BY hpp.sort_order ASC
        """,
        [section_key]
    ) or []


def get_products_by_ids(product_ids):
    """Fetch active products by explicit ID list, preserving the given order."""
    if not product_ids:
        return []
    placeholders = ",".join(["?"] * len(product_ids))
    rows = db.query(
        f"{PRODUCTS_SELECT} WHERE p.id IN ({placeholders}) AND p.is_active = 1",
        list(product_ids)
    )
    by_id = {r["id"]: r for r in rows}
    return [by_id[pid] for pid in product_ids if pid in by_id]


@ttl_cache(ttl_seconds=120)
def get_product_detail(product_id):
    product = db.query_one(f"{PRODUCTS_SELECT} WHERE p.id = ?", [product_id])
    if not product:
        return None, [], [], [], []

    images = db.query(
        """SELECT m.file_url AS image_url, pi.is_primary,
                  COALESCE(m.alt_text, '') AS alt_text
           FROM product_images pi
           JOIN media m ON m.id = pi.media_id
           WHERE pi.product_id = ?
           ORDER BY pi.is_primary DESC, pi.display_order""",
        [product_id],
    )

    variations = db.query(
        "SELECT * FROM product_variations WHERE product_id = ?", [product_id]
    )

    base_price = float(product.get("sale_price") or product.get("price") or 0)
    base_stock = int(product.get("stock_quantity") or 0)

    # Batch-load all variation→attribute_value mappings in ONE query
    if variations:
        var_ids      = [v["id"] for v in variations]
        placeholders = ",".join(["?"] * len(var_ids))
        all_vav      = db.query(
            f"SELECT variation_id, attribute_value_id "
            f"FROM variation_attribute_values WHERE variation_id IN ({placeholders})",
            var_ids,
        )
        vav_map = {}
        for row in all_vav:
            vav_map.setdefault(str(row["variation_id"]), []).append(row["attribute_value_id"])
        for v in variations:
            v["price"]               = base_price
            v["stock_quantity"]      = base_stock
            v["attribute_value_ids"] = vav_map.get(str(v["id"]), [])
    else:
        for v in variations:
            v["price"]               = base_price
            v["stock_quantity"]      = base_stock
            v["attribute_value_ids"] = []

    if product.get("type") == "variable":
        product["price"] = base_price if base_price > 0 else float(product.get("price") or 0)

    reviews = db.query(
        """SELECT r.*, r.body AS comment, (u.first_name || ' ' || u.last_name) AS reviewer_name
           FROM product_reviews r LEFT JOIN users u ON u.id = r.user_id
           WHERE r.product_id = ? AND r.is_approved = 1
           ORDER BY r.created_at DESC""",
        [product_id],
    )

    attributes = db.query("""
        SELECT a.id, a.name, a.slug
        FROM attributes a
        JOIN product_attributes pa ON pa.attribute_id = a.id
        WHERE pa.product_id = ?
        ORDER BY pa.display_order ASC
    """, [product_id])

    if not attributes:
        attributes = db.query("""
            SELECT DISTINCT a.id, a.name, a.slug
            FROM attributes a
            JOIN attribute_values av ON av.attribute_id = a.id
            JOIN variation_attribute_values vav ON vav.attribute_value_id = av.id
            JOIN product_variations pv ON pv.id = vav.variation_id
            WHERE pv.product_id = ?
        """, [product_id])

    # Batch-load attribute values with correct priority:
    # 1. Admin-selected values for this product (product_attribute_values)
    # 2. Values linked via generated variations (variation_attribute_values)
    # 3. All values for the attribute (last resort — should rarely be reached)
    if attributes:
        attr_ids     = [a["id"] for a in attributes]
        placeholders = ",".join(["?"] * len(attr_ids))

        # Priority 1: values the admin explicitly checked for this product
        pav_rows = db.query(
            f"""SELECT DISTINCT av.attribute_id, av.id, av.value
                FROM attribute_values av
                JOIN product_attribute_values pav ON pav.attribute_value_id = av.id
                WHERE av.attribute_id IN ({placeholders}) AND pav.product_id = ?
                ORDER BY av.value ASC""",
            attr_ids + [product_id],
        )
        pav_map = {}
        for row in pav_rows:
            pav_map.setdefault(str(row["attribute_id"]), []).append(
                {"id": str(row["id"]), "value": row["value"]}
            )

        # Priority 2: values linked through generated product variations
        var_rows = db.query(
            f"""SELECT DISTINCT av.attribute_id, av.id, av.value
                FROM attribute_values av
                JOIN variation_attribute_values vav ON vav.attribute_value_id = av.id
                JOIN product_variations pv ON pv.id = vav.variation_id
                WHERE av.attribute_id IN ({placeholders}) AND pv.product_id = ?
                ORDER BY av.value ASC""",
            attr_ids + [product_id],
        )
        var_map = {}
        for row in var_rows:
            var_map.setdefault(str(row["attribute_id"]), []).append(
                {"id": str(row["id"]), "value": row["value"]}
            )

        for attr in attributes:
            aid    = str(attr["id"])
            values = pav_map.get(aid) or var_map.get(aid)
            if not values:
                # Priority 3: load all values for this attribute (fallback)
                fallback = db.query(
                    "SELECT id, value FROM attribute_values "
                    "WHERE attribute_id = ? ORDER BY value ASC",
                    [attr["id"]],
                )
                values = [{"id": str(r["id"]), "value": r["value"]} for r in fallback]
            attr["values"] = values

    return product, images, variations, reviews, attributes


@ttl_cache(ttl_seconds=120)
def get_related_products(category_slug, exclude_id, limit=4):
    # Try fetching from the exact category first
    results = db.query(
        f"{PRODUCTS_MINIMAL_SELECT} WHERE p.is_active = 1 AND c.slug = ? AND p.id != ? "
        f"ORDER BY p.created_at DESC LIMIT ?",
        [category_slug, exclude_id, limit],
    )
    # If we don't have enough, fall back to other active products from any category
    if len(results) < limit:
        already_fetched = [r["id"] for r in results]
        already_fetched.append(exclude_id)
        
        needed = limit - len(results)
        placeholders = ",".join(["?"] * len(already_fetched))
        
        fallback_query = (
            f"{PRODUCTS_MINIMAL_SELECT} WHERE p.is_active = 1 AND p.id NOT IN ({placeholders}) "
            f"ORDER BY p.created_at DESC LIMIT ?"
        )
        fallback_results = db.query(fallback_query, already_fetched + [needed])
        results.extend(fallback_results)
        
    return results[:limit]


@ttl_cache(ttl_seconds=120)
def get_categories():
    return db.query("""
        SELECT c.id, c.name, c.slug, c.parent_id, cp.name AS parent_name, c.image_url AS img,
               (
                   SELECT COUNT(*)
                   FROM products p
                   WHERE p.is_active = 1
                     AND (
                         p.category_id = c.id
                         OR p.category_id IN (
                             SELECT id FROM categories WHERE parent_id = c.id
                         )
                     )
               ) AS product_count
        FROM categories c
        LEFT JOIN categories cp ON cp.id = c.parent_id
        ORDER BY
            CASE WHEN c.slug = 'eyeglasses' THEN 0
                 WHEN c.slug = 'contacts'   THEN 1
                 WHEN c.slug = 'sunglasses' THEN 2
                 ELSE 3 END ASC,
            c.name ASC
    """)


@ttl_cache(ttl_seconds=120)
def get_brands():
    return db.query("""
        SELECT b.id, b.name, b.slug, COUNT(p.id) AS product_count
        FROM brands b
        LEFT JOIN products p ON p.brand_id = b.id AND p.is_active = 1
        GROUP BY b.id, b.name, b.slug
        ORDER BY b.name
    """)


@ttl_cache(ttl_seconds=120)
def get_admin_stats():
    """All dashboard stats in a single round-trip."""
    row = db.query_one("""
        SELECT
            (SELECT COUNT(*)                  FROM products WHERE is_active = 1)                    AS total_products,
            (SELECT COUNT(*)                  FROM orders)                                              AS total_orders,
            (SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status != 'cancelled')            AS total_revenue,
            (SELECT COUNT(*)                  FROM users   WHERE role = 'customer')                    AS total_customers,
            (SELECT COUNT(*)                  FROM orders  WHERE status = 'pending')                   AS pending_orders,
            (SELECT COUNT(*)                  FROM products WHERE stock_quantity <= 5 AND is_active = 1) AS low_stock
    """) or {}
    return {
        "total_products":  row.get("total_products",  0),
        "total_orders":    row.get("total_orders",    0),
        "total_revenue":   float(row.get("total_revenue", 0)),
        "total_customers": row.get("total_customers", 0),
        "pending_orders":  row.get("pending_orders",  0),
        "low_stock":       row.get("low_stock",       0),
    }
