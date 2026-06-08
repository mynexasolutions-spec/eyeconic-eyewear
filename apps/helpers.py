import re
import functools
import time
import markupsafe
import cloudinary
import cloudinary.uploader
import db


def slugify(text):
    if not text:
        return ""
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def ttl_cache(ttl_seconds=60):
    def decorator(func):
        cache = {}

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            cached = cache.get(key)
            if cached and now - cached[0] < ttl_seconds:
                return cached[1]
            result = func(*args, **kwargs)
            cache[key] = (now, result)
            return result

        wrapper.cache_clear = cache.clear
        return wrapper
    return decorator


def get_unique_slug(table, base_slug, exclude_id=None):
    slug = base_slug or "item"
    counter = 1
    while True:
        query = f"SELECT id FROM {table} WHERE slug = ?"
        params = [slug]
        if exclude_id:
            query += " AND id != ?"
            params.append(exclude_id)
        if not db.query_one(query, params):
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def get_store_settings():
    try:
        rows = db.query("SELECT key, value FROM store_settings")
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {"cod_enabled": "true", "online_payment_enabled": "false"}


@ttl_cache(ttl_seconds=60)
def get_cached_store_settings():
    return get_store_settings()


def refresh_cart_prices(cart):
    refreshed = {}
    subtotal  = 0
    if not cart:
        return refreshed, subtotal

    product_ids = list({str(item.get("product_id", "")).strip()
                        for item in cart.values() if item.get("product_id")})
    if not product_ids:
        return refreshed, subtotal

    placeholders = ",".join(["?"] * len(product_ids))
    rows = db.query(
        f"SELECT id, name, sku, price, sale_price, stock_quantity, stock_status "
        f"FROM products WHERE id IN ({placeholders})",
        product_ids,
    )
    product_map = {str(r["id"]): r for r in rows}

    for key, item in cart.items():
        product_id = str(item.get("product_id", "")).strip()
        product    = product_map.get(product_id)
        if not product:
            continue
        price    = float(product.get("sale_price") or product.get("price") or 0)
        
        # Look up lens option price modifier if present
        lens_option_id = item.get("lens_option_id")
        if lens_option_id:
            try:
                lens_opt = db.query_one("SELECT price_modifier FROM lens_options WHERE id = ?", [lens_option_id])
                if lens_opt:
                    price += float(lens_opt["price_modifier"] or 0.0)
            except Exception:
                pass

        new_item = dict(item)
        new_item["price"] = price
        if not new_item.get("sku"):
            new_item["sku"] = product.get("sku", "")
        refreshed[key] = new_item
        subtotal += price * int(new_item.get("qty", 0))

    return refreshed, subtotal


# ─── Jinja2 globals / filters ──────────────────────────────────────────────────

import os

# Cloudinary is configured from the CLOUDINARY_URL environment variable
# which is loaded via python-dotenv at app startup.
cloudinary.config(from_url=os.getenv("CLOUDINARY_URL"))


def handle_upload(file, folder="chasma-gallery"):
    """Upload a file to Cloudinary and return its secure URL, or None on failure."""
    if not file or not file.filename:
        return None
    try:
        result = cloudinary.uploader.upload(
            file,
            folder=folder,
            resource_type="image",
            overwrite=False,
        )
        return result["secure_url"]
    except Exception as e:
        raise RuntimeError(f"Image upload failed: {e}") from e


