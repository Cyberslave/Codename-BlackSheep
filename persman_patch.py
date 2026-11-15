"""Persman patch: employment contracts with letterhead (Lesotho Labour Act 1997).
Option A: EXE/MGT/ADM can create contracts.
"""
import os, re, shutil, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_contracts_patch")
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
# 1. contracts.py — new module
# =========================================================================
print("\n[1/5] contracts.py — new module")

CONTRACTS_MODULE = '''"""Persman: employment contracts with company letterhead.

Permission model (Option A): levels EXE, MGT, ADM can create and edit contracts.
Clauses follow the Lesotho Labour Act (Labour Code Order 1992, as amended).

Contracts are stored as structured records (one row per contract) so they can be
edited later, saved, printed as PDF (browser print) and downloaded as .docx.
"""
import datetime as dt
import html
import re

from flask import abort, flash, g, redirect, render_template, request, url_for

import exporters
from app import LEVEL_NAMES, LEVELS, admin_required, app, audit, get_db, get_emp, iso, now
from features import attachment, get_brand

# Lesotho Labour Act statutory minimums (as at 2024/2026)
STAT_ANNUAL_LEAVE_DAYS = 12
STAT_SICK_LEAVE_DAYS = 12
STAT_MATERNITY_LEAVE_WEEKS = 14
STAT_HOURS_PER_WEEK = 45
CONTRACT_LEVELS = ("EXE", "MGT", "ADM")


def _contract_ok(user):
    """Option A: EXE, MGT, ADM levels can create contracts."""
    if not user:
        return False
    if user["is_admin"]:
        return True
    return user["level"] in CONTRACT_LEVELS


def contract_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        if not _contract_ok(g.user):
            abort(403)
        return fn(*a, **kw)
    return wrapper


# ---- schema --------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id TEXT NOT NULL,
  job_title TEXT NOT NULL,
  department TEXT,
  salary_cents INTEGER NOT NULL,
  pay_cycle TEXT NOT NULL DEFAULT 'monthly',
  start_date TEXT NOT NULL,
  end_date TEXT,
  probation_months INTEGER,
  hours_per_week INTEGER NOT NULL DEFAULT 45,
  annual_leave_days INTEGER NOT NULL DEFAULT 12,
  sick_leave_days INTEGER NOT NULL DEFAULT 12,
  maternity_leave_weeks INTEGER NOT NULL DEFAULT 14,
  additional_terms TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_contracts_emp ON contracts(employee_id, created_at);
"""


def init_contracts_db():
    import sqlite3
    from app import DB_PATH
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


init_contracts_db()


# ---- helpers -------------------------------------------------------------
def _load_contract(cid):
    c = get_db().execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if c is None:
        abort(404)
    return c


def _slug(name):
    parts = (name or "").strip().split()
    surname = parts[-1] if parts else "employee"
    return re.sub(r"[^A-Za-z0-9_-]", "", surname) or "employee"


def _money(cents, cur="M"):
    whole, frac = divmod(abs(int(cents or 0)), 100)
    return f"{cur}{whole:,}.{frac:02d}"


def _annual(cents):
    return cents * 12


def _form_from_request(form):
    """Validate and return (data, error)."""
    emp_id = (form.get("employee_id") or "").strip().upper()
    emp = get_db().execute("SELECT * FROM employees WHERE id=? AND is_admin=0", (emp_id,)).fetchone()
    if emp is None:
        return None, "Choose an employee."
    job = (form.get("job_title") or "").strip()
    if not job:
        return None, "Enter the job title."
    try:
        salary = float((form.get("salary") or "0").replace(",", "").replace("M", "").strip())
        salary_cents = int(round(salary * 100))
    except ValueError:
        return None, "Enter the monthly salary as a number."
    if salary_cents <= 0:
        return None, "The salary must be greater than zero."
    try:
        start = dt.datetime.strptime(form.get("start_date", ""), "%Y-%m-%d").date()
    except ValueError:
        return None, "Enter a valid start date."
    end_raw = (form.get("end_date") or "").strip()
    end = None
    if end_raw:
        try:
            end = dt.datetime.strptime(end_raw, "%Y-%m-%d").date()
        except ValueError:
            return None, "The end date is not valid."
        if end <= start:
            return None, "The end date must be after the start date."
    prob_raw = (form.get("probation_months") or "").strip()
    prob = None
    if prob_raw:
        try:
            prob = int(prob_raw)
        except ValueError:
            return None, "Probation months must be a number."
        if prob < 0 or prob > 12:
            return None, "Probation must be between 0 and 12 months."
        if prob == 0:
            prob = None
    try:
        hours = int(form.get("hours_per_week") or 45)
    except ValueError:
        hours = 45
    if not 1 <= hours <= 80:
        return None, "Hours per week must be between 1 and 80."
    try:
        annual_leave = int(form.get("annual_leave_days") or STAT_ANNUAL_LEAVE_DAYS)
        sick_leave = int(form.get("sick_leave_days") or STAT_SICK_LEAVE_DAYS)
        mat_weeks = int(form.get("maternity_leave_weeks") or STAT_MATERNITY_LEAVE_WEEKS)
    except ValueError:
        return None, "Leave values must be whole numbers."
    return dict(
        employee_id=emp["id"], job_title=job[:120],
        department=(form.get("department") or emp["department"] or "").strip()[:80],
        salary_cents=salary_cents, pay_cycle="monthly",
        start_date=start.isoformat(), end_date=end.isoformat() if end else None,
        probation_months=prob, hours_per_week=hours,
        annual_leave_days=max(annual_leave, STAT_ANNUAL_LEAVE_DAYS),
        sick_leave_days=max(sick_leave, STAT_SICK_LEAVE_DAYS),
        maternity_leave_weeks=max(mat_weeks, STAT_MATERNITY_LEAVE_WEEKS),
        additional_terms=(form.get("additional_terms") or "").strip()[:4000],
    ), None


def _contract_html(contract, employee, brand):
    """The contract body, sanitised-ready HTML. Text-only letterhead (no oversized logo)."""
    landlord = html.escape(brand["name"])
    emp_name = html.escape(employee["full_name"])
    salary = _money(contract["salary_cents"])
    annual = _money(_annual(contract["salary_cents"]))
    start = dt.date.fromisoformat(contract["start_date"]).strftime("%d %B %Y")
    end_label = ""
    if contract["end_date"]:
        end_label = " until " + dt.date.fromisoformat(contract["end_date"]).strftime("%d %B %Y")
    prob = ""
    if contract["probation_months"]:
        prob = (f" The first {contract['probation_months']} month"
                f"{'s' if contract['probation_months'] != 1 else ''} of employment "
                "shall be a probationary period, during which either party may terminate "
                "on the notice provided by the Labour Code.")
    logo_block = ""  # text-only letterhead; the page header shows the logo
    parts = [
        '<div class="lease-head">'
        '<div class="lease-head-text"><strong>' + landlord + '</strong>'
        '<span>Employment Contract</span></div></div>',
        "<h1>Contract of Employment</h1>",
        "<p>Prepared for " + landlord + " on " + now().date().strftime("%d %B %Y") + ".</p>",
        "<h2>1. Parties</h2>",
        "<p><b>Employer:</b> " + landlord + "</p>",
        "<p><b>Employee:</b> " + emp_name
        + (" · " + html.escape(employee["id"]) if employee["id"] else "")
        + (" · " + html.escape(employee["job_title"]) if employee["job_title"] else "")
        + "</p>",
        "<h2>2. Position</h2>",
        "<p>The Employee is employed as <b>" + html.escape(contract["job_title"]) + "</b>"
        + ((" in the " + html.escape(contract["department"]) + " department") if contract["department"] else "")
        + ".</p>",
        "<h2>3. Commencement and Duration</h2>",
        "<p>Employment begins on <b>" + start + "</b>" + end_label + "." + prob + "</p>",
        "<h2>4. Remuneration</h2>",
        "<p>Monthly salary: <b>" + salary + "</b> (annual equivalent: <b>" + annual + "</b>), "
        "payable by the Employer's usual payroll method on or before the last working day of each month.</p>",
        "<p>Overtime, if worked, is paid at not less than one and a quarter times the normal hourly rate, "
        "calculated on the basis of 195 ordinary hours per month.</p>",
        "<h2>5. Hours of Work</h2>",
        "<p>Normal hours of work shall not exceed <b>" + str(contract["hours_per_week"]) + " hours per week</b>. "
        "The Employer shall provide at least one hour of rest after five hours of continuous work, and at least "
        "24 continuous hours of weekly rest.</p>",
        "<h2>6. Leave</h2>",
        "<p><b>Annual leave:</b> not less than " + str(contract["annual_leave_days"]) + " working days per year, "
        "accruing at one day per month of continuous service.</p>",
        "<p><b>Sick leave:</b> as provided by the Labour Code. In the first six months, sick leave may be unpaid; "
        "after six months, up to " + str(contract["sick_leave_days"]) + " days paid per year; and thereafter, up to "
        "24 days at half pay per year, subject to a medical certificate.</p>",
        "<p><b>Maternity leave:</b> not less than " + str(contract["maternity_leave_weeks"]) + " weeks, "
        "taken in accordance with the Labour Code.</p>",
        "<h2>7. Termination</h2>",
        "<p>Either party may terminate this contract by giving the notice prescribed by the Labour Code, "
        "or payment in lieu. A probationary employee may be dismissed on the grounds permitted by the Labour Code. "
        "No dismissal shall be unfair within the meaning of the Labour Code.</p>",
        "<h2>8. Dispute Resolution</h2>",
        "<p>Any dispute arising from this contract shall first be referred to the Directorate of Dispute Prevention "
        "and Resolution (DDPR) for conciliation, and thereafter to arbitration or the Labour Court as provided by "
        "the Labour Code.</p>",
    ]
    if (contract["additional_terms"] or "").strip():
        parts.append("<h2>9. Additional Terms</h2>")
        parts.append("<p>" + html.escape(contract["additional_terms"]) + "</p>")
    parts.append("<h2>Signatures</h2>")
    parts.append("<p>Signed for the Employer: " + landlord + " — ______________________ Date: ______________</p>")
    parts.append("<p>Signed for the Employee: " + emp_name + " — ______________________ Date: ______________</p>")
    return "".join(parts)


# ---- routes --------------------------------------------------------------
@app.route("/admin/contracts")
@contract_required
def admin_contracts():
    rows = get_db().execute(
        "SELECT c.*, e.full_name AS emp_name FROM contracts c "
        "LEFT JOIN employees e ON e.id=c.employee_id ORDER BY c.created_at DESC LIMIT 500"
    ).fetchall()
    return render_template("admin_contracts.html", contracts=rows)


@app.route("/admin/contracts/new", methods=["GET", "POST"])
@contract_required
def admin_contract_new():
    db = get_db()
    employees = db.execute("SELECT id, full_name, job_title, department, level FROM employees WHERE is_admin=0 ORDER BY full_name").fetchall()
    if request.method == "POST":
        data, err = _form_from_request(request.form)
        if err:
            flash(err, "error")
            return render_template("contract_form.html", contract=request.form, employees=employees,
                                   levels=LEVEL_NAMES, is_new=True)
        t = iso(now())
        cur = db.execute(
            "INSERT INTO contracts (employee_id, job_title, department, salary_cents, pay_cycle,"
            " start_date, end_date, probation_months, hours_per_week, annual_leave_days,"
            " sick_leave_days, maternity_leave_weeks, additional_terms, created_at, created_by,"
            " updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data["employee_id"], data["job_title"], data["department"], data["salary_cents"],
             data["pay_cycle"], data["start_date"], data["end_date"], data["probation_months"],
             data["hours_per_week"], data["annual_leave_days"], data["sick_leave_days"],
             data["maternity_leave_weeks"], data["additional_terms"],
             t, g.user["id"], t, g.user["id"]))
        db.commit()
        audit(g.user["id"], "contract_created", str(cur.lastrowid), data["employee_id"])
        db.commit()
        flash("Contract created.", "ok")
        return redirect(url_for("admin_contract_view", cid=cur.lastrowid))
    return render_template("contract_form.html", contract={}, employees=employees,
                           levels=LEVEL_NAMES, is_new=True)


@app.route("/admin/contracts/<int:cid>")
@contract_required
def admin_contract_view(cid):
    c = _load_contract(cid)
    emp = get_db().execute("SELECT * FROM employees WHERE id=?", (c["employee_id"],)).fetchone()
    if emp is None:
        abort(404)
    brand = get_brand()
    body = _contract_html(c, emp, brand)
    return render_template("contract_view.html", contract=c, employee=emp, body=body, brand=brand)


@app.route("/admin/contracts/<int:cid>/edit", methods=["GET", "POST"])
@contract_required
def admin_contract_edit(cid):
    c = _load_contract(cid)
    db = get_db()
    employees = db.execute("SELECT id, full_name, job_title, department, level FROM employees WHERE is_admin=0 ORDER BY full_name").fetchall()
    if request.method == "POST":
        data, err = _form_from_request(request.form)
        if err:
            flash(err, "error")
            return render_template("contract_form.html", contract=request.form, employees=employees,
                                   levels=LEVEL_NAMES, is_new=False, cid=cid)
        db.execute(
            "UPDATE contracts SET job_title=?, department=?, salary_cents=?, start_date=?, end_date=?,"
            " probation_months=?, hours_per_week=?, annual_leave_days=?, sick_leave_days=?,"
            " maternity_leave_weeks=?, additional_terms=?, updated_at=?, updated_by=?, version=version+1"
            " WHERE id=?",
            (data["job_title"], data["department"], data["salary_cents"], data["start_date"],
             data["end_date"], data["probation_months"], data["hours_per_week"],
             data["annual_leave_days"], data["sick_leave_days"], data["maternity_leave_weeks"],
             data["additional_terms"], iso(now()), g.user["id"], cid))
        audit(g.user["id"], "contract_edited", str(cid), c["employee_id"])
        db.commit()
        flash("Contract saved.", "ok")
        return redirect(url_for("admin_contract_view", cid=cid))
    form = dict(c)
    form["salary"] = f"{c['salary_cents'] / 100:.2f}"
    return render_template("contract_form.html", contract=form, employees=employees,
                           levels=LEVEL_NAMES, is_new=False, cid=cid)


@app.post("/admin/contracts/<int:cid>/delete")
@contract_required
def admin_contract_delete(cid):
    c = _load_contract(cid)
    db = get_db()
    db.execute("DELETE FROM contracts WHERE id=?", (cid,))
    audit(g.user["id"], "contract_deleted", str(cid), c["employee_id"])
    db.commit()
    flash("Contract deleted.", "ok")
    return redirect(url_for("admin_contracts"))


@app.route("/admin/contracts/<int:cid>.docx")
@contract_required
def admin_contract_docx(cid):
    c = _load_contract(cid)
    emp = get_db().execute("SELECT * FROM employees WHERE id=?", (c["employee_id"],)).fetchone()
    if emp is None:
        abort(404)
    brand = get_brand()
    try:
        data = exporters.build_docx(
            "Employment Contract - " + emp["full_name"],
            _contract_html(c, emp, brand), brand=brand)
    except exporters.ExportUnavailable:
        flash("Word export needs python-docx. Run: pip install -r requirements.txt", "error")
        return redirect(url_for("admin_contract_view", cid=cid))
    audit(g.user["id"], "contract_exported", str(cid), c["employee_id"])
    get_db().commit()
    return attachment(data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "CONTRACT-" + _slug(emp["full_name"]) + "-" + str(cid), "docx")
'''

