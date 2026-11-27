import secrets
"""Multi-site core: detection, isolation, and management.

Every request is scoped to a site (holding company or subsidiary) based on
the URL prefix:

    /holding/...   → holding company
    /acme/...      → Acme subsidiary
    /foo/...       → Foo subsidiary

Users belong to a site. A user from Acme cannot access Foo's routes.
Holding-company administrators can access any site's admin area.
"""
import os
import re
import sqlite3

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from app import (DB_PATH, LEVELS, admin_required, app, audit, get_db, iso, now)


SITES_DIR = None  # set in init_sites()


# ---- schema -------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  tagline TEXT,
  body_color TEXT DEFAULT '#ffffff',
  navbar_color TEXT DEFAULT '#000000',
  navbar_text_color TEXT DEFAULT '#ffffff',
  logo_ext TEXT,
  is_holding INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_slug TEXT NOT NULL,
  account_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'staff',
  created_at TEXT NOT NULL,
  UNIQUE(site_slug, account_id)
);
"""


def init_sites():
    """Create the sites tables and ensure a holding site exists."""
    global SITES_DIR
    from app import DATA
    SITES_DIR = DATA / "sites"
    SITES_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    have = con.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    if have == 0:
        con.execute(
            "INSERT INTO sites (slug, name, is_holding, created_at) VALUES (?,?,1,?)",
            ("holding", os.environ.get("SITE_NAME", "Your Holding Company"), iso(now())))
    con.commit()
    con.close()


init_sites()


def load_site_to_g():
    """Called from a before_request handler. Reads the (already rewritten)
    SCRIPT_NAME and sets g.site, g.site_slug, g.url_prefix."""
    script_name = request.environ.get("SCRIPT_NAME", "")
    site_slug = script_name.lstrip("/") or "holding"

    db = get_db()
    site = db.execute(
        "SELECT * FROM sites WHERE slug=? AND is_active=1", (site_slug,)).fetchone()
    if site:
        g.site = site
        g.site_slug = site["slug"]
        g.url_prefix = f"/{site['slug']}"
    else:
        holding = db.execute(
            "SELECT * FROM sites WHERE is_holding=1 AND is_active=1").fetchone()
        g.site = holding
        g.site_slug = holding["slug"] if holding else "holding"
        g.url_prefix = f"/{g.site_slug}" if holding else ""

# ---- site detection middleware ------------------------------------------
def detect_site():
    """Runs before every request. Reads the URL prefix, looks up the site,
    and rewrites the path so Flask routes match the existing handlers."""
    # Static files and health checks: no site context needed
    if request.path.startswith("/static/") or request.path in ("/healthz", "/favicon.ico"):
        g.site = None
        g.site_slug = None
        g.url_prefix = ""
        return

    db = get_db()
    # Find the site whose slug matches the first path segment
    parts = request.path.lstrip("/").split("/", 1)
    first = parts[0].lower() if parts else ""
    site = db.execute(
        "SELECT * FROM sites WHERE slug=? AND is_active=1", (first,)).fetchone()

    if site:
        g.site = site
        g.site_slug = site["slug"]
        g.url_prefix = f"/{site['slug']}"
        # Rewrite the path so Flask's routing sees the un-prefixed route
        request.environ["SCRIPT_NAME"] = f"/{site['slug']}"
        rest = "/" + parts[1] if len(parts) > 1 else "/"
        request.environ["PATH_INFO"] = rest
    else:
        # No prefix: fall back to holding company
        holding = db.execute(
            "SELECT * FROM sites WHERE is_holding=1 AND is_active=1").fetchone()
        if holding:
            g.site = holding
            g.site_slug = holding["slug"]
            g.url_prefix = f"/{holding['slug']}"
            # Redirect legacy URLs (e.g. /admin) to the holding prefix (/holding/admin)
            if request.method == "GET" and not request.path.startswith(f"/{holding['slug']}"):
                if not request.path.startswith("/static/") and request.path not in ("/healthz",):
                    new_path = f"/{holding['slug']}{request.path}"
                    query = request.query_string.decode()
                    if query:
                        new_path += "?" + query
                    return redirect(new_path)
        else:
            abort(500, "No sites configured")


class SiteMiddleware:
    """WSGI middleware that detects the site prefix and rewrites the path
    BEFORE Flask matches the URL. This is what makes /acme/admin route to
    the /admin handler."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")

        # Static files and health checks pass through
        if path.startswith("/static/") or path in ("/healthz", "/favicon.ico"):
            return self.app(environ, start_response)

        # Look up the site from the first path segment
        parts = path.lstrip("/").split("/", 1)
        first = parts[0].lower() if parts else ""

        import sqlite3
        from app import DB_PATH
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        site = con.execute(
            "SELECT slug FROM sites WHERE slug=? AND is_active=1", (first,)).fetchone()
        con.close()

        if site:
            # Rewrite: strip /<slug> from PATH_INFO, add to SCRIPT_NAME
            environ["SCRIPT_NAME"] = f"/{first}"
            rest = "/" + parts[1] if len(parts) > 1 else "/"
            environ["PATH_INFO"] = rest
        else:
            # No prefix: redirect to holding
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            holding = con.execute(
                "SELECT slug FROM sites WHERE is_holding=1 AND is_active=1").fetchone()
            con.close()
            if holding and path != "/" and not path.startswith("/static/"):
                # Preserve existing route behavior for legacy URLs — redirect
                from urllib.parse import quote
                query = environ.get("QUERY_STRING", "")
                location = f"/{holding['slug']}{path}"
                if query:
                    location += "?" + query
                if environ.get("REQUEST_METHOD") == "GET":
                    start_response("302 Found", [("Location", location)])
                    return [b""]

        return self.app(environ, start_response)


