"""Real-browser test (needs: pip install playwright && playwright install chromium).
Run with: python tests/browser_test.py"""
import datetime as dt
import os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TMP = tempfile.mkdtemp(prefix="persman_browser_")
PORT = 5099
BASE = f"http://127.0.0.1:{PORT}"
env = dict(os.environ, PERSMAN_DATA=TMP, PERSMAN_DEV="1")
out = subprocess.run([sys.executable, "-m", "flask", "--app", "app", "create-admin", "--name", "Boss", "--password", "Adm1nPassword9"],
                     cwd=ROOT, env=env, capture_output=True, text=True).stdout
ADMIN = re.search(r"ID: (\S+)", out).group(1)
srv = subprocess.Popen([sys.executable, "-m", "flask", "--app", "app", "run", "-p", str(PORT)], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try: urllib.request.urlopen(BASE + "/login"); break
    except Exception: time.sleep(0.2)

problems = []
steps = 0
def step(msg):
    global steps; steps += 1; print(f"  ok {steps}: {msg}")

def watch(page, name):
    page.on("console", lambda m: problems.append(f"[{name}] console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"[{name}] page error: {e}"))

def signin(page, eid, pw):
    page.goto(BASE + "/login"); page.fill("[name=employee_id]", eid); page.fill("[name=password]", pw)
    page.click("main button[type=submit]")

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx_admin, ctx_a, ctx_b = (browser.new_context(accept_downloads=True) for _ in range(3))
        admin, a, b = ctx_admin.new_page(), ctx_a.new_page(), ctx_b.new_page()
        for pg, n in ((admin, "admin"), (a, "alice"), (b, "bob")): watch(pg, n)

        # admin creates two employees
        signin(admin, ADMIN, "Adm1nPassword9")
        creds = {}
        for name, level in (("Alice Mokoena", "MGT"), ("Bob Thabane", "STF")):
            admin.goto(BASE + "/admin/employees/new"); admin.fill("[name=full_name]", name); admin.select_option("[name=level]", level)
            admin.click("main button[type=submit]")
            creds[name] = (admin.locator("dd.mono").nth(0).inner_text(), admin.locator("dd.mono").nth(1).inner_text())
        step("admin created employees")
        for pg, name in ((a, "Alice Mokoena"), (b, "Bob Thabane")):
            eid, pw = creds[name]; signin(pg, eid, pw)
            pg.fill("[name=current]", pw); pg.fill("[name=new]", "Secret2026pw"); pg.fill("[name=confirm]", "Secret2026pw"); pg.click("main button[type=submit]")
            expect(pg.locator("h1")).to_contain_text(name)
        con = sqlite3.connect(os.path.join(TMP, "persman.db"))
        bob_ref = con.execute("SELECT ref FROM employees WHERE full_name='Bob Thabane'").fetchone()[0]
        alice_ref = con.execute("SELECT ref FROM employees WHERE full_name='Alice Mokoena'").fetchone()[0]
        step("employees signed in and set passwords")

        # ---- document editor ----
        a.goto(BASE + "/workspace"); a.click("text=New document")
        expect(a.locator("#editor")).to_have_attribute("contenteditable", "true")
        a.fill("#title", "Quarterly report")
        a.click("#editor"); a.keyboard.type("Quarterly summary")
        a.keyboard.press("Control+A"); a.click("[data-cmd=bold]"); a.click("[data-cmd=h1]"); a.click("[data-cmd=center]")
        a.keyboard.press("End"); a.keyboard.press("Enter"); a.keyboard.type("Sales grew steadily.")
        answers = ["2", "3"]; a.on("dialog", lambda d: d.accept(answers.pop(0)))
        a.click("[data-cmd=table]")
        a.click("[data-cmd=insertUnorderedList]"); a.keyboard.type("point one")
        expect(a.locator("#status")).to_have_text("Unsaved changes")
        a.click("#save"); expect(a.locator("#status")).to_contain_text("Saved at")
        step("document edited and saved")
        a.reload()
        expect(a.locator("#editor h1")).to_contain_text("Quarterly summary")
        expect(a.locator("#editor h1")).to_have_class(re.compile("ta-center"))
        expect(a.locator("#editor table td")).to_have_count(6)
        expect(a.locator("#title")).to_have_value("Quarterly report")
        step("formatting, heading, alignment and table survive a reload")
        a.click("#editor"); a.keyboard.type(" EXTRA"); a.keyboard.press("Control+S")
        expect(a.locator("#status")).to_contain_text("Saved at")
        step("Ctrl+S saves")
        from docx import Document as WordDoc
        a.click("#editor"); a.keyboard.type(" UNSAVED-TAIL")
        with a.expect_download() as dl: a.click("#export")
        wd = WordDoc(dl.value.path())
        alltext = " | ".join([p_.text for p_ in wd.paragraphs] + [c_.text for t_ in wd.tables for r_ in t_.rows for c_ in r_.cells])
        assert "UNSAVED-TAIL" in alltext, alltext
        assert any(p_.style.name == "Heading 1" and "Quarterly summary" in p_.text for p_ in wd.paragraphs), [(p_.style.name, p_.text) for p_ in wd.paragraphs]
        assert len(wd.tables) == 1 and len(wd.tables[0].rows) == 2 and any(p_.style.name == "List Bullet" for p_ in wd.paragraphs)
        assert dl.value.suggested_filename.endswith(".docx")
        step("Download Word saves unsaved typing first, and the .docx has the text and heading")

        # share with Bob read-only, check read-only view in a real browser
        a.goto(BASE + "/workspace"); a.click("text=Share"); a.select_option("[name=ref]", bob_ref); a.click("button:has-text('Share')")
        b.goto(BASE + "/workspace"); b.click("text=Quarterly report")
        expect(b.locator("#editor")).not_to_have_attribute("contenteditable", "true")
        expect(b.locator("#status")).to_have_text("View only")
        step("shared document is read-only for the colleague")

        # ---- spreadsheet ----
        a.goto(BASE + "/workspace"); a.click("text=New spreadsheet")
        a.fill("#title", "Budget")
        a.fill("input[data-key=A1]", "10"); a.fill("input[data-key=A2]", "20"); a.fill("input[data-key=A3]", "=SUM(A1:A2)")
        a.fill("input[data-key=B1]", '=IF(A3>25,"High","Low")'); a.fill("input[data-key=B2]", "=1/0"); a.fill("input[data-key=B3]", "=B3+1")
        a.focus("input[data-key=C1]")
        expect(a.locator("input[data-key=A3]")).to_have_value("30")
        expect(a.locator("input[data-key=B1]")).to_have_value("High")
        expect(a.locator("input[data-key=B2]")).to_have_value("#DIV/0!")
        expect(a.locator("input[data-key=B3]")).to_have_value("#CIRC!")
        a.fill("input[data-key=A1]", "100"); a.focus("input[data-key=C1]")
        expect(a.locator("input[data-key=A3]")).to_have_value("120")
        step("formulas calculate and update live (sum, IF, errors, circular)")
        a.click("input[data-key=A1]"); a.keyboard.press("Enter")
        assert a.evaluate("document.activeElement.dataset.key") == "A2"; step("Enter moves down a row")
        a.click("#add-rows"); expect(a.locator("table.sheet tbody tr")).to_have_count(85)
        a.click("#save"); expect(a.locator("#status")).to_contain_text("Saved at")
        a.reload()
        expect(a.locator("input[data-key=A3]")).to_have_value("120")
        a.focus("input[data-key=A3]"); expect(a.locator("input[data-key=A3]")).to_have_value("=SUM(A1:A2)")
        step("spreadsheet persists; focusing a cell shows its formula")
        with a.expect_download() as dl: a.click("#csv")
        path = dl.value.path(); csv_text = open(path, encoding="utf-8-sig").read()
        assert csv_text.splitlines()[0].startswith("100,High") and "120" in csv_text, csv_text
        step("CSV export contains calculated values")
        from openpyxl import load_workbook
        a.fill("input[data-key=A1]", "777"); a.focus("input[data-key=C1]")
        with a.expect_download() as dl: a.click("#export")
        xlsx_path = os.path.join(TMP, "download.xlsx"); dl.value.save_as(xlsx_path); ws = load_workbook(xlsx_path).active
        assert ws["A1"].value == 777 and ws["A3"].value == "=SUM(A1:A2)" and ws["B1"].value == '=IF(A3>25,"High","Low")', (ws["A1"].value, ws["A3"].value)
        step("Download Excel saves first; numbers and live formulas arrive in the .xlsx")
        a.goto(BASE + "/settings"); expect(a.locator("h1")).to_have_text("Account"); expect(a.locator("input[name=email_notify]")).to_be_checked()
        step("account page shows email alert setting")

        # ---- messaging ----
        a.goto(f"{BASE}/messages/{bob_ref}")
        a.fill("#msgform textarea", "Hello Bob <b>x</b>"); a.click("#msgform button")
        expect(a.locator(".msg.mine p")).to_have_text("Hello Bob <b>x</b>")
        b.goto(f"{BASE}/messages/{alice_ref}")
        expect(b.locator(".msg.theirs p")).to_have_text("Hello Bob <b>x</b>")
        step("message sent live and shown (escaped) to the recipient")
        b.fill("#msgform textarea", "Hi Alice"); b.keyboard.press("Control+Enter")
        expect(b.locator(".msg.mine p")).to_have_text("Hi Alice")
        expect(a.locator(".msg.theirs p")).to_have_text("Hi Alice", timeout=15000)
        step("reply arrives by live polling without a reload")

        # ---- calendar + toast notification ----
        b.goto(BASE + "/workspace"); b.wait_for_timeout(2500)
        soon = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(minutes=10)
        a.goto(BASE + "/calendar/events/new")
        a.fill("[name=title]", "Stand-up"); a.fill("[name=start_date]", soon.strftime("%Y-%m-%d")); a.fill("[name=start_time]", soon.strftime("%H:%M"))
        a.select_option("[name=reminder]", "15"); a.check(f"input[name=attendees][value='{bob_ref}']"); a.click("button:has-text('Create event')")
        expect(a.locator("h1")).to_have_text("Stand-up")
        b.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(b.locator(".toast").first).to_be_visible(timeout=8000)
        expect(b.locator("#badge-notifications")).to_be_visible()
        step("invitee gets a pop-up alert and a badge without reloading")
        b.goto(BASE + "/calendar"); expect(b.locator("a.chip", has_text="Stand-up")).to_be_visible()
        b.goto(BASE + "/notifications"); expect(b.locator("ul.notes li.unread")).not_to_have_count(0)
        b.click("ul.notes li.unread a >> nth=0"); expect(b.locator("h1")).to_have_text("Stand-up")
        step("calendar shows the event; alert opens it and marks it read")

        # ---- branding ----
        import struct, zlib
        def tiny_png():
            raw = b"".join(b"\x00" + bytes((20, 120, 160)) * 4 for _ in range(4))
            chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
            return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        logo = os.path.join(TMP, "logo.png"); open(logo, "wb").write(tiny_png())
        expect(a.locator(".brand span")).to_have_text("Persman")
        admin.goto(BASE + "/admin/branding"); admin.fill("[name=company_name]", "Northwind Traders"); admin.set_input_files("[name=logo]", logo)
        admin.click("main button:has-text('Save branding')")
        expect(admin.locator(".flash")).to_contain_text("Branding saved")
        expect(admin.locator("header .brand span")).to_have_text("Northwind Traders")
        assert admin.evaluate("document.querySelector('header .brand img').naturalWidth") == 4
        a.goto(BASE + "/calendar"); expect(a.locator("header .brand span")).to_have_text("Northwind Traders"); expect(a).to_have_title(re.compile("Northwind Traders"))
        assert a.evaluate("document.querySelector('header .brand img').complete && document.querySelector('header .brand img').naturalWidth") == 4
        guest = browser.new_page(); watch(guest, "guest"); guest.goto(BASE + "/login")
        expect(guest.locator(".loginbrand .company")).to_have_text("Northwind Traders"); expect(guest.locator(".loginbrand img")).to_be_visible()
        step("company name and logo appear in the header, tab title and sign-in page for everyone")
        browser.close()
finally:
    srv.terminate(); shutil.rmtree(TMP, ignore_errors=True)

print("PROBLEMS:", *problems, sep="\n  " if problems else " ") if problems else print("no console errors or CSP violations")
sys.exit(1 if problems else 0)