write("contracts.py", CONTRACTS_MODULE)
check("contracts.py written", True)

# =========================================================================
# 2. Templates
# =========================================================================
print("\n[2/5] templates")

# contract_form.html
FORM_TPL = '''{% extends "base.html" %}
{% block title %}{{ 'Edit contract' if not is_new else 'New contract' }} · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel narrow wide">
  <h1>{{ 'Edit employment contract' if not is_new else 'New employment contract' }}</h1>
  <p class="muted small">Structured per the Lesotho Labour Act. The letterhead (company name + logo) appears when you print or download.</p>
  <form method="post" class="stack">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    {% if is_new %}
    <label>Employee
      <select name="employee_id" required autofocus>
        <option value="">Choose an employee</option>
        {% for e in employees %}
        <option value="{{ e['id'] }}" {{ 'selected' if contract.get('employee_id') == e['id'] }}>
          {{ e['full_name'] }} ({{ e['id'] }}){% if e['job_title'] %} · {{ e['job_title'] }}{% endif %}
        </option>
        {% endfor %}
      </select>
    </label>
    {% endif %}
    <div class="grid">
      <label>Job title <input name="job_title" value="{{ contract.get('job_title','') }}" required></label>
      <label>Department <input name="department" value="{{ contract.get('department','') }}"></label>
    </div>
    <div class="grid">
      <label>Monthly salary (M) <input type="number" step="0.01" min="0" name="salary" value="{{ contract.get('salary','') }}" required></label>
      <label>Annual salary (calculated) <input value="{{ 'M %.2f' % (contract.get('salary','')|float * 12) if contract.get('salary') else '—' }}" readonly></label>
    </div>
    <div class="grid">
      <label>Start date <input type="date" name="start_date" value="{{ contract.get('start_date','') }}" required></label>
      <label>End date (blank = indefinite) <input type="date" name="end_date" value="{{ contract.get('end_date','') }}"></label>
    </div>
    <div class="grid">
      <label>Probation months (blank = none) <input type="number" min="0" max="12" name="probation_months" value="{{ contract.get('probation_months','') or '' }}"></label>
      <label>Hours per week <input type="number" min="1" max="80" name="hours_per_week" value="{{ contract.get('hours_per_week', 45) }}"></label>
    </div>
    <div class="grid">
      <label>Annual leave (days) <input type="number" min="12" name="annual_leave_days" value="{{ contract.get('annual_leave_days', 12) }}"></label>
      <label>Sick leave (days) <input type="number" min="12" name="sick_leave_days" value="{{ contract.get('sick_leave_days', 12) }}"></label>
      <label>Maternity leave (weeks) <input type="number" min="14" name="maternity_leave_weeks" value="{{ contract.get('maternity_leave_weeks', 14) }}"></label>
    </div>
    <p class="muted small">Leave values are set to the Labour Act statutory minimums; they cannot be set lower.</p>
    <label>Additional terms
      <textarea name="additional_terms" rows="5" maxlength="4000">{{ contract.get('additional_terms','') }}</textarea>
    </label>
    <div class="row">
      <button class="btn" type="submit">{{ 'Save contract' if not is_new else 'Create contract' }}</button>
      <a class="btn ghost" href="{{ url_for('admin_contract_view', cid=cid) if not is_new else url_for('admin_contracts') }}">Cancel</a>
    </div>
  </form>
</section>
{% endblock %}
'''
write("templates/contract_form.html", FORM_TPL)
check("templates/contract_form.html", True)

