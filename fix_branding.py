"""Set the holding site's name and link the logo."""
import sqlite3
import shutil
from pathlib import Path

con = sqlite3.connect("data/persman.db")

# 1. Update the name
con.execute(
    "UPDATE sites SET name=?, tagline=? WHERE slug='holding'",
    ("Codename BlackSheep", ""),
)

# 2. Find and copy the logo
DATA = Path("data")
sites_holding = DATA / "sites" / "holding"
sites_holding.mkdir(parents=True, exist_ok=True)

# Try common locations for the old logo
candidates = [
    DATA / "branding" / "logo.png",
    DATA / "branding" / "logo.jpg",
    DATA / "branding" / "logo.jpeg",
    DATA / "logo.png",
    DATA / "logo.jpg",
]
found = None
for p in candidates:
    if p.exists():
        found = p
        break

if found:
    ext = found.suffix.lstrip(".").lower()
    dest = sites_holding / f"logo.{ext}"
    shutil.copy(found, dest)
    con.execute("UPDATE sites SET logo_ext=? WHERE slug='holding'", (ext,))
    print(f"Logo copied from {found} to {dest}")
else:
    print("No logo file found in the usual places. You can upload one later at /holding/admin/sites/holding/edit.")

con.commit()

# Show the result
print("\nSites table:")
for row in con.execute("SELECT slug, name, logo_ext, is_holding FROM sites"):
    print(f"  {row[0]:10}  name={row[1]!r:25}  logo={row[2]}  holding={row[3]}")

con.close()
