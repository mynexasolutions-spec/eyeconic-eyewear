"""
app.py — Application factory for ChasmaGallery.
"""
import os
from flask import Flask, render_template, session, request
from flask_compress import Compress
from dotenv import load_dotenv

load_dotenv()

from extensions import csrf, limiter, handle_csrf_error
from helpers import register_jinja
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
        return {"cart_count": count, "current_user": session.get("user")}

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

    @app.route('/debug-static')
    def debug_static():
        import os
        return {
            "static_folder": app.static_folder,
            "root_path": app.root_path,
            "cwd": os.getcwd(),
            "static_exists": os.path.exists(app.static_folder) if app.static_folder else False,
            "logo_exists": os.path.exists(os.path.join(app.static_folder, "images/logo.jpg")) if app.static_folder and os.path.exists(app.static_folder) else False,
            "files_in_static": os.listdir(app.static_folder) if app.static_folder and os.path.exists(app.static_folder) else []
        }

    @app.route('/debug-reviews')
    def debug_reviews():
        import inspect
        import queries
        try:
            source = inspect.getsource(queries.get_product_detail)
            product, images, variations, reviews, attributes = queries.get_product_detail("3ec11ff6-cb24-408f-b4af-3160c9606a0d")
            return {
                "source": source,
                "num_reviews": len(reviews),
                "reviews": [{k: str(v) for k, v in r.items()} for r in reviews] if reviews else []
            }
        except Exception as e:
            return {"error": str(e)}

    return app


app = create_app()

import os as _os
if _os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    try:
        db.migrate()
    except Exception as _e:
        print(f"[db.migrate] {_e}")

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_ENV", "development") != "production"
    app.run(debug=debug, port=port, host="0.0.0.0")