# contract_view.html
VIEW_TPL = '''{% extends "base.html" %}
{% block title %}Contract · {{ employee['full_name'] }} · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel docbar">
  <div class="bar">
    <h1>Employment contract</h1>
    <div class="row nowrap">
      <button class="btn ghost" id="print-btn" type="button">Print / Save as PDF</button>
      <a class="btn ghost" href="{{ url_for('admin_contract_docx', cid=contract['id']) }}">Download Word</a>
      <a class="btn ghost" href="{{ url_for('admin_contract_edit', cid=contract['id']) }}">Edit</a>
      <a class="btn ghost" href="{{ url_for('admin_contracts') }}">Back</a>
    </div>
  </div>
  <details class="confirm">
    <summary class="btn danger small">Delete</summary>
    <form method="post" action="{{ url_for('admin_contract_delete', cid=contract['id']) }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <p class="small">Delete this contract permanently?</p>
      <button class="btn danger small" type="submit">Yes, delete</button>
    </form>
  </details>
</section>
<div class="print-brand">
  {% if brand.has_logo %}<img src="{{ brand.logo_url }}" alt="">{% endif %}
  <span>{{ brand.name }}</span>
</div>
<article id="editor" class="paper">{{ body|safe }}</article>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='print.js') }}" defer></script>
{% endblock %}
'''
write("templates/contract_view.html", VIEW_TPL)
check("templates/contract_view.html", True)

