"""Persman: Website admin (site colours + home-page gallery)."""
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
def _commit_to_github(message, include_photos=False):
    """Commit the DB (and optionally data/photos/) to GitHub so writes survive restarts."""
    try:
        from app import commit_github
        return commit_github(message, include_employees=include_photos)
    except Exception as e:
        print(f"[website._commit_to_github] {e}", flush=True)
        return False

PHOTO_DIR = DATA / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

MAX_PHOTOS = 40
MAX_PER_UPLOAD = 8
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 60_000_000
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

DEFAULT_THEME = {
    "body_color": "#ffffff",
    "navbar_color": "#000000",
    "navbar_text_color": "#ffffff",
    "button_follow_navbar": "1",
    "button_color": "",          # empty = derive from navbar
    "button_text_color": "",     # empty = derive from navbar_text
}
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
        "SELECT key, value FROM app_settings WHERE key IN "
        "('body_color','navbar_color','navbar_text_color',"
        " 'button_follow_navbar','button_color','button_text_color')"
    ).fetchall())
    theme = dict(DEFAULT_THEME)
    for k in ("body_color", "navbar_color", "navbar_text_color", "button_color", "button_text_color"):
        if rows.get(k) and COLOUR_RE.match(rows[k]):
            theme[k] = rows[k]
    if rows.get("button_follow_navbar") in ("0", "1"):
        theme["button_follow_navbar"] = rows["button_follow_navbar"]
    # Resolve effective button colours
    if theme["button_follow_navbar"] == "1":
        theme["button_color"] = theme["navbar_color"]
        theme["button_text_color"] = theme["navbar_text_color"]
    else:
        if not theme["button_color"]:
            theme["button_color"] = theme["navbar_color"]
        if not theme["button_text_color"]:
            theme["button_text_color"] = theme["navbar_text_color"]
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
        "--btn-bg:" + t["button_color"] + ";"
        "--btn-text:" + t["button_text_color"] + ";"
        "}"
        ".top{background:var(--ink-navbar);color:var(--ink-navbar-text)}"
        ".top nav a{color:var(--ink-navbar-text);opacity:.85}"
        ".top nav a:hover,.top nav a:focus-visible{opacity:1;border-bottom-color:var(--ink-navbar-text)}"
        ".top .brand span{color:var(--ink-navbar-text)}"
        # Buttons follow --btn-bg / --btn-text
        ".btn{background:var(--btn-bg);border-color:var(--btn-bg);color:var(--btn-text)}"
        ".btn:hover{filter:brightness(0.88)}"
        ".btn.ghost{background:transparent;color:var(--btn-bg);border-color:var(--btn-bg)}"
        ".btn.ghost:hover{background:rgba(0,0,0,0.06);filter:none}"
        ".top .btn.ghost{color:var(--ink-navbar-text);border-color:rgba(255,255,255,.4)}"
        ".top .btn.ghost:hover{background:rgba(255,255,255,.12)}"
    )
    return Response(css, mimetype="text/css", headers={"Cache-Control": "public, max-age=300"})


# ---- gallery helpers ----------------------------------------------------
def _sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
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
            # button follow toggle
            follow = "1" if request.form.get("button_follow_navbar") == "1" else "0"
            if follow != get_theme()["button_follow_navbar"]:
                _set_setting(db, "button_follow_navbar", follow)
                changed.append("button_follow_navbar=" + follow)
            # only store overrides when NOT following
            if follow == "0":
                for key in ("button_color", "button_text_color"):
                    value = (request.form.get(key) or "").strip()
                    if COLOUR_RE.match(value) and value.upper() != (get_theme()[key] or "").upper():
                        _set_setting(db, key, value)
                        changed.append(f"{key}={value}")
            else:
                # clear overrides — they no longer apply
                _set_setting(db, "button_color", "")
                _set_setting(db, "button_text_color", "")
            if changed:
                audit(g.user["id"], "branding_changed", None, "; ".join(changed))
            g.pop("theme", None)
            db.commit()
            flash("Colours saved." if changed else "Nothing changed.", "ok")
            return redirect(url_for("admin_website"))
    count = get_db().execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    return render_template("admin_website.html", theme=get_theme(), photos_count=count)


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
                _commit_to_github(f"Gallery: add {added} photo(s)")
                flash(f"Added {added} photo{'s' if added != 1 else ''}.", "ok")
            for e in errors:
                flash(e + ". It was not added.", "error")
        return redirect(url_for("admin_gallery"))
      return render_template("admin_gallery.html", photos=_photos(),have_pil=HAVE_PIL, max_photos=MAX_PHOTOS, per_upload=MAX_PER_UPLOAD)


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
