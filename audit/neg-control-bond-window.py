#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neg-control-bond-window.py — prove the bond-window assertions can FAIL.

Restores the 0.8.0 substring fallback in engine/gsas_export.py (`all(e in k2 for e in els)`),
runs verify-bond-window.py, requires the two collision cases to fail, then restores the file
byte-exactly. Same discipline as the other controls: a check that cannot fail is not a check.

Usage: python neg-control-bond-window.py [<dsh-xrd dir>]
"""
import subprocess
import sys
from pathlib import Path

# Paths resolve from THIS file (the module ships inside the package as audit/).
HERE = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else (HERE.parent.parent / "dsh-xrd")
SRC = ROOT / "engine" / "gsas_export.py"
TEST = HERE / "verify-bond-window.py"

original = SRC.read_text(encoding="utf-8")
GOOD = """        want = _element_pair(pair)
        v = bond_el.get(want) if want else None
        if not v:"""
BAD = """        want = None
        v = None
        if not v:"""
if GOOD not in original:
    sys.exit("anchor not found — the fix is not where the control expects it")
mutated = original.replace(GOOD, BAD, 1)
# and put the substring loop back
MUT_LOOP = """            v = bond_map.get(key) or bond_map.get(pair)"""
SUB_LOOP = """            v = bond_map.get(key) or bond_map.get(pair)
            if not v:
                _els = [e.strip() for e in str(pair).split('-') if e.strip()]
                if len(_els) == 2:
                    for _k2, _vals in bond_map.items():
                        if all(e in _k2 for e in _els):
                            v, key = _vals, _k2
                            break"""
mutated = mutated.replace(MUT_LOOP, SUB_LOOP, 1)
if mutated == original:
    sys.exit("mutation produced no change — the control is meaningless")


def run_test():
    out = subprocess.run([sys.executable, str(TEST), str(ROOT)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout + out.stderr


try:
    SRC.write_text(mutated, encoding="utf-8")
    mutated_out = run_test()
finally:
    SRC.write_text(original, encoding="utf-8")
restored_ok = SRC.read_text(encoding="utf-8") == original
restored_out = run_test()


def fails_of(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("FAIL")]


def verdict(text):
    for ln in text.splitlines():
        if "ALL PASS" in ln or "FAILURES" in ln:
            return ln.strip()
    return "(none)"


print("--- control 1: substring fallback restored (the 0.8.0 defect) ---")
for ln in fails_of(mutated_out):
    print(ln)
print("verdict:", verdict(mutated_out))
print("--- control 2: file restored ---")
print("FAILs:", len(fails_of(restored_out)))
print("verdict:", verdict(restored_out))
print(f"file restored byte-exact: {restored_ok}")
mut_fails = fails_of(mutated_out)
ok = bool(mut_fails) and not fails_of(restored_out) and restored_ok
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