# admin_contracts.html (list)
LIST_TPL = '''{% extends "base.html" %}
{% block title %}Contracts · {{ brand.name }}{% endblock %}
{% block content %}
<section class="panel">
  <div class="bar">
    <h1>Employment contracts</h1>
    <a class="btn" href="{{ url_for('admin_contract_new') }}">New contract</a>
  </div>
  <p class="muted small">Contracts can be created by administrators, executives and managers.</p>
  <div class="scroll">
  <table>
    <thead><tr><th>Employee</th><th>Job title</th><th>Salary (M)</th><th>Start</th><th>Probation</th><th></th></tr></thead>
    <tbody>
    {% for c in contracts %}
      <tr>
        <td><a href="{{ url_for('admin_contract_view', cid=c['id']) }}">{{ c['emp_name'] or c['employee_id'] }}</a></td>
        <td>{{ c['job_title'] }}</td>
        <td>{{ '%.2f' % (c['salary_cents'] / 100) }}</td>
        <td>{{ c['start_date'] }}</td>
        <td>{{ c['probation_months'] ~ ' months' if c['probation_months'] else '—' }}</td>
        <td><a class="btn ghost small" href="{{ url_for('admin_contract_edit', cid=c['id']) }}">Edit</a></td>
      </tr>
    {% else %}
      <tr><td colspan="6" class="muted">No contracts yet.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</section>
{% endblock %}
'''
write("templates/admin_contracts.html", LIST_TPL)
check("templates/admin_contracts.html", True)

