"""End-to-end tests for Persman. Run with: python tests/server_test.py"""
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="persman_test_")
os.environ["PERSMAN_DATA"] = TMP
os.environ["PERSMAN_DEV"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# --- an "old" database (before this release) must be upgraded in place ------
con = sqlite3.connect(os.path.join(TMP, "persman.db"))
con.execute("""CREATE TABLE employees (id TEXT PRIMARY KEY, full_name TEXT NOT NULL, email TEXT, phone TEXT,
  department TEXT, job_title TEXT, level TEXT NOT NULL, password_hash TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 1, is_admin INTEGER NOT NULL DEFAULT 0,
  failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created_at TEXT NOT NULL, created_by TEXT)""")
con.execute("INSERT INTO employees (id, full_name, level, password_hash, created_at) VALUES ('STF-25-OLD1','Old Timer','STF','x','2025-01-01T00:00:00')")
con.commit(); con.close()

import app as A  # noqa: E402
import features as Fe  # noqa: E402
from click.testing import CliRunner  # noqa: E402

ok = 0
def check(cond, msg):
    global ok
    assert cond, "FAILED: " + msg
    ok += 1

# ---- migration ------------------------------------------------------------
con = sqlite3.connect(A.DB_PATH)
ref = con.execute("SELECT ref FROM employees WHERE id='STF-25-OLD1'").fetchone()[0]
check(ref and len(ref) == 16, "existing employee got a ref")
con.close()

# ---- sanitiser --------------------------------------------------------------
dirty = ('<p onclick="x()" style="text-align:center">Hi<script>alert(1)</script></p>'
         '<a href="javascript:alert(1)">bad</a><a href="https://ok.example/x?a=1&b=2">good</a>'
         '<img src=x onerror=alert(1)><iframe src="//evil"></iframe><b>bold</b><style>p{}</style>'
         '<td colspan="2" rowspan="999">c</td><h1 class="ta-right evil">t</h1><div>unclosed')
clean = Fe.clean_html(dirty)
for bad in ("<script", "onclick", "javascript:", "<img", "onerror", "<iframe", "<style", "rowspan", "evil\""):
    check(bad not in clean, f"sanitiser removed {bad!r}")
check('class="ta-center"' in clean and 'class="ta-right"' in clean, "alignment kept as class")
check('href="https://ok.example/x?a=1&amp;b=2"' in clean and "<b>bold</b>" in clean, "safe markup kept")
check(clean.endswith("</div>"), "unclosed tags are closed")
check(Fe.clean_html("") == "<p><br></p>", "empty doc gets a paragraph")
sheet = Fe.clean_sheet({"rows": 99999, "cells": {"A1": 5, "ZZZ9": "x", "B2": "=A1*2", "bad": "y", "C3": ""}})
check(sheet["rows"] == 1000 and sheet["cells"] == {"A1": "5", "B2": "=A1*2"}, "sheet validated")

# ---- helpers ------------------------------------------------------------------
def token(c, url="/login"):
    return re.search(r'name="csrf" content="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)
def login(c, eid, pw):
    return c.post("/login", data={"_csrf": token(c), "employee_id": eid, "password": pw}, follow_redirects=True)
def api(c, method, url, body=None, csrf=True):
    h = {"X-CSRF-Token": token(c, "/notifications")} if csrf else {}
    return getattr(c, method)(url, json=body, headers=h)
def post(c, url, data=None, page="/notifications", **kw):
    d = dict(data or {}); d["_csrf"] = token(c, page)
    return c.post(url, data=d, **kw)

r = CliRunner().invoke(A.create_admin, ["--name", "Boss Admin", "--password", "Adm1nPassword9"])
admin_id = re.search(r"ID: (\S+)", r.output).group(1)
adm = A.app.test_client(); login(adm, admin_id, "Adm1nPassword9")

def make_employee(name, level):
    html = adm.post("/admin/employees/new", data={"_csrf": token(adm, "/admin/employees/new"), "full_name": name, "level": level, "job_title": "Tester"}).get_data(as_text=True)
    eid = re.search(r'mono">([A-Z]{3}-\d\d-[A-Z0-9]{4})<', html).group(1)
    pw = re.search(r'Temporary password</dt><dd class="mono">([^<]+)<', html).group(1)
    c = A.app.test_client(); login(c, eid, pw)
    c.post("/password", data={"_csrf": token(c, "/password"), "current": pw, "new": "Secret2026pw", "confirm": "Secret2026pw"})
    ref = sqlite3.connect(A.DB_PATH).execute("SELECT ref FROM employees WHERE id=?", (eid,)).fetchone()[0]
    return eid, ref, c

alice_id, alice_ref, alice = make_employee("Alice Mokoena", "MGT")
bob_id, bob_ref, bob = make_employee("Bob Thabane", "STF")
carol_id, carol_ref, carol = make_employee("Carol Ntho", "STF")
db = lambda: sqlite3.connect(A.DB_PATH)

# ---- workspace documents ---------------------------------------------------------
r = post(alice, "/workspace/new", {"kind": "doc"})
doc_url = r.headers["Location"]; doc_id = int(doc_url.rsplit("/", 1)[1])
check((A.EMP_DIR / alice_id / "workspace" / f"{doc_id}.html").exists(), "document stored in employee folder")
page = alice.get(doc_url).get_data(as_text=True)
check('contenteditable="true"' in page, "owner gets the editor")
r = api(alice, "post", f"/api/workspace/{doc_id}/save", {"title": "Plan", "content": dirty, "base_version": 1})
check(r.status_code == 200 and r.get_json()["version"] == 2, "save works")
saved = (A.EMP_DIR / alice_id / "workspace" / f"{doc_id}.html").read_text()
check("<script" not in saved and "onclick" not in saved, "saved file is sanitised")
r = api(alice, "post", f"/api/workspace/{doc_id}/save", {"title": "Plan", "content": "<p>x</p>", "base_version": 1})
check(r.status_code == 409, "stale save is rejected, not silently overwritten")
r = api(alice, "post", f"/api/workspace/{doc_id}/save", {"title": "Plan", "content": "<p>x</p>", "base_version": 2}, csrf=False)
check(r.status_code == 400 and "error" in r.get_json(), "API call without CSRF token is refused with JSON")
check(A.app.test_client().post(f"/api/workspace/{doc_id}/save", json={}).status_code in (400, 401), "anonymous API call refused")
check(bob.get(doc_url).status_code == 404, "other employees cannot open it")
check(api(bob, "post", f"/api/workspace/{doc_id}/save", {"content": "<p>hack</p>", "base_version": 2}).status_code == 404, "other employees cannot save")
r = post(alice, f"/workspace/{doc_id}/share", {"action": "add", "ref": bob_ref, "perm": "view"}, page=f"/workspace/{doc_id}/share")
check(db().execute("SELECT COUNT(*) FROM notifications WHERE employee_id=? AND kind='share'", (bob_id,)).fetchone()[0] == 1, "share notifies")
page = bob.get(doc_url).get_data(as_text=True)
check(bob.get(doc_url).status_code == 200 and 'contenteditable="true"' not in page, "view-only share is read-only")
check(api(bob, "post", f"/api/workspace/{doc_id}/save", {"content": "<p>hack</p>", "base_version": 2}).status_code == 403, "view-only user cannot save")
post(alice, f"/workspace/{doc_id}/share", {"action": "add", "ref": bob_ref, "perm": "edit"}, page=f"/workspace/{doc_id}/share")
r = api(bob, "post", f"/api/workspace/{doc_id}/save", {"title": "Plan v2", "content": "<p>bob was here</p>", "base_version": 2})
check(r.status_code == 200, "edit share can save")
check(adm.get(doc_url).status_code == 200 and api(adm, "post", f"/api/workspace/{doc_id}/save", {"content": "x", "base_version": 3}).status_code == 403, "admin can view but not edit")
check(post(bob, f"/workspace/{doc_id}/delete").status_code == 403, "editor cannot delete someone else's document")
check(b"bob was here" in alice.get(f"/workspace/{doc_id}/download").data, "HTML download works")
check(carol.get("/workspace").status_code == 200 and b"Plan v2" in bob.get("/workspace").data, "shared doc listed for bob")

r = post(alice, "/workspace/new", {"kind": "sheet"}); sheet_id = int(r.headers["Location"].rsplit("/", 1)[1])
page = alice.get(f"/workspace/{sheet_id}").get_data(as_text=True)
check('id="sheet-data"' in page and "formula.js" in page, "spreadsheet page loads")
r = api(alice, "post", f"/api/workspace/{sheet_id}/save", {"title": "Budget", "content": {"rows": 80, "cells": {"A1": "10", "A2": "=A1*2", "nope": "x"}}, "base_version": 1})
check(r.status_code == 200, "sheet saves")
stored = json.loads((A.EMP_DIR / alice_id / "workspace" / f"{sheet_id}.json").read_text())
check(stored["cells"] == {"A1": "10", "A2": "=A1*2"} and stored["rows"] == 80, "sheet stored cleanly")
check(api(alice, "post", f"/api/workspace/{sheet_id}/save", {"content": "not-an-object", "base_version": 2}).status_code == 400, "bad sheet data rejected")
check(api(alice, "post", f"/api/workspace/{doc_id}/save", {"content": "a" * (3 * 1024 * 1024), "base_version": 3}).status_code == 413, "oversized document rejected")

r = post(alice, f"/workspace/{sheet_id}/delete")
check(not (A.EMP_DIR / alice_id / "workspace" / f"{sheet_id}.json").exists() and r.status_code == 302, "owner can delete (file removed)")

# ---- calendar ----------------------------------------------------------------------
t0 = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
start = t0 + dt.timedelta(minutes=10)
form = {"title": "Team <b>meeting</b>", "start_date": start.strftime("%Y-%m-%d"), "start_time": start.strftime("%H:%M"),
        "end_date": start.strftime("%Y-%m-%d"), "end_time": (start + dt.timedelta(hours=1)).strftime("%H:%M"),
        "reminder": "15", "location": "Room 2", "attendees": [bob_ref, alice_ref], "everyone": "1"}
r = post(alice, "/calendar/events/new", form, page="/calendar/events/new")
ev_id = int(r.headers["Location"].rsplit("/", 1)[1])
row = db().execute("SELECT audience FROM events WHERE id=?", (ev_id,)).fetchone()
check(row[0] == "invited", "non-admin cannot make company-wide events")
check(db().execute("SELECT COUNT(*) FROM event_attendees WHERE event_id=?", (ev_id,)).fetchone()[0] == 1, "owner not invited to own event")
check(db().execute("SELECT COUNT(*) FROM notifications WHERE employee_id=? AND kind='invite'", (bob_id,)).fetchone()[0] == 1, "invitee notified")
s = bob.get("/api/summary?after=0").get_json()
check(any(n["title"].startswith("Reminder:") for n in s["new"]), "reminder generated when due")
check(len(bob.get("/api/summary?after=0").get_json()["new"]) == len(s["new"]), "reminder not repeated")
check(any(n["title"].startswith("Reminder:") for n in alice.get("/api/summary?after=0").get_json()["new"]), "owner gets a reminder too")
cal = bob.get("/calendar").get_data(as_text=True)
check("Team &lt;b&gt;meeting&lt;/b&gt;" in cal and "<b>meeting" not in cal, "event title is escaped")
check(carol.get(f"/calendar/events/{ev_id}").status_code == 404, "uninvited employee cannot see a private event")
post(bob, f"/calendar/events/{ev_id}/rsvp", {"status": "accepted"}, page=f"/calendar/events/{ev_id}")
check(db().execute("SELECT COUNT(*) FROM notifications WHERE employee_id=? AND kind='rsvp'", (alice_id,)).fetchone()[0] == 1, "owner told about RSVP")
check(post(carol, f"/calendar/events/{ev_id}/rsvp", {"status": "accepted"}, page="/calendar").status_code in (400, 404), "non-attendee cannot RSVP")
later = start + dt.timedelta(hours=3)
form2 = dict(form, start_time=later.strftime("%H:%M"), end_time=(later + dt.timedelta(hours=1)).strftime("%H:%M"), start_date=later.strftime("%Y-%m-%d"), end_date=later.strftime("%Y-%m-%d"), attendees=[bob_ref, carol_ref])
post(alice, f"/calendar/events/{ev_id}/edit", form2, page=f"/calendar/events/{ev_id}/edit")
check(db().execute("SELECT COUNT(*) FROM notifications WHERE employee_id=? AND kind='update'", (bob_id,)).fetchone()[0] == 1, "time change notifies attendees")
check(db().execute("SELECT COUNT(*) FROM notifications WHERE employee_id=? AND kind='invite'", (carol_id,)).fetchone()[0] == 1, "new invitee notified")
check(db().execute("SELECT COUNT(*) FROM event_reminders WHERE event_id=?", (ev_id,)).fetchone()[0] == 0, "reminders reset after reschedule")
check(post(bob, f"/calendar/events/{ev_id}/edit", form2, page="/calendar").status_code == 403, "attendee cannot edit")
bad = dict(form, end_date=(start - dt.timedelta(days=2)).strftime("%Y-%m-%d"))
check(b"cannot end before" in post(alice, "/calendar/events/new", bad, page="/calendar/events/new", follow_redirects=True).data, "end before start rejected")
post(bob, f"/calendar/events/{ev_id}/rsvp", {"status": "declined"}, page=f"/calendar/events/{ev_id}")
check(b"Team &lt;b&gt;" not in bob.get(f"/calendar?month={later.strftime('%Y-%m')}").data, "declined event hidden")
check(post(bob, f"/calendar/events/{ev_id}/delete", page="/calendar").status_code == 403, "attendee cannot delete")
post(alice, f"/calendar/events/{ev_id}/delete", page="/calendar")
check(db().execute("SELECT COUNT(*) FROM notifications WHERE employee_id=? AND kind='cancel'", (carol_id,)).fetchone()[0] == 1, "cancellation notifies")

# company-wide, all-day event by admin
d = (t0 + dt.timedelta(days=1)).strftime("%Y-%m-%d")
r = post(adm, "/calendar/events/new", {"title": "Year-end party", "start_date": d, "all_day": "1", "everyone": "1", "reminder": ""}, page="/calendar/events/new")
cid = int(r.headers["Location"].rsplit("/", 1)[1])
check(db().execute("SELECT COUNT(*) FROM notifications WHERE kind='invite' AND title LIKE 'Company event%'").fetchone()[0] >= 3, "company event notifies everyone")
check(b"Year-end party" in carol.get(f"/calendar?month={d[:7]}").data, "company event visible to all")

# ---- notifications ---------------------------------------------------------------------
db_ = db(); db_.execute("INSERT INTO notifications (employee_id, kind, title, link, created_at) VALUES (?,?,?,?,?)", (bob_id, "x", "evil", "https://evil.example", "2026-01-01T00:00:00")); db_.commit()
nid = db_.execute("SELECT MAX(id) FROM notifications").fetchone()[0]
r = bob.get(f"/notifications/{nid}/open")
check(r.headers["Location"].endswith("/notifications"), "external notification link is not followed")
check(alice.get(f"/notifications/{nid}/open").status_code == 404, "cannot open someone else's notification")
post(bob, "/notifications/read-all")
check(bob.get("/api/summary").get_json()["notifications"] == 0, "mark all read")

# ---- messaging ------------------------------------------------------------------------------
r = api(alice, "post", f"/api/messages/{bob_ref}", {"body": "Hi <script>alert(1)</script>"})
check(r.status_code == 200 and r.get_json()["mine"], "message sent")
check(bob.get("/api/summary").get_json()["messages"] == 1, "unread count")
page = bob.get(f"/messages/{alice_ref}").get_data(as_text=True)
check("&lt;script&gt;" in page and "<script>alert" not in page, "message body is escaped")
check(bob.get("/api/summary").get_json()["messages"] == 0, "opening the thread marks read")
api(bob, "post", f"/api/messages/{alice_ref}", {"body": "Hello back"})
got = alice.get(f"/api/messages/{bob_ref}?after=0").get_json()["messages"]
check([m["body"] for m in got] == ["Hello back"] and not got[0]["mine"], "poll returns only incoming")
check(alice.get(f"/messages/{alice_ref}").status_code == 404, "cannot message yourself")
check(alice.get("/messages/doesnotexist").status_code == 404, "unknown person")
check(api(alice, "post", f"/api/messages/{bob_ref}", {"body": "  "}).status_code == 400, "empty message refused")
check(alice_id not in bob.get("/messages/new").get_data(as_text=True) and alice_id not in bob.get("/calendar/events/new").get_data(as_text=True), "login IDs are not exposed to colleagues")
check("Hello back" not in adm.get("/messages").get_data(as_text=True), "admin cannot read others' messages")
check(post(bob, "/messages/broadcast", {"audience": "all", "body": "hi"}, page="/messages").status_code == 403, "only admin can broadcast")
post(adm, "/messages/broadcast", {"audience": "STF", "body": "Staff meeting moved"}, page="/messages/new")
check(db().execute("SELECT COUNT(*) FROM messages WHERE body='Staff meeting moved'").fetchone()[0] == 3, "broadcast reaches exactly the STF-level employees (Bob, Carol and the migrated Old Timer)")

# ---- Word / Excel export ---------------------------------------------------------------------------
import io, email, email.utils, socket, socketserver, struct, threading, zlib
from docx import Document as DocxDocument
from openpyxl import load_workbook
api(alice, "post", f"/api/workspace/{doc_id}/save", {"title": "Pelo ya Basotho – plan", "content": "<h1>Heading</h1><p>bob was here <b>twice</b></p>", "base_version": db().execute("SELECT version FROM workdocs WHERE id=?", (doc_id,)).fetchone()[0]})
r = alice.get(f"/workspace/{doc_id}/export.docx")
check(r.status_code == 200 and r.mimetype.endswith("wordprocessingml.document") and "filename*=UTF-8''" in r.headers["Content-Disposition"], "Word export served with a safe download name")
wd = DocxDocument(io.BytesIO(r.data))
check(any(p.text == "Heading" and p.style.name == "Heading 1" for p in wd.paragraphs) and any("bob was here" in p.text for p in wd.paragraphs), "Word file has the saved content")
check(bob.get(f"/workspace/{doc_id}/export.docx").status_code == 200 and adm.get(f"/workspace/{doc_id}/export.docx").status_code == 200, "shared user and admin can export")
check(carol.get(f"/workspace/{doc_id}/export.docx").status_code == 404, "no access, no export")
check(alice.get(f"/workspace/{doc_id}/export.xlsx").status_code == 404, "wrong type refused")
check(db().execute("SELECT COUNT(*) FROM audit WHERE action='workspace_exported'").fetchone()[0] >= 3, "exports are recorded in the audit trail")
r = post(alice, "/workspace/new", {"kind": "sheet"}); xid = int(r.headers["Location"].rsplit("/", 1)[1])
api(alice, "post", f"/api/workspace/{xid}/save", {"title": "Budget", "base_version": 1, "content": {"rows": 60, "cells": {
    "A1": "10", "A2": "32", "A3": "=SUM(A1:A2)", "B1": "=cmd|' /C calc'!A0", "B2": "Total"}}})
r = alice.get(f"/workspace/{xid}/export.xlsx")
ws = load_workbook(io.BytesIO(r.data)).active
check(r.status_code == 200 and ws["A3"].value == "=SUM(A1:A2)" and ws["A1"].value == 10 and ws["B1"].data_type == "s", "Excel export has numbers, formulas and neutralised unsafe cells")
Fe.exporters.HAVE_DOCX = False
r = alice.get(f"/workspace/{doc_id}/export.docx", follow_redirects=True)
Fe.exporters.HAVE_DOCX = True
check(b"python-docx" in r.data, "missing library gives a clear message, not an error page")

# ---- account settings -----------------------------------------------------------------------------------
check(b"Email alerts" in bob.get("/settings").data, "settings page")
con = db(); con.execute("UPDATE employees SET email='bob@example.test' WHERE id=?", (bob_id,)); con.execute("UPDATE employees SET email='carol@example.test', email_notify=0 WHERE id=?", (carol_id,)); con.commit()
post(bob, "/settings", {"email": "evil@example.test"}, page="/settings")
check(db().execute("SELECT email, email_notify FROM employees WHERE id=?", (bob_id,)).fetchone() == ("bob@example.test", 0), "employee cannot change own address; unticked box turns alerts off")
post(bob, "/settings", {"email_notify": "1"}, page="/settings")
check(db().execute("SELECT email_notify FROM employees WHERE id=?", (bob_id,)).fetchone()[0] == 1, "alerts can be turned back on")
check(b"valid email" in post(adm, "/settings", {"email": "not-an-email", "email_notify": "1"}, page="/settings", follow_redirects=True).data, "bad admin address refused")
post(adm, "/settings", {"email": "boss@example.test", "email_notify": "1"}, page="/settings")
check(db().execute("SELECT email FROM employees WHERE id=?", (admin_id,)).fetchone()[0] == "boss@example.test", "admin can set own address")

# ---- email alerts (a real SMTP conversation with a tiny fake server) -------------------------------------
INBOX = []
class FakeSMTP(socketserver.StreamRequestHandler):
    def handle(self):
        w = lambda t: self.wfile.write((t + "\r\n").encode())
        w("220 fake ESMTP"); data, buf = False, []
        for raw in self.rfile:
            line = raw.rstrip(b"\r\n")
            if data:
                if line == b".": INBOX.append(email.message_from_bytes(b"\r\n".join(buf))); buf, data = [], False; w("250 queued")
                else: buf.append(line[1:] if line.startswith(b"..") else line)
            elif line.upper() == b"DATA": w("354 go"); data = True
            elif line.upper() == b"QUIT": w("221 bye"); break
            else: w("250 ok")
class Server(socketserver.ThreadingTCPServer): allow_reuse_address = True; daemon_threads = True
smtp = Server(("127.0.0.1", 0), FakeSMTP); threading.Thread(target=smtp.serve_forever, daemon=True).start()
good_port = str(smtp.server_address[1])
def free_port():
    s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); p_ = s_.getsockname()[1]; s_.close(); return str(p_)
os.environ.update(PERSMAN_SMTP_HOST="127.0.0.1", PERSMAN_SMTP_PORT=good_port, PERSMAN_SMTP_SECURITY="none",
                  PERSMAN_SMTP_FROM="persman@example.test", PERSMAN_BASE_URL="https://hr.example.test/", PERSMAN_EMAIL_DELAY_MIN="0")
run = lambda *args: A.app.test_cli_runner().invoke(args=list(args))
body_of = lambda m: m.get_payload(decode=True).decode()
to_bob = lambda: [m for m in INBOX if m["To"] == "bob@example.test"]

r = run("send-emails")
check(r.exit_code == 0 and "Sent 1 email" in r.output, "first run sends the unread message digest only: " + r.output)
check("Boss Admin" in body_of(INBOX[-1]) and "Staff meeting moved" not in body_of(INBOX[-1]), "digest names the sender but never the message text")
post(adm, "/calendar/events/new", {"title": "All-hands\r\nBcc: evil@example.test", "start_date": d, "start_time": "09:00", "everyone": "1", "reminder": ""}, page="/calendar/events/new")
ev_all = db().execute("SELECT MAX(id) FROM events").fetchone()[0]
r = run("send-emails")
alert = [m for m in to_bob() if "All-hands" in (m["Subject"] or "")]
check(len(alert) == 1 and alert[0]["Bcc"] is None and "\n" not in alert[0]["Subject"] and email.utils.parseaddr(alert[0]["From"]) == ("Persman", "persman@example.test"), "alert email sent, header injection blocked")
check(f"https://hr.example.test/calendar/events/{ev_all}" in body_of(alert[0]) and "https://hr.example.test/settings" in body_of(alert[0]), "email links point at the real site")
check(not [m for m in INBOX if m["To"] == "carol@example.test"] and not [m for m in INBOX if "oldtimer" in (m["To"] or "")], "no emails for people who turned alerts off or have no address")
n_before = len(INBOX); run("send-emails")
check(len(INBOX) == n_before, "nothing is sent twice")
post(adm, "/calendar/events/new", {"title": "Seen already", "start_date": d, "start_time": "11:00", "everyone": "1", "reminder": ""}, page="/calendar/events/new")
seen_id = db().execute("SELECT id FROM notifications WHERE employee_id=? AND title LIKE '%Seen already%'", (bob_id,)).fetchone()[0]
bob.get(f"/notifications/{seen_id}/open"); run("send-emails")
check(not [m for m in INBOX if "Seen already" in (m["Subject"] or "")], "alerts already read in Persman are not emailed")

soon2 = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(minutes=10)
post(adm, "/calendar/events/new", {"title": "Offline reminder", "start_date": soon2.strftime("%Y-%m-%d"), "start_time": soon2.strftime("%H:%M"), "reminder": "15", "attendees": [bob_ref]}, page="/calendar/events/new")
run("send-emails")
check([m for m in to_bob() if "Reminder: Offline reminder" in body_of(m)], "reminders are prepared and emailed even when nobody is online (batched with the invitation)")

# failures are retried a limited number of times
os.environ["PERSMAN_SMTP_PORT"] = free_port()
post(adm, "/calendar/events/new", {"title": "Will fail", "start_date": d, "start_time": "13:00", "everyone": "1", "reminder": ""}, page="/calendar/events/new")
r = run("send-emails")
check(r.exit_code == 1 and "failed" in r.output, "failed delivery is reported with a non-zero exit code")
for _ in range(5): run("send-emails")
tries = db().execute("SELECT email_tries FROM notifications WHERE employee_id=? AND title LIKE '%Will fail%'", (bob_id,)).fetchone()[0]
r = run("send-emails")
check(tries == 5 and r.exit_code == 0 and "Sent 0 email(s); 0 failed" in r.output, "gives up after 5 tries instead of retrying forever")
os.environ["PERSMAN_SMTP_PORT"] = good_port
r = run("test-email", "qa@example.test")
check(r.exit_code == 0 and INBOX[-1]["Subject"] == "[Persman] Test email" and INBOX[-1]["To"] == "qa@example.test", "test-email command works")
os.environ["PERSMAN_SMTP_FROM"] = ""
r = run("send-emails")
check(r.exit_code != 0 and "PERSMAN_SMTP_FROM" in r.output, "missing sender address is explained")
os.environ["PERSMAN_SMTP_FROM"] = "persman@example.test"
os.environ["PERSMAN_SMTP_HOST"] = ""
r = run("send-emails")
check(r.exit_code == 0 and "not set up" in r.output, "without SMTP settings the job still prepares reminders and says so")

# ---- pages respect CSP (no inline scripts or styles) -----------------------------------------------
for url in ("/workspace", "/settings", doc_url, "/calendar", "/calendar/events/new", "/notifications", "/messages", f"/messages/{bob_ref}", "/messages/new"):
    h = alice.get(url).get_data(as_text=True)
    check(not re.search(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)', h) and ' style="' not in h and " onclick=" not in h, f"{url} has no inline script/style")
check("default-src 'self'" in alice.get("/calendar").headers["Content-Security-Policy"], "CSP header present")

# ---- removal cascades --------------------------------------------------------------------------------
r = post(alice, "/workspace/new", {"kind": "doc"}); keep = int(r.headers["Location"].rsplit("/", 1)[1])
post(alice, "/calendar/events/new", dict(form, attendees=[bob_ref], start_date=d, end_date=d), page="/calendar/events/new")
adm.post(f"/admin/employees/{alice_id}/remove", data={"_csrf": token(adm, f"/admin/employees/{alice_id}"), "confirm": alice_id})
c = db()
for table, col in (("workdocs", "owner_id"), ("events", "owner_id"), ("notifications", "employee_id")):
    check(c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (alice_id,)).fetchone()[0] == 0, f"{table} cleaned up after removal")
check(c.execute("SELECT COUNT(*) FROM messages WHERE sender_id=? OR recipient_id=?", (alice_id, alice_id)).fetchone()[0] == 0, "messages cleaned up")
check(c.execute("SELECT COUNT(*) FROM doc_shares WHERE doc_id=?", (doc_id,)).fetchone()[0] == 0, "shares cleaned up")
archived = [p for p in A.ARCHIVE_DIR.iterdir() if p.name.startswith(alice_id)]
check(archived and (archived[0] / "workspace" / f"{keep}.html").exists(), "workspace files archived with the folder")
check(bob.get(f"/messages/{alice_ref}").status_code == 404, "removed person's thread is gone")

# ---- branding: company name and logo ---------------------------------------------------------------------
def png(w=2, h=2, rgb=(200, 30, 30)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
def brand_post(name, logo=None, fname="logo.png", action="save"):
    data = {"_csrf": token(adm, "/admin/branding"), "action": action, "company_name": name}
    if logo is not None: data["logo"] = (io.BytesIO(logo), fname)
    return adm.post("/admin/branding", data=data, content_type="multipart/form-data", follow_redirects=True)
setting = lambda k: (db().execute("SELECT value FROM app_settings WHERE key=?", (k,)).fetchone() or [""])[0]
anon = A.app.test_client()

page = anon.get("/login").get_data(as_text=True)
check("<title>Sign in · Persman</title>" in page and 'class="company">Persman<' in page and "branding/logo" not in page, "default name shown, no logo")
check(bob.get("/admin/branding").status_code == 403 and anon.get("/branding/logo").status_code == 404, "only the admin can edit branding; no logo until one is uploaded")
check(b"Branding saved" in brand_post("Acme <b>Ltd</b>", png()).data, "branding saved")
page = anon.get("/login").get_data(as_text=True)
check("Acme &lt;b&gt;Ltd&lt;/b&gt;" in page and "<b>Ltd" not in page and 'src="/branding/logo?v=' in page and "<title>Sign in · Acme" in page, "sign-in page shows name (escaped), logo and title")
r = anon.get("/branding/logo")
check(r.status_code == 200 and r.mimetype == "image/png" and r.data == png() and r.headers["X-Content-Type-Options"] == "nosniff", "logo served publicly with the right type")
for url in ("/workspace", "/calendar", "/notifications", "/settings", "/messages", "/messages/new", "/calendar/events/new", "/nothing-here"):
    h = bob.get(url).get_data(as_text=True)
    check("Persman" not in h and "Acme" in h and '<link rel="icon" href="/branding/logo' in h, f"{url} is fully branded (no product name left)")
check('class="brand"' in adm.get("/admin").get_data(as_text=True) and "Acme" in adm.get("/admin/branding").get_data(as_text=True), "admin pages branded")

before = setting("company_name")
check(b"not a supported image" in brand_post("Evil Corp", b"MZ\x90\x00 definitely not an image", "logo.png").data, "executable renamed .png refused (checked by content)")
check(b"not a supported image" in brand_post("Evil Corp", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "logo.svg").data, "SVG refused")
check(b"larger than 2 MB" in brand_post("Evil Corp", png() + b"\0" * (3 * 1024 * 1024)).data, "oversized logo refused")
check(setting("company_name") == before and anon.get("/branding/logo").data == png(), "a refused upload changes nothing")
brand_post("  Acme   Ltd\r\n\x07Holdings " + "x" * 100)
check(len(setting("company_name")) == 80 and "\n" not in setting("company_name") and "\x07" not in setting("company_name") and setting("company_name").startswith("Acme Ltd Holdings"), "name is tidied and limited to 80 characters")
brand_post("Acme Ltd")

# the logo and name reach exports and email
r = post(carol, "/workspace/new", {"kind": "doc"}); cdoc = int(r.headers["Location"].rsplit("/", 1)[1])
wd = DocxDocument(io.BytesIO(carol.get(f"/workspace/{cdoc}/export.docx").data))
hdr = wd.sections[0].header
check("Acme Ltd" in hdr.paragraphs[0].text and any(r_.reltype.endswith("/image") for r_ in hdr.part.rels.values()) and wd.core_properties.author == "Acme Ltd", "Word header carries the company name and logo")
r = post(carol, "/workspace/new", {"kind": "sheet"}); csheet = int(r.headers["Location"].rsplit("/", 1)[1])
check(load_workbook(io.BytesIO(carol.get(f"/workspace/{csheet}/export.xlsx").data)).properties.creator == "Acme Ltd", "Excel file credits the company")
os.environ["PERSMAN_SMTP_HOST"] = "127.0.0.1"
r = run("test-email", "qa@example.test")
check(INBOX[-1]["Subject"] == "[Acme Ltd] Test email" and email.utils.parseaddr(INBOX[-1]["From"]) == ("Acme Ltd", "persman@example.test"), "emails are sent in the company's name")
post(adm, "/calendar/events/new", {"title": "Branded mail", "start_date": d, "start_time": "15:00", "everyone": "1", "reminder": ""}, page="/calendar/events/new")
run("send-emails")
mail = [m for m in to_bob() if "Branded mail" in body_of(m)][-1]
check(mail["Subject"].startswith("[Acme Ltd]") and "alerts are on in Acme Ltd" in body_of(mail), "alert emails use the company name")

# someone who must still change their password can see the logo
html = adm.post("/admin/employees/new", data={"_csrf": token(adm, "/admin/employees/new"), "full_name": "New Starter", "level": "INT"}).get_data(as_text=True)
nid_ = re.search(r'mono">([A-Z]{3}-\d\d-[A-Z0-9]{4})<', html).group(1); npw = re.search(r'Temporary password</dt><dd class="mono">([^<]+)<', html).group(1)
newbie = A.app.test_client(); login(newbie, nid_, npw)
check(newbie.get("/branding/logo").status_code == 200 and "branding/logo" in newbie.get("/password").get_data(as_text=True), "logo shows on the forced password-change page")

# replacing and removing
brand_post("Acme Ltd", b"\xff\xd8\xff\xe0" + b"0" * 64, "x.jpg")
check(sorted(p_.name for p_ in Fe.BRANDING_DIR.iterdir()) == ["logo.jpg"] and anon.get("/branding/logo").mimetype == "image/jpeg", "new logo replaces the old file")
check(carol.get(f"/workspace/{cdoc}/export.docx").status_code == 200, "an unreadable logo never breaks a Word export")
brand_post("Acme Ltd", action="remove_logo")
page = anon.get("/login").get_data(as_text=True)
check(anon.get("/branding/logo").status_code == 404 and not list(Fe.BRANDING_DIR.iterdir()) and "branding/logo" not in page and 'class="company">Acme Ltd<' in page, "removing the logo keeps the name")
brand_post("")
check('class="company">Persman<' in anon.get("/login").get_data(as_text=True), "clearing the name goes back to the default")
check(db().execute("SELECT COUNT(*) FROM audit WHERE action='branding_changed'").fetchone()[0] >= 5, "branding changes are in the audit trail")
smtp.shutdown()

# ---- admin view of an employee's workspace -------------------------------------------------------------
r = post(bob, "/workspace/new", {"kind": "doc"})
page = adm.get(f"/admin/employees/{bob_id}").get_data(as_text=True)
check("Workspace documents" in page and "Untitled document" in page, "admin sees employee's workspace documents")

shutil.rmtree(TMP, ignore_errors=True)
print(f"ALL {ok} CHECKS PASSED")
