"""Persman patch: Website admin (colours + home-page gallery)."""
import os, re, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_website_patch")
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
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

results = []
def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}  {detail}")
    results.append((name, ok))

# =========================================================================
# 1. website.py — new module: colours + gallery
# =========================================================================
print("\n[1/6] website.py — colours + gallery module")

WEBSITE_MODULE = '''"""Persman: Website admin (site colours + home-page gallery)."""
import io
import os
import re
import secrets
import sqlite3
import warnings
from pathlib import Path

from flask import (Response, abort, flash, g, redirect, render_template,
                   request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from app import DATA, DB_PATH, admin_required, app, audit, get_db, iso, now
import features

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


PHOTO_DIR = DATA / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

MAX_PHOTOS = 40
MAX_PER_UPLOAD = 8
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 60_000_000
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

DEFAULT_THEME = {"body_color": "#edf1f3", "navbar_color": "#14272d", "navbar_text_color": "#ffffff"}
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---- schema -------------------------------------------------------------
def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS photos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ext TEXT NOT NULL,
      has_thumb INTEGER NOT NULL DEFAULT 0,
      caption TEXT,
      position INTEGER NOT NULL,
      uploaded_at TEXT NOT NULL,
      uploaded_by TEXT
    )""")
    con.commit()
    con.close()


_init_db()


# ---- theme helpers ------------------------------------------------------
def get_theme():
    if "theme" in g:
        return g.theme
    rows = dict(get_db().execute(
        "SELECT key, value FROM app_settings WHERE key IN ('body_color','navbar_color','navbar_text_color')"
    ).fetchall())
    theme = dict(DEFAULT_THEME)
    for k in theme:
        if rows.get(k) and COLOUR_RE.match(rows[k]):
            theme[k] = rows[k]
    g.theme = theme
    return theme


def _set_setting(db, key, value):
    features._set_setting(db, key, value)


@app.context_processor
def inject_theme():
    return {"theme": get_theme()}


@app.route("/branding/theme.css")
def branding_theme():
    t = get_theme()
    css = (
        ":root{"
        "--bg:" + t["body_color"] + ";"
        "--ink-navbar:" + t["navbar_color"] + ";"
        "--ink-navbar-text:" + t["navbar_text_color"] + ";"
        "}"
        ".top{background:var(--ink-navbar);color:var(--ink-navbar-text)}"
        ".top nav a{color:var(--ink-navbar-text);opacity:.85}"
        ".top nav a:hover,.top nav a:focus-visible{opacity:1;border-bottom-color:var(--ink-navbar-text)}"
        ".top .brand span{color:var(--ink-navbar-text)}"
    )
    return Response(css, mimetype="text/css", headers={"Cache-Control": "public, max-age=300"})


# ---- gallery helpers ----------------------------------------------------
def _sniff_image(data):
    if data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        return "png"
    if data[:3] == b"\\xff\\xd8\\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _process(data):
    kind = _sniff_image(data)
    if kind not in MIME:
        raise ValueError("is not a JPG, PNG or WebP photo")
    if not HAVE_PIL:
        return data, None, kind
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            im.load()
            im = ImageOps.exif_transpose(im)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("is too large in pixels")
    except Exception:
        raise ValueError("could not be read as an image")
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.getchannel("A"))
        im = flat
    im = im.convert("RGB")
    im.thumbnail((1800, 1800))
    main = io.BytesIO()
    im.save(main, "JPEG", quality=85, optimize=True)
    thumb_im = im.copy()
    thumb_im.thumbnail((640, 640))
    thumb = io.BytesIO()
    thumb_im.save(thumb, "JPEG", quality=80, optimize=True)
    return main.getvalue(), thumb.getvalue(), "jpg"


def _photos():
    return get_db().execute("SELECT * FROM photos ORDER BY position, id").fetchall()


# ---- admin: Website (colours) -------------------------------------------
@app.route("/admin/website", methods=["GET", "POST"])
@admin_required
def admin_website():
    db = get_db()
    if request.method == "POST":
        if request.form.get("action") == "save_colours":
            changed = []
            for key in ("body_color", "navbar_color", "navbar_text_color"):
                value = (request.form.get(key) or "").strip()
                if not COLOUR_RE.match(value):
                    continue
                if value.upper() != get_theme()[key].upper():
                    _set_setting(db, key, value)
                    changed.append(f"{key}={value}")
            if changed:
                audit(g.user["id"], "branding_changed", None, "; ".join(changed))
            g.pop("theme", None)
            db.commit()
            flash("Colours saved." if changed else "Nothing changed.", "ok")
            return redirect(url_for("admin_website"))
    return render_template("admin_website.html", theme=get_theme())


# ---- admin: gallery -----------------------------------------------------
@app.route("/admin/website/gallery", methods=["GET", "POST"])
@admin_required
def admin_gallery():
    db = get_db()
    if request.method == "POST":
        files = [f for f in request.files.getlist("photos") if f and f.filename]
        have = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        if not files:
            flash("Choose one or more photos.", "error")
        elif len(files) > MAX_PER_UPLOAD:
            flash(f"Add up to {MAX_PER_UPLOAD} photos at a time.", "error")
        elif have + len(files) > MAX_PHOTOS:
            flash(f"The gallery can hold {MAX_PHOTOS} photos.", "error")
        else:
            added, errors = 0, []
            for f in files:
                label = (f.filename or "photo")[:40]
                data = f.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    errors.append(f"{label} is larger than 10 MB")
                    continue
                try:
                    main, thumb, ext = _process(data)
                except ValueError as e:
                    errors.append(f"{label} {e}")
                    continue
                pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM photos").fetchone()[0]
                pid = db.execute(
                    "INSERT INTO photos (ext, has_thumb, caption, position, uploaded_at, uploaded_by) "
                    "VALUES (?,?,?,?,?,?)",
                    (ext, 1 if thumb else 0, "", pos, iso(now()), g.user["id"])).lastrowid
                (PHOTO_DIR / f"{pid}.{ext}").write_bytes(main)
                if thumb:
                    (PHOTO_DIR / f"{pid}_t.jpg").write_bytes(thumb)
                added += 1
            if added:
                audit(g.user["id"], "photos_added", None, f"{added} photo(s)")
                db.commit()
                flash(f"Added {added} photo{'s' if added != 1 else ''}.", "ok")
            for e in errors:
                flash(e + ". It was not added.", "error")
        return redirect(url_for("admin_gallery"))
    return render_template("admin_gallery.html", photos=_photos(),
                           have_pil=HAVE_PIL, max_photos=MAX_PHOTOS, per_upload=MAX_PER_UPLOAD)


@app.route("/photos/<int:pid>")
def photo_file(pid):
    db = get_db()
    row = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if row is None:
        abort(404)
    if request.args.get("t") == "1" and row["has_thumb"]:
        return send_from_directory(PHOTO_DIR, f"{pid}_t.jpg", mimetype="image/jpeg", max_age=86400)
    return send_from_directory(PHOTO_DIR, f"{pid}.{row['ext']}", mimetype=MIME[row["ext"]], max_age=86400)


@app.post("/admin/website/gallery/<int:pid>/caption")
@admin_required
def photo_caption(pid):
    db = get_db()
    if db.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone() is None:
        abort(404)
    db.execute("UPDATE photos SET caption=? WHERE id=?",
               (" ".join((request.form.get("caption") or "").split())[:200], pid))
    db.commit()
    flash("Caption saved.", "ok")
    return redirect(url_for("admin_gallery"))


@app.post("/admin/website/gallery/<int:pid>/move")
@admin_required
def photo_move(pid):
    db = get_db()
    row = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if row is None:
        abort(404)
    if request.form.get("direction") == "up":
        other = db.execute("SELECT * FROM photos WHERE position < ? ORDER BY position DESC LIMIT 1",
                           (row["position"],)).fetchone()
    else:
        other = db.execute("SELECT * FROM photos WHERE position > ? ORDER BY position ASC LIMIT 1",
                           (row["position"],)).fetchone()
    if other:
        db.execute("UPDATE photos SET position=? WHERE id=?", (other["position"], pid))
        db.execute("UPDATE photos SET position=? WHERE id=?", (row["position"], other["id"]))
        db.commit()
    return redirect(url_for("admin_gallery"))


@app.post("/admin/website/gallery/<int:pid>/delete")
@admin_required
def photo_delete(pid):
    db = get_db()
    row = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if row is None:
        abort(404)
    for name in (f"{pid}.{row['ext']}", f"{pid}_t.jpg"):
        (PHOTO_DIR / name).unlink(missing_ok=True)
    db.execute("DELETE FROM photos WHERE id=?", (pid,))
    audit(g.user["id"], "photo_deleted", None, f"photo {pid}")
    db.commit()
    flash("Photo deleted.", "ok")
    return redirect(url_for("admin_gallery"))


# ---- public: home-page gallery -----------------------------------------
@app.route("/gallery")
def public_gallery():
    return render_template("gallery.html", photos=_photos())
'''