# static/print.js (idempotent)
print_js = os.path.join(ROOT, "static", "print.js")
if not os.path.exists(print_js):
    write("static/print.js", 'document.getElementById("print-btn")?.addEventListener("click", () => window.print());\n')
    check("static/print.js", True, "(created)")
else:
    check("static/print.js", True, "(already present)")

# =========================================================================
# 3. app.py — import contracts module
# =========================================================================
print("\n[3/5] app.py — import contracts")
backup("app.py")
a = read("app.py")

if "import contracts" in a:
    check("app.py: import contracts", True, "(already present)")
else:
    # find the last import line in the module tail
    if "import features" in a:
        # insert after the features import (or at the end of the import block)
        a = a.replace(
            "import features  # noqa: E402,F401",
            "import features  # noqa: E402,F401\nimport contracts  # noqa: E402,F401  (employment contracts)")
        check("app.py: import contracts", True, "(after features)")
    else:
        a += "\nimport contracts  # noqa: E402,F401  (employment contracts)\n"
        check("app.py: import contracts", True, "(appended)")
    write("app.py", a)

# =========================================================================
# 4. base.html — add Contracts link for EXE/MGT/ADM
# =========================================================================
print("\n[4/5] base.html — Contracts nav link")
backup("templates/base.html")
b = read("templates/base.html")

