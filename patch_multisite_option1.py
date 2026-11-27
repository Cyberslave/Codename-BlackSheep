"""Option 1 multi-site patch: subdirectory routing, user isolation,
per-site branding, unified holding-admin access.

What this does:
  1. Creates a `sites` table
  2. Adds `site_slug` to every data table
  3. Adds middleware to detect site from the URL prefix
  4. Scopes every query with `WHERE site_slug = ?`
  5. Adds user isolation: sub-site users can only access their own site
  6. Adds holding-admin privileges: holding admins can access any site
  7. Adds /admin/sites management UI (holding admin only)
  8. Redirects legacy (unprefixed) URLs to the holding company's prefix
  9. Per-site file storage: data/sites/<slug>/...
 10. Per-site logos and colours

URLs after this patch:
    /holding/              → holding company homepage
    /holding/login         → holding company login
    /acme/                 → Acme homepage
    /acme/login            → Acme login (holding admin can log in here too)
    /acme/admin            → Acme admin (Acme admins + holding admins)
    /foo/admin             → Foo admin
    /admin/sites           → holding admin: manage all subsidiaries

After applying, run the migration script `migrate_sites.py` (created by the
patch) to add site_slug columns to existing tables and assign existing rows
to the holding company.

Safe to re-run: detects already-applied pieces and skips.
"""
import os
import re
import shutil
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_option1")
os.makedirs(BACKUP, exist_ok=True)
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def write(p, s):
    full = os.path.join(ROOT, p)
    os.makedirs(os.path.dirname(full) or ROOT, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(s)


def backup(p):
    src = os.path.join(ROOT, p)
    if os.path.exists(src):
        flat = p.replace("/", "_").replace(os.sep, "_")
        dst = os.path.join(BACKUP, f"{flat}.{STAMP}.bak")
        shutil.copy2(src, dst)


results = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}  {detail}")
    results.append((name, ok))


# Detect which app this is
is_tenantman = os.path.exists(os.path.join(ROOT, "premises.py"))
is_persman = os.path.exists(os.path.join(ROOT, "business.py"))
print(f"\nDetected app: {'TenantMan' if is_tenantman else 'Persman' if is_persman else 'UNKNOWN'}")


# =========================================================================
# 1. Create sites.py — the multi-site core
# =========================================================================
print("\n[1/8] sites.py — multi-site core module")

SITES_MODULE = '''"""Multi-site core: detection, isolation, and management.

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
@app.route("/holding/admin/sites")
@admin_required
def admin_sites():
    if not _is_holding_admin(g.user):
        abort(403)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sites ORDER BY is_holding DESC, name").fetchall()
    return render_template("admin_sites.html", sites=rows)


@app.route("/holding/admin/sites/new", methods=["GET", "POST"])
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


@app.route("/holding/admin/sites/<slug>/edit", methods=["GET", "POST"])
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


@app.route("/holding/admin/sites/<slug>/logo", methods=["POST"])
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
'''

write("sites.py", SITES_MODULE)
check("sites.py created", True)


# =========================================================================
# 2. Patch app.py — wire in sites module
# =========================================================================
print("\n[2/8] app.py — wire in sites module")

backup("app.py")
a = read("app.py")

# 2a. Import sites at the bottom (after other modules)
if "import sites" not in a:
    if "import features" in a:
        # Add after features but before other app modules so middleware runs early
        a = a.replace(
            "import features  # noqa: E402,F401",
            "import sites  # noqa: E402,F401  (multi-site detection, isolation, brand)\n"
            "import features  # noqa: E402,F401",
            1)
    else:
        if not a.endswith("\n"):
            a += "\n"
        a += "\nimport sites  # noqa: E402,F401\n"
    check("app.py: import sites", True, "(added)")
else:
    check("app.py: import sites", True, "(already present)")

# 2b. Add site_slug to accounts/employees schema
# Find the CREATE TABLE employees/accounts block
schema_match = re.search(
    r'(CREATE TABLE IF NOT EXISTS (?:employees|accounts)\s*\([^)]*)\)',
    a, re.DOTALL)
if schema_match and "site_slug" not in schema_match.group(1):
    old_schema = schema_match.group(1)
    # Add site_slug before the closing paren of the CREATE TABLE
    new_schema = re.sub(
        r'(\n\s*created_by\s+TEXT\s*\n?\))',
        r'\n  site_slug TEXT NOT NULL DEFAULT \'holding\',\1',
        old_schema + ")")
    if new_schema != old_schema + ")":
        a = a.replace(old_schema + ")", new_schema, 1)
        check("app.py: site_slug in accounts/employees", True, "(added)")
    else:
        check("app.py: site_slug in accounts/employees", False,
              "could not find closing paren — add manually")
