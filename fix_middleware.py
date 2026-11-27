"""Fix: install the WSGI middleware that rewrites /<site>/... BEFORE routing,
and the before_request that sets g.site for template rendering."""
import os
import shutil
import datetime

BACKUP = "backups_middleware_fix_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
os.makedirs(BACKUP, exist_ok=True)


def backup(path):
    if os.path.exists(path):
        shutil.copy(path, os.path.join(BACKUP, path.replace("/", "_").replace("\\", "_")))


# ---- 1. Rewrite sites.py to include the WSGI middleware ----
backup("sites.py")
s = open("sites.py", encoding="utf-8").read()

if "class SiteMiddleware" not in s:
    middleware_code = '''

# ---- WSGI middleware: runs BEFORE Flask routes ----
class SiteMiddleware:
    """Reads the first path segment as a site slug and rewrites the request
    so Flask's normal routing matches the un-prefixed route. This is what
    makes /acme/admin hit the /admin handler."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")

        # Pass through static files and health checks
        if path.startswith("/static/") or path in ("/healthz", "/favicon.ico"):
            return self.wsgi_app(environ, start_response)

        import sqlite3
        from app import DB_PATH

        parts = path.lstrip("/").split("/", 1)
        first = parts[0].lower() if parts else ""

        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        site = con.execute(
            "SELECT slug FROM sites WHERE slug=? AND is_active=1", (first,)
        ).fetchone()
        con.close()

        if site:
            # Rewrite: /acme/admin -> SCRIPT_NAME=/acme, PATH_INFO=/admin
            environ["SCRIPT_NAME"] = f"/{first}"
            rest = "/" + parts[1] if len(parts) > 1 else "/"
            environ["PATH_INFO"] = rest
        else:
            # No site prefix — redirect to holding (or let the request through)
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            holding = con.execute(
                "SELECT slug FROM sites WHERE is_holding=1 AND is_active=1"
            ).fetchone()
            con.close()

            if holding and path not in ("/",) and not path.startswith("/static/"):
                # Redirect legacy unprefixed URL to holding-prefixed
                if environ.get("REQUEST_METHOD") == "GET":
                    query = environ.get("QUERY_STRING", "")
                    location = f"/{holding['slug']}{path}"
                    if query:
                        location += "?" + query
                    start_response("302 Found", [("Location", location)])
                    return [b""]
            # If holding exists and path is "/", rewrite to holding
            if holding:
                environ["SCRIPT_NAME"] = f"/{holding['slug']}"
                environ["PATH_INFO"] = path

        return self.wsgi_app(environ, start_response)


def load_site_to_g():
    """Set g.site and g.site_slug based on SCRIPT_NAME (set by SiteMiddleware).
    Called from a before_request handler in app.py."""
    from flask import request, g
    from app import get_db
    script_name = request.environ.get("SCRIPT_NAME", "").strip("/")
    site_slug = script_name or "holding"

    db = get_db()
    site = db.execute(
        "SELECT * FROM sites WHERE slug=? AND is_active=1", (site_slug,)
    ).fetchone()
    if not site:
        site = db.execute(
            "SELECT * FROM sites WHERE is_holding=1 AND is_active=1"
        ).fetchone()
        site_slug = site["slug"] if site else "holding"
    g.site = site
    g.site_slug = site_slug
    g.url_prefix = f"/{site_slug}" if site_slug else ""
'''
    s = s + middleware_code
    open("sites.py", "w", encoding="utf-8").write(s)
    print("sites.py: appended SiteMiddleware and load_site_to_g")
else:
    print("sites.py: SiteMiddleware already present")

# ---- 2. Update app.py to use the WSGI middleware ----
backup("app.py")
a = open("app.py", encoding="utf-8").read()

# 2a. Remove the broken before_request detect_site (which tries to rewrite after routing)
import re
# Remove any before_request that calls detect_site or _detect_site
a = re.sub(
    r'@app\.before_request\ndef _detect_site\(\):\n(?:\s+.*\n)*?\n',
    "",
    a,
)
a = re.sub(
    r'@app\.before_request\ndef _detect_site\(\):\n(?:\s+.*\n)*?\n',
    "",
    a,
)
a = re.sub(
    r'@app\.before_request\ndef _load_site_context\(\):\n(?:\s+.*\n)*?\n',
    "",
    a,
)

# 2b. Add correct before_request that sets g.site (AFTER routing, but the WSGI
#     middleware already rewrote the path so g.site_slug is correct)
if "load_site_to_g" not in a:
    context_hook = '''
# ---- Set site context on every request (WSGI middleware already rewrote path) ----
@app.before_request
def _set_site_context():
    from sites import load_site_to_g
    load_site_to_g()

'''
    # Insert right after "import sites" or the last import
    if "import sites" in a:
        a = a.replace(
            "import sites  # noqa: E402,F401  (multi-site detection, isolation, brand)",
            "import sites  # noqa: E402,F401  (multi-site detection, isolation, brand)"
            + "\n" + context_hook,
            1,
        )
    else:
        a = a + "\n" + context_hook

# 2c. Add the WSGI middleware installation at the very bottom
if "app.wsgi_app = SiteMiddleware" not in a:
    wsgi_block = '''

# ---- Install WSGI middleware (must be at the very bottom, after app is created) ----
from sites import SiteMiddleware
app.wsgi_app = SiteMiddleware(app.wsgi_app)
'''
    a = a.rstrip() + wsgi_block + "\n"
    print("app.py: appended WSGI middleware installation")

open("app.py", "w", encoding="utf-8").write(a)
print("app.py: updated")

# ---- 3. Show the state ----
print("\n=== Checking app.py ===")
import subprocess
result = subprocess.run(
    ["findstr", "/C:", "SiteMiddleware", "app.py"],
    capture_output=True, text=True,
)
print(result.stdout or "(not found)")

result = subprocess.run(
    ["findstr", "/C:", "load_site_to_g", "app.py"],
    capture_output=True, text=True,
)
print(result.stdout or "(not found)")

print("\nDone. Restart the server:")
print("  Ctrl+C")
print("  flask --app app.py run")
print("\nThen visit http://localhost:5000/ and check:")
print("  - View source: <title> should show your brand name")
print("  - The URL in <a class='brand'> should be /holding/")
print("  - /branding/theme.css should be /holding/branding/theme.css")