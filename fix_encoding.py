"""
Repairs UTF-8 text that was wrongly re-saved as Windows-1252 (shows as  â€"  â†•  Â·  etc.)
and removes the BOM that PowerShell adds.

Run from the project root (folder with manage.py):
    python fix_encoding.py            -> dry run, only reports
    python fix_encoding.py --write    -> actually fixes the files
Then check with:  git diff
"""
import pathlib, re, sys

FOLDER = "templates"          # change if needed
run = re.compile(r"[^\x00-\x7f]+")   # every group of non-ASCII characters

def fix_run(m):
    s = m.group(0)
    try:
        return s.encode("cp1252").decode("utf-8")   # undo the wrong decoding
    except UnicodeError:
        return s                                      # genuine character, leave alone

changed = 0
for p in sorted(pathlib.Path(FOLDER).rglob("*.html")):
    raw = p.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    if had_bom:
        raw = raw[3:]
    text = raw.decode("utf-8")
    fixed = run.sub(fix_run, text)
    if fixed != text or had_bom:
        changed += 1
        print(("fixing  " if "--write" in sys.argv else "needs fix  ") + str(p))
        if "--write" in sys.argv:
            p.write_bytes(fixed.encode("utf-8"))   # UTF-8, no BOM
print(f"\n{changed} file(s) affected." + ("" if "--write" in sys.argv else "  (dry run - add --write to fix)"))
