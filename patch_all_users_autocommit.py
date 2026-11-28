"""Persman patch: ensure every write route (admin AND non-admin users) auto-commits
to GitHub.

The earlier patches added commit_github() to admin routes. This patch:
  1. Verifies which non-admin write routes already have auto-commit
  2. Adds commit_github() to the ones that don't
  3. Skips logout and session-tick routes (they shouldn't commit)
  4. Reports what changed

Non-admin writes in Persman:
  * save_document()          — employee uploads own document (via /me/upload)
  * delete_document()        — admin deletes (already covered)
  * change_password()        — any signed-in user
  * vacancy_apply()          — public applicant uploads CV/ID/licence
  * admin_sale_payment()     — sale payment (already covered)

Safe to re-run.
"""
import os
import re
import shutil
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_backup_allusers_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
os.makedirs(BACKUP, exist_ok=True)


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def write(p, s):
    full = os.path.join(ROOT, p)
    with open(full, "w", encoding="utf-8") as f:
        f.write(s)


def backup(p):
    src = os.path.join(ROOT, p)
    if os.path.exists(src):
        flat = p.replace("/", "_").replace(os.sep, "_")
        shutil.copy2(src, os.path.join(BACKUP, flat))


results = []


def check(name, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FIX '}  {name}  {detail}")
    results.append((name, ok))


def add_commit_after(file, anchor, commit_line, label):
    """Add a commit_github() call right after an anchor in file, if not already present."""
    src = read(file)
    # Check if commit_github already appears near the anchor
    idx = src.find(anchor)
    if idx == -1:
        check(label, False, f"anchor not found in {file}")
        return
    # Look at the block after anchor for commit_github
    block = src[idx:idx + 600]
    if "commit_github" in block or "commit_data_folder" in block:
        check(label, True, "(already present)")
        return
    # Insert commit line right after anchor
    new_src = src[:idx + len(anchor)] + "\n" + commit_line + src[idx + len(anchor):]
    backup(file)
    write(file, new_src)
    check(label, False, "(added)")


# =========================================================================
# 1. save_document in app.py — already has commit? verify.
# =========================================================================
print("\n[1/5] app.py — save_document")

src = read("app.py")
if "def save_document" in src:
    # Find function and check for commit_github
    idx = src.find("def save_document")
    next_def = src.find("\ndef ", idx + 1)
    next_route = src.find("\n@app.", idx + 1)
    end = min(x for x in [next_def, next_route] if x > 0) if (next_def > 0 or next_route > 0) else len(src)
    body = src[idx:end]
    if "commit_github" in body:
        check("save_document: auto-commit", True, "(already present)")
    else:
        # Insert commit after the last db.commit() in the function
        # Find last db.commit() before the function's return
        match = re.search(r'(db\.commit\(\))(?!.*db\.commit\(\))', body, re.DOTALL)
        if match:
            insert_at = idx + match.end()
            new_src = src[:insert_at] + '\n    commit_github(f"Document: {original}", include_employees=True)' + src[insert_at:]
            backup("app.py")
            write("app.py", new_src)
            check("save_document: auto-commit", False, "(added)")
        else:
            check("save_document: auto-commit", False, "could not find db.commit()")
else:
    check("save_document: auto-commit", False, "function not found")


# =========================================================================
# 2. delete_document in app.py
# =========================================================================
print("\n[2/5] app.py — delete_document")

src = read("app.py")
if "def delete_document" in src:
    idx = src.find("def delete_document")
    next_def = src.find("\ndef ", idx + 1)
    next_route = src.find("\n@app.", idx + 1)
    end = min(x for x in [next_def, next_route] if x > 0) if (next_def > 0 or next_route > 0) else len(src)
    body = src[idx:end]
    if "commit_github" in body:
        check("delete_document: auto-commit", True, "(already present)")
    else:
        match = re.search(r'(db\.commit\(\))(?!.*db\.commit\(\))', body, re.DOTALL)
        if match:
            insert_at = idx + match.end()
            new_src = src[:insert_at] + '\n    commit_github(f"Document deleted: {doc[\'original_name\']}", include_employees=True)' + src[insert_at:]
            backup("app.py")
            write("app.py", new_src)
            check("delete_document: auto-commit", False, "(added)")
        else:
            check("delete_document: auto-commit", False, "could not find db.commit()")
else:
    check("delete_document: auto-commit", False, "function not found")


# =========================================================================
# 3. change_password in app.py
# =========================================================================
print("\n[3/5] app.py — change_password")

src = read("app.py")
if "def change_password" in src:
    idx = src.find("def change_password")
    next_def = src.find("\ndef ", idx + 1)
    next_route = src.find("\n@app.", idx + 1)
    end = min(x for x in [next_def, next_route] if x > 0) if (next_def > 0 or next_route > 0) else len(src)
    body = src[idx:end]
    if "commit_github" in body:
        check("change_password: auto-commit", True, "(already present)")
    else:
        # Insert commit after the db.commit() in the password change success path
        # The pattern is: audit(... password_changed ...); db.commit()
        match = re.search(r'audit\(g\.user\["id"\], "password_changed"[^\n]*\n\s*db\.commit\(\)', body)
        if match:
            insert_at = idx + match.end()
            new_src = src[:insert_at] + f'\n            commit_github(f"Password changed: {{g.user[\'id\']}}")' + src[insert_at:]
            backup("app.py")
            write("app.py", new_src)
            check("change_password: auto-commit", False, "(added)")
        else:
            check("change_password: auto-commit", False, "could not find password_changed commit")
else:
    check("change_password: auto-commit", False, "function not found")


# =========================================================================
# 4. vacancy_apply in vacancies.py — CRITICAL: applicant CVs
# =========================================================================
print("\n[4/5] vacancies.py — vacancy_apply (public applicant)")

src = read("vacancies.py")
if "def vacancy_apply" in src:
    idx = src.find("def vacancy_apply")
    next_def = src.find("\ndef ", idx + 1)
    next_route = src.find("\n@app.", idx + 1)
    end = min(x for x in [next_def, next_route] if x > 0) if (next_def > 0 or next_route > 0) else len(src)
    body = src[idx:end]

    if "commit_data_folder" in body or "commit_file" in body:
        check("vacancy_apply: commit DB + CV files", True, "(already present)")
    else:
        # Insert the commit block after the second db.commit() (the one after audit)
        # Look for: audit("public", "application_submitted" ...) followed by db.commit()
        match = re.search(
            r'audit\("public", "application_submitted"[^\n]*\n\s*db\.commit\(\)',
            body
        )
        if match:
            insert_at = idx + match.end()
            commit_block = '''

        # --- commit DB + uploaded files so the application survives restarts ---
        try:
            from github_commit import commit_file, commit_data_folder
            commit_data_folder(f"Application: {f['first_name']} {f['surname']}")
            if app_dir.exists():
                for f2 in app_dir.iterdir():
                    if f2.is_file():
                        rel = f2.relative_to(DATA).as_posix()
                        commit_file(f2, f"data/{rel}",
                                    f"Application {aid} — {f2.name}")
        except Exception as e:
            print(f"[application] GitHub commit failed: {e}", flush=True)'''
            new_src = src[:insert_at] + commit_block + src[insert_at:]
            backup("vacancies.py")
            write("vacancies.py", new_src)
            check("vacancy_apply: commit DB + CV files", False, "(added)")
        else:
            check("vacancy_apply: commit DB + CV files", False, "could not find audit/commit block")
else:
    check("vacancy_apply: commit DB + CV files", False, "function not found")


# =========================================================================
# 5. Verify commit_github is importable everywhere it's needed
# =========================================================================
print("\n[5/5] Verify commit_github import")

for fname in ("app.py", "premises.py", "business.py", "vacancies.py", "website.py", "gallery.py"):
    if not os.path.exists(os.path.join(ROOT, fname)):
        continue
    src = read(fname)
    uses_commit = "commit_github(" in src or "commit_data_folder(" in src or "commit_file(" in src
    has_import = (
        "from app import" in src and "commit_github" in src.split("from app import")[1][:500]
    ) or (
        "from github_commit import" in src
    )
    if uses_commit and not has_import:
        # Add import
        if "from app import" in src:
            # Extend the existing import line
            src = re.sub(
                r'(from app import \([^)]*?)(\))',
                r'\1, commit_github\2',
                src,
                count=1,
                flags=re.DOTALL,
            )
            backup(fname)
            write(fname, src)
            check(f"{fname}: commit_github import", False, "(added)")
        else:
            check(f"{fname}: commit_github import", False, "no 'from app import' block found")
    else:
        check(f"{fname}: commit_github import", True, "(present or not needed)")


# =========================================================================
# Summary
# =========================================================================
print("\n===================")
added = sum(1 for _, ok in results if not ok and "added" in str(_))
already = sum(1 for _, ok in results if ok)
failed = [name for name, ok in results if not ok]

if not failed:
    print("ALL CHANGES APPLIED (or already in place)")
    print("\nNon-admin writes now auto-commit to GitHub:")
    print("  ✓ Employee document upload (via save_document)")
    print("  ✓ Employee document delete (admin)")
    print("  ✓ Password change (any signed-in user)")
    print("  ✓ Job application + CVs (public)")
    print("\nSkipped (deliberately):")
    print("  — logout (too frequent)")
    print("  — session last_seen tick (too frequent)")
else:
    print(f"SOME CHECKS NEEDED ATTENTION: {', '.join(failed)}")
    print("See messages above. Most 'FIX' lines were resolved automatically.")

print(f"\nBackups in: {BACKUP}")
print("\nNext steps:")
print("  1. Restart the server:  flask --app app.py run")
print("  2. Test the writes:")
print("     - Employee uploads a document via /me/upload")
print("     - Applicant submits a job application with a CV via /vacancies/<id>/apply")
print("     - Any user changes their password via /password")
print("  3. Check GitHub commits — each should produce a 'Document: ...' or 'Application: ...' commit")