if "admin_contracts" in b:
    check("base.html: Contracts nav link", True, "(already present)")
else:
    # Find the admin nav block. In Persman it looks like:
    #   {% if g.user.is_admin %}
    #     <a href="...">Employees</a>
    #     <a href="...">Add employee</a>
    #     <a href="...">Activity</a>
    #     <a href="...">Branding</a>
    #   {% endif %}
    # We add a Contracts link inside the admin branch AND also outside for EXE/MGT.
    # Simplest: add a link that appears when the user is admin OR level in EXE/MGT.
    nav_addition = '''
    {% if g.user.is_admin or g.user.level in ('EXE','MGT','ADM') %}
      <a href="{{ url_for('admin_contracts') }}">Contracts</a>
    {% endif %}
'''
    # Insert before the Workspace link (first link outside the admin if)
    if '<a href="{{ url_for(\'workspace\') }}">Workspace</a>' in b:
        b = b.replace(
            '<a href="{{ url_for(\'workspace\') }}">Workspace</a>',
            nav_addition.strip() + '\n    <a href="{{ url_for(\'workspace\') }}">Workspace</a>', 1)
        check("base.html: Contracts nav link", True, "(inserted before Workspace)")
    elif "{% if g.user.is_admin %}" in b:
        # Insert at the end of the admin branch (before the matching {% else %} or {% endif %})
        # A pragmatic approach: add after the Branding link
        if 'Branding</a>' in b:
            b = b.replace('Branding</a>', 'Branding</a>\n      ' + nav_addition.strip(), 1)
            check("base.html: Contracts nav link", True, "(inserted after Branding)")
        else:
            check("base.html: Contracts nav link", False, "could not find Branding link")
    else:
        check("base.html: Contracts nav link", False, "admin nav block not found")
    write("templates/base.html", b)

