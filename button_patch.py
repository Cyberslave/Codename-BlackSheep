"""Persman patch: buttons inherit navbar colour, with optional override."""
import os, re, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_button_colours")
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
# 1. website.py — theme now knows about buttons
# =========================================================================
print("\n[1/3] website.py — button colours in the theme")
backup("website.py")
w = read("website.py")

# 1a. Extend DEFAULT_THEME with button keys
old_default = 'DEFAULT_THEME = {"body_color": "#edf1f3", "navbar_color": "#14272d", "navbar_text_color": "#ffffff"}'
new_default = (
    'DEFAULT_THEME = {\n'
    '    "body_color": "#edf1f3",\n'
    '    "navbar_color": "#14272d",\n'
    '    "navbar_text_color": "#ffffff",\n'
    '    "button_follow_navbar": "1",\n'
    '    "button_color": "",          # empty = derive from navbar\n'
    '    "button_text_color": "",     # empty = derive from navbar_text\n'
    '}'
)
if new_default in w:
    check("DEFAULT_THEME extended", True, "(already present)")
elif old_default in w:
    w = w.replace(old_default, new_default, 1)
    check("DEFAULT_THEME extended", True, "(added button keys)")
else:
    check("DEFAULT_THEME extended", False, "not found")

# 1b. Extend get_theme to read the new keys
old_get = '''    rows = dict(get_db().execute(
        "SELECT key, value FROM app_settings WHERE key IN ('body_color','navbar_color','navbar_text_color')"
    ).fetchall())
    theme = dict(DEFAULT_THEME)
    for k in theme:
        if rows.get(k) and COLOUR_RE.match(rows[k]):
            theme[k] = rows[k]
    g.theme = theme
    return theme'''
new_get = '''    rows = dict(get_db().execute(
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
    return theme'''
if new_get in w:
    check("get_theme extended", True, "(already present)")
elif old_get in w:
    w = w.replace(old_get, new_get, 1)
    check("get_theme extended", True, "(now handles button colours)")
else:
    check("get_theme extended", False, "get_theme body not found")

# 1c. Extend theme.css with button rules
old_css = '''    css = (
        ":root{"
        "--bg:" + t["body_color"] + ";"
        "--ink-navbar:" + t["navbar_color"] + ";"
        "--ink-navbar-text:" + t["navbar_text_color"] + ";"
        "}"
        ".top{background:var(--ink-navbar);color:var(--ink-navbar-text)}"
        ".top nav a{color:var(--ink-navbar-text);opacity:.85}"
        ".top nav a:hover,.top nav a:focus-visible{opacity:1;border-bottom-color:var(--ink-navbar-text)}"
        ".top .brand span{color:var(--ink-navbar-text)}"
    )'''
new_css = '''    css = (
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
    )'''
if new_css in w:
    check("theme.css button rules", True, "(already present)")
elif old_css in w:
    w = w.replace(old_css, new_css, 1)
    check("theme.css button rules", True, "(added)")
else:
    check("theme.css button rules", False, "css builder not found — check manually")

# 1d. Extend the POST handler for colours
old_post = '''        if request.form.get("action") == "save_colours":
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
            return redirect(url_for("admin_website"))'''
new_post = '''        if request.form.get("action") == "save_colours":
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
            return redirect(url_for("admin_website"))'''
if new_post in w:
    check("save handler extended", True, "(already present)")
elif old_post in w:
    w = w.replace(old_post, new_post, 1)
    check("save handler extended", True, "(handles button colours)")
else:
    check("save handler extended", False, "POST block not found")

write("website.py", w)

# =========================================================================
# 2. templates/admin_website.html — button controls
# =========================================================================
print("\n[2/3] templates/admin_website.html — button controls")
tpl = read("templates/admin_website.html")

old_form_end = '''    <p class="muted small">Pick readable combinations — dark text on a light bar, or white text on a dark bar. Leave the defaults to undo.</p>
    <button class="btn" type="submit">Save colours</button>
  </form>'''

