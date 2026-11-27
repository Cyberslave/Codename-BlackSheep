# Save as reset_app_settings.py
import sqlite3
con = sqlite3.connect("data/persman.db")
updates = {
    "brand_name": "Nku Holdings",
    "body_color": "#ffffff",
    "navbar_color": "#000000",
    "navbar_text_color": "#ffffff",
}
for key, value in updates.items():
    con.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
con.commit()
print("app_settings updated")
con.close()