write("website.py", WEBSITE_MODULE)
check("website.py written", True)

# =========================================================================
# 2. templates/admin_website.html — colours form
# =========================================================================
print("\n[2/6] templates/admin_website.html")
WEBSITE_TPL = '''{% extends "base.html" %}
{% block title %}Website · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel narrow wide">
  <h1>Website</h1>
  <p class="muted">Change the colours of the site and manage the home-page gallery.</p>

  <h2>Site colours</h2>
  <form method="post" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="action" value="save_colours">
    <label>Page background <input type="color" name="body_color" value="{{ theme.body_color }}"></label>
    <label>Navigation bar <input type="color" name="navbar_color" value="{{ theme.navbar_color }}"></label>
    <label>Navigation text <input type="color" name="navbar_text_color" value="{{ theme.navbar_text_color }}"></label>
    <p class="muted small">Pick readable combinations — dark text on a light bar, or white text on a dark bar. Leave the defaults to undo.</p>
    <button class="btn" type="submit">Save colours</button>
  </form>

  <h2 style="margin-top:2rem">Home-page gallery</h2>
  <p class="muted small">Photos uploaded here appear on the public <a href="{{ url_for('public_gallery') }}">gallery page</a>. Up to 40 photos, JPG/PNG/WebP, 10 MB each.{% if not have_pil %} (Install Pillow for automatic resizing and EXIF stripping.){% endif %}</p>
  <p><a class="btn" href="{{ url_for('admin_gallery') }}">Manage gallery ({{ photos_count }} photo{{ 's' if photos_count != 1 else '' }})</a></p>
</section>
{% endblock %}
'''
write("templates/admin_website.html", WEBSITE_TPL)
check("templates/admin_website.html", True)

