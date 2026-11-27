"""Debug: check what's in the sites table and app_settings."""
import sqlite3

con = sqlite3.connect("data/persman.db")
con.row_factory = sqlite3.Row

print("=== sites table ===")
for row in con.execute("SELECT * FROM sites"):
    print(dict(row))

print("\n=== app_settings brand keys ===")
for row in con.execute(
    "SELECT key, value FROM app_settings WHERE key IN "
    "('brand_name','body_color','navbar_color','navbar_text_color')"
):
    print(dict(row))

con.close()