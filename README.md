# Persman – employee management system

Flask + SQLite web app. Administrator adds employees; each gets a level-coded ID, a personal
document folder and a temporary password. Every sign-in and sign-out is logged.

## Run it locally
    python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    flask --app app create-admin                          # prints the administrator's ID
    PERSMAN_DEV=1 flask --app app run                     # Windows PowerShell: $env:PERSMAN_DEV="1"
Open http://127.0.0.1:5000 and sign in with the administrator ID.
`PERSMAN_DEV=1` only lets the session cookie work over plain http; never use it in production.

## Branding (company name and logo)
The administrator opens **Branding** to enter the company name and upload a logo (PNG, JPG, GIF or WebP, up to 2 MB;
SVG is refused for safety). They appear in the header, on the sign-in page, in browser tab titles and as the tab icon,
on printed documents, in the header of Word exports, in the author of Excel exports and in emails (subject prefix,
sender name and footer). With no name set, the default name "Persman" is shown; with no logo, only the name is shown.
The logo is stored in `data/branding/`. Word exports include PNG, JPG and GIF logos (not WebP).

## Workspace, calendar, alerts and messages
- **Workspace**: employees create, edit, save and delete *documents* (rich text: headings, bold/italic,
  lists, tables, links, alignment) and *spreadsheets* (26 columns, formulas such as `=SUM(A1:A10)`,
  `=B2*C2`, `=IF(A1>100,"High","Low")`; functions SUM, AVERAGE, MIN, MAX, COUNT, IF, ROUND, ABS, SQRT,
  CONCAT). Files live in `data/employees/<ID>/workspace/`. Autosave every 20 s, Ctrl+S, and a guard that
  refuses to overwrite a newer version saved by someone else. Documents can be shared (view or edit),
  printed / saved as PDF from the browser, or downloaded as **Word (.docx)** and **Excel (.xlsx)** (spreadsheets also as CSV).
  Spreadsheet formulas arrive in Excel as real, live formulas; the file recalculates when opened. Anything
  that is not a valid, supported formula is exported as plain text, never as a formula.
- **Calendar**: month view, events with location, notes, all-day option, invitations with Accept/Decline,
  reminders (at start up to 1 day before) and administrator-created company-wide events.
- **Alerts**: bell badge, pop-up toasts and an Alerts page for invitations, shared documents, RSVPs,
  cancellations and reminders. Optional desktop alerts while a Persman tab is open, and optional
  **email alerts** (see below).
- **Messages**: private one-to-one conversations with live updates; the administrator can send one
  message to everyone or to one level. The administrator cannot read other people's messages.
- Colleagues see names and job titles only. Login IDs are never shown to other employees.
- Existing databases are upgraded automatically on start-up.
- Times you type are read as `PERSMAN_UTC_OFFSET` hours from UTC (one office time zone for everyone).

## Email alerts (optional)
Email stays off until you give Persman a mail server. Use your company's mail service, or any provider that
offers SMTP (for a quick test, a Gmail or Outlook account with an "app password").

    PERSMAN_SMTP_HOST=smtp.yourprovider.com
    PERSMAN_SMTP_PORT=587                  # 587 starttls (default), 465 ssl, 25 none
    PERSMAN_SMTP_SECURITY=starttls         # starttls, ssl or none
    PERSMAN_SMTP_USER=persman@yourcompany.com
    PERSMAN_SMTP_PASSWORD=...
    PERSMAN_SMTP_FROM=persman@yourcompany.com
    PERSMAN_BASE_URL=https://hr.yourdomain.com     # used for links inside emails
    PERSMAN_EMAIL_DELAY_MIN=2              # wait this long so alerts you already saw are not emailed

1. Check the settings: `flask --app app test-email you@example.com`
2. Run this every minute with the same environment variables as the app:
   `flask --app app send-emails`
   It prepares event reminders for everyone (so reminders work even when nobody is signed in) and emails
   unread alerts and a short "you have unread messages" note. Message text is never put in an email.
   - Linux cron: `* * * * * cd /opt/persman && PERSMAN_DATA=/var/lib/persman ... venv/bin/flask --app app send-emails`
   - Windows Task Scheduler: a task that repeats every minute running `venv\Scripts\flask.exe --app app send-emails`
     with the folder set as "Start in".
3. Employees need an email address (the administrator adds it on their employee page) and can switch
   alerts off under **Account**. Failed emails are retried up to 5 times, then dropped.

## Tests
    python tests/server_test.py            # permissions, sanitising, calendar, messages, exports, email (real SMTP conversation)
    python tests/office_test.py            # Word/Excel files; formulas cross-checked against LibreOffice if installed
    node tests/formula_test.js             # spreadsheet formula engine
    pip install playwright && playwright install chromium
    python tests/browser_test.py           # drives a real browser through every feature

## Known limits
In-app reminders appear while a person has Persman open or the next time they sign in; use the email job for
reminders when they are away. No SMS. Documents have no embedded images, and you cannot yet import .docx/.xlsx
files into the workspace. Excel recalculates formulas when it opens the file, so a phone preview that does not
calculate may show blanks. Keep one database file per Persman install (2 gunicorn workers is fine).

## Employee IDs
Format `LEVEL-YY-XXXX`, e.g. `MGT-26-7K2Q`: EXE Executive, MGT Management, SUP Supervisor,
STF Staff, INT Intern, ADM Administrator. Edit `LEVELS` in `app.py` to change the levels.
The 4-character suffix is random, so IDs cannot be guessed from a sequence.

## Where data lives (`data/`, or `PERSMAN_DATA`)
- `persman.db` – employees, sign-in log, document index, audit trail
- `employees/<ID>/` – each employee's documents
- `archive/<ID>__removed_<timestamp>/` – folders of removed employees (delete by hand when retention allows)
Back this folder up regularly.

## Putting it on the internet
1. Rent a small Linux server (VPS) and point a domain name at it.
2. `pip install -r requirements.txt` then run behind gunicorn:
       PERSMAN_BEHIND_PROXY=1 PERSMAN_UTC_OFFSET=2 gunicorn -w 2 -b 127.0.0.1:8000 app:app
   Keep it as a systemd service so it restarts automatically.
3. Put nginx in front, serve it over HTTPS with a free Let's Encrypt certificate (certbot),
   set `client_max_body_size 16m;` and add `limit_req` on `/login` to slow password guessing.
4. Never run it over plain http: passwords and session cookies would travel unprotected.

Environment variables: `PERSMAN_DATA`, `PERSMAN_SECRET` (session key; auto-generated if unset),
`PERSMAN_UTC_OFFSET` (hours; times are stored in UTC), `PERSMAN_BEHIND_PROXY`, `PERSMAN_DEV`, plus the `PERSMAN_SMTP_*` settings above.

## Built-in safeguards
Hashed passwords; forced password change at first sign-in; lock for 15 min after 5 failed sign-ins;
30-minute idle timeout (recorded as "Timed out"); CSRF protection; admin-only removal with typed
confirmation; employees can only open their own documents; uploads limited to 16 MB and safe file types;
audit trail of admin actions, document downloads and failed sign-ins.
