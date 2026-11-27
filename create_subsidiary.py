"""Create a subsidiary. Usage: python create_subsidiary.py acme "Acme Properties" """
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

if len(sys.argv) < 3:
    print("Usage: python create_subsidiary.py <slug> \"<Name>\"")
    sys.exit(1)

slug = sys.argv[1].lower().strip()
name = sys.argv[2].strip()

if slug in ("holding", "admin", "static", "api", "p", "branding"):
    print(f"Slug '{slug}' is reserved. Pick another.")
    sys.exit(1)

con = sqlite3.connect("data/persman.db")

# Insert the site
try:
    con.execute(
        "INSERT INTO sites (slug, name, tagline, body_color, navbar_color, "
        "navbar_text_color, is_holding, is_active, created_at) "
        "VALUES (?, ?, '', '#ffffff', '#000000', '#ffffff', 0, 1, ?)",
        (slug, name, datetime.utcnow().isoformat()),
    )
    con.commit()
except sqlite3.IntegrityError:
    print(f"Slug '{slug}' already exists.")
    sys.exit(1)

# Create the folder for this site's files
folder = Path("data") / "sites" / slug
folder.mkdir(parents=True, exist_ok=True)

print(f"Subsidiary created: /{slug}/ → {name}")
print(f"Logo folder: {folder}")
print(f"Log in at: http://localhost:5000/{slug}/login")
print(f"Admin at: http://localhost:5000/{slug}/admin")
print()
print("Note: Users for this site must be created with site_slug = '{slug}'")
print("Until the query-scoping patch is applied, all sites share data.")

con.close()