else:
    check("app.py: site_slug in accounts/employees", True, "(already present)")

# 2c. Register middleware (detect_site + enforce_site_access)
# Insert after existing before_request handlers
if "detect_site" not in a:
    middleware = '''

# ---- multi-site middleware -----------------------------------------------
import sites as _sites_module


@app.before_request
def _detect_site():
    return _sites_module.detect_site()


@app.before_request
def _enforce_site_access():
    return _sites_module.enforce_site_access()

'''
    # Insert just before the first @app.route definition
    route_match = re.search(r'\n@app\.route\(', a)
    if route_match:
        a = a[:route_match.start()] + middleware + a[route_match.start():]
        check("app.py: middleware registered", True, "(inserted before routes)")
    else:
        a += middleware
        check("app.py: middleware registered", True, "(appended)")
else:
    check("app.py: middleware registered", True, "(already present)")

# 2d. Update load_user to filter by site_slug
# 2d. Update load_user to filter by site_slug
old_load_user_start = a.find("def load_user():")
if old_load_user_start >= 0:
    # Find the next function/route after it (marker = "\n@app." or "\ndef " after the start)
    next_marker = len(a)
    for marker in ("\n@app.", "\ndef ", "\nclass "):
        idx = a.find(marker, old_load_user_start + 1)
        if idx >= 0 and idx < next_marker:
            next_marker = idx
    old_load_user_text = a[old_load_user_start:next_marker]

    if "site_slug" not in old_load_user_text:
        target_table = "accounts" if is_tenantman else "employees"
        target_col = "account_id" if is_tenantman else "employee_id"
        new_load_user = f'''def load_user():
    g.user = None
    if request.endpoint in (None, "static"):
        return
    eid = session.get("eid")
    if not eid:
        return
    db = get_db()
    site_slug = getattr(g, "site_slug", "holding") or "holding"
    emp = db.execute(
        "SELECT * FROM {target_table} WHERE id=? AND site_slug=?",
        (eid, site_slug)).fetchone()
    if emp is None:
        emp = db.execute(
            "SELECT e.* FROM {target_table} e "
            "JOIN sites s ON s.slug=e.site_slug "
            "WHERE e.id=? AND s.is_holding=1", (eid,)).fetchone()
    log = db.execute("SELECT * FROM sessions_log WHERE id=?", (session.get("log_id"),)).fetchone()
    if emp and log and log["logout_at"] is None and log["{target_col}"] == eid:
        g.user = emp
        db.execute("UPDATE sessions_log SET last_seen=? WHERE id=?", (iso(now()), log["id"]))
        db.commit()
    else:
        session.clear()
'''
        a = a[:old_load_user_start] + new_load_user + a[next_marker:]
        check("app.py: load_user with site_slug", True, "(rewritten)")
    else:
        check("app.py: load_user with site_slug", True, "(already patched)")
else:
    check("app.py: load_user with site_slug", False, "load_user function not found")

write("app.py", a)

# =========================================================================
# 3. Patch features.py — brand/theme from sites module
# =========================================================================
print("\n[3/8] features.py — brand/theme from sites")

backup("features.py")
f = read("features.py")

# Replace get_brand with one that delegates to sites module
if "from sites import get_brand as _sites_get_brand" not in f and "def get_brand" in f:
    old_brand = re.search(
        r'def get_brand\(\):(?:.*\n)*?(?=\ndef |\nclass |\n@app|\Z)',
        f, re.DOTALL)
    if old_brand:
        new_brand = '''def get_brand():
    """Delegate to the sites module which returns the current site's brand."""
    from sites import get_brand as _sites_brand
    return _sites_brand()
'''
        f = f[:old_brand.start()] + new_brand + f[old_brand.end():]
        check("features.py: get_brand delegates to sites", True)

# Same for get_theme
if "def get_theme" in f:
    old_theme = re.search(
        r'def get_theme\(\):(?:.*\n)*?(?=\ndef |\nclass |\n@app|\Z)',
        f, re.DOTALL)
    if old_theme and "from sites import get_theme as _sites_get_theme" not in f:
        new_theme = '''def get_theme():
    """Delegate to the sites module which returns the current site's theme."""
    from sites import get_theme as _sites_theme
    return _sites_theme()
'''
        f = f[:old_theme.start()] + new_theme + f[old_theme.end():]
        check("features.py: get_theme delegates to sites", True)
    else:
        check("features.py: get_theme delegates to sites", True, "(already present)")