# =========================================================================
# 3. templates/admin_gallery.html
# =========================================================================
print("\n[3/6] templates/admin_gallery.html")
GALLERY_TPL = '''{% extends "base.html" %}
{% block title %}Gallery · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel">
  <div class="bar">
    <h1>Home-page gallery</h1>
    <a class="btn ghost" href="{{ url_for('admin_website') }}">Back to Website</a>
  </div>
  <form method="post" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label>Add photos (up to {{ per_upload }} at a time)
      <input type="file" name="photos" accept="image/png,image/jpeg,image/webp" multiple>
    </label>
    <p class="muted small">Total limit: {{ max_photos }} photos.</p>
    <button class="btn" type="submit">Upload</button>
  </form>
</section>

<section class="panel">
  <h2>Photos</h2>
  {% if photos %}
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem">
    {% for p in photos %}
    <div style="border:1px solid var(--line);border-radius:6px;padding:.6rem;background:#fff">
      <a href="{{ url_for('photo_file', pid=p['id']) }}" target="_blank">
        <img src="{{ url_for('photo_file', pid=p['id'], t=1) if p['has_thumb'] else url_for('photo_file', pid=p['id']) }}"
             alt="" style="width:100%;height:150px;object-fit:cover;border-radius:4px">
      </a>
      <form method="post" action="{{ url_for('photo_caption', pid=p['id']) }}" class="stack" style="margin-top:.4rem">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <input name="caption" value="{{ p['caption'] or '' }}" placeholder="Caption (optional)" style="margin:0;padding:.3rem .4rem;font-size:.85rem">
        <div class="row nowrap" style="gap:.3rem">
          <button class="btn ghost small" type="submit">Save caption</button>
        </div>
      </form>
      <div class="row nowrap" style="gap:.3rem;margin-top:.3rem">
        <form method="post" action="{{ url_for('photo_move', pid=p['id']) }}"><input type="hidden" name="_csrf" value="{{ csrf_token() }}"><input type="hidden" name="direction" value="up"><button class="btn ghost small" type="submit">↑</button></form>
        <form method="post" action="{{ url_for('photo_move', pid=p['id']) }}"><input type="hidden" name="_csrf" value="{{ csrf_token() }}"><input type="hidden" name="direction" value="down"><button class="btn ghost small" type="submit">↓</button></form>
        <form method="post" action="{{ url_for('photo_delete', pid=p['id']) }}"><input type="hidden" name="_csrf" value="{{ csrf_token() }}"><button class="btn danger small" type="submit">Delete</button></form>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <p class="muted">No photos yet. Upload the first one above.</p>
  {% endif %}
</section>
{% endblock %}
'''
write("templates/admin_gallery.html", GALLERY_TPL)
check("templates/admin_gallery.html", True)

