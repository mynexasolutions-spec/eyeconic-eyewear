import concurrent.futures
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response, jsonify
import db
from helpers import get_cached_store_settings
from queries import (
    get_products, get_categories, get_brands,
    get_product_detail,
    get_homepage_products, get_trending_shapes, get_featured_categories,
    get_home_sections_all, get_home_product_picks_all, get_products_by_ids,
    ensure_builtin_home_sections, HOME_BUILTIN_CAROUSEL_DEFAULTS,
)

# Master, whole-section on/off switches (Admin > Home Page > toggle on each
# card). Separate from the per-item is_active flags — this hides every item in
# the section at once, e.g. taking down all product carousels in one click.
HOME_SECTION_VISIBILITY_TYPES = ["hero", "category", "carousel", "policy", "banner", "stat", "testimonial", "instagram"]

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    try:
        ensure_builtin_home_sections()
    except Exception:
        pass

    # These 6 reads are independent of each other — fire them on separate
    # pooled connections concurrently instead of one after another (same
    # pattern as get_product_detail() in queries.py). Was the dominant cost
    # of the home page: ~6 sequential DB round trips collapse into ~1.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        f_products   = ex.submit(get_homepage_products)
        f_shapes     = ex.submit(get_trending_shapes)
        f_categories = ex.submit(get_featured_categories)
        f_sections   = ex.submit(get_home_sections_all)
        f_settings   = ex.submit(get_cached_store_settings)
        f_picks      = ex.submit(get_home_product_picks_all)

        try:
            data = f_products.result()
            featured              = data["featured"]
            latest                = data["latest"]
            popular               = data["popular"]
            promo1                = data["promo1"]
            promo2                = data["promo2"]
            men_products          = data["men"]
            women_products        = data["women"]
            kids_products         = data["kids"]
            sun_products          = data["sunglasses"]
            blue_products         = data["blue_light"]
            accessories_products  = data.get("accessories", [])
            optical_products      = data["optical"]
        except Exception as e:
            featured = latest = popular = promo1 = promo2 = []
            men_products = women_products = kids_products = sun_products = blue_products = accessories_products = optical_products = []
            flash(f"Data loading error: {e}", "error")

        try:
            trending_shapes = f_shapes.result()
        except Exception:
            trending_shapes = []

        try:
            featured_categories = f_categories.result()
        except Exception:
            featured_categories = []

        try:
            sections           = f_sections.result()
            hero_slides        = sections.get("hero", [])
            home_categories    = sections.get("category", [])
            home_stats         = sections.get("stat", [])
            home_banners       = sections.get("banner", [])
            home_testimonials  = sections.get("testimonial", [])
            home_instagram     = sections.get("instagram", [])
            home_carousel_defs = sections.get("carousel", [])
            home_shape_defs    = sections.get("shape", [])
            home_policy        = sections.get("policy", [])
        except Exception:
            hero_slides = home_categories = home_stats = home_banners = home_testimonials = home_instagram = []
            home_carousel_defs = []
            home_shape_defs = []
            home_policy = []

        try:
            settings = f_settings.result()
        except Exception:
            settings = {}

        try:
            all_picks = f_picks.result()
        except Exception:
            all_picks = {}

    # Master, whole-section switches (Admin > Home Page > the toggle on each
    # card's header). Checked before the per-item content so a section that's
    # turned off stays off even if it still has items/a fallback default.
    section_visible = {t: settings.get(f"home_visible_{t}", "true") != "false" for t in HOME_SECTION_VISIBILITY_TYPES}

    # Guard against a blank hero if the table is empty (e.g. an admin deleted every slide)
    if not section_visible["hero"]:
        hero_slides = []
    elif not hero_slides:
        hero_slides = [{
            "badge_text": "New Collection 2025", "title": "See The World", "subtitle": "Differently",
            "body": "Premium eyewear crafted for those who refuse to blend in.",
            "image_url": "https://images.unsplash.com/photo-1526045612212-70caf35c14df?w=1600&q=85&auto=format&fit=crop",
            "cta_text": "Shop Now", "cta_link": url_for("public.shop"),
            "cta2_text": "Explore Collection", "cta2_link": url_for("public.shop"),
        }]

    if not section_visible["category"]:
        home_categories = []
    elif not home_categories:
        home_categories = [
            {"title": "Men", "image_url": "man.webp", "cta_text": "Explore Now", "cta_link": url_for("public.shop", category="men")},
            {"title": "Women", "image_url": "woman.webp", "cta_text": "Explore Now", "cta_link": url_for("public.shop", category="women")},
            {"title": "Kids", "image_url": "kid.webp", "cta_text": "Explore Now", "cta_link": url_for("public.shop", category="kids")},
        ]

    if not section_visible["stat"]:
        home_stats = []
    if not section_visible["policy"]:
        home_policy = []
    if not section_visible["banner"]:
        home_banners = []
    if not section_visible["testimonial"]:
        home_testimonials = []
    if not section_visible["instagram"]:
        home_instagram = []

    # Section show/hide toggles. Shape, the 7 product carousels, and any custom
    # carousel are each driven by their own home_sections row's is_active flag
    # (Admin > Home Page), further gated by the "carousel" master switch above.
    builtin_carousels = {} if not section_visible["carousel"] else {
        c["id"]: c for c in home_carousel_defs if c["id"] in HOME_BUILTIN_CAROUSEL_DEFAULTS
    }
    home_shape = home_shape_defs[0] if home_shape_defs else None
    home_visible = {
        "shape": home_shape is not None,
        "hero": section_visible["hero"], "category": section_visible["category"],
        "instagram": section_visible["instagram"],
    }
    for key in HOME_BUILTIN_CAROUSEL_DEFAULTS:
        home_visible[key] = key in builtin_carousels

    # Manual product curation overrides (Admin > Home Page > Product Carousels)
    # all_picks was already fetched in the parallel block above.
    try:
        picked = all_picks.get("bestsellers")
        if picked:
            featured = get_products_by_ids(picked)
        picked = all_picks.get("men")
        if picked:
            men_products = get_products_by_ids(picked)
        picked = all_picks.get("women")
        if picked:
            women_products = get_products_by_ids(picked)
        picked = all_picks.get("kids")
        if picked:
            kids_products = get_products_by_ids(picked)
        picked = all_picks.get("accessories")
        if picked:
            accessories_products = get_products_by_ids(picked)
        picked = all_picks.get("sunglasses")
        if picked:
            sun_products = get_products_by_ids(picked)
        picked = all_picks.get("eyeglasses")
        if picked:
            optical_products = get_products_by_ids(picked)
    except Exception:
        pass

    # Custom, admin-defined product carousels (e.g. "Premium Glasses"). The 7
    # built-in ones are excluded here — they render in their own fixed section
    # blocks above using builtin_carousels, not this generic loop. A carousel
    # with no products picked yet is skipped — there's no automatic fallback for
    # an arbitrary custom carousel the way there is for the built-in sections.
    home_carousels = []
    try:
        if section_visible["carousel"]:
            for c in home_carousel_defs:
                if c["id"] in HOME_BUILTIN_CAROUSEL_DEFAULTS:
                    continue
                products = get_products_by_ids(all_picks.get(c["id"], []))
                if products:
                    home_carousels.append({**c, "products": products})
    except Exception:
        home_carousels = []

    return render_template(
        "index.html",
        featured=featured, latest=latest, popular=popular,
        promo1=promo1, promo2=promo2,
        men_products=men_products, women_products=women_products,
        kids_products=kids_products, sun_products=sun_products,
        blue_products=blue_products, accessories_products=accessories_products,
        optical_products=optical_products,
        trending_shapes=trending_shapes,
        home_categories=home_categories,
        featured_categories=featured_categories,
        hero_slides=hero_slides, home_stats=home_stats, home_banners=home_banners,
        home_testimonials=home_testimonials, home_instagram=home_instagram,
        home_visible=home_visible, home_carousels=home_carousels,
        builtin_carousels=builtin_carousels, home_shape=home_shape, home_policy=home_policy,
    )


