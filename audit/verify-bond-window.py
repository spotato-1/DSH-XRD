#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify-bond-window.py — the bond-window check must match ELEMENT PAIRS exactly.

The defect (codex review 2026-09-27, `engine/gsas_export.py`): the window lookup fell
back to `all(e in k2 for e in els)`, i.e. string containment on species keys. So a
requested "P-O" window was satisfied by a "Pb-O" contact and "C-N" by "C-Na" — the check
then judged the WRONG atom pair and could report pass/fail for a chemistry nobody asked
about. This is an OUTPUT-side physical validation, so a wrong verdict is worse than a
missing one.

Cases:
  A  Pb+O structure, caller window "P-O"     -> "no such contact found" (never a length
                                                violation against Pb-O)
  B  C/N/Na structure, window "C-N"          -> the 1.15 A contact is judged; the 3.0 A
                                                C-Na distance must not enter the window
  C  real P-O at 1.50 A, window [1.4,1.7]    -> clean pass (positive control)
  D  unit: _element_pair() identity + the two reported collisions

Usage: python verify-bond-window.py [<dsh-xrd dir>]
"""
import importlib.util
import shutil
import sys
from pathlib import Path

# Resolve from THIS file, never a literal maintainer path: the module ships inside the
# package (audit/), and a hardcoded tree leaks the maintainer's layout (codex review
# 2026-09-27). Dev layout: <repo>/workspace/xrd-plugin-checks -> <repo>/dsh-xrd.
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent.parent.parent / "dsh-xrd")
ENGINE = ROOT / "engine" / "gsas_export.py"
if not ENGINE.exists():
    sys.exit(f"engine not found: {ENGINE}")

# Scratch INSIDE the workspace: the harness's %TEMP% is a per-session directory that a
# sandboxed process may not create files in (measured: PermissionError on mkdtemp).
WORK = Path(__file__).resolve().parent / ".bond-work"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("gsas_export_probe", ENGINE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

passes, fails = [], []


def ok(cond, label, detail=""):
    (passes if cond else fails).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"\n        {detail}" if (detail and not cond) else ""))


CIF = """data_probe
_cell_length_a 10.0
_cell_length_b 10.0
_cell_length_c 10.0
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_symmetry_space_group_name_H-M 'P 1'
loop_
  _atom_site_label
  _atom_site_type_symbol
  _atom_site_fract_x
  _atom_site_fract_y
  _atom_site_fract_z
  _atom_site_occupancy
{rows}
"""


def write_cif(tmp, rows):
    """rows: list of (label, symbol, x, y, z)."""
    body = "\n".join(f"  {lab} {sym} {x:.4f} {y:.4f} {z:.4f} 1.0" for lab, sym, x, y, z in rows)
    p = Path(tmp) / "probe.cif"
    p.write_text(CIF.format(rows=body), encoding="utf-8")
    return str(p)


def sanity(cif, bond_expect):
    # bond_expect_defaults off: this test is about CALLER-supplied windows only
    return mod.sanity_check(cif, {"bond_expect": bond_expect, "bond_expect_defaults": False})


print("== D. element-pair parsing (the fix's primitive) ==")
ok(mod._element_pair("P-O") == ("O", "P"), "_element_pair('P-O') -> ('O','P')", str(mod._element_pair("P-O")))
ok(mod._element_pair("Pb-O") == ("O", "Pb"), "_element_pair('Pb-O') -> ('O','Pb')")
ok(mod._element_pair("O2--Pb2+") == ("O", "Pb"), "charge decorations dissolve ('O2--Pb2+' -> ('O','Pb'))",
   str(mod._element_pair("O2--Pb2+")))
ok(mod._element_pair("Mn0.62-O") == ("Mn", "O"), "occupancy decoration dissolves ('Mn0.62-O')",
   str(mod._element_pair("Mn0.62-O")))
ok(mod._element_pair("N1-C1") == ("C", "N"), "site-numbered labels dissolve ('N1-C1')",
   str(mod._element_pair("N1-C1")))
ok(mod._element_pair("P-O") != mod._element_pair("Pb-O"), "THE reported collision: 'P-O' and 'Pb-O' are different pairs")
ok(mod._element_pair("C-N") != mod._element_pair("C-Na"), "THE reported collision: 'C-N' and 'C-Na' are different pairs")
ok(mod._element_pair("garbage") is None, "an unusable key returns None instead of guessing")

if True:
    print("== A. Pb+O structure, caller asks for P-O ==")
    cif = write_cif(WORK, [("Pb1", "Pb", 0.0, 0.0, 0.0), ("O1", "O", 0.0, 0.0, 0.23)])
    r = sanity(cif, {"P-O": [1.4, 1.7]})
    viol = r.get("bond_violations", [])
    ok(any(v.get("issue") == "no such contact found" for v in viol),
       "P-O is reported MISSING on a Pb-O structure (not silently matched)", str(viol))
    ok(not any("expected" in v for v in viol),
       "no length verdict is issued against the Pb-O contact", str(viol))
    ok(r.get("bonds"), "the Pb-O contact itself is still reported in bonds", str(r.get("bonds")))

    print("== B. C/N/Na structure, caller asks for C-N ==")
    cif = write_cif(WORK, [("C1", "C", 0.0, 0.0, 0.0), ("N1", "N", 0.0, 0.0, 0.115),
                           ("Na1", "Na", 0.30, 0.0, 0.0)])
    r = sanity(cif, {"C-N": [1.05, 1.25]})
    viol = r.get("bond_violations", [])
    ok(not viol, "the 1.15 A C-N contact satisfies [1.05,1.25] -> no violation", str(viol))
    # the discriminating case: tighten the window so the C-N contact itself is out of range;
    # the verdict must come from C-N (1.15) and never from the 3.0 A C-Na contact
    r2 = sanity(cif, {"C-N": [1.30, 1.40]})
    v2 = r2.get("bond_violations", [])
    ok(bool(v2) and v2[0].get("n_out", 0) >= 1 and all(
        abs(x - 1.15) < 0.02 for x in (v2[0].get("range") or [])),
       "a violating C-N window is judged on the C-N distance only", str(v2))

    print("== C. real P-O contact (positive control) ==")
    cif = write_cif(WORK, [("P1", "P", 0.0, 0.0, 0.0), ("O1", "O", 0.0, 0.0, 0.15)])
    r = sanity(cif, {"P-O": [1.4, 1.7]})
    ok(not r.get("bond_violations", []), "a genuine 1.50 A P-O passes [1.4,1.7]", str(r.get("bond_violations")))
    r = sanity(cif, {"P-O": [1.6, 1.8]})
    just = [v for v in r.get("bond_violations", []) if "expected" in v]
    ok(len(just) == 1 and just[0]["pair"] == "P-O", "the same contact is caught when the window is wrong", str(r.get("bond_violations")))

shutil.rmtree(WORK, ignore_errors=True)

print(f"\n  {'FAILURES' if fails else 'ALL PASS'} — pass={len(passes)} fail={len(fails)}")
for f in fails:
    print(f"   - {f}")
sys.exit(1 if fails else 0)
