"""
migrate_to_pg.py — One-time script: copy SQLite data into Supabase PostgreSQL
and create the full schema.

Run from the apps/ directory:
    python migrate_to_pg.py
"""
import os
import sqlite3
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
SQLITE_PATH = os.getenv("SQLITE_PATH", "../chasma.db")   # adjust if needed

PG_HOST     = "aws-1-ap-northeast-1.pooler.supabase.com"
PG_PORT     = 5432
PG_DBNAME   = "postgres"
PG_USER     = "postgres.grqtfwedffslmvufznzx"
PG_PASSWORD = "Eyeconic@0302"

# ── PostgreSQL schema ──────────────────────────────────────────────────────────
# Column names here match what the routes actually use (some differ from the
# original SQLite DDL in db.py).

PG_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    first_name TEXT DEFAULT '',
    last_name  TEXT DEFAULT '',
    email      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role       TEXT DEFAULT 'customer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id             TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    name           TEXT NOT NULL,
    slug           TEXT UNIQUE NOT NULL,
    parent_id      TEXT REFERENCES categories(id),
    image_url      TEXT DEFAULT '',
    is_active      INTEGER DEFAULT 1,
    is_featured    INTEGER DEFAULT 0,
    display_order  INTEGER DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS brands (
    id         TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    name       TEXT NOT NULL,
    slug       TEXT UNIQUE NOT NULL,
    image_url  TEXT DEFAULT '',
    is_active  INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attributes (
    id            TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    name          TEXT NOT NULL,
    slug          TEXT UNIQUE NOT NULL,
    display_order INTEGER DEFAULT 0,
    image_url     TEXT DEFAULT '',
    is_featured   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attribute_values (
    id            TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    attribute_id  TEXT NOT NULL REFERENCES attributes(id),
    value         TEXT NOT NULL,
    image_url     TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media (
    id         TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    file_url   TEXT NOT NULL,
    alt_text   TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id                TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    name              TEXT NOT NULL,
    slug              TEXT UNIQUE NOT NULL,
    sku               TEXT UNIQUE,
    type              TEXT DEFAULT 'simple',
    short_description TEXT DEFAULT '',
    description       TEXT DEFAULT '',
    price             DECIMAL(10,2) DEFAULT 0,
    sale_price        DECIMAL(10,2),
    stock_quantity    INTEGER DEFAULT 0,
    stock_status      TEXT DEFAULT 'in_stock',
    manage_stock      BOOLEAN DEFAULT FALSE,
    category_id       TEXT REFERENCES categories(id),
    brand_id          TEXT REFERENCES brands(id),
    is_active         INTEGER DEFAULT 1,
    is_featured       INTEGER DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS product_images (
    id            TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    product_id    TEXT NOT NULL REFERENCES products(id),
    media_id      TEXT NOT NULL REFERENCES media(id),
    is_primary    INTEGER DEFAULT 0,
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product_variations (
    id             TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    product_id     TEXT NOT NULL REFERENCES products(id),
    sku            TEXT,
    price          DECIMAL(10,2) DEFAULT 0,
    sale_price     DECIMAL(10,2),
    stock_quantity INTEGER DEFAULT 0,
    stock_status   TEXT DEFAULT 'in_stock',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS variation_attribute_values (
    id                 TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    variation_id       TEXT NOT NULL REFERENCES product_variations(id),
    attribute_value_id TEXT NOT NULL REFERENCES attribute_values(id)
);

CREATE TABLE IF NOT EXISTS product_attributes (
    id            TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    product_id    TEXT NOT NULL REFERENCES products(id),
    attribute_id  TEXT NOT NULL REFERENCES attributes(id),
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product_attribute_values (
    id                 TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    product_id         TEXT NOT NULL REFERENCES products(id),
    attribute_value_id TEXT NOT NULL REFERENCES attribute_values(id)
);

CREATE TABLE IF NOT EXISTS product_reviews (
    id         TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    product_id TEXT NOT NULL REFERENCES products(id),
    user_id    TEXT REFERENCES users(id),
    rating     INTEGER DEFAULT 5,
    title      TEXT DEFAULT '',
    body       TEXT DEFAULT '',
    is_approved INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id                   TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    order_number         TEXT,
    user_id              TEXT REFERENCES users(id),
    status               TEXT DEFAULT 'pending',
    subtotal             DECIMAL(10,2) DEFAULT 0,
    shipping_amount      DECIMAL(10,2) DEFAULT 0,
    total_amount         DECIMAL(10,2) DEFAULT 0,
    payment_method       TEXT DEFAULT 'cod',
    payment_status       TEXT DEFAULT 'pending',
    shipping_address_json TEXT DEFAULT '',
    customer_name        TEXT DEFAULT '',
    customer_email       TEXT DEFAULT '',
    customer_phone       TEXT DEFAULT '',
    notes                TEXT DEFAULT '',
    coupon_code          TEXT DEFAULT '',
    discount_amount      DECIMAL(10,2) DEFAULT 0,
    cancelled_at         TIMESTAMP,
    cancel_reason        TEXT DEFAULT '',
    awb_number           TEXT DEFAULT '',
    shipment_courier     TEXT DEFAULT '',
    shipment_tracking_url TEXT DEFAULT '',
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id                    TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    order_id              TEXT NOT NULL REFERENCES orders(id),
    product_id            TEXT REFERENCES products(id),
    variation_id          TEXT,
    quantity              INTEGER DEFAULT 1,
    unit_price            DECIMAL(10,2) DEFAULT 0,
    total_price           DECIMAL(10,2) DEFAULT 0,
    product_name_snapshot TEXT DEFAULT '',
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS coupons (
    id                   TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    code                 TEXT UNIQUE NOT NULL,
    type                 TEXT DEFAULT 'percent',
    value                DECIMAL(10,2) DEFAULT 0,
    min_order_amount     DECIMAL(10,2) DEFAULT 0,
    usage_limit          INTEGER,
    usage_limit_per_user INTEGER DEFAULT 1,
    max_discount         DECIMAL(10,2),
    is_active            INTEGER DEFAULT 1,
    expires_at           TIMESTAMP,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS coupon_usages (
    id        TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    coupon_id TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    order_id  TEXT NOT NULL,
    used_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS store_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_addresses (
    id            TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    user_id       TEXT NOT NULL,
    label         TEXT DEFAULT 'Home',
    first_name    TEXT DEFAULT '',
    last_name     TEXT DEFAULT '',
    phone         TEXT DEFAULT '',
    address_line1 TEXT NOT NULL DEFAULT '',
    address_line2 TEXT DEFAULT '',
    city          TEXT DEFAULT '',
    state         TEXT DEFAULT '',
    pincode       TEXT DEFAULT '',
    country       TEXT DEFAULT 'India',
    is_default    BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    id            TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    email         TEXT UNIQUE NOT NULL,
    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Home page content blocks (hero slides, trust stats, lifestyle banner,
-- testimonials, instagram tiles) — managed from Admin > Home Page.
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
);

-- Curated per-section product picks for home page product carousels.
-- Empty for a section_key = that section falls back to automatic selection.
CREATE TABLE IF NOT EXISTS home_product_picks (
    id          TEXT PRIMARY KEY DEFAULT lower(encode(gen_random_bytes(16), 'hex')),
    section_key TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    sort_order  INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_products_is_active      ON products(is_active);
CREATE INDEX IF NOT EXISTS idx_products_category_id    ON products(category_id);
CREATE INDEX IF NOT EXISTS idx_products_brand_id       ON products(brand_id);
CREATE INDEX IF NOT EXISTS idx_products_is_featured    ON products(is_featured);
CREATE INDEX IF NOT EXISTS idx_products_created_at     ON products(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_products_price          ON products(price);
CREATE INDEX IF NOT EXISTS idx_product_images_prod_pri ON product_images(product_id, is_primary);
CREATE INDEX IF NOT EXISTS idx_product_variations_pid  ON product_variations(product_id);
CREATE INDEX IF NOT EXISTS idx_vav_variation_id        ON variation_attribute_values(variation_id);
CREATE INDEX IF NOT EXISTS idx_orders_user_id          ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status           ON orders(status);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id    ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_user_addresses_user_id  ON user_addresses(user_id);
CREATE INDEX IF NOT EXISTS idx_categories_slug         ON categories(slug);
CREATE INDEX IF NOT EXISTS idx_brands_slug             ON brands(slug);
CREATE INDEX IF NOT EXISTS idx_attributes_slug         ON attributes(slug);
CREATE INDEX IF NOT EXISTS idx_home_sections_type      ON home_sections(section_type, is_active, sort_order);
CREATE INDEX IF NOT EXISTS idx_home_product_picks_sec  ON home_product_picks(section_key, sort_order);

-- Default store settings
INSERT INTO store_settings (key, value) VALUES
    ('cod_enabled','true'), ('online_payment_enabled','false'),
    ('upi_id',''), ('bank_name',''), ('bank_account',''), ('bank_ifsc',''),
    ('razorpay_key_id',''), ('razorpay_key_secret',''),
    ('shipping_fee','99'), ('free_shipping_threshold','999'),
    ('free_shipping_enabled','true'), ('free_shipping_all','false'),
    ('social_instagram_url','https://www.instagram.com/eyeconiceyewear01')
ON CONFLICT (key) DO NOTHING;
"""

# ── Tables to copy (SQLite col → PG col remapping where they differ) ───────────
# Format: (sqlite_table, pg_table, {sqlite_col: pg_col} or None)
TABLES = [
    ("users",                    "users",                    None),
    ("categories",               "categories",               None),
    ("brands",                   "brands",                   None),
    ("attributes",               "attributes",               None),
    ("attribute_values",         "attribute_values",         None),
    ("media",                    "media",                    None),
    ("products",                 "products",                 None),
    ("product_images",           "product_images",           None),
    ("product_variations",       "product_variations",       None),
    ("variation_attribute_values","variation_attribute_values", None),
    ("product_attributes",       "product_attributes",       None),
    ("product_attribute_values", "product_attribute_values", None),
    ("product_reviews",          "product_reviews",          None),
    ("orders",                   "orders",                   None),
    ("order_items",              "order_items",              None),
    # coupons: old column names → new names
    ("coupons",                  "coupons", {
        "min_order": "min_order_amount",
        "max_uses":  "usage_limit",
        "per_user":  "usage_limit_per_user",
    }),
    ("coupon_usages",            "coupon_usages",            None),
    ("store_settings",           "store_settings",           None),
    ("user_addresses",           "user_addresses",           None),
    ("newsletter_subscribers",   "newsletter_subscribers",   None),
]


def connect_sqlite():
    path = SQLITE_PATH
    if not os.path.exists(path):
        # try sibling path
        alt = os.path.join(os.path.dirname(__file__), "..", "chasma.db")
        if os.path.exists(alt):
            path = alt
        else:
            raise FileNotFoundError(
                f"SQLite DB not found at {SQLITE_PATH!r} or {alt!r}. "
                "Set SQLITE_PATH env var or put chasma.db in the project root."
            )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def connect_pg():
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DBNAME,
        user=PG_USER,
        password=PG_PASSWORD,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def sqlite_tables(sq_conn):
    cur = sq_conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in cur.fetchall()}


def copy_table(sq_conn, pg_conn, sq_table, pg_table, col_remap):
    sq_cur = sq_conn.cursor()
    sq_cur.execute(f"SELECT * FROM {sq_table}")
    rows = sq_cur.fetchall()
    if not rows:
        print(f"  {sq_table}: 0 rows (skipped)")
        return

    # Build column list from SQLite result
    sq_cols = [desc[0] for desc in sq_cur.description]

    # Determine which SQLite cols exist in PG (get PG column names)
    pg_cur = pg_conn.cursor()
    pg_cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (pg_table,)
    )
    pg_col_set = {r["column_name"] for r in pg_cur.fetchall()}

    # Map SQLite column names → PG column names, skipping unknown cols
    mapped_cols = []   # (sq_col, pg_col)
    for sc in sq_cols:
        pc = (col_remap or {}).get(sc, sc)
        if pc in pg_col_set:
            mapped_cols.append((sc, pc))

    if not mapped_cols:
        print(f"  {sq_table}: no matching columns found, skipped")
        return

    pg_cols  = [mc[1] for mc in mapped_cols]
    sq_idxs  = [sq_cols.index(mc[0]) for mc in mapped_cols]
    placeholders = ", ".join(["%s"] * len(pg_cols))
    col_str      = ", ".join(pg_cols)
    insert_sql   = (
        f"INSERT INTO {pg_table} ({col_str}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )

    inserted = skipped = 0
    for row in rows:
        values = tuple(row[i] for i in sq_idxs)
        try:
            pg_cur.execute(insert_sql, values)
            inserted += 1
        except Exception as e:
            pg_conn.rollback()
            skipped += 1
            print(f"    WARN row skipped in {sq_table}: {e}")

    pg_conn.commit()
    print(f"  {sq_table}: {inserted} inserted, {skipped} skipped")


def main():
    print("=== Eyeconic SQLite to Supabase PostgreSQL Migration ===\n")

    print("Connecting to SQLite …")
    sq_conn = connect_sqlite()

    print("Connecting to Supabase PostgreSQL …")
    pg_conn = connect_pg()
    print("Connected.\n")

    print("Creating PostgreSQL schema …")
    pg_cur = pg_conn.cursor()
    # Execute schema statements one by one for clearer error messages
    for stmt in PG_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                pg_cur.execute(stmt)
                pg_conn.commit()
            except Exception as e:
                pg_conn.rollback()
                print(f"  WARN schema stmt failed: {e}")
    print("Schema ready.\n")

    existing_sq_tables = sqlite_tables(sq_conn)

    print("Copying data …")
    for sq_table, pg_table, col_remap in TABLES:
        if sq_table not in existing_sq_tables:
            print(f"  {sq_table}: not found in SQLite, skipped")
            continue
        copy_table(sq_conn, pg_conn, sq_table, pg_table, col_remap)

    sq_conn.close()
    pg_conn.close()
    print("\nMigration complete!")
    print("Next steps:")
    print("  1. Update DATABASE_URL in .env to the Supabase URL")
    print("  2. Delete chasma.db")
    print("  3. Deploy to Railway / Render")


if __name__ == "__main__":
    main()
