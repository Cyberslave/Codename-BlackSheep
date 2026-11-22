"""Persman: public homepage — visible to signed-out visitors.

Shows the gallery (from website.py) and a preview of open vacancies.
"""
from flask import redirect, render_template, url_for

from app import app, get_db, g, now
from features import get_brand


@app.route("/")
def public_home():
    # Signed-in users keep their existing home (admin dashboard or /me)
    if g.user:
        return redirect(url_for("home"))

    db = get_db()
    # Latest 6 photos for the gallery preview
    photos = db.execute(
        "SELECT * FROM photos ORDER BY position, id LIMIT 6").fetchall()
    # Latest 3 open vacancies
    vacancies = db.execute(
        "SELECT * FROM vacancies WHERE status='open' "
        "AND (closes_on IS NULL OR closes_on >= ?) "
        "ORDER BY created_at DESC LIMIT 3",
        (now().date().isoformat(),)).fetchall()
    return render_template("public_home.html", photos=photos, vacancies=vacancies)