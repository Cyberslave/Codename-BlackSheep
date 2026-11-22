"""Persman: employment contracts with company letterhead.

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
from letterhead import letterhead_html

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


def _money(cents, cur="R"):
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
        
    parts = [
        letterhead_html(brand, "Employment Contract"),
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