# ---- access control middleware ------------------------------------------
def enforce_site_access():
    """Runs after load_user (so g.user is set). Blocks cross-site access."""
    # Site detection must have run
    if not hasattr(g, "site") or g.site is None:
        return

    user = g.get("user")
    if not user:
        return  # public routes are open

    # Determine the user's own site
    user_site = user["site_slug"] if "site_slug" in user.keys() else "holding"

    # Holding admins can access any site
    if _is_holding_admin(user):
        return

    # Non-holding user: can only access their own site
    if user_site != g.site_slug:
        abort(403, "You do not have access to that site.")


def _is_holding_admin(user):
    """True if this user belongs to the holding company and is an admin."""
    if not user:
        return False
    if not user["is_admin"]:
        return False
    user_site = user["site_slug"] if "site_slug" in user.keys() else "holding"
    holding = get_db().execute(
        "SELECT slug FROM sites WHERE is_holding=1").fetchone()
    return holding and user_site == holding["slug"]


# ---- brand helper -------------------------------------------------------
def get_brand():
    """Return the current site's brand. Falls back to a neutral default."""
    site = g.get("site")
    if site:
        return {
            "name": site["name"],
            "tagline": site["tagline"] or "",
            "has_logo": bool(site["logo_ext"]),
            "logo_url": url_for("site_logo", slug=site["slug"]) if site["logo_ext"] else "",
        }
    return {"name": "Your Company", "tagline": "", "has_logo": False, "logo_url": ""}


def get_theme():
    """Return the current site's colour scheme."""
    site = g.get("site")
    if site:
        return {
            "body_color": site["body_color"] or "#ffffff",
            "navbar_color": site["navbar_color"] or "#000000",
            "navbar_text_color": site["navbar_text_color"] or "#ffffff",
            "button_follow_navbar": "1",
            "button_color": "",
            "button_text_color": "",
        }
    return {
        "body_color": "#ffffff",
        "navbar_color": "#000000",
        "navbar_text_color": "#ffffff",
        "button_follow_navbar": "1",
        "button_color": "",
        "button_text_color": "",
    }


# ---- query scoping helper -----------------------------------------------
def scoped(sql, *args, include_holding=False):
    """Return (sql_with_site_filter, args) appropriate for the current user.

    For holding admins: returns the SQL unchanged (they see everything).
    For everyone else: adds `AND site_slug = ?` with the current site.

    Use everywhere you query site-scoped tables.
    """
    user = g.get("user")
    if user and _is_holding_admin(user) and include_holding:
        return sql, list(args)
    return sql + " AND site_slug = ?", list(args) + [g.site_slug]


# ---- public routes ------------------------------------------------------
@app.route("/<slug>/branding/logo")
def site_logo(slug):
    """Serve a site's logo file from data/sites/<slug>/logo.<ext>."""
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None or not site["logo_ext"]:
        abort(404)
    from flask import send_from_directory
    folder = SITES_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    return send_from_directory(folder, f"logo.{site['logo_ext']}",
                               max_age=3600)