CLOUDINARY_MAPPING = {
  "hero/hero1_classic_glasses.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410761/chasma-gallery/hero/wu2nu6affbnpbx1qp2qs.jpg",
  "hero/hero2_sunglasses.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410761/chasma-gallery/hero/flyndisgsvsdz5nm4mh5.jpg",
  "sections/cat_contacts.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410762/chasma-gallery/sections/jqjo2r5q5lmmz3swkfah.jpg",
  "sections/cat_eyewear.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410763/chasma-gallery/sections/qipkkszcot28lztdbmqj.jpg",
  "sections/promo_sunglasses.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410764/chasma-gallery/sections/ysfuj7eomzbpxtkatre2.jpg",
  "sections/shape_geometric.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410765/chasma-gallery/sections/xj8z9xoq7lkduxuxk6oj.jpg",
  "sections/shape_rectangle.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410765/chasma-gallery/sections/g9g8mo7a3xaxlyuxrkbf.jpg",
  "sections/shape_round.jpg": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410766/chasma-gallery/sections/lfhmk5a4itzqv8z2yraz.jpg",
  "about/Ameen_chasma.webp": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410767/chasma-gallery/about/mqlc5ukyblmdkwcnol5m.webp",
  "hero.png": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410768/chasma-gallery/static/rlw5brukbebp1w0ge3uh.jpg",
  "logo.png": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410769/chasma-gallery/static/socsmbmyepmlksqadees.png",
  "men.png": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410770/chasma-gallery/static/ti8vlzhnzkopmrs1h6tw.jpg",
  "placeholder.png": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410770/chasma-gallery/static/we9iq7o3zsg4axrtkwbi.png",
  "women.png": "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410772/chasma-gallery/static/abrqjacgak4pj7k4yjoj.jpg"
}

def resolve_image(image_url):
    from flask import url_for
    # Default Cloudinary Placeholder
    PLACEHOLDER = CLOUDINARY_MAPPING.get("placeholder.png", "https://res.cloudinary.com/dljlnkh6x/image/upload/v1777410770/chasma-gallery/static/we9iq7o3zsg4axrtkwbi.png")
    
    if not image_url:
        return PLACEHOLDER
    
    if image_url.startswith("http"):
        return image_url
        
    # Check mapping first
    clean_url = image_url.lstrip("/")
    if clean_url.startswith("images/"):
        clean_url = clean_url.replace("images/", "", 1)
        
    if clean_url in CLOUDINARY_MAPPING:
        return CLOUDINARY_MAPPING[clean_url]
        
    # If it's a relative path, we check if it's meant to be a static asset
    if image_url.startswith("/uploads/") or image_url.startswith("uploads/"):
        return url_for("static", filename=image_url.lstrip("/"))
        
    # Fallback to static images folder for remaining local assets
    return url_for("static", filename=f"images/{image_url.lstrip('/')}")


def _strip_html(text):
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = clean.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&gt;', '>').replace('&lt;', '<')
    clean = re.sub(r'\n+', '\n', clean)
    return re.sub(r' +', ' ', clean).strip()


def format_description(text):
    if not text:
        return ""
    text = _strip_html(text)
    text = text.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    rows, paras = [], []
    for line in [l.strip() for l in text.split("\n")]:
        if not line:
            continue
        m = re.match(r"^[•*-]?\s*(.+?)\s*:-\s*(.+)$", line)
        if m:
            rows.append((m.group(1).strip(), m.group(2).strip()))
        else:
            paras.append(re.sub(r"^[•*-]\s*", "", line))
    parts = []
    if rows:
        trs = "".join(
            f'<tr><td class="desc-key">{markupsafe.escape(k)}</td>'
            f'<td class="desc-val">{markupsafe.escape(v)}</td></tr>'
            for k, v in rows
        )
        parts.append(f'<table class="desc-table"><tbody>{trs}</tbody></table>')
    parts += [f'<p class="desc-para">{markupsafe.escape(p)}</p>' for p in paras]
    return markupsafe.Markup("".join(parts))


def format_short_desc(text):
    if not text:
        return ""
    text = _strip_html(text)
    text = text.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"^[•*-]\s*", "", l.strip()) for l in text.split("\n") if l.strip()]
    return markupsafe.Markup(
        "".join(f'<p class="short-desc-line">{markupsafe.escape(l)}</p>' for l in lines)
    )


def register_jinja(app):
    app.jinja_env.globals["resolve_image"] = resolve_image
    app.jinja_env.filters["format_description"] = format_description
    app.jinja_env.filters["format_short_desc"] = format_short_desc
