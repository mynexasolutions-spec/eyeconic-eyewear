"""
app.py — Application factory for ChasmaGallery.
"""
import os
import uuid
from flask import Flask, render_template, session, request
from flask_compress import Compress
from dotenv import load_dotenv

load_dotenv()

from extensions import csrf, limiter, handle_csrf_error
from helpers import register_jinja, get_cached_store_settings
import db

compress = Compress()


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-key-change-in-production")

    # Payload limit: 16MB
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

    # Session configuration
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV", "development") == "production"
    app.config["PERMANENT_SESSION_LIFETIME"] = 86400

    # CSRF configuration
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    app.config["WTF_CSRF_CHECK_DEFAULT"] = True

    # Gzip compression — compresses HTML/JSON/CSS responses automatically
    app.config["COMPRESS_ALGORITHM"] = "gzip"
    app.config["COMPRESS_LEVEL"] = 6
    app.config["COMPRESS_MIN_SIZE"] = 500

    # Initialize extensions
    compress.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Register CSRF error handler
    app.register_error_handler(400, handle_csrf_error)

    # Register Jinja2 helpers and globals
    register_jinja(app)

    # Session initialization
    @app.before_request
    def ensure_session():
        session.setdefault("_csrf_initialized", True)

    # Cache-Control headers for static files
    @app.after_request
    def set_cache_headers(response):
        if request.path.startswith("/static/"):
            # Static assets: cache for 1 year (they're versioned via filenames)
            response.cache_control.max_age = 31536000
            response.cache_control.public = True
            response.cache_control.immutable = True
        elif request.path in ("/", "/shop") or request.path.startswith("/product/"):
            # Public pages: allow shared caches for 60 seconds
            response.cache_control.max_age = 60
            response.cache_control.public = True
        return response

    @app.context_processor
    def inject_globals():
        cart  = session.get("cart", {})
        count = sum(item.get("qty", 0) for item in cart.values())
        try:
            settings = get_cached_store_settings()
        except Exception:
            settings = {}
        instagram_url = settings.get("social_instagram_url") or "https://www.instagram.com/eyeconiceyewear01"
        instagram_handle = instagram_url.rstrip("/").rsplit("/", 1)[-1] or "eyeconiceyewear01"
        return {
            "cart_count": count, "current_user": session.get("user"),
            "instagram_url": instagram_url, "instagram_handle": instagram_handle,
        }

    # Blueprints
    from routes.public   import bp as public_bp
    from routes.auth     import bp as auth_bp
    from routes.cart     import bp as cart_bp
    from routes.checkout import bp as checkout_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(cart_bp)
    app.register_blueprint(checkout_bp)

    # Admin routes
    from routes.admin import register as reg_admin
    reg_admin(app)

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    return app


app = create_app()


