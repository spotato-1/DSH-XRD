# _neg-control-catalog.py — negative control for integrity-check section 7.
# Mutates ONE detail field out of ONE catalog row, runs the checker, restores the file,
# and prints section 7. Without this, "section 7 all PASS" could just mean the check
# never reads anything. Python (not pwsh) because node spawn with piped stdio is
# blocked under the sandbox, while subprocess.run(capture_output=True) works.
#
# Paths resolve from this file's own location (this ships inside the package as
# audit/), and the tree under test can be overridden with DSH_XRD_SRC.
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def default_root() -> Path:
    """Dev layout: <repo>/workspace/xrd-plugin-checks -> <repo>/dsh-xrd.
    Package layout: <pkg>/audit -> <pkg> (the package IS the plugin tree)."""
    if os.environ.get("DSH_XRD_SRC"):
        return Path(os.environ["DSH_XRD_SRC"])
    sibling = HERE.parent / "dsh-xrd"
    return sibling if sibling.is_dir() else HERE.parent


CHECKS = HERE
ROOT = default_root()
CATMD = ROOT / "skills" / "xrd-workflow" / "refs" / "defect-catalog.md"
if not CATMD.is_file():
    sys.exit(f"catalog not found: {CATMD}\npass DSH_XRD_SRC=<plugin dir>")

original = CATMD.read_text(encoding="utf-8")
# drop the pitfall line of one row only (the row is chosen by its stable id)
TARGET = "- **pitfall**：位移与 **Uiso 强相关**，容易互相补偿\n"
mutated = original.replace(TARGET, "", 1)
if mutated == original:
    sys.exit("mutation target not found — the control is meaningless (did the row change?)")


def run_checker() -> str:
    out = subprocess.run([  # noqa: S603
        "node", str(CHECKS / "integrity-check.mjs"), "--root", str(ROOT),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout + out.stderr


try:
    CATMD.write_text(mutated, encoding="utf-8")
    mutated_out = run_checker()
finally:
    CATMD.write_text(original, encoding="utf-8")
restored_ok = CATMD.read_text(encoding="utf-8") == original
restored_out = run_checker()


def section7(text: str) -> list:
    lines, on = [], False
    for ln in text.splitlines():
        if ln.startswith("== 7."):
            on = True
        elif ln.startswith("== ") and on:
            on = False
        if on:
            lines.append(ln)
    return lines


print("--- control 1: one row's pitfall removed (expect FAIL) ---")
for ln in section7(mutated_out):
    if "FAIL" in ln or ln.startswith("== 7"):
        print(ln)
print("--- control 2: file restored (expect all PASS) ---")
for ln in section7(restored_out):
    if "FAIL" in ln or ln.startswith("== 7"):
        print(ln)
print(f"file restored byte-exact: {restored_ok}")
fails_mut = sum("FAIL" in ln for ln in section7(mutated_out))
fails_res = sum("FAIL" in ln for ln in section7(restored_out))
print("VERDICT:", "PASS" if fails_mut == 1 and fails_res == 0 and restored_ok else "FAIL")