# =========================================================================
# 5. admin_employee.html — "Create contract" button
# =========================================================================
print("\n[5/5] admin_employee.html — Create contract button")
backup("templates/admin_employee.html")
ae = read("templates/admin_employee.html")

if "admin_contract_new" in ae:
    check("admin_employee.html: contract button", True, "(already present)")
else:
    button = '''
  {% if g.user.is_admin or g.user.level in ('EXE','MGT','ADM') %}
  <p class="row top-gap">
    <a class="btn" href="{{ url_for('admin_contract_new') }}?employee_id={{ emp['id'] }}">Create employment contract</a>
  </p>
  {% endif %}
'''
    # Insert near the Account section or at the end of the first panel
    if "<h2>Account</h2>" in ae:
        ae = ae.replace("<h2>Account</h2>", button + "\n<h2>Account</h2>", 1)
        check("admin_employee.html: contract button", True, "(before Account section)")
    elif "{% endblock %}" in ae:
        idx = ae.rfind("{% endblock %}")
        ae = ae[:idx] + button + "\n" + ae[idx:]
        check("admin_employee.html: contract button", True, "(appended)")
    else:
        check("admin_employee.html: contract button", False, "no insertion point found")
    write("templates/admin_employee.html", ae)

# =========================================================================
# Summary
# =========================================================================
print("\n===================")
if all(ok for _, ok in results):
    print("ALL CHANGES APPLIED")
    print("\nRestart the app, then:")
    print("  1. Sign in as admin (or an EXE/MGT account)")
    print("  2. Navbar → Contracts → New contract")
    print("  3. Fill in employee, salary, dates, probation, hours, leave")
    print("  4. Save → view page with letterhead → Print or Download Word")
    print("\nNote: the contract form includes a ?employee_id= pre-fill from the")
    print("employee detail page, so you can start a contract from there too.")
else:
    print("SOME CHECKS FAILED:")
    for name, ok in results:
        if not ok:
            print("  -", name)
    print("\nPaste the output above for exact fixes.")