def _ensure_home_sections_table():
    """Create + seed the home_sections table. Called once by _run_startup_bootstrap()
    below — never directly on every request/cold start (see that function's
    docstring for why that used to be a major perf problem)."""
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS home_sections (
                id           TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
                section_type TEXT NOT NULL,
                title        TEXT DEFAULT '',
                subtitle     TEXT DEFAULT '',
                body         TEXT DEFAULT '',
                badge_text   TEXT DEFAULT '',
                image_url    TEXT DEFAULT '',
                link_url     TEXT DEFAULT '',
                cta_text     TEXT DEFAULT '',
                cta_link     TEXT DEFAULT '',
                cta2_text    TEXT DEFAULT '',
                cta2_link    TEXT DEFAULT '',
                rating       INTEGER DEFAULT 5,
                sort_order   INTEGER DEFAULT 0,
                is_active    INTEGER DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_home_sections_type "
            "ON home_sections(section_type, is_active, sort_order)"
        )
        def _count(section_type):
            row = db.query_one(
                "SELECT COUNT(*) as count FROM home_sections WHERE section_type=?",
                [section_type]
            )
            return row.get("count", 0) if row else 0

        if _count("hero") == 0:
            hero_slides = [
                ("New Collection 2025", "See The World", "Differently",
                 "Premium eyewear crafted for those who refuse to blend in. Handcrafted frames that define your identity.",
                 "https://images.unsplash.com/photo-1526045612212-70caf35c14df?w=1600&q=85&auto=format&fit=crop",
                 "Shop Now", "/shop", "Explore Collection", "/shop"),
                ("Accessories Collection", "Complete Your", "Look",
                 "Premium lens cleaners, microfiber cloths, and contact solutions to keep your vision crystal clear.",
                 "https://images.unsplash.com/photo-1604537529428-15bcbeecfe4d?w=1600&q=85&auto=format&fit=crop",
                 "Shop Accessories", "/shop?category=accessories", "Learn More", "/shop"),
                ("Exclusive Sale — Up to 50% Off", "Style For", "Every Face",
                 "From classic round frames to bold wayfarers — discover the perfect pair that speaks to your soul.",
                 "https://images.unsplash.com/photo-1508214751196-bcfd4ca60f91?w=1600&q=85&auto=format&fit=crop",
                 "Shop Sale", "/shop?on_sale=1", "All Collections", "/shop"),
            ]
            for i, (badge, title, subtitle, body, image_url, cta_text, cta_link, cta2_text, cta2_link) in enumerate(hero_slides):
                db.execute(
                    "INSERT INTO home_sections (id, section_type, badge_text, title, subtitle, body, "
                    "image_url, cta_text, cta_link, cta2_text, cta2_link, sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [str(uuid.uuid4()), "hero", badge, title, subtitle, body, image_url,
                     cta_text, cta_link, cta2_text, cta2_link, i]
                )

            print("[db.migrate] Seeded default hero slides.")

        if _count("category") == 0:
            category_tiles = [
                ("Men", "man.webp", "/shop?category=men"),
                ("Women", "woman.webp", "/shop?category=women"),
                ("Kids", "kid.webp", "/shop?category=kids"),
            ]
            for i, (name, image_url, link) in enumerate(category_tiles):
                db.execute(
                    "INSERT INTO home_sections (id, section_type, title, image_url, cta_text, cta_link, sort_order) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [str(uuid.uuid4()), "category", name, image_url, "Explore Now", link, i]
                )
            print("[db.migrate] Seeded default category tiles.")

        if _count("stat") == 0:
            stats = [
                ("50K+", "Happy Customers"), ("4.8/5", "Average Rating"),
                ("100+", "Premium Styles"), ("20+", "Cities Delivered"),
                ("99%", "Satisfaction Rate"),
            ]
            for i, (value, label) in enumerate(stats):
                db.execute(
                    "INSERT INTO home_sections (id, section_type, title, subtitle, sort_order) "
                    "VALUES (?,?,?,?,?)",
                    [str(uuid.uuid4()), "stat", value, label, i]
                )
            print("[db.migrate] Seeded default trust stats.")

        if _count("banner") == 0:
            db.execute(
                "INSERT INTO home_sections (id, section_type, badge_text, title, subtitle, body, "
                "image_url, cta_text, cta_link, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [str(uuid.uuid4()), "banner", "Made For Every Moment", "Style That Fits", "Every You",
                 "From workdays to weekends, find eyewear that matches your vibe, your energy, your world.",
                 "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=1600&q=85&auto=format&fit=crop",
                 "Explore Collection", "/shop", 0]
            )
            print("[db.migrate] Seeded default lifestyle banner.")

        if _count("testimonial") == 0:
            testimonials = [
                ("Rohit Sharma", "Mumbai", "The quality is outstanding and so comfortable to wear all day. These frames have completely replaced all my old glasses. Highly recommended!"),
                ("Ananya Verma", "Delhi", "Stylish, lightweight and great packaging. My go-to eyewear brand now. The blue light glasses really help with my screen time."),
                ("Karan Mehta", "Bengaluru", "Fast delivery, premium packaging and amazing product quality. The aviator frames look exactly like the photos. Will buy again!"),
                ("Neha Kapoor", "Pune", "Finally found blue light glasses that actually work and look good! I wear them every day and my eye strain has reduced so much."),
                ("Priya Rao", "Hyderabad", "Absolutely love the cat-eye frames! Got so many compliments. The packaging was beautiful and delivery was super fast."),
                ("Arjun Kumar", "Chennai", "Best purchase this year. The sunglasses are premium quality and the polarised lenses are incredible. Worth every rupee!"),
                ("Sunita Mehta", "Jaipur", "Ordered the kids frames for my daughter and she absolutely loves them! Sturdy, flexible and so cute. Great for school."),
            ]
            for i, (name, city, text) in enumerate(testimonials):
                db.execute(
                    "INSERT INTO home_sections (id, section_type, title, subtitle, body, rating, sort_order) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [str(uuid.uuid4()), "testimonial", name, city, text, 5, i]
                )
            print("[db.migrate] Seeded default testimonials.")

        if _count("instagram") == 0:
            for i in range(1, 5):
                db.execute(
                    "INSERT INTO home_sections (id, section_type, image_url, sort_order) VALUES (?,?,?,?)",
                    [str(uuid.uuid4()), "instagram", f"{i}.webp", i - 1]
                )
            print("[db.migrate] Seeded default instagram tiles.")
    except Exception as _e:
        print(f"[db.migrate] Error setting up home_sections: {_e}")