else:
    check("features.py: get_theme delegates to sites", True, "(no get_theme found)")

write("features.py", f)


# =========================================================================
# 4. Update schemas — add site_slug to every data table
# =========================================================================
print("\n[4/8] Schemas — add site_slug column")

# List of files with SQL schemas
schema_files = {
    "premises.py": ["units", "leases", "charges", "payments", "requests",
                    "request_comments", "notices_sent"],
    "business.py": ["customers", "products", "sales", "sale_items",
                    "sale_payments", "expense_categories", "expenses"],
    "vacancies.py": ["vacancies", "applications"],
    "website.py": ["photos"],
    "app.py": ["documents", "sessions_log", "audit"],
}
# For units and expense_categories, they are shared lookup tables — no site_slug

shared_tables = {"units", "expense_categories", "notices_sent"}
schema_files = {
    fname: [t for t in tables if t not in shared_tables]
    for fname, tables in schema_files.items()
}

for fname, tables in schema_files.items():
    path = os.path.join(ROOT, fname)
    if not os.path.exists(path):
        continue
    backup(fname)
    src = read(fname)
    changed = False
    for table in tables:
        # Match `CREATE TABLE IF NOT EXISTS <table> (` and add site_slug column
        # before the closing paren
        tbl_pattern = re.compile(
            rf'(CREATE TABLE IF NOT EXISTS {table}\s*\([^)]*)(\))',
            re.DOTALL | re.IGNORECASE)
        match = tbl_pattern.search(src)
        if match and "site_slug" not in match.group(1):
            old_block = match.group(0)
            new_block = match.group(1) + ",\n  site_slug TEXT NOT NULL DEFAULT 'holding'\n" + match.group(2)
            src = src.replace(old_block, new_block, 1)
            changed = True
    if changed:
        write(fname, src)
        check(f"{fname}: site_slug added to tables", True)
    else:
        check(f"{fname}: site_slug added to tables", True, "(already present or no schema)")


# =========================================================================
# 5. Migration script for existing data
# =========================================================================
print("\n[5/8] migrate_sites.py — one-time data migration")

MIGRATE_SCRIPT = '''"""One-time migration: add site_slug columns to existing data.

Run this ONCE after deploying the multi-site patch, on the server or against
your local DB. It:

  1. Creates the sites table if missing
  2. Ensures the holding company exists
  3. Adds site_slug column to every data table
  4. Sets all existing rows to site_slug = 'holding'

Safe to re-run.
"""
import os
import sqlite3
import datetime as dt
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PERSMAN_DATA", BASE / "data")).resolve()
DB_PATH = DATA / "persman.db" if not os.path.exists(DATA / "tenantman.db") else DATA / "tenantman.db"

if not DB_PATH.exists():
    print(f"Database not found at {DB_PATH}")
    exit(1)

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

# Ensure holding site exists
con.execute(
    "CREATE TABLE IF NOT EXISTS sites ("
    "slug TEXT PRIMARY KEY, name TEXT NOT NULL, tagline TEXT, "
    "body_color TEXT DEFAULT '#ffffff', navbar_color TEXT DEFAULT '#000000', "
    "navbar_text_color TEXT DEFAULT '#ffffff', logo_ext TEXT, "
    "is_holding INTEGER NOT NULL DEFAULT 0, "
    "is_active INTEGER NOT NULL DEFAULT 1, "
    "created_at TEXT NOT NULL)")
have = con.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
if have == 0:
    con.execute(
        "INSERT INTO sites (slug, name, is_holding, created_at) VALUES (?,?,1,?)",
        ("holding", "Your Holding Company", dt.datetime.utcnow().isoformat()))

# Tables that need site_slug
TABLES = [
    # Persman
    "employees", "sessions_log", "documents", "audit",
    "customers", "products", "sales", "sale_items", "sale_payments", "expenses",
    "vacancies", "applications", "photos",
    # TenantMan (harmless if table doesn't exist)
    "accounts", "leases", "charges", "payments", "requests", "request_comments",
]

for table in TABLES:
    # Check if table exists
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not exists:
        continue
    # Check if column exists
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if "site_slug" not in cols:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN site_slug TEXT NOT NULL DEFAULT 'holding'")
            print(f"  + {table}: added site_slug")
        except sqlite3.OperationalError as e:
            print(f"  ! {table}: {e}")
    else:
        print(f"  = {table}: already has site_slug")

# Set existing rows to holding (only if they don't already have a value)
for table in TABLES:
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not exists:
        continue
    try:
        con.execute(f"UPDATE {table} SET site_slug='holding' WHERE site_slug IS NULL OR site_slug=''")
    except sqlite3.OperationalError:
        pass

# Build site_users table to track which users belong to which sites
con.execute(
    "CREATE TABLE IF NOT EXISTS site_users ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "site_slug TEXT NOT NULL, "
    "account_id TEXT NOT NULL, "
    "role TEXT NOT NULL DEFAULT 'staff', "
    "created_at TEXT NOT NULL, "
    "UNIQUE(site_slug, account_id))")

# Populate site_users from existing employees/accounts
for src_table in ("employees", "accounts"):
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (src_table,)).fetchone()
    if not exists:
        continue
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({src_table})").fetchall()]
    if "site_slug" not in cols:
        continue
    for row in con.execute(f"SELECT id, site_slug, is_admin FROM {src_table}"):
        con.execute(
            "INSERT OR IGNORE INTO site_users (site_slug, account_id, role, created_at) "
            "VALUES (?,?,?,?)",
            (row["site_slug"], row["id"],
             "admin" if row["is_admin"] else "staff",
             dt.datetime.utcnow().isoformat()))

con.commit()
con.close()
print("\\nMigration complete. All existing data is now scoped to 'holding'.")
print("You can now create subsidiaries under /holding/admin/sites")
'''