new_form_end = '''    <p class="muted small">Pick readable combinations — dark text on a light bar, or white text on a dark bar. Leave the defaults to undo.</p>

    <h3 style="margin-top:1.25rem">Buttons</h3>
    <label class="check">
      <input type="checkbox" name="button_follow_navbar" value="1" id="btn-follow"
             {{ 'checked' if theme.button_follow_navbar == '1' }}>
      Use the navigation bar colour for buttons (recommended)
    </label>

    <div id="btn-overrides" style="{{ 'display:none' if theme.button_follow_navbar == '1' }}">
      <div class="grid">
        <label>Button colour <input type="color" name="button_color" value="{{ theme.button_color or theme.navbar_color }}"></label>
        <label>Button text <input type="color" name="button_text_color" value="{{ theme.button_text_color or theme.navbar_text_color }}"></label>
      </div>
      <p class="muted small">Only used when "Use the navigation bar colour" is unticked.</p>
    </div>

    <button class="btn" type="submit">Save colours</button>
  </form>

  <script>
    (function () {
      var cb = document.getElementById("btn-follow");
      var box = document.getElementById("btn-overrides");
      if (!cb || !box) return;
      cb.addEventListener("change", function () {
        box.style.display = cb.checked ? "none" : "";
      });
    })();
  </script>'''

if new_form_end in tpl:
    check("admin_website.html: button controls", True, "(already present)")
elif old_form_end in tpl:
    tpl = tpl.replace(old_form_end, new_form_end, 1)
    write("templates/admin_website.html", tpl)
    check("admin_website.html: button controls", True, "(added)")
else:
    check("admin_website.html: button controls", False,
          "colour form ending not found — check manually")

# =========================================================================
# 3. Allow inline <script> in this template — CSP has no script-src
#    so inline scripts in templates will be blocked. That's a problem for
#    the show/hide of button overrides above. Options:
#    a) use a checked attribute that toggles via CSS (:has), but not
#       supported everywhere.
#    b) move the toggle into a separate static/js file.
#    c) relax CSP for this one page.
#    (b) is cleanest. Let's do (b).
# =========================================================================
print("\n[3/3] move the toggle script to static/site-colours.js")
JS = '''// Show / hide button colour overrides based on the "follow navbar" checkbox.
(function () {
  function init() {
    var cb = document.getElementById("btn-follow");
    var box = document.getElementById("btn-overrides");
    if (!cb || !box) return;
    function sync() { box.style.display = cb.checked ? "none" : ""; }
    cb.addEventListener("change", sync);
    sync();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
'''
write("static/site-colours.js", JS)

# remove the inline <script> block from the template, reference the file instead
tpl = read("templates/admin_website.html")
inline_script = '''  <script>
    (function () {
      var cb = document.getElementById("btn-follow");
      var box = document.getElementById("btn-overrides");
      if (!cb || !box) return;
      cb.addEventListener("change", function () {
        box.style.display = cb.checked ? "none" : "";
      });
    })();
  </script>'''
external = '  <script src="{{ url_for(\'static\', filename=\'site-colours.js\') }}" defer></script>'
if inline_script in tpl:
    tpl = tpl.replace(inline_script, external, 1)
    write("templates/admin_website.html", tpl)
    check("inline script moved to static/site-colours.js", True, "(replaced)")
else:
    if external not in tpl:
        # add it right after the </form>
        tpl = tpl.replace("</form>", "</form>\n" + external, 1)
        write("templates/admin_website.html", tpl)
        check("inline script moved to static/site-colours.js", True, "(appended)")
    else:
        check("inline script moved to static/site-colours.js", True, "(already external)")

# =========================================================================
# Summary
# =========================================================================
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHANGES APPLIED")
    print("\nRestart the app and open /admin/website.")
    print("  - New 'Buttons' section under the colour pickers")
    print("  - Ticked (default): buttons use the navbar colour")
    print("  - Untick to reveal button-colour + button-text pickers")
    print("  - Save → buttons across the site follow the choice")
else:
    print("SOME CHECKS FAILED:")
    for name, ok in results:
        if not ok:
            print("  -", name)
    print("\nPaste the output above.")