@app.route("/<slug>/branding/theme.css")
def site_theme_css(slug):
    """Serve the site-specific theme CSS."""
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None:
        abort(404)
    from flask import Response
    css = (
        ":root{"
        "--bg:" + (site["body_color"] or "#ffffff") + ";"
        "--ink-navbar:" + (site["navbar_color"] or "#000000") + ";"
        "--ink-navbar-text:" + (site["navbar_text_color"] or "#ffffff") + ";"
        "}"
        ".top{background:var(--ink-navbar);color:var(--ink-navbar-text)}"
        ".top nav a{color:var(--ink-navbar-text);opacity:.85}"
        ".top nav a:hover,.top nav a:focus-visible{opacity:1;border-bottom-color:var(--ink-navbar-text)}"
        ".top .brand span{color:var(--ink-navbar-text)}"
    )
    return Response(css, mimetype="text/css",
                    headers={"Cache-Control": "public, max-age=300"})


# ---- admin: manage subsidiaries -----------------------------------------
@app.route("/admin/sites")
@admin_required
def admin_sites():
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sites ORDER BY is_holding DESC, name").fetchall()
    return render_template("admin_sites.html", sites=rows)


@app.route("/admin/sites/new", methods=["GET", "POST"])
@admin_required
def admin_sites_new():
    if not _is_holding_admin(g.user):
        abort(403)
    if request.method == "POST":
        slug = re.sub(r"[^a-z0-9-]", "-",
                      request.form.get("slug", "").strip().lower())[:40].strip("-")
        name = request.form.get("name", "").strip()[:80]
        if not slug or not name:
            flash("Enter a name and a URL slug.", "error")
            return render_template("admin_site_form.html", site=request.form, is_new=True)
        if slug in ("holding", "admin", "static", "api", "p", "branding"):
            flash("That slug is reserved.", "error")
            return render_template("admin_site_form.html", site=request.form, is_new=True)
        db = get_db()
        if db.execute("SELECT 1 FROM sites WHERE slug=?", (slug,)).fetchone():
            flash("That slug is already in use.", "error")
            return render_template("admin_site_form.html", site=request.form, is_new=True)
        db.execute(
            "INSERT INTO sites (slug, name, tagline, body_color, navbar_color, "
            "navbar_text_color, is_holding, created_at) VALUES (?,?,?,?,?,?,0,?)",
            (slug, name, request.form.get("tagline", "").strip()[:200],
             request.form.get("body_color", "#ffffff"),
             request.form.get("navbar_color", "#000000"),
             request.form.get("navbar_text_color", "#ffffff"),
             iso(now())))
        (SITES_DIR / slug).mkdir(parents=True, exist_ok=True)
        audit(g.user["id"], "site_created", slug, name)
        db.commit()
        flash(f"Subsidiary '{name}' created at /{slug}/", "ok")
        return redirect(url_for("admin_sites"))
    return render_template("admin_site_form.html", site={}, is_new=True)


@app.route("/admin/sites/<slug>/edit", methods=["GET", "POST"])
@admin_required
def admin_sites_edit(slug):
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        if not name:
            flash("The name cannot be empty.", "error")
            return render_template("admin_site_form.html", site=dict(site), is_new=False)
        db.execute(
            "UPDATE sites SET name=?, tagline=?, body_color=?, navbar_color=?, "
            "navbar_text_color=?, is_active=? WHERE slug=?",
            (name, request.form.get("tagline", "").strip()[:200],
             request.form.get("body_color", "#ffffff"),
             request.form.get("navbar_color", "#000000"),
             request.form.get("navbar_text_color", "#ffffff"),
             1 if request.form.get("is_active") == "1" else 0,
             slug))
        audit(g.user["id"], "site_edited", slug, name)
        db.commit()
        flash("Site updated.", "ok")
        return redirect(url_for("admin_sites"))
    return render_template("admin_site_form.html", site=dict(site), is_new=False)


