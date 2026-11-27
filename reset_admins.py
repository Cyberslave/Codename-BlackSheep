"""Reset all admin passwords to a known value. Run once after migration."""
import sqlite3
from werkzeug.security import generate_password_hash

NEW_PASSWORD = "Admin123456"   # change this if you want

con = sqlite3.connect("data/persman.db")
new_hash = generate_password_hash(NEW_PASSWORD)
cur = con.execute(
    "UPDATE employees SET password_hash=?, must_change_password=0 WHERE is_admin=1",
    (new_hash,),
)
con.commit()
print(f"Updated {cur.rowcount} admin account(s) to password: {NEW_PASSWORD}")

# Show the admins so you know the IDs
print("\nAdmin accounts:")
for row in con.execute(
    "SELECT id, full_name, site_slug FROM employees WHERE is_admin=1 ORDER BY id"
):
    print(f"  {row[0]}  {row[1]}  site={row[2]}")

con.close()