# =========================================================================
# 4. templates/gallery.html — public gallery page
# =========================================================================
print("\n[4/6] templates/gallery.html")
PUBLIC_GALLERY_TPL = '''{% extends "base.html" %}
{% block title %}Gallery · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel">
  <h1>Gallery</h1>
  {% if photos %}
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem;margin-top:1rem">
    {% for p in photos %}
    <figure style="margin:0">
      <a href="{{ url_for('photo_file', pid=p['id']) }}" target="_blank">
        <img src="{{ url_for('photo_file', pid=p['id'], t=1) if p['has_thumb'] else url_for('photo_file', pid=p['id']) }}"
             alt="{{ p['caption'] or '' }}" style="width:100%;height:200px;object-fit:cover;border-radius:6px">
      </a>
      {% if p['caption'] %}<figcaption class="small muted" style="margin-top:.3rem">{{ p['caption'] }}</figcaption>{% endif %}
    </figure>
    {% endfor %}
  </div>
  {% else %}
  <p class="muted">No photos yet.</p>
  {% endif %}
</section>
{% endblock %}
'''
write("templates/gallery.html", PUBLIC_GALLERY_TPL)
check("templates/gallery.html", True)

# =========================================================================
# 5. app.py — import website module
# =========================================================================
print("\n[5/6] app.py — import website")
backup("app.py")
a = read("app.py")
if "import website" in a:
    check("app.py: import website", True, "(already present)")
else:
    if "import features" in a:
        a = a.replace("import features  # noqa: E402,F401",
                      "import features  # noqa: E402,F401\nimport website   # noqa: E402,F401  (site colours + gallery)")
        check("app.py: import website", True, "(after features)")
    else:
        a += "\nimport website  # noqa: E402,F401\n"
        check("app.py: import website", True, "(appended)")
    write("app.py", a)

# =========================================================================
# 6. base.html — Website nav link
# =========================================================================
print("\n[6/6] base.html — Website nav link")
backup("templates/base.html")
b = read("templates/base.html")

# Add link to theme stylesheet in <head>
if 'url_for("branding_theme")' not in b and "branding_theme" not in b:
    style_tag = '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">'
    if style_tag in b:
        b = b.replace(style_tag, style_tag + '\n<link rel="stylesheet" href="{{ url_for(\'branding_theme\') }}">', 1)
        check("base.html: theme stylesheet", True, "(inserted)")
    else:
        check("base.html: theme stylesheet", False, "style.css link not found")
else:
    check("base.html: theme stylesheet", True, "(already present)")

# Add Website nav link in admin branch
if "admin_website" in b:
    check("base.html: Website nav link", True, "(already present)")
else:
    if "Branding</a>" in b:
        b = b.replace("Branding</a>",
                      "Branding</a>\n      <a href=\"{{ url_for('admin_website') }}\">Website</a>", 1)
        check("base.html: Website nav link", True, "(after Branding)")
    elif "Activity</a>" in b:
        b = b.replace("Activity</a>",
                      "Activity</a>\n      <a href=\"{{ url_for('admin_website') }}\">Website</a>", 1)
        check("base.html: Website nav link", True, "(after Activity)")
    else:
        check("base.html: Website nav link", False, "no anchor found in admin nav")
write("templates/base.html", b)

# Pass photos_count to the website template
w = read("website.py")
if "photos_count=" not in w:
    w = w.replace(
        'return render_template("admin_website.html", theme=get_theme())',
        'count = get_db().execute("SELECT COUNT(*) FROM photos").fetchone()[0]\n'
        '    return render_template("admin_website.html", theme=get_theme(), photos_count=count)'
    )
    write("website.py", w)
    check("website.py: photos_count passed", True, "(inserted)")
else:
    check("website.py: photos_count passed", True, "(already present)")

# =========================================================================
# Summary
# =========================================================================
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHANGES APPLIED")
    print("\nRestart the app. What you get:")
    print("  - Navbar: new 'Website' link (admin only)")
    print("  - /admin/website: three colour pickers + gallery link")
    print("  - /admin/website/gallery: upload, caption, reorder, delete photos")
    print("  - /gallery: public photo gallery page")
    print("  - /branding/theme.css: the CSS variables the colours write to")
else:
    print("SOME CHECKS FAILED:")
    for name, ok in results:
        if not ok:
            print("  -", name)
    print("\nPaste the output above for exact fixes.")