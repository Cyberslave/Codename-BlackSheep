"""Fix: remove /holding prefix from admin_sites routes so they match
the URL that the WSGI middleware presents to Flask."""
import re
import shutil
import datetime
import os

BACKUP = "_backup_route_fix_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
os.makedirs(BACKUP, exist_ok=True)
shutil.copy("sites.py", os.path.join(BACKUP, "sites.py"))

s = open("sites.py", encoding="utf-8").read()

# Fix route decorators — remove /holding prefix, remove duplicate
replacements = [
    ('@app.route("/holding/admin/sites")',
     '@app.route("/admin/sites")'),
    ('@app.route("/holding/admin/sites/new", methods=["GET", "POST"])',
     '@app.route("/admin/sites/new", methods=["GET", "POST"])'),
    ('@app.route("/holding/admin/sites/<slug>/edit", methods=["GET", "POST"])',
     '@app.route("/admin/sites/<slug>/edit", methods=["GET", "POST"])'),
    ('@app.route("/holding/admin/sites/<slug>/logo", methods=["POST"])',
     '@app.route("/admin/sites/<slug>/logo", methods=["POST"])'),
    ('@app.route("/<slug>/admin/sites/admins")\n@app.route("/holding/admin/sites/<slug>/admins")',
     '@app.route("/admin/sites/<slug>/admins")'),
    ('@app.route("/holding/admin/sites/<slug>/admins/new", methods=["POST"])',
     '@app.route("/admin/sites/<slug>/admins/new", methods=["POST"])'),
    ('@app.route("/holding/admin/sites/<slug>/admins/<eid>/delete", methods=["POST"])',
     '@app.route("/admin/sites/<slug>/admins/<eid>/delete", methods=["POST"])'),
]

changed = 0
for old, new in replacements:
    if old in s and old != new:
        s = s.replace(old, new, 1)
        changed += 1
        print(f"  Replaced: {old[:60]}...")

if changed:
    open("sites.py", "w", encoding="utf-8").write(s)
    print(f"\n✓ {changed} route(s) fixed in sites.py")
else:
    print("\nNo routes needed fixing — checking current state...")

# Show current routes
print("\nCurrent @app.route lines in sites.py:")
for line in s.splitlines():
    if line.strip().startswith('@app.route'):
        print(f"  {line.strip()}")