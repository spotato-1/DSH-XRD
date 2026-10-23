# _sample-shape-scan.py — enumerate sample-code-SHAPED tokens in a plugin tree.
#
# The release builder's audit rule is `\b[a-z]{2}\d{2}\b` with re.IGNORECASE, applied
# AFTER the scrub table. So any such token whose lowercase form is NOT a scrub key
# survives into the package and fails the build audit. This script finds them by
# enumeration (the 0.7.0 / 0.7.3 lesson: never check a list against itself, and never
# trust the eye).
#
# It resolves every path from its own location and carries NO literal sample code:
# the ignore list is read out of the builder when the dev tree is present (the same
# "read the tables from the builder, never duplicate them" rule three-copy-check.mjs
# follows), and is empty inside a shipped package — where the scrub has already run,
# so ANY matching token is a finding.
#
# Usage: python _sample-shape-scan.py [<plugin root>] [--ignore key1,key2]
import os
import re
import sys
from pathlib import Path

import json  # noqa: F401  (kept for parity with the other audit helpers)

HERE = Path(__file__).resolve().parent


def default_root() -> Path:
    """Dev layout: <repo>/workspace/xrd-plugin-checks -> <repo>/dsh-xrd.
    Package layout: <pkg>/audit -> <pkg> (the tree is the package itself)."""
    if os.environ.get("DSH_XRD_SRC"):
        return Path(os.environ["DSH_XRD_SRC"])
    sibling = HERE.parent / "dsh-xrd"
    return sibling if sibling.is_dir() else HERE.parent


def keys_from_builder() -> set:
    """Scrub keys read from the builder, when it exists. Never duplicated here."""
    builder = HERE.parent / "build-xrd-release.mjs"
    if not builder.is_file():
        return set()
    text = builder.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"const SCRUB = \[(.*?)\n\]", text, re.S)
    if not block:
        return set()
    return {m.lower() for m in re.findall(r"'([^']+)'", block.group(1))}


argv = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = Path(argv[0]) if argv else default_root()
ignore = keys_from_builder()
for i, a in enumerate(sys.argv):
    if a == "--ignore" and i + 1 < len(sys.argv):
        ignore |= {k.strip().lower() for k in sys.argv[i + 1].split(",") if k.strip()}

SKIP_DIRS = {"node_modules", ".git", "dist", "__pycache__", ".work", ".GSASII"}
TEXT_EXT = {".js", ".mjs", ".cjs", ".py", ".md", ".json", ".yml", ".yaml",
            ".txt", ".pcr", ".cif", ".dat", ".prm", ".cfg", ".ini", ".patch"}
SHAPE = re.compile(r"\b[a-z]{2}\d{2}\b", re.IGNORECASE)

hits: dict[str, list[str]] = {}
files = 0
for p in ROOT.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
        continue
    if any(part in SKIP_DIRS or part.startswith(".") for part in p.parts):
        continue
    files += 1
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        for m in SHAPE.finditer(line):
            tok = m.group(0)
            if tok.lower() in ignore:
                continue                      # scrubbed before the audit runs
            hits.setdefault(tok, []).append(f"{p.relative_to(ROOT)}:{i}")

print(f"scanned {files} text file(s) under {ROOT}")
print(f"ignore list ({len(ignore)}): {', '.join(sorted(ignore)) if ignore else '(none)'}")
if not hits:
    print("no un-scrubbed sample-code-shaped token — the build audit's shape rule will pass")
    raise SystemExit(0)
print(f"FOUND {len(hits)} distinct token(s) that would survive the scrub:")
for tok, where in sorted(hits.items()):
    print(f"  {tok}: {len(where)} occurrence(s)")
    for w in where[:10]:
        print(f"      {w}")
raise SystemExit(1)