def _ensure_home_product_picks_table():
    """Curated per-section product lists for the home page carousels (Best Sellers,
    Men's/Women's/Kids' Eyewear, Accessories, Sunglasses, Eyeglasses). An empty table
    for a given section_key means that section still falls back to its automatic
    category/featured-based selection."""
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS home_product_picks (
                id          TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
                section_key TEXT NOT NULL,
                product_id  TEXT NOT NULL,
                sort_order  INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_home_product_picks_section "
            "ON home_product_picks(section_key, sort_order)"
        )
    except Exception as _e:
        print(f"[db.migrate] Error setting up home_product_picks: {_e}")


def _ensure_shipping_columns():
    """Add iThink Logistics shipment columns to orders if they're missing.
    Called once by _run_startup_bootstrap() below."""
    try:
        for col in ("awb_number", "shipment_courier", "shipment_tracking_url"):
            db.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT ''")
    except Exception as _e:
        print(f"[db.migrate] Error setting up shipping columns: {_e}")


def _ensure_perf_indexes():
    """A few FK-lookup indexes used by the product detail page that were
    missing from the original schema. CREATE INDEX IF NOT EXISTS is cheap and
    safe to run alongside the other one-time setup below."""
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_product_reviews_product ON product_reviews(product_id, is_approved)",
        "CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_product_attribute_values_product ON product_attribute_values(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_attribute_values_attribute ON attribute_values(attribute_id)",
        "CREATE INDEX IF NOT EXISTS idx_variation_attribute_values_avid ON variation_attribute_values(attribute_value_id)",
    ]
    for sql in statements:
        try:
            db.execute(sql)
        except Exception as _e:
            print(f"[db.migrate] Error creating index ({sql}): {_e}")


def _run_startup_bootstrap():
    """One-time schema setup + default-data seeding.

    This used to run unconditionally on every process start — including every
    serverless cold start — costing ~15 sequential DB round trips (2 CREATE
    TABLE, 2 CREATE INDEX, 6 COUNT checks, 3 ALTER TABLE, 1 more COUNT) before
    a single request could be served. On a low-traffic Vercel deployment,
    cold starts are frequent, so that tax was being paid constantly and was
    the single biggest contributor to "the site feels slow."

    Now it's gated behind one fast lookup: once the schema/seed work has run
    successfully, a marker row is written to store_settings, and every
    subsequent boot (cold or warm) just does that one SELECT and returns.
    """
    try:
        row = db.query_one("SELECT value FROM store_settings WHERE key='_bootstrap_done'")
        if row and row.get("value") == "true":
            return
    except Exception:
        pass  # store_settings itself may not exist yet on a brand-new database

    _ensure_home_sections_table()
    _ensure_home_product_picks_table()
    _ensure_shipping_columns()
    _ensure_perf_indexes()

    try:
        db.migrate()
        count_res = db.query_one("SELECT COUNT(*) as count FROM store_settings")
        if count_res and count_res.get("count") == 0:
            defaults = [
                ("cod_enabled", "true"),
                ("online_payment_enabled", "false"),
                ("free_shipping_enabled", "true"),
                ("free_shipping_all", "false"),
                ("shipping_fee", "49"),
                ("free_shipping_threshold", "599"),
            ]
            for key, val in defaults:
                db.execute("INSERT INTO store_settings (key, value) VALUES (?, ?)", [key, val])
            print("[db.migrate] Seeded default store settings.")
    except Exception as _e:
        print(f"[db.migrate] {_e}")

    try:
        db.execute(
            "INSERT INTO store_settings (key, value) VALUES ('_bootstrap_done','true') "
            "ON CONFLICT (key) DO UPDATE SET value='true'"
        )
    except Exception as _e:
        print(f"[db.migrate] Error setting bootstrap marker: {_e}")


_run_startup_bootstrap()

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_ENV", "development") != "production"
    app.run(debug=debug, port=port, host="0.0.0.0")