@app.route("/admin/sites/<slug>/logo", methods=["POST"])
@admin_required
def admin_sites_logo(slug):
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None:
        abort(404)
    file = request.files.get("logo")
    if not file or not file.filename:
        flash("Choose a logo file.", "error")
        return redirect(url_for("admin_sites_edit", slug=slug))
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "svg"):
        flash("Logo must be PNG, JPG, WEBP, or SVG.", "error")
        return redirect(url_for("admin_sites_edit", slug=slug))
    folder = SITES_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    # Remove old logo file if present
    for old_ext in ("png", "jpg", "jpeg", "webp", "svg"):
        (folder / f"logo.{old_ext}").unlink(missing_ok=True)
    file.save(folder / f"logo.{ext}")
    db.execute("UPDATE sites SET logo_ext=? WHERE slug=?", (ext, slug))
    audit(g.user["id"], "site_logo_uploaded", slug, ext)
    db.commit()
    flash("Logo updated.", "ok")
    return redirect(url_for("admin_sites_edit", slug=slug))

# ---- admin: manage subsidiary admins -------------------------------------
@app.route("/<slug>/admin/sites/admins")
@app.route("/holding/admin/sites/<slug>/admins")
@admin_required
def admin_site_admins(slug):
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None:
        abort(404)
    admins = db.execute(
        "SELECT * FROM employees WHERE site_slug=? AND is_admin=1 ORDER BY full_name",
        (slug,)
    ).fetchall()
    return render_template("admin_site_admins.html",
                           site=site, admins=admins)


@app.route("/holding/admin/sites/<slug>/admins/new", methods=["POST"])
@admin_required
def admin_site_admins_new(slug):
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None:
        abort(404)

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    password = request.form.get("password", "")

    errors = []
    if not full_name:
        errors.append("Full name is required.")
    if not password or len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    elif not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        errors.append("Password must contain letters and numbers.")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors.append("Email looks invalid.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin_site_admins", slug=slug))

    # Generate a unique employee ID for this site
    ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    yy = now().strftime("%y")
    for _ in range(100):
        eid = f"ADM-{yy}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(4))
        if not db.execute("SELECT 1 FROM employees WHERE id=?", (eid,)).fetchone():
            break
    else:
        flash("Could not generate a unique ID. Try again.", "error")
        return redirect(url_for("admin_site_admins", slug=slug))

    from werkzeug.security import generate_password_hash
    db.execute(
        "INSERT INTO employees (id, full_name, email, phone, level, password_hash, "
        "must_change_password, is_admin, site_slug, created_at, created_by) "
        "VALUES (?, ?, ?, ?, 'ADM', ?, 0, 1, ?, ?, ?)",
        (eid, full_name, email, phone, generate_password_hash(password),
         slug, iso(now()), g.user["id"]),
    )
    # Register in site_users if it exists
    try:
        db.execute(
            "INSERT OR IGNORE INTO site_users (site_slug, account_id, role, created_at) "
            "VALUES (?, ?, 'admin', ?)",
            (slug, eid, iso(now())),
        )
    except Exception:
        pass

    audit(g.user["id"], "site_admin_created", slug, f"{full_name} ({eid})")
    db.commit()
    commit_github(f"Site admin created: {eid} for {slug}")

    flash(f"Admin created for {site['name']}.", "ok")
    flash(f"ID: {eid} — give this and the password you set to the new admin.", "ok")
    return redirect(url_for("admin_site_admins", slug=slug))


@app.post("/holding/admin/sites/<slug>/admins/<eid>/delete")
@admin_required
def admin_site_admins_delete(slug, eid):
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if site is None:
        abort(404)
    emp = db.execute(
        "SELECT * FROM employees WHERE id=? AND site_slug=? AND is_admin=1",
        (eid, slug)
    ).fetchone()
    if emp is None:
        abort(404)
    # Prevent deleting the last admin of a site
    count = db.execute(
        "SELECT COUNT(*) FROM employees WHERE site_slug=? AND is_admin=1",
        (slug,)
    ).fetchone()[0]
    if count <= 1:
        flash("Cannot delete the last admin of this site.", "error")
        return redirect(url_for("admin_site_admins", slug=slug))
    db.execute("DELETE FROM employees WHERE id=?", (eid,))
    try:
        db.execute("DELETE FROM site_users WHERE site_slug=? AND account_id=?", (slug, eid))
    except Exception:
        pass
    audit(g.user["id"], "site_admin_deleted", slug, eid)
    db.commit()
    flash(f"Admin {eid} removed from {site['name']}.", "ok")
    return redirect(url_for("admin_site_admins", slug=slug))