@bp.route("/shop")
def shop():
    search          = request.args.get("search", request.args.get("q", "")).strip()
    selected_cats   = tuple(s for s in request.args.getlist("category") if s)
    selected_brands = tuple(s for s in request.args.getlist("brand")    if s)
    sort            = request.args.get("sort", "created_at_desc")
    shape           = request.args.get("shape", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    on_sale         = bool(request.args.get("on_sale", ""))
    featured        = bool(request.args.get("featured", ""))
    min_price       = request.args.get("min_price", "").strip()
    max_price       = request.args.get("max_price", "").strip()
    try:
        min_price_val = float(min_price) if min_price else None
        max_price_val = float(max_price) if max_price else None
    except ValueError:
        min_price_val = max_price_val = None
    # Independent reads — fire concurrently instead of one after another
    # (same pattern as index()/get_product_detail()).
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_products = ex.submit(
            get_products,
            search=search, categories=selected_cats, brands=selected_brands,
            shape=shape, sort=sort, page=page, per_page=18, on_sale=on_sale,
            featured=featured, min_price=min_price_val, max_price=max_price_val,
        )
        f_categories = ex.submit(get_categories)
        f_brands     = ex.submit(get_brands)
        f_shapes     = ex.submit(get_trending_shapes)

        try:
            products, total, total_pages = f_products.result()
            all_categories = f_categories.result()
            all_brands     = f_brands.result()
            trending_shapes = f_shapes.result()
        except Exception as e:
            products, total, total_pages = [], 0, 1
            all_categories = all_brands = trending_shapes = []
            flash(f"Database error: {e}", "error")

    # Build parent → children tree for the sidebar accordion
    parent_cats  = [c for c in all_categories if not c.get("parent_id")]
    children_map = {}
    for c in all_categories:
        pid = c.get("parent_id")
        if pid:
            children_map.setdefault(str(pid), []).append(c)

    return render_template(
        "shop.html",
        products=products, total_count=total, total_pages=total_pages,
        current_page=page,
        categories=all_categories, brands=all_brands,
        parent_cats=parent_cats, children_map=children_map,
        search=search,
        current_categories=selected_cats,
        current_brands=selected_brands,
        current_sort=sort, current_shape=shape,
        on_sale=on_sale,
        trending_shapes=trending_shapes,
        min_price=min_price, max_price=max_price,
    )


@bp.route("/product/<product_id>")
def product_detail(product_id):
    try:
        # related products + lens options are now fetched inside
        # get_product_detail(), concurrently with everything else, instead
        # of as separate sequential calls after it returns.
        product, images, variations, reviews, attributes, related, lens_types = get_product_detail(product_id)
    except Exception as e:
        flash(f"Error loading product: {e}", "error")
        return redirect(url_for("public.shop"))
    if not product:
        abort(404)


    return render_template(
        "product.html",
        product=product, images=images, variations=variations,
        reviews=reviews, attributes=attributes, related=related,
        lens_types=lens_types,
    )


@bp.route("/category/<slug>")
def category_page(slug):
    return redirect(url_for("public.shop", category=slug))


@bp.route("/brand/<slug>")
def brand_page(slug):
    return redirect(url_for("public.shop", brand=slug))


@bp.route("/about")
def about():
    return render_template("about.html")


@bp.route("/privacy-policy")
@bp.route("/terms")
@bp.route("/terms-and-privacy")
def terms_and_privacy():
    return render_template("terms_and_privacy.html")


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        email   = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()
        if not all([name, email, message]):
            flash("Please fill in all required fields.", "error")
        else:
            flash("Thank you for your message! We'll get back to you soon.", "success")
            return redirect(url_for("public.contact"))
    return render_template("contact.html")


@bp.route("/subscribe", methods=["POST"])
def subscribe():
    email = request.form.get("email", "").strip()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in (request.headers.get("Accept") or "")
    if email:
        try:
            db.execute("INSERT INTO newsletter_subscribers (email) VALUES (?) ON CONFLICT (email) DO NOTHING", [email])
            message = "Thank you for subscribing to our newsletter!"
            if is_ajax:
                return jsonify({"success": True, "message": message})
            flash(message, "success")
        except Exception as e:
            message = "An error occurred while subscribing."
            if is_ajax:
                return jsonify({"success": False, "message": message}), 500
            flash(message, "error")
    else:
        message = "Please enter a valid email address."
        if is_ajax:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "error")
    return redirect(request.referrer or url_for("public.index"))


