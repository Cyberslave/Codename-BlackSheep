"""One-time migration: add site_slug columns to existing data.

Run this ONCE after deploying the multi-site patch, on the server or against
your local DB. It:

  1. Creates the sites table if missing
  2. Ensures the holding company exists
  3. Adds site_slug column to every data table
  4. Sets all existing rows to site_slug = 'holding'

Safe to re-run.
"""
import os
import sqlite3
import datetime as dt
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PERSMAN_DATA", BASE / "data")).resolve()
DB_PATH = DATA / "persman.db" if not os.path.exists(DATA / "tenantman.db") else DATA / "tenantman.db"

if not DB_PATH.exists():
    print(f"Database not found at {DB_PATH}")
    exit(1)

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

# Ensure holding site exists
con.execute(
    "CREATE TABLE IF NOT EXISTS sites ("
    "slug TEXT PRIMARY KEY, name TEXT NOT NULL, tagline TEXT, "
    "body_color TEXT DEFAULT '#ffffff', navbar_color TEXT DEFAULT '#000000', "
    "navbar_text_color TEXT DEFAULT '#ffffff', logo_ext TEXT, "
    "is_holding INTEGER NOT NULL DEFAULT 0, "
    "is_active INTEGER NOT NULL DEFAULT 1, "
    "created_at TEXT NOT NULL)")
have = con.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
if have == 0:
    con.execute(
        "INSERT INTO sites (slug, name, is_holding, created_at) VALUES (?,?,1,?)",
        ("holding", "Your Holding Company", dt.datetime.utcnow().isoformat()))

# Tables that need site_slug
TABLES = [
    # Persman
    "employees", "sessions_log", "documents", "audit",
    "customers", "products", "sales", "sale_items", "sale_payments", "expenses",
    "vacancies", "applications", "photos",
    # TenantMan (harmless if table doesn't exist)
    "accounts", "leases", "charges", "payments", "requests", "request_comments",
]

for table in TABLES:
    # Check if table exists
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not exists:
        continue
    # Check if column exists
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if "site_slug" not in cols:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN site_slug TEXT NOT NULL DEFAULT 'holding'")
            print(f"  + {table}: added site_slug")
        except sqlite3.OperationalError as e:
            print(f"  ! {table}: {e}")
    else:
        print(f"  = {table}: already has site_slug")

# Set existing rows to holding (only if they don't already have a value)
for table in TABLES:
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not exists:
        continue
    try:
        con.execute(f"UPDATE {table} SET site_slug='holding' WHERE site_slug IS NULL OR site_slug=''")
    except sqlite3.OperationalError:
        pass

# Build site_users table to track which users belong to which sites
con.execute(
    "CREATE TABLE IF NOT EXISTS site_users ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "site_slug TEXT NOT NULL, "
    "account_id TEXT NOT NULL, "
    "role TEXT NOT NULL DEFAULT 'staff', "
    "created_at TEXT NOT NULL, "
    "UNIQUE(site_slug, account_id))")

# Populate site_users from existing employees/accounts
for src_table in ("employees", "accounts"):
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (src_table,)).fetchone()
    if not exists:
        continue
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({src_table})").fetchall()]
    if "site_slug" not in cols:
        continue
    for row in con.execute(f"SELECT id, site_slug, is_admin FROM {src_table}"):
        con.execute(
            "INSERT OR IGNORE INTO site_users (site_slug, account_id, role, created_at) "
            "VALUES (?,?,?,?)",
            (row["site_slug"], row["id"],
             "admin" if row["is_admin"] else "staff",
             dt.datetime.utcnow().isoformat()))

con.commit()
con.close()
print("\nMigration complete. All existing data is now scoped to 'holding'.")
print("You can now create subsidiaries under /holding/admin/sites")
