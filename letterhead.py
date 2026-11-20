"""Persman: shared letterhead for every official document.

Produces an HTML snippet (logo embedded as base64 so it survives browser
print / PDF and HTML→docx conversion). Falls back to text-only if the logo
is missing or can't be loaded.
"""
import base64
import html
from pathlib import Path
from urllib.request import urlopen


_logo_cache = {}


def _load_logo_bytes(url):
    if not url:
        return None
    if url in _logo_cache:
        return _logo_cache[url]
    data = None
    try:
        if url.startswith("/"):
            from app import app, DATA
            if url.startswith("/static/"):
                path = Path(app.static_folder) / url[len("/static/"):]
                if path.exists():
                    data = path.read_bytes()
            else:
                for candidate in (DATA / "logo", DATA / "branding" / "logo",
                                  DATA / Path(url).name):
                    if candidate.exists():
                        data = candidate.read_bytes()
                        break
        else:
            with urlopen(url, timeout=5) as resp:
                data = resp.read()
    except Exception:
        data = None
    if data:
        _logo_cache[url] = data
    return data


def _guess_mime(url):
    u = (url or "").lower()
    if u.endswith(".png"):
        return "image/png"
    if u.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if u.endswith(".svg"):
        return "image/svg+xml"
    if u.endswith(".webp"):
        return "image/webp"
    return "image/png"


def letterhead_html(brand, subtitle):
    brand = brand or {}
    name = html.escape(str(brand.get("name") or ""))
    subtitle = html.escape(str(subtitle or ""))
    logo_html = ""
    if brand.get("has_logo") and brand.get("logo_url"):
        data = _load_logo_bytes(brand["logo_url"])
        if data:
            mime = _guess_mime(brand["logo_url"])
            b64 = base64.b64encode(data).decode("ascii")
            logo_html = (
                f'<img src="data:{mime};base64,{b64}" alt="" '
                'style="max-height:70px;max-width:220px;display:block;margin-bottom:10px">'
            )
    return (
        '<div class="doc-letterhead" '
        'style="border-bottom:2px solid #000;padding-bottom:12px;margin-bottom:22px">'
        + logo_html +
        f'<div style="font-size:1.4em;font-weight:700">{name}</div>'
        f'<div style="font-size:.95em;color:#555;text-transform:uppercase;'
        f'letter-spacing:.05em;margin-top:4px">{subtitle}</div>'
        '</div>'
    )