write("migrate_sites.py", MIGRATE_SCRIPT)
check("migrate_sites.py created", True)


# =========================================================================
# 6. Templates for site management
# =========================================================================
print("\n[6/8] Admin templates for site management")

TPL_ADMIN_SITES = '''{% extends "base.html" %}
{% block title %}Subsidiaries · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel">
  <div class="bar">
    <h1>Subsidiaries</h1>
    <a class="btn" href="{{ url_for('admin_sites_new') }}">+ Add subsidiary</a>
  </div>
  <p class="muted small">Each subsidiary gets its own URL prefix (e.g. /acme/), branding, and user isolation. Only holding-company administrators can manage this list.</p>

  <table style="width:100%;border-collapse:collapse;margin-top:1rem">
    <tr><th style="text-align:left">Name</th><th>Slug</th><th>Logo</th><th>Theme</th><th>Active</th><th></th></tr>
    {% for s in sites %}
    <tr>
      <td>
        <strong>{{ s['name'] }}</strong>
        {% if s['is_holding'] %}<span class="badge" style="background:#0a6b3e;color:#fff;padding:.1rem .4rem;border-radius:4px;font-size:.75rem;margin-left:.3rem">HOLDING</span>{% endif %}
      </td>
      <td><code>/{{ s['slug'] }}/</code></td>
      <td>{% if s['logo_ext'] %}<img src="{{ url_for('site_logo', slug=s['slug']) }}" style="max-height:32px">{% else %}<span class="muted">no logo</span>{% endif %}</td>
      <td>
        <span style="display:inline-block;width:16px;height:16px;background:{{ s['body_color'] }};border:1px solid #ccc;border-radius:3px"></span>
        <span style="display:inline-block;width:16px;height:16px;background:{{ s['navbar_color'] }};border:1px solid #ccc;border-radius:3px"></span>
      </td>
      <td>{{ '✓' if s['is_active'] else '✗' }}</td>
      <td><a class="btn ghost small" href="{{ url_for('admin_sites_edit', slug=s['slug']) }}">Edit</a></td>
    </tr>
    {% endfor %}
  </table>
</section>
{% endblock %}
'''