@bp.route("/sitemap.xml")
def sitemap():
    base = request.host_url.rstrip("/")

    static_pages = [
        ("",         "1.0", "daily"),
        ("/shop",    "0.9", "daily"),
        ("/about",   "0.7", "monthly"),
        ("/contact", "0.7", "monthly"),
    ]

    try:
        products = db.query(
            "SELECT id, updated_at FROM products WHERE is_active = 1 ORDER BY updated_at DESC"
        )
    except Exception:
        products = []

    try:
        categories = db.query(
            "SELECT slug FROM categories WHERE is_active = 1"
        )
    except Exception:
        categories = []

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    for path, priority, freq in static_pages:
        lines.append(
            f"  <url><loc>{base}{path}</loc>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{priority}</priority></url>"
        )

    for p in products:
        updated = p.get("updated_at")
        lastmod = f"<lastmod>{updated.strftime('%Y-%m-%d')}</lastmod>" if updated else ""
        lines.append(
            f"  <url><loc>{base}/product/{p['id']}</loc>"
            f"{lastmod}<changefreq>weekly</changefreq>"
            f"<priority>0.8</priority></url>"
        )

    for c in categories:
        lines.append(
            f"  <url><loc>{base}/shop?category={c['slug']}</loc>"
            f"<changefreq>weekly</changefreq>"
            f"<priority>0.7</priority></url>"
        )

    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="application/xml")


@bp.route("/robots.txt")
def robots():
    base = request.host_url.rstrip("/")
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /cart\n"
        "Disallow: /checkout\n"
        "Disallow: /account\n"
        "Disallow: /login\n"
        "Disallow: /register\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(content, mimetype="text/plain")
