"""Patch: add UI for creating subsidiary admins from the holding dashboard.

Adds:
  /holding/admin/sites/<slug>/admins        — list admins of a subsidiary
  /holding/admin/sites/<slug>/admins/new    — create a new admin (POST)
  templates/admin_site_admins.html
  A link from /holding/admin/sites/<slug>/edit → admins page
"""
import os
import shutil
import datetime
import re

BACKUP = "_backup_site_admins_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
os.makedirs(BACKUP, exist_ok=True)


def backup(p):
    if os.path.exists(p):
        shutil.copy(p, os.path.join(BACKUP, p.replace("/", "_").replace("\\", "_")))


results = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}  {detail}")
    results.append((name, ok))


# =========================================================================
# 1. Append routes to sites.py
# =========================================================================
print("\n[1/3] sites.py — append admin-creation routes")

backup("sites.py")
s = open("sites.py", encoding="utf-8").read()

if "def admin_site_admins(" in s:
    check("sites.py: admin creation routes", True, "(already present)")
else:
    routes_code = '''

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
    elif not (re.search(r"[A-Za-z]", password) and re.search(r"\\d", password)):
        errors.append("Password must contain letters and numbers.")
    if email and not re.match(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", email):
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
'''
    s = s.rstrip() + routes_code + "\n"
    open("sites.py", "w", encoding="utf-8").write(s)
    check("sites.py: admin creation routes", True, "(appended)")

# Ensure `secrets` is imported in sites.py
s = open("sites.py", encoding="utf-8").read()
if not re.search(r'^import secrets\b', s, re.MULTILINE):
    s = "import secrets\n" + s
    open("sites.py", "w", encoding="utf-8").write(s)
    check("sites.py: import secrets", True, "(added)")
else:
    check("sites.py: import secrets", True, "(already present)")


# =========================================================================
# 2. Create the template
# =========================================================================
print("\n[2/3] templates/admin_site_admins.html")

TPL = '''{% extends "base.html" %}
{% block title %}Administrators · {{ site['name'] }}{% endblock %}
{% block content %}
<section class="panel">
  <div class="bar">
    <h1>Administrators of {{ site['name'] }}</h1>
    <a class="btn ghost" href="{{ url_for('admin_sites_edit', slug=site['slug']) }}">Back to site settings</a>
  </div>

  <p class="muted small">
    These administrators can log in at <code>/{{ site['slug'] }}/login</code>
    and manage only {{ site['name'] }}'s data.
    They cannot access other subsidiaries or the holding company.
  </p>

  <h2>Current administrators</h2>
  {% if admins %}
  <table style="width:100%;border-collapse:collapse;margin-top:.5rem">
    <tr>
      <th style="text-align:left">ID</th>
      <th>Name</th>
      <th>Email</th>
      <th>Phone</th>
      <th></th>
    </tr>
    {% for a in admins %}
    <tr>
      <td><code>{{ a['id'] }}</code></td>
      <td>{{ a['full_name'] }}</td>
      <td>{{ a['email'] or '—' }}</td>
      <td>{{ a['phone'] or '—' }}</td>
      <td>
        <form method="post" action="{{ url_for('admin_site_admins_delete', slug=site['slug'], eid=a['id']) }}"
              onsubmit="return confirm('Delete this administrator? This cannot be undone.')"
              style="display:inline">
          <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
          <button class="btn danger small" type="submit">Delete</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">No administrators yet for this site.</p>
  {% endif %}

  <h2 style="margin-top:2rem">Add an administrator</h2>
  <form method="post" action="{{ url_for('admin_site_admins_new', slug=site['slug']) }}" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label>Full name <input name="full_name" required autofocus></label>
    <div class="grid">
      <label>Email <input type="email" name="email" placeholder="optional"></label>
      <label>Phone <input name="phone" placeholder="optional"></label>
    </div>
    <label>Password
      <input type="password" name="password" required minlength="8" autocomplete="new-password">
      <span class="muted small">At least 8 characters, with letters and numbers.</span>
    </label>
    <button class="btn" type="submit">Create administrator</button>
    <p class="muted small">
      You'll see the generated employee ID after saving.
      Give it, along with the password, to the new administrator.
      They can change the password after first sign-in.
    </p>
  </form>
</section>
{% endblock %}
'''

os.makedirs("templates", exist_ok=True)
open("templates/admin_site_admins.html", "w", encoding="utf-8").write(TPL)
check("templates/admin_site_admins.html", True)


# =========================================================================
# 3. Add "Administrators" link to the site edit page
# =========================================================================
print("\n[3/3] templates/admin_site_form.html — add link to admins page")

site_form = "templates/admin_site_form.html"
if os.path.exists(site_form):
    backup(site_form)
    sf = open(site_form, encoding="utf-8").read()
    if "admin_site_admins" in sf:
        check("site form: admins link", True, "(already present)")
    else:
        # Insert a section after the logo form (or before {% endblock %})
        new_section = '''
  {% if not is_new %}
  <h2 style="margin-top:2rem">Administrators</h2>
  <p class="muted small">
    Manage the people who can sign in to {{ site['name'] }} and manage only
    that subsidiary's data.
  </p>
  <p>
    <a class="btn" href="{{ url_for('admin_site_admins', slug=site['slug']) }}">
      Manage administrators
    </a>
  </p>
  {% endif %}
'''
        # Insert before {% endblock %}
        if "{% endblock %}" in sf:
            sf = sf.replace("{% endblock %}", new_section + "\n{% endblock %}", 1)
        else:
            sf = sf + new_section
        open(site_form, "w", encoding="utf-8").write(sf)
        check("site form: admins link", True, "(added)")
else:
    check("site form: admins link", False, "admin_site_form.html not found")


# =========================================================================
# Summary
# =========================================================================
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHANGES APPLIED")
    print("\nNext steps:")
    print("  1. Restart the server:")
    print("       Ctrl+C")
    print("       flask --app app.py run")
    print("  2. Log in as a holding admin.")
    print("  3. Go to /holding/admin/sites")
    print("  4. Create a subsidiary (or edit an existing one).")
    print("  5. On the edit page, click 'Manage administrators'.")
    print("  6. Add an admin — the generated ID and your password will log them in.")
else:
    print("SOME CHECKS FAILED — see above")