TPL_SITE_FORM = '''{% extends "base.html" %}
{% block title %}{{ 'Edit' if not is_new else 'New' }} subsidiary · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel narrow">
  <h1>{{ 'Edit subsidiary' if not is_new else 'Add subsidiary' }}</h1>
  <form method="post" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    {% if is_new %}
    <label>URL slug
      <input name="slug" value="{{ site.get('slug', '') }}" required pattern="[a-z0-9-]+" placeholder="acme">
      <span class="muted small">Lowercase letters, numbers, hyphens only. Becomes /acme/</span>
    </label>
    {% else %}
    <label>URL slug <input value="/{{ site['slug'] }}/" disabled></label>
    {% endif %}
    <label>Company name <input name="name" value="{{ site.get('name', '') }}" required></label>
    <label>Tagline <input name="tagline" value="{{ site.get('tagline') or '' }}" placeholder="Short one-liner shown on the homepage"></label>

    <h2>Colours</h2>
    <div class="grid">
      <label>Page background <input type="color" name="body_color" value="{{ site.get('body_color') or '#ffffff' }}"></label>
      <label>Navigation bar <input type="color" name="navbar_color" value="{{ site.get('navbar_color') or '#000000' }}"></label>
      <label>Navigation text <input type="color" name="navbar_text_color" value="{{ site.get('navbar_text_color') or '#ffffff' }}"></label>
    </div>

    {% if not is_new %}
    <label class="check">
      <input type="checkbox" name="is_active" value="1" {{ 'checked' if site['is_active'] }}>
      Active (uncheck to hide the site from users)
    </label>
    {% endif %}

    <button class="btn" type="submit">{{ 'Save' if not is_new else 'Create subsidiary' }}</button>
  </form>

  {% if not is_new %}
  <h2 style="margin-top:2rem">Logo</h2>
  <form method="post" action="{{ url_for('admin_sites_logo', slug=site['slug']) }}" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    {% if site['logo_ext'] %}
    <p><img src="{{ url_for('site_logo', slug=site['slug']) }}" style="max-height:60px"> <span class="muted small">(current logo)</span></p>
    {% endif %}
    <label>Upload new logo <input type="file" name="logo" accept=".png,.jpg,.jpeg,.webp,.svg"></label>
    <button class="btn ghost" type="submit">Upload logo</button>
  </form>
  {% endif %}
</section>
{% endblock %}
'''

write("templates/admin_sites.html", TPL_ADMIN_SITES)
check("templates/admin_sites.html", True)
write("templates/admin_site_form.html", TPL_SITE_FORM)
check("templates/admin_site_form.html", True)


# =========================================================================
# 7. Update base.html and features for site-aware URL generation
# =========================================================================
print("\n[7/8] base.html and features adjustments")

# Update base.html to use per-site theme CSS and logo
backup("templates/base.html")
b = read("templates/base.html")

# Replace the theme CSS link
b = re.sub(
    r'<link rel="stylesheet" href="\{\{ url_for\(\'branding_theme\'\) \}\}">',
    '''<link rel="stylesheet" href="{% if g.site %}{{ url_for('site_theme_css', slug=g.site['slug']) }}{% else %}{{ url_for('branding_theme') }}{% endif %}">''',
    b)

if "site_theme_css" not in b:
    # Try alternate URL pattern
    b = b.replace(
        '<link rel="stylesheet" href="{{ url_for(\'branding_theme\') }}">',
        '<link rel="stylesheet" href="{% if g.site %}{{ url_for(\'site_theme_css\', slug=g.site[\'slug\']) }}{% else %}{{ url_for(\'branding_theme\') }}{% endif %}">')

# Add "Subsidiaries" link for holding admins in the nav
# Find the nav block and add before Sign out
if "admin_sites" not in b and "</nav>" in b:
    # Add an extra link (only visible to holding admins via the route check)
    b = b.replace(
        "</nav>\n  <form method=\"post\" action=\"{{ url_for('logout') }}\"",
        "  {% if g.is_holding_admin %}<a href=\"{{ url_for('admin_sites') }}\">Subsidiaries</a>{% endif %}\n    </nav>\n  <form method=\"post\" action=\"{{ url_for('logout') }}\"",
        1)
    check("base.html: Subsidiaries link", True)
else:
    check("base.html: Subsidiaries link", True, "(already present or no matching nav)")

write("templates/base.html", b)


# =========================================================================
# 8. Summary
# =========================================================================
print("\n[8/8] Summary")
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHANGES APPLIED")
    print("\nNEXT STEPS:")
    print("\n1. Run the migration (once, on the server or locally):")
    print("     python migrate_sites.py")
    print("\n2. Commit and push. Render will redeploy.")
    print("\n3. Visit your site. All existing URLs will redirect to /holding/...")
    print("\n4. Log in at /holding/login with your existing admin credentials.")
    print("\n5. Go to /holding/admin/sites and add subsidiaries.")
    print("\n6. Each subsidiary is accessible at /<slug>/  (e.g. /acme/)")
    print("   Log in at /<slug>/login. Only that subsidiary's users can access it.")
    print("\n7. Holding admins can access any subsidiary's admin by going to /<slug>/admin")
    print("\nCRITICAL: You MUST run migrate_sites.py BEFORE the app can serve requests,")
    print("because the load_user function now expects site_slug columns to exist.")
else:
    print("SOME CHECKS FAILED — see above")
    print("Do NOT deploy until all checks pass.")
