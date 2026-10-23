#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gsas_engine — GSAS-II Rietveld refinement driver (executor, FINAL-OPTIMIZATION stage).

Pure executor: reads a *plan JSON* describing an XRD data file, an instrument
parameter file, a CIF (or existing .gpx), and an optimization intent (which
parameter families to refine); drives GSAS-II's scripting interface
(GSASIIscriptable / G2Project) through a refinement ladder; writes a result
JSON (metrics + per-phase cells + .lst tail) and keeps all GSAS-II artifacts.

FIXES vs the original bundled engine (bug list C1/C2, verified in
PB/XRD/job_gsas/gsas_driver.py):
  C1: phase CIF is added WITH histograms=... and every phase is linked to every
      histogram (belt & braces) — the original "no linked histograms" /
      "Constraint error: No phases or no histograms in phases!" crash is gone.
  C2: ladder is not hard-wired to 3 background coeffs / Mustrain+Size together /
      atoms=XU. New plan keys:
        "n_back":     int background Chebyshev coeffs (default 3)
        "inst":       refine instrument params, e.g. ["U","V","W","X","Y","Zero"]
        "atoms_map":  {label: flags} instead of blanket atoms flags
        "atoms_flags":"XU" default blanket flags (used when no atoms_map)
        "size_only":  profile step refines Size only (not Mustrain) 
        "cell2":      True: cell refines for ALL phases in one step
                      False: cell refines main phase only
        "scale2_init": pre-ladder scale value for 2nd+ phases (refine off)
        "open2":      ["scale2","cell2"] post-ladder open (AFTER main ladder;
                      never open 2nd-phase scale+cell together with main-cell)
        "max_cycles": refinement cycles
  0.8.10 (two-phase hand-off, per-phase switches, entry policy):
        "phases":     [{...}] per-phase switches, index = phase order from cif/cif2:
                        {"cell":bool, "scale":bool, "size":bool,
                         "mustrain":bool|"isotropic"|"uniaxial"|"generalized",
                         "hstrain":bool, "pref":bool, "atoms":"XU"|flags}
                      ABSENT => legacy behaviour (one global step per family,
                      cell2/size_only/scale2_init/open2 keep working unchanged).
        "scale":      new refine family: phase fraction (Scale) per phase.
        "handoff":    path to a manual-engine result JSON -> seeds phase Scale,
                      cell and instrument U/V/W/X/Y/Zero BEFORE the ladder;
                      anything not transferred is reported, never silently dropped.
        "entry":      "hold"  => background-only verification step first, then the
                                requested families released ONE at a time;
                      "direct" => legacy single batch, no rollback.
                      Default: "hold" when continuing an existing .gpx or when a
                      handoff is given, else "direct".
        "rollback":   bool (default true): a step whose wR does not improve on the
                      best-so-far is undone from a snapshot instead of kept.
        "rollback_tol": float tolerance in wR units (default 0.0).
  LADDER MECHANICS (learned the hard way, 2026-10-02 - keep these two invariants):
  (a) GSAS-II's refine() is FILE-BASED: G2Project.refine() calls
      GSASIIstrMain.Refine(self.filename) and index_ids() auto-saves, so the
      refinement reads and writes self.filename. `gpx.save(other_path)` REBINDS
      self.filename ("Updates self.filename if a new filename provided"), which
      turns a snapshot written that way into the WORKING file - the next
      refinement then overwrites it and every rollback "restores" the state it was
      meant to undo. Snapshots are therefore COPY files, and self.filename is
      pinned to proj_used for the whole run.
  (b) A step GSAS-II itself reports as failed ("Invalid cell metric tensor ...",
      "Refinement failed") RETURNS NORMALLY with partially updated parameters, so
      it must be caught by tapping GSAS-II's own stdout and is never allowed to
      become the best state. Only a failure that the ladder could NOT undo reaches
      `warning`/ok:false; a failure that was rolled back is reported in `ladder`.
  Result adds "phases":[{"name","cell"}], "lst_tail" (last .lst lines),
  "entry", "ladder":[{step,label,wR_before,wR_after,accepted,rolled_back,
  gsas_error?,cell_change_pct?}] and
  "handoff":{transferred:[...], not_transferred:[...]} when a handoff was used.

USAGE POLICY (hard rule from the user, enforced at the skill level, NOT here):
GSAS-II is a FINAL-OPTIMIZATION option ONLY. It may be invoked only AFTER an
AutoFP (xrd_refine_auto) run or a manual engine (xrd_refine_manual) run has
already produced a converged result for this sample. This file never decides
that ordering — the tool wrapper / skills carry the gate.

Errors: never a bare throw — on failure result carries {"ok": false, "error": "...",
"log_tail": "..."} and whatever artifacts exist are kept.
"""
import argparse
import json
import math
import os
import re
import shutil
import sys

from result_io import write_result
from proc_guard import start_guard, run_guarded_main

# element symbols accepted as atom TYPES; a type outside this set means the file
# carries something that is not an element (see _element_symbol / the 'Fen_' bug)
try:
    from pymatgen.core import Element as _PmgElement
    _ELEMENTS = {str(e.symbol) for e in _PmgElement}
except Exception:                                   # pymatgen absent: fall back
    _ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr
Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re
Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn
Nh Fl Mc Lv Ts Og""".split())


def log(msg):
    print(msg, flush=True)


def _param_lookup(params):
    """Normalise a manual-engine result "params" block into {name: value}.

    The bounded engine has written it both as a dict {name: {value, stderr}} and
    as a list of health rows, so accept both instead of guessing.
    """
    out = {}
    try:
        if isinstance(params, dict):
            for k, v in params.items():
                if isinstance(v, dict):
                    out[str(k)] = v.get("value")
                else:
                    out[str(k)] = v
        elif isinstance(params, list):
            for row in params:
                if isinstance(row, dict) and "name" in row:
                    out[str(row["name"])] = row.get("value")
    except Exception:
        pass
    return out


def apply_handoff(gpx, path, log):
    """0.8.10: seed a GSAS-II project from a bounded-manual-engine result JSON.

    Design order (user, 2026-10-02): the MANUAL engine settles the two-phase
    solution first, then GSAS-II takes over for the final refinement. Before this
    existed nothing crossed that boundary except the background curve, so a phase
    ratio the manual engine had just fitted was silently replaced by 1:1 and GSAS
    then absorbed the difference into background/profile.

    Transferred, and only when the manual run actually reports it:
      * phase fraction (Scale) for the 2nd+ phases: ratio = s2_<i>_scale / scale
      * the main phase cell, from result["structure"]["lattice"]
      * instrument U/V/W/Zero, when the manual run refined them, CONVERTED from the
        manual engine's convention to GSAS-II's (U/V/W x1e4/8ln2, Zero x1 - see the
        notes at the transfer itself). X/Y are listed but INERT: the manual engine
        has no X/Y parameter at all.
    Deliberately NOT transferred (reported, never invented): Size/Mustrain (a
    different parameterisation - it must be re-fitted), background (use
    xrd_gsas_ops "import_background"), atom coordinates/occupancies (the CIF is
    the structure source of record).
    Warnings (not refusals) are raised when a transferred value cannot mean what it
    looks like: a phase the manual run fitted to essentially zero (it starts
    invisible in GSAS-II), or instrument terms more than 10x away from what the
    instrument file already carries. Both are reported, never silently absorbed.
    """
    info = {"source": path, "transferred": [], "not_transferred": [],
            "ratio": None, "warnings": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            man = json.load(fh)
    except Exception as e:
        info["warnings"].append("handoff unreadable: %s" % e)
        return info

    pm = _param_lookup(man.get("params"))
    phs = gpx.phases()
    main_scale = pm.get("scale")

    # The manual run's own fit quality travels with the seed: a hand-off is only
    # worth taking from a solution the caller can see the quality of (measured
    # 2026-10-02: a two-phase manual run reported Rwp 53.3% and had driven the 2nd
    # phase's Scale to 2.8e-9 - a seed GSAS-II would have started from with that
    # phase invisible, and nothing in the old output said so).
    try:
        _rwp = (man.get("metrics") or {}).get("Rwp")
        if _rwp is not None:
            info["source_rwp"] = float(_rwp)
    except Exception:
        pass

    # ---- 1. phase ratio -> Scale of the 2nd+ phases (main phase stays the 1.0 ref)
    if len(phs) > 1 and main_scale not in (None, 0):
        for i, ph in enumerate(phs[1:]):
            s2 = pm.get("s2_%d_scale" % i)
            if s2 is None:
                info["not_transferred"].append(
                    "phase '%s' Scale: the manual result has no s2_%d_scale" % (ph.name, i))
                continue
            try:
                ratio = float(s2) / float(main_scale)
                if not (ratio == ratio) or ratio <= 0 or ratio > 1e6:
                    raise ValueError("implausible ratio %r" % ratio)
                for hn in (ph.data.get("Histograms") or {}):
                    ph.data["Histograms"][hn]["Scale"][0] = ratio
                info["transferred"].append("phase '%s' Scale = %.6g (ratio to main)" % (ph.name, ratio))
                if info["ratio"] is None:
                    info["ratio"] = ratio
                if ratio < 1e-4:
                    # Transferred faithfully, but the caller MUST know: at this scale
                    # the phase is invisible in the starting model. It means the manual
                    # run fitted it away (or never really had it), so the GSAS-II
                    # refinement that follows cannot be a two-phase refinement at all.
                    info["warnings"].append(
                        "phase '%s' was fitted to essentially ZERO by the manual run "
                        "(Scale ratio %.3g to the main phase): it is transferred as such, "
                        "so the GSAS-II fit starts with this phase invisible - re-do the "
                        "two-phase manual fit before handing off" % (ph.name, ratio))
                    log("handoff WARNING: " + info["warnings"][-1])
            except Exception as e:
                info["not_transferred"].append("phase '%s' Scale: %s" % (ph.name, e))
    elif len(phs) > 1:
        info["not_transferred"].append("phase ratio: the manual result has no main 'scale'")

    # ---- 2. main-phase cell (manual result carries the refined lattice)
    lat = ((man.get("structure") or {}).get("lattice") or {})
    if lat:
        try:
            gen = phs[0].data["General"]
            c = gen["Cell"]
            for j, k in enumerate(("a", "b", "c", "alpha", "beta", "gamma")):
                if lat.get(k) is not None:
                    c[j + 1] = float(lat[k])
            info["transferred"].append("main phase cell from the manual result")
        except Exception as e:
            info["not_transferred"].append("main phase cell: %s" % e)
    else:
        info["not_transferred"].append("cell: the manual result carries no lattice")

    # ---- 3. instrument parameters that the manual run refined (same quantity)
    # TWO defects fixed here, both measured 2026-10-02 on a real 11-BM hand-off:
    #  (a) the lookup used `key in ip` with ip = h0.data["Instrument Parameters"],
    #      but in GSAS-II that entry is a LIST of per-bank dicts, so no key ever
    #      matched, the transfer did nothing and it reported "nothing usable in the
    #      manual result" — a silently dead code path (the manual result DID carry
    #      U/V/W/zero).
    #  (b) UNITS - the two engines describe DIFFERENT quantities, which is why this
    #      conversion cannot be copied from either one; it was read out of both
    #      sources (user-reported defect 2026-10-05 - before it, U/V/W were 8ln2
    #      too large):
    #      * GSAS-II's U/V/W are the coefficients of the GAUSSIAN VARIANCE in
    #        CENTIDEGREES^2. GSASIIpwd.py:926 defines sig = sqrt(U tan^2+V tan+W)
    #        and GSASIIpwd.py:909-910 turns it into degrees and a full width:
    #        `widths = [np.sqrt(sig)/100., gam/100.]`, `fwhm = 2.355*widths[0] + ...`
    #        => sqrt(U..) is a STANDARD DEVIATION in centidegrees.
    #      * The manual engine's U/V/W are the coefficients of FWHM^2 in DEGREES^2:
    #        xrd_engine.hwidth returns sqrt(U tan^2+V tan+W) and THAT value is H, the
    #        full width at half maximum handed to the standard Gaussian g_area
    #        (`exp(-4 ln2 d^2/H^2)`).
    #      * Hence U/V/W = manual x 100^2/(2 sqrt(2 ln2))^2 = manual x 10000/8ln2
    #        = x1803.4. The previous x1e4 seeded a profile 5.545x too broad - below
    #        the 10x implausible-value guard below, so it was never refused; it just
    #        started the refinement from a visibly wrong width. (The earlier reading
    #        that justified x1e4 took sqrt(1.163) centideg as a FWHM: as a standard
    #        deviation 1.078 centideg IS 2.54 centideg = 0.0254 deg of FWHM, a
    #        perfectly normal instrument width.)
    #      * Zero is in DEGREES: G2lat.Dsp2pos returns 2*asind(...) + Zero with the
    #        arcsine in degrees, and the measured effect is brutal - seeding the
    #        manual zero converted as if centidegrees (x100 = -0.028) collapsed the
    #        fit from wR 25.90 to 74.54, while Zero=0.0 gave 25.90. Hence Zero x1.
    #      * X/Y are Lorentzian terms in centidegrees (deg -> centideg): x100.
    #      The conversion actually applied is stated in what was transferred.
    #  (c) SLOT: an entry is [reference, value, refine-flag] and GSAS reads the
    #      VALUE at index 1 (GSASIIstrIO: inst['Lam'][1], inst['difC'][1];
    #      gsas_ops.py reads v[1] with v[0] only as a fallback). Writing [0] is
    #      inert - measured on one and the same project, refining background only:
    #      baseline wR 25.849063 / initial chi^2 47812, U written to [0] => wR and
    #      initial chi^2 IDENTICAL, U written to [1] => initial chi^2 2.9175e+05
    #      (the broader profile is finally seen) and the refinement diverges.
    moved = []
    try:
        h0 = gpx.histograms()[0]
        _ip_raw = h0.data.get("Instrument Parameters") or []
        ip = _ip_raw[0] if isinstance(_ip_raw, list) and _ip_raw else (
            _ip_raw if isinstance(_ip_raw, dict) else {})
        # FWHM^2(deg^2) -> sigma^2(centideg^2): x1e4 for deg->centideg, /8ln2 for
        # FWHM->sigma^2 (see (b) above). X/Y rows are INERT - no manual result
        # carries them - and their own GSAS-II convention (Lorentzian terms, HWHM
        # vs FWHM) has NOT been verified here: measure before relying on them.
        _FWHM2SIG = 1e4 / (8.0 * math.log(2.0))              # 1803.37
        _XFER = (("U", _FWHM2SIG, "U"), ("V", _FWHM2SIG, "V"), ("W", _FWHM2SIG, "W"),
                 ("X", 1e2, "X"), ("Y", 1e2, "Y"), ("Zero", 1.0, "zero"))

        def _ip_val(entry):
            """Current value of an instrument-parameter entry ([ref, value, flag])."""
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                try:
                    return float(entry[1])
                except Exception:
                    pass
            try:
                return float(entry[0])
            except Exception:
                return None

        def _ip_set(entry, val):
            if isinstance(entry, list) and len(entry) >= 2:
                entry[1] = val
            else:
                entry[0] = val

        for key, fac, src in _XFER:
            if src not in pm or pm[src] is None or key not in ip:
                continue
            try:
                val = float(pm[src]) * fac
            except Exception:
                continue
            if not (val == val) or abs(val) > 1e6:
                continue
            old = _ip_val(ip[key])
            if old not in (None, 0) and abs(val) > 1e-9:
                big, small = max(abs(val), abs(old)), min(abs(val), abs(old))
                if small > 0 and big / small > 10:
                    # REFUSED, not merely reported. Measured on this very hand-off
                    # (2026-10-02): the manual fit had let U/V/W run away (Rwp 53%)
                    # and its converted U was 217x the 11-BM calibration value; the
                    # transfer took the delivered wR from 25.53% to 71.82%, i.e. the
                    # "seed" DESTROYED a good instrument state, and the entry step
                    # then accepted that wreck as the ladder's baseline. A manual
                    # value an order of magnitude away from a calibration-derived one
                    # is a broken fit, so the instrument file's value stands.
                    info["warnings"].append(
                        "instrument '%s': the manual value (%.4g -> %.4g in GSAS-II units) "
                        "is %.0fx the value this project already carries (%.4g); REFUSED, "
                        "the instrument parameter was NOT overwritten - re-fit the manual "
                        "profile before handing off if that seed is really wanted"
                        % (key, pm[src], val, big / small, old))
                    info["not_transferred"].append("instrument %s: implausible seed (%.0fx)"
                                                   % (key, big / small))
                    log("handoff WARNING: " + info["warnings"][-1])
                    continue
            _ip_set(ip[key], val)
            moved.append("%s=%.4g (manual %.4g x%g)" % (key, val, pm[src], fac))
    except Exception as e:
        info["warnings"].append("instrument transfer failed: %s" % e)
    if moved:
        info["transferred"].append("instrument " + "; ".join(moved))
    else:
        info["not_transferred"].append(
            "instrument U/V/W/X/Y/Zero: the manual result carries none of them")

    info["not_transferred"].extend([
        "Size/Mustrain: re-parameterised quantity - let the ladder fit it",
        "background: use xrd_gsas_ops 'import_background' for the manual curve",
        "atom coordinates/occupancies: the CIF is the source of record",
    ])
    log("handoff from %s: transferred=%s; not transferred=%s"
        % (path, info["transferred"] or "nothing", info["not_transferred"]))
    return info


def run(plan, guard=None):
    gsas_root = plan.get("gsas_root") or os.environ.get("DSH_GSAS_ROOT") or ""
    if not gsas_root:
        raise RuntimeError(
            'GSAS-II root not configured: pass "gsas_root" in the plan, set DSH_GSAS_ROOT, '
            'or add "gsas_root" to xrd.local.json')
    if not os.path.exists(os.path.join(gsas_root, "GSASII", "GSASIIscriptable.py")):
        return {"ok": False, "error": f"gsas_root missing GSASII package: {gsas_root}"}
    sys.path.insert(0, gsas_root)

    out_dir = plan.get("out_dir") or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    proj = os.path.join(out_dir, "gsas_proj.gpx")

    def _element_symbol(raw, fallback_label="", strict=False):
        """Element symbol for `raw`, '' when unknown, or '?' when DEFINITELY not an
        element.

        Needed because a CIF's atom TYPE can be something that is not an element:
        when the type-symbol column is missing, GSAS-II's CIF reader falls back to
        the atom LABEL, and a pymatgen-style label such as "FeN_1" is not an
        element. GSAS-II then silently substitutes Xe (or H) for the type.

        strict=True never falls back to the label and never salvages a symbol out
        of an arbitrary string - it answers "is this declared value a legitimate
        element?", where '?' means no and '' means unparseable.
        """
        cands = (raw,) if strict else (raw, fallback_label)
        for cand in cands:
            s = str(cand or "").strip()
            if not s:
                continue
            try:
                from pymatgen.core import Element
                return str(Element(s).symbol)      # clean single-symbol value
            except Exception:
                pass
            m = re.match(r"^([A-Za-z]{1,2})$", s)  # bare symbol
            if m and m.group(1).capitalize() in _ELEMENTS:
                return m.group(1).capitalize()
            m = re.match(r"^([A-Z][a-z]?)(?=[0-9+\-]|$)", s)      # "Fe1", "Fe3+"
            if m and m.group(1) in _ELEMENTS:
                return m.group(1)
            if strict:
                return "?"                         # definitely not an element
        return ""

    def _same_element(a, b):
        """True when two names denote the same element once symbols are resolved.

        '?' never matches a genuine symbol — otherwise "FeN_1" would be salvaged
        into "Fe" and the mismatch this check exists to catch would be accepted.
        """
        ea, eb = _element_symbol(a, strict=True), _element_symbol(b, strict=True)
        if ea == "?" or eb == "?":
            return ea == eb
        if ea and eb:
            return ea == eb
        return str(a or "").strip() == str(b or "").strip()

    def _mass_of(el):
        """Atomic mass, from GSAS-II's own table when available, else pymatgen."""
        try:
            from GSASII import GSASIIElem
            return float(GSASIIElem.AtmData([el])[el][1])
        except Exception:
            pass
        try:
            from pymatgen.core import Element
            return float(Element(el).atomic_mass)
        except Exception:
            return 1.0

    def _declared_types_from_cif(path):
        """{rounded (x,y,z): [(declared type_symbol, label), ...]} as written in the CIF.

        A LIST per coordinate, not a single value: in a defective/mixed site two
        rows can share the exact same position (e.g. "FeN_1" and "Mn1" both at
        0.5,0,0) and a single-value map would silently drop one of them.
        """
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            return {}
        lines = text.splitlines()
        out, i = {}, 0
        while i < len(lines):
            if lines[i].strip().lower() != "loop_":
                i += 1
                continue
            tags, j = [], i + 1
            while j < len(lines) and lines[j].strip().startswith("_"):
                tags.append(lines[j].strip().lower())
                j += 1
            if not any(t.startswith("_atom_site_") for t in tags):
                i = j
                continue
            idx = {t: k for k, t in enumerate(tags)}
            xi, yi, zi = (idx.get("_atom_site_fract_x"), idx.get("_atom_site_fract_y"),
                          idx.get("_atom_site_fract_z"))
            ti = idx.get("_atom_site_type_symbol")
            li = idx.get("_atom_site_label")
            while j < len(lines):
                s = lines[j].strip()
                if not s or s.startswith("#") or s.startswith("_") or s.lower() == "loop_":
                    break
                j += 1
                p = s.replace("'", " ").replace('"', " ").split()
                if len(p) < len(tags) or xi is None or ti is None:
                    continue
                try:
                    key = (round(float(p[xi]), 3), round(float(p[yi]), 3), round(float(p[zi]), 3))
                except (ValueError, TypeError):
                    continue
                out.setdefault(key, []).append((p[ti], p[li] if li is not None else ""))
            i = j
        return out

    def check_atom_types_vs_cif(ph, cif):
        """Compare each atom's GSAS-II TYPE against what the CIF declares.

        When a CIF's `_atom_site_type_symbol` is not an element, GSAS-II does not
        fail - it silently SUBSTITUTES an element (Xe, or H for a lowercase-looking
        symbol), so the structure scatters wrongly while everything downstream
        looks healthy, and refinement later dies with a bare `KeyError: 'Fen_'`.
        Matching by site coordinates keeps this independent of atom order.
        """
        checks = []
        if not (cif and os.path.isfile(cif)):
            return checks
        declared = _declared_types_from_cif(cif)
        if not declared:
            return checks
        gen = ph.data.get("General") or {}
        ptr = gen.get("AtomPtrs") or [3, 1, 7, 9]
        cx, ct = ptr[0], ptr[1]
        for at in (ph.data.get("Atoms") or []):
            try:
                key = (round(float(at[cx]), 3), round(float(at[cx + 1]), 3),
                       round(float(at[cx + 2]), 3))
            except (ValueError, TypeError):
                continue
            dec = declared.get(key) or []
            if not dec:
                continue
            got = str(at[ct] or "")
            if any(_same_element(got, sym) for sym, _lbl in dec):
                continue                    # GSAS-II kept what the CIF declared
            bad = [c for c in dec if _element_symbol(c[0], strict=True) == "?"]
            if not bad:
                continue                    # substitution, but the CIF type was sane
            sym, label = bad[0]
            checks.append(
                "site %r at %s: the CIF declares _atom_site_type_symbol %r, which is not an "
                "element - GSAS-II silently replaced the atom type with %r, so this site "
                "scatters wrongly and a later refinement fails with a bare KeyError. Write a "
                "real element in _atom_site_type_symbol, keeping the long name only in "
                "_atom_site_label." % (label or "?", key, sym, got))
        return checks

    def ensure_element_tables(gpx):
        """Fill PER-PHASE General tables for every element the phase carries.

        GSAS-II resolves neutron scattering lengths from `phase['General']
        ['Isotope']` (GSASIIElem.GetBLtable: `'Nat' in isotope[El]`) and uses
        General['AtomMass'] / General['NoAtoms'] for composition; a phase that
        carries an element missing from those tables aborts refinement with
        KeyError('<El>') — observed as `KeyError: 'Mo'` from
        GSASIIstrIO.GetPhaseData -> GetBLtable. Phases created through GSAS-II's
        own add_phase get these filled; phases injected by hand-edited gpx or
        external scripts do not.

        NOTE: `AtomMass` is a LIST parallel to `AtomTypes` — `Isotope` and
        `NoAtoms` are dicts. Treating AtomMass as a dict raised "list indices
        must be integers or slices, not str", which aborted this whole helper
        silently (the tables were never filled).

        2026-09-18 REWRITE (the "'Fen_'" crash): the key must be the atom TYPE,
        never a string derived from the atom LABEL. GSAS-II reads the type from
        the CIF `_atom_site_type_symbol` column, while `GetBLtable` iterates
        General['AtomTypes'] and builds `AtmBlens[getElSym(El) + '_']`. Deriving
        the key from the label ("FeN_1" -> "Fen") therefore wrote a useless table
        entry AND, because a single failure aborted the whole loop, left the real
        tables unfilled — refinement then died with a bare `KeyError: 'Fen_'`
        (GSASIIElem.py:146) instead of an actionable message. Each atom is now
        handled independently, so one bad site cannot wipe out the tables for the
        whole project, and anything unfillable is returned as a warning.

        Returns a list of warning strings (empty when everything was fine).
        """
        checks = []
        try:
            for ph in gpx.phases():
                gen = ph.data.setdefault("General", {})
                iso = gen.setdefault("Isotope", {})
                ams = gen.setdefault("AtomMass", [])
                nat = gen.setdefault("NoAtoms", {})
                atypes = gen.setdefault("AtomTypes", [])
                for atom in ph.atoms():
                    try:
                        typ = str(getattr(atom, "type", "") or "").strip()
                        lbl = str(getattr(atom, "label", "") or "")
                        key = typ or lbl
                        if not key:
                            continue
                        el = _element_symbol(typ, lbl)
                        if el not in _ELEMENTS:
                            checks.append(
                                "%s: atom type %r (site %r) is not an element symbol - GSAS-II "
                                "replaces such a type with Xe/H, which scatters wrongly, and the "
                                "refinement then dies with a bare KeyError; the usual cause is a "
                                "CIF whose _atom_site_type_symbol column holds the site LABEL "
                                "(e.g. 'FeN_1'). Regenerate the CIF with a real element in "
                                "_atom_site_type_symbol." % (ph.name, typ or lbl, lbl or "?"))
                            continue
                        if key not in iso:
                            iso[key] = "Nat"          # 'Nat' => natural abundance
                            log(f"{ph.name}: added Isotope[{key}]='Nat'")
                        if key not in nat:
                            nat[key] = 0.0
                        if key not in atypes:
                            atypes.append(key)
                            # keep the parallel AtomMass list aligned with AtomTypes
                            if isinstance(ams, list):
                                ams.append(_mass_of(el))
                                log(f"{ph.name}: added AtomMass[{key}]={_mass_of(el)} (list, appended)")
                            else:
                                ams[key] = _mass_of(el)
                                log(f"{ph.name}: added AtomMass[{key}]={ams[key]}")
                    except Exception as e:      # one bad site must not stop the rest
                        checks.append("%s: could not fill element tables for one site: %s"
                                      % (ph.name, e))
        except Exception as e:
            checks.append("element-table preparation skipped: %s" % e)
        return checks

    try:
        from GSASII import GSASIIscriptable as G2sc

        data = plan.get("data") or {}
        cif = plan.get("cif")
        gpx_old = plan.get("gpx")
        # #2: a 0-byte .gpx is the residue of a run that died right after GSAS-II
        # created the file (GSAS creates it, then a later failure leaves it
        # empty). Continuing such a file only yields the opaque
        # "No Phases in GPX file"; refuse it up front with an actionable message.
        if gpx_old and os.path.exists(gpx_old) and os.path.getsize(gpx_old) == 0:
            return {"ok": False,
                    "error": f"gpx is an empty 0-byte file (failed-run placeholder): {gpx_old} "
                             f"— pass data+cif to build a new project instead of continuing it"}
        # ---- 0.9.0 wavelength (EXPLICIT-ONLY) --------------------------------
        # plan["wavelength"] = {"kalpha1":…, "kalpha2":…, "ratio":…}. NOTHING below
        # runs unless it is present: without it the project keeps the λ its
        # instrument file carries (byte-identical behaviour).
        # BOTH paths write the Lam / Lam1+Lam2 slots of the loaded histogram directly
        # (via _wl_write below). They used to differ - a NEW histogram was built with
        # a `wavelength=` argument - but GSAS-II's add_powder_histogram has NO such
        # argument (GSASIIscriptable.py:1044: datafile, iparams, phases, fmthint,
        # databank, instbank, multiple, URL - and no **kwargs); `wavelength=` belongs
        # to add_simulated_powder_histogram (:1197), which builds a CALCULATED
        # pattern. Passing it to the real-data loader raised TypeError, and because it
        # was passed unconditionally (None when no λ was requested) EVERY new-project
        # GSAS refinement failed, reported as the misleading "could not import powder
        # data". The FORM is decided by the slots the histogram actually carries: a
        # Lam1-style prm takes [k1, k2], a Lam-style one a scalar.
        wl = plan.get("wavelength") or None
        wl_form = "mono"
        if wl:
            try:
                _p = plan.get("instprm") or ""
                if os.path.isfile(_p):
                    for _l in open(_p, encoding="utf-8", errors="replace"):
                        if "ICONS" in _l:
                            _s = _l.split("ICONS", 1)[1]
                            if float((_s[10:20] or "0").strip() or 0):
                                wl_form = "doublet"
                            break
            except Exception:
                pass
            log("wavelength: explicit request kalpha1=%s kalpha2=%s (instrument file form: %s)"
                % (wl.get("kalpha1"), wl.get("kalpha2"), wl_form))

        def _wl_slots(hist):
            """(kind, one-line view) of a histogram's λ slots, or (None, None)."""
            try:
                ip = (hist.data.get("Instrument Parameters") or [{}])[0]
            except Exception:
                return None, None
            if "Lam1" in ip:
                return "doublet", "Lam1=%s Lam2=%s" % (
                    ip["Lam1"][1], (ip.get("Lam2") or [None, None])[1])
            if "Lam" in ip:
                return "mono", "Lam=%s" % ip["Lam"][1]
            return None, None

        def _wl_write(hist):
            """Write the requested λ into one histogram's slots; return a log line.

            Index 1 is the VALUE GSAS-II reads (index 0 is only the reference), the
            same slot apply_handoff uses - writing [0] alone is inert. Called for a
            freshly created histogram as well as for a reopened one: the caller
            saves the project afterwards (gpx.save), which is what persists this.
            """
            kind, before = _wl_slots(hist)
            _nm = getattr(hist, "name", "?")
            if kind is None:
                return "%s carries neither Lam nor Lam1 - wavelength NOT set" % _nm
            try:
                ip = (hist.data.get("Instrument Parameters") or [{}])[0]
                if kind == "doublet":
                    ip["Lam1"][0] = ip["Lam1"][1] = float(wl.get("kalpha1"))
                    ip["Lam2"][0] = ip["Lam2"][1] = float(wl.get("kalpha2") or 0)
                else:
                    ip["Lam"][0] = ip["Lam"][1] = float(wl.get("kalpha1"))
                    if wl.get("kalpha2"):
                        return ("%s is a monochromatic (Lam) histogram: only kalpha1=%s "
                                "was written and kalpha2=%s was DROPPED - a doublet needs "
                                "a Lam1/Lam2-style instrument file"
                                % (_nm, wl.get("kalpha1"), wl.get("kalpha2")))
            except Exception as exc:
                return "%s could not be set (%s)" % (_nm, exc)
            _k2, after = _wl_slots(hist)
            return "%s %s -> %s" % (_nm, before, after)

        reopened = bool(gpx_old and os.path.exists(gpx_old))
        # The path the caller should be handed back: the file we actually used
        # or wrote. Returning out_dir/gsas_proj.gpx while continuing an existing
        # project handed back a non-existent "placeholder" path (reported bug).
        proj_used = proj

        if reopened:
            log(f"reopening project {gpx_old}")
            gpx = G2sc.G2Project(gpxfile=gpx_old)
            if wl:
                for _h in gpx.histograms():
                    log("wavelength: " + _wl_write(_h))
            # continue into a NEW file so the user's original gpx is preserved
            proj_used = os.path.join(out_dir, "gsas_proj_continued.gpx")
            try:
                gpx.filename = proj_used
            except Exception:
                pass
        else:
            # C1 fix: if a stale project exists, drop it so we rebuild cleanly
            # with phases linked to histograms.
            if os.path.exists(proj):
                try:
                    os.remove(proj)
                except Exception:
                    pass
            log(f"creating project {proj}")
            gpx = G2sc.G2Project(newgpx=proj)
            if data.get("file") and os.path.exists(data["file"]):
                log(f"adding powder histogram {data['file']}")
                # 0.8.8 (e2e 2026-09-28): the hint used to default to "GSAS powder".
                # GSAS-II's import_generic SKIPS every reader whose formatName does not
                # contain the hint (GSASIIscriptable.py: `if fmthint is not None and
                # fmthint not in reader.formatName: continue`), and only G2pwd_fxye is
                # named 'GSAS powder data' — so a bare two-column .dat, a Topas .xye, a
                # FullProf .dat or a Bruker RAW file could NOT be read at all, with the
                # misleading "No reader could read file" as the only report. Measured
                # control: the SAME Y2O3.dat reads as 7351 points through the Topas xye
                # reader with no hint and fails with the hint. The hint is now OFF by
                # default (GSAS-II tries every powder reader and keeps the first that
                # works) and can still be pinned explicitly via plan/args "fmthint".
                fmthint = data.get("fmthint") or None
                try:
                    hist_objs = gpx.add_powder_histogram(
                        data["file"],
                        plan.get("instprm") or "",
                        fmthint=fmthint,
                        # 0.9.0 wavelength is NOT passed here: this loader accepts no
                        # wavelength keyword (see the block above _wl_write), and passing
                        # one unconditionally broke EVERY new project. The λ is written
                        # into the freshly created histogram's slots below, exactly as
                        # for a reopened project.
                    )
                    if wl:
                        for _h in ([hist_objs] if not isinstance(hist_objs, list) else hist_objs):
                            log("wavelength: " + _wl_write(_h))
                except Exception as exc:      # noqa: BLE001 - re-raise with a usable hint
                    # The tail is CONDITIONAL (measured 2026-09-28): appending the
                    # format advice to every failure told a user whose pattern had read
                    # perfectly well that "GSAS-II reads ... convert first with
                    # xrd_data_io", when the real cause was a missing instrument
                    # parameter file. Only name the cause the error actually shows.
                    low = str(exc).lower()
                    if 'no reader' in low or 'could not read file' in low or 'could not be read' in low:
                        tail = (" GSAS-II reads GSAS .fxye/.gda, Topas .xye/.chi, FullProf"
                                " .dat, Bruker RAW/brml, Rigaku .txt/.ras, csv and powder"
                                " CIF; convert first with xrd_data_io (to twocol/fxye/dat)"
                                " if the file is a vendor or bare text file.")
                    elif 'instrument param' in low:
                        tail = (" Pass instprm=<INST_XRY.PRM>: GSAS-II reads the pattern but"
                                " needs instrument parameters to build the histogram.")
                    else:
                        tail = ""
                    raise RuntimeError(
                        f"GSAS-II could not import powder data '{data['file']}'"
                        f" (fmthint={fmthint!r}): {exc}.{tail}"
                    ) from exc
                if not isinstance(hist_objs, list):
                    hist_objs = [hist_objs]
                if cif and os.path.exists(cif):
                    log(f"adding phase {cif}")
                    gpx.add_phase(cif, fmthint="CIF", histograms=hist_objs)
                cif2 = plan.get("cif2")
                if cif2 and os.path.exists(cif2):
                    log(f"adding second phase {cif2}")
                    gpx.add_phase(cif2, fmthint="CIF", histograms=hist_objs)
                # C1 belt & braces: link every phase to every histogram
                for h in gpx.histograms():
                    for p in gpx.phases():
                        try:
                            if h.name not in (p.data["Histograms"] or {}):
                                log(f"linking {h.name} <-> {p.name}")
                                gpx.link_histogram_phase(h.name, p)
                        except Exception as e:
                            log(f"link skipped {h.name}<->{p.name}: {e}")

        # #5: fill per-phase General tables (Isotope/AtomMass/NoAtoms/AtomTypes)
        # for both fresh and reopened projects — a phase carrying an element
        # absent from these tables aborts refinement with KeyError('<El>').
        # The helper now also reports atom TYPES that are not elements (the
        # "'Fen_'" crash); those are surfaced in the result instead of dying
        # inside GSAS-II with a bare KeyError.
        table_checks = ensure_element_tables(gpx)
        # silent type substitution check (needs the CIF the phase came from)
        _cifs = [c for c in (plan.get("cif"), plan.get("cif2")) if c]
        for _i, _ph in enumerate(gpx.phases()):
            _cif = _cifs[_i] if _i < len(_cifs) else (_cifs[-1] if _cifs else None)
            table_checks.extend(check_atom_types_vs_cif(_ph, _cif))
        if table_checks:
            log("element-table warnings:\n  - " + "\n  - ".join(table_checks))

        # #2 guard: a reopened project with no phases / no histograms is exactly
        # the "gsas_proj.gpx looked like a placeholder" failure (GSAS-II then
        # dies with "No Phases in GPX file"). Report it clearly instead of
        # pretending the run succeeded, and never hand such a file back.
        if len(gpx.phases()) == 0:
            # NOTE: omit keys whose value would be null — the tool schema declares
            # them as string/array, and a null makes the host reject the whole
            # result with "returned invalid output".
            return {"ok": False,
                    "error": "project has no phases (the .gpx is empty/placeholder); "
                             "pass data+cif to build a new project instead of continuing it"}

        maxcyc = int(plan.get("max_cycles") or 8)
        nback = int(plan.get("n_back") or 3)
        inst_list = plan.get("inst") or []
        afl = plan.get("atoms_flags") or "XU"
        amap = plan.get("atoms_map") or {}
        size_only = bool(plan.get("size_only"))
        cell2 = bool(plan.get("cell2", True))
        all_phs = gpx.phases()

        # optional pre-ladder second-phase scale fix
        if len(all_phs) > 1 and plan.get("scale2_init") is not None:
            s2 = float(plan["scale2_init"])
            for ph in all_phs[1:]:
                for hn in (ph.data.get("Histograms") or {}):
                    ph.data["Histograms"][hn]["Scale"][0] = s2
                    ph.data["Histograms"][hn]["Scale"][1] = False
                log(f"second phase {ph.name}: Scale set to {s2} (not refined yet)")

        # ---- 0.8.10: manual-engine hand-off (before the ladder, before any step) --
        handoff_info = None
        _ho = plan.get("handoff")
        if _ho:
            if os.path.exists(_ho):
                handoff_info = apply_handoff(gpx, _ho, log)
            else:
                handoff_info = {"source": _ho, "transferred": [], "not_transferred": [],
                                "warnings": ["handoff file not found: %s" % _ho]}
                log("handoff file not found: %s" % _ho)

        refine = plan.get("refine") or []
        pspecs = plan.get("phases") or []           # 0.8.10 per-phase switches
        any_per_phase = bool(pspecs)

        def _spec(i, key, default=None):
            if i < len(pspecs) and isinstance(pspecs[i], dict) and key in pspecs[i]:
                return pspecs[i][key]
            return default

        steps = []                                  # [(label, gsas step dict)]
        if not any_per_phase:
            # ---- legacy path: unchanged from 0.8.9 (one global step per family)
            if "background" in refine:
                steps.append(("background", {"set": {"Background": {"no. coeffs": nback, "refine": True}}}))
            if "inst" in refine and inst_list:
                steps.append(("inst", {"set": {"Instrument Parameters": inst_list}}))
            if "cell" in refine:
                if cell2:
                    steps.append(("cell:all", {"set": {"Cell": True}}))
                else:
                    steps.append(("cell:" + all_phs[0].name, {"phases": [all_phs[0].name], "set": {"Cell": True}}))
            if "profile" in refine:
                if size_only:
                    steps.append(("profile:size", {"set": {"Size": {"type": "isotropic", "refine": True}}}))
                else:
                    steps.append(("profile:size+mustrain",
                                  {"set": {"Mustrain": {"type": "isotropic", "refine": True},
                                           "Size": {"type": "isotropic", "refine": True}}}))
            if "scale" in refine:
                for _ph in all_phs[1:]:
                    steps.append(("scale:" + _ph.name, {"phases": [_ph.name], "set": {"Scale": True}}))
            if "atoms" in refine:
                if amap:
                    for lbl, fl in amap.items():
                        for atom in gpx.phases()[0].atoms():
                            try:
                                if atom.label == lbl or (str(atom) and lbl in str(atom)):
                                    atom.refinement_flags = fl
                            except Exception:
                                pass
                    steps.append(("atoms:current-flags", {}))  # refine with current flags
                else:
                    steps.append(("atoms:" + all_phs[0].name,
                                  {"phases": [all_phs[0].name], "set": {"Atoms": {"all": afl}}}))
        else:
            # ---- 0.8.10 path: every family is emitted PER PHASE, so the caller can
            # freeze the main phase and release the second one (the GSAS-II tutorial
            # recipe: main cell/size/mustrain OFF, second phase fraction + single
            # isotropic microstrain ON). Anything the spec leaves out falls back to
            # the legacy default for that phase, reported in the step label.
            if "background" in refine:
                steps.append(("background", {"set": {"Background": {"no. coeffs": nback, "refine": True}}}))
            if "inst" in refine and inst_list:
                # NOTE: instrument parameters are HISTOGRAM-level in GSAS-II; they
                # cannot be per phase. Reported in the result so nobody looks for it.
                steps.append(("inst", {"set": {"Instrument Parameters": inst_list}}))
            for _pi, _ph in enumerate(all_phs):
                if "cell" in refine:
                    want = _spec(_pi, "cell", None)
                    if want is None:
                        want = True if cell2 else (_pi == 0)
                    if want:
                        steps.append(("cell:" + _ph.name, {"phases": [_ph.name], "set": {"Cell": True}}))
                if "scale" in refine:
                    want = _spec(_pi, "scale", None)
                    if want is None:
                        want = _pi > 0
                    if want:
                        steps.append(("scale:" + _ph.name, {"phases": [_ph.name], "set": {"Scale": True}}))
                if "profile" in refine:
                    size = _spec(_pi, "size", None)
                    must = _spec(_pi, "mustrain", None)
                    if size is None and must is None:
                        size, must = (True, False) if size_only else (True, True)
                    st = {}
                    if size:
                        st["Size"] = {"type": "isotropic", "refine": True}
                    if must:
                        st["Mustrain"] = {"type": must if isinstance(must, str) else "isotropic",
                                          "refine": True}
                    if st:
                        steps.append(("profile:" + _ph.name, {"phases": [_ph.name], "set": st}))
                if "atoms" in refine:
                    aflags = _spec(_pi, "atoms", None) or afl
                    if aflags:
                        steps.append(("atoms:" + _ph.name,
                                      {"phases": [_ph.name], "set": {"Atoms": {"all": aflags}}}))
        if not steps:
            steps.append(("background", {"set": {"Background": {"no. coeffs": nback, "refine": True}}}))
        try:
            gpx.data["Controls"]["data"]["max cyc"] = maxcyc
        except Exception:
            pass
        log(f"running {len(steps)} step(s): {refine}  maxcyc={maxcyc}")
        # P1 (0.8.5): the checkpoint used to sit BEFORE the single batched call
        # `gpx.do_refinements(steps)`, so a stop request could not be honoured between
        # refinement steps at all — the comment claimed "a stop request lands BETWEEN
        # refinement steps" while the code only ever checked once, before all of them.
        # Calling one step at a time is the same sequence GSAS-II itself performs for a
        # list, and it puts a real checkpoint between steps (and lets the run end at a
        # safe point instead of only via a tree kill).
        # ---- 0.8.10: entry policy + accept/rollback ladder ------------------------
        # "hold" (default when CONTINUING an existing .gpx or when a manual hand-off
        # was supplied) verifies the incoming state with the first step, then releases
        # the requested families ONE at a time, keeping a step only when it improves
        # wR. "direct" is the legacy single batch with no rollback.
        # Rationale (measured 2026-10-02): a two-phase project that entered GSAS-II
        # from an already-good state was destroyed by the legacy batch
        # (wR 30.42 -> 77.19, cubic cell collapsed to 7.29/6.70/13.52).
        entry = plan.get("entry")
        if entry not in ("hold", "direct"):
            entry = "hold" if (reopened or plan.get("handoff")) else "direct"
        _rb = plan.get("rollback")
        rollback = (entry == "hold") if _rb is None else bool(_rb)
        tol = float(plan.get("rollback_tol") or 0.0)

        def _wr_now():
            vals = []
            for _h in gpx.histograms():
                try:
                    v = float(_h.get_wR())
                    if v == v:
                        vals.append(v)
                except Exception:
                    pass
            return round(sum(vals) / len(vals), 6) if vals else None

        def _cells_now():
            """Cell parameters of every phase as {name: [a,b,c,alpha,beta,gamma]}."""
            cur = {}
            for _p in gpx.phases():
                try:
                    celld = (_p.data.get("General") or {}).get("Cell") or []
                    cur[str(_p.name)] = [float(v) for v in celld[1:7]]
                except Exception:
                    pass
            return cur

        def _cell_shift(before, after, pct=2.0):
            """Per-phase parameters that moved more than `pct` % during a step.

            Reported only (acceptance stays a wR decision); it is what makes a
            step that quietly drove a cell somewhere absurd visible.
            """
            moved = {}
            for _n, _b in before.items():
                _a = after.get(_n)
                if not _a or len(_a) != len(_b):
                    continue
                _d = []
                for _x, _y in zip(_b, _a):
                    _d.append(round((_y - _x) / _x * 100.0, 2)
                              if abs(_x) > 1e-6 else 0.0)
                if any(abs(v) > pct for v in _d):
                    moved[_n] = _d
            return moved

        class _Tap(object):
            """Tee stdout/stderr into a buffer while letting everything through.

            GSAS-II reports a failed refinement on stdout ("Invalid cell metric
            tensor for phase #1 (CuO)", "****ERROR - Refinement failed") and then
            RETURNS NORMALLY, leaving the phase parameters partially updated.
            Without this tap such a step is indistinguishable from a good one, and
            because acceptance is decided on wR alone a broken state could even be
            crowned "best".
            """
            def __init__(self):
                self.buf = []

            def __enter__(self):
                self._out, self._err = sys.stdout, sys.stderr
                sys.stdout = self
                sys.stderr = self
                return self

            def __exit__(self, *exc):
                sys.stdout, sys.stderr = self._out, self._err
                return False

            def write(self, s):
                self.buf.append(s)
                try:
                    return self._out.write(s)
                except Exception:
                    return len(s)

            def flush(self):
                try:
                    return self._out.flush()
                except Exception:
                    pass

            def first_error(self, markers):
                for _line in "".join(self.buf).splitlines():
                    _low = _line.lower()
                    if any(_m in _low for _m in markers):
                        return _line.strip()[:200]
                return None

        # GSAS-II's refine() is FILE-BASED: G2Project.refine() calls
        # GSASIIstrMain.Refine(self.filename, ...) and index_ids() auto-saves, so
        # the refinement reads and writes self.filename. `gpx.save(other_path)`
        # REBINDS self.filename (G2Project.save docstring: "Updates self.filename
        # if a new filename provided") - so a snapshot written with save() becomes
        # the WORKING file and the next refinement overwrites it with the refined
        # state. Measured 2026-10-02 on a two-phase project: every step reported
        # rolled_back=True while restoring the very state it was supposed to undo
        # (each wR_before equalled the previous step's wR_after) and the delivered
        # .gpx carried the rejected CuO cell 4.4989/3.0618/4.7171/108.26 while its
        # input cell was 4.6795/3.4206/5.1253/99.83. Snapshots are therefore taken
        # by COPYING the working file, and self.filename stays pinned to proj_used.
        _ERR_MARKERS = ("invalid cell metric tensor", "refinement failed",
                        "matrix singular", "singular matrix", "****error")
        gpx.save(proj_used)          # materialise the working file + pin filename
        ladder = []
        _snap = os.path.join(out_dir, ".xrd-ladder-snap.gpx")
        _best_snap = os.path.join(out_dir, ".xrd-ladder-best.gpx")
        _best = None
        _state_is_best = True
        _ladder_warn = []

        def _keep_best(tag):
            try:
                gpx.save(proj_used)
                shutil.copyfile(proj_used, _best_snap)
            except Exception as _e:
                log("ladder best-state snapshot failed (%s): %s" % (tag, _e))

        for _si, (_lbl, _st) in enumerate(steps, 1):
            _before = _wr_now()
            _cells_before = _cells_now()
            if guard is not None:
                guard.checkpoint('gsas refine step %d/%d' % (_si, len(steps)))
            _snapped = False
            if rollback:
                try:
                    gpx.save(proj_used)                  # refresh the working file
                    shutil.copyfile(proj_used, _snap)
                    _snapped = True
                except Exception as _e:
                    log("ladder snapshot failed: %s" % _e)
            _tap = _Tap()
            _failed = None
            try:
                with _tap:
                    gpx.do_refinements([_st])
            except Exception as _e:
                _failed = "do_refinements raised: %s" % _e
            if _failed is None:
                _failed = _tap.first_error(_ERR_MARKERS)
            _after = _wr_now()
            rec = {"step": _si, "label": _lbl}
            if _before is not None:
                rec["wR_before"] = _before
            if _after is not None:
                rec["wR_after"] = _after
            _moved = _cell_shift(_cells_before, _cells_now())
            if _moved:
                rec["cell_change_pct"] = _moved
            if _failed:
                rec["gsas_error"] = _failed
                log("step %d/%d %s: GSAS-II reported a failure: %s"
                    % (_si, len(steps), _lbl, _failed))
            if _best is None and not _failed:
                # the first step only verifies the incoming state (entry policy);
                # a step that FAILED is never allowed to define "best"
                _best = _after
                rec["accepted"] = True
                rec["note"] = ("entry verification step (nothing but this family moved)"
                               if entry == "hold" else "first step")
                _state_is_best = True
                _keep_best("step %d" % _si)
            elif _failed or _after is None or _after > _best + tol:
                rec["accepted"] = False
                rec["rolled_back"] = False
                _why = ("GSAS-II refinement failed" if _failed
                        else "wR %s worse than best %s" % (_after, _best))
                if rollback and _snapped:
                    try:
                        gpx = G2sc.G2Project(gpxfile=_snap)
                        gpx.filename = proj_used
                        gpx.save(proj_used)   # rolled-back state = working file
                        rec["rolled_back"] = True
                        rec["note"] = "%s - state restored" % _why
                        _state_is_best = True
                    except Exception as _e:
                        rec["note"] = "rollback FAILED (worse state kept): %s" % _e
                        _state_is_best = False
                        _ladder_warn.append("step %d (%s): rollback failed - %s"
                                            % (_si, _lbl, _e))
                else:
                    rec["note"] = "%s - kept (rollback off)" % _why
                    _state_is_best = False
                    if _failed:
                        # a failed step that was NOT undone leaves its half-updated
                        # parameters in the delivered state: that is a real problem
                        # with the result, so it reaches the verdict (ok:false).
                        _ladder_warn.append("step %d (%s) FAILED inside GSAS-II and was "
                                            "kept (no rollback): %s" % (_si, _lbl, _failed))
            else:
                rec["accepted"] = True
                if _after < _best:
                    _best = _after
                _state_is_best = True
                _keep_best("step %d" % _si)
            ladder.append(rec)
            log("step %d/%d %s: wR %s -> %s accepted=%s rolled_back=%s"
                % (_si, len(steps), _lbl, _before, _after,
                   rec.get("accepted"), rec.get("rolled_back")))
        log("ladder: %d step(s), best wR=%s, entry=%s" % (len(ladder), _best, entry))

        # ---- deliver the BEST accepted state, not merely the last one -----------
        # Contract of the ladder (same principle as the fp2k driver, which delivers
        # its cached start state instead of a worse final one): the .gpx handed back
        # is the best state the ladder reached. It only has work to do when the last
        # step left a worse state behind - rollback OFF, or a rollback that failed;
        # with a working rollback the in-memory state already IS the best one.
        if rollback and not _state_is_best and _best is not None:
            try:
                gpx = G2sc.G2Project(gpxfile=_best_snap)
                gpx.filename = proj_used
                gpx.save(proj_used)
                _state_is_best = True
                log("ladder: delivered state RESTORED to the best accepted step (wR %s)"
                    % _best)
            except Exception as _e:
                _ladder_warn.append("could not restore the best accepted state: %s" % _e)
                log("ladder: best-state restore FAILED: %s" % _e)

        # ---- Uiso LOWER-BOUND clamp (requested 2026-09-19) ---------------------
        # GSAS has no built-in lower bound for Uiso, so a refinement can walk a
        # thermal parameter to zero or negative (unphysical: negative U means the
        # atom scatters "too sharply" and is a classic sign of an over-fitted or
        # badly scaled model). The recipe is the proven project one
        # (PB/XRD/mono2phase/gsas_uclamp.py): refine -> push every Uiso below the
        # floor back up to the floor -> refine again, until a round finds nothing.
        # Unlike the post-hoc `uiso_clamp` ACTION in gsas_ops (which is a separate,
        # explicitly requested step), this runs INSIDE the normal ladder so the
        # reported Uiso is already floored.
        #   "uiso_floor": {"enabled": true, "min": 0.005, "max_rounds": 6}
        #   {"enabled": false}  -> explicitly allowed: some models legitimately
        #                          refine to a very small / negative U (e.g. an
        #                          atom that is really a partially occupied split
        #                          site), and the value is then reported as-is.
        uf = plan.get("uiso_floor") or {}
        uf_enabled = bool(uf.get("enabled", False))
        uf_min = float(uf.get("min", 0.005))
        uf_rounds = int(uf.get("max_rounds", 6))
        uiso_floor_log = []
        if uf_enabled:
            log(f"Uiso floor active: min={uf_min} U (max {uf_rounds} round(s))")
            for rnd in range(max(0, uf_rounds)):
                clamped = []
                for ph in gpx.phases():
                    ptr = (ph.data.get("General") or {}).get("AtomPtrs") or [3, 1, 7, 9]
                    ct, cia = ptr[1], ptr[3]
                    for at in (ph.data.get("Atoms") or []):
                        try:
                            u = float(at[cia + 1])
                        except Exception:
                            continue
                        if u < uf_min:
                            at[cia + 1] = uf_min
                            # a clamped parameter is no longer measured: stop refining
                            # it by dropping the U flag, otherwise the next round just
                            # walks it below the floor again
                            at[ct + 1] = str(at[ct + 1]).replace("U", "")
                            clamped.append({"atom": str(at[ct - 1]), "was": u})
                uiso_floor_log.append({"round": rnd, "clamped": clamped})
                if not clamped:
                    log(f"Uiso floor: round {rnd} clean")
                    break
                log(f"Uiso floor: round {rnd} clamped {len(clamped)} atom(s) up to {uf_min}")
                try:
                    if guard is not None:
                        guard.checkpoint('uiso floor round %d' % rnd)
                    gpx.do_refinements([{}])
                except Exception as e:
                    log(f"Uiso floor: re-refine failed: {e}")
                    break
            n_clamped = sum(len(r["clamped"]) for r in uiso_floor_log)
            log(f"Uiso floor: {n_clamped} clamp event(s) over {len(uiso_floor_log)} round(s)")

        # optional post-ladder: open second-phase scale / cell refinement.
        # Applied AFTER the main ladder — building these flags during the ladder
        # would make them take effect early (2nd phase of the same family + main
        # cell together degenerates the LS).
        open2 = plan.get("open2") or []
        if open2 and len(all_phs) > 1:
            changed = False
            if "scale2" in open2:
                hname = gpx.histograms()[0].name
                for ph in all_phs[1:]:
                    try:
                        ph.data["Histograms"][hname]["Scale"][1] = True
                    except Exception as e:
                        log(f"scale2 open failed for {ph.name}: {e}")
                log("post-ladder: opening second-phase Scale refinement")
                changed = True
            if "cell2" in open2:
                try:
                    all_phs[1].data["General"]["Cell"][0] = True
                    log("post-ladder: opening second-phase Cell refinement")
                    changed = True
                except Exception as e:
                    log(f"cell2 open failed: {e}")
            if changed:
                if guard is not None:
                    guard.checkpoint('post-ladder refine')
                gpx.do_refinements([{}])

        # Save to proj_used explicitly. On a reopen this is a NEW file
        # (gsas_proj_continued.gpx) so the caller's original .gpx is never
        # overwritten; on a fresh build it is the newly created project.
        gpx.save(proj_used)

        # collect metrics — everything must be a JSON-native type (numpy scalars
        # / arrays would blow up json.dump and the tool result schema).
        def _num(v):
            try:
                f = float(v)
                return f if f == f and abs(f) != float("inf") else None  # drop NaN/inf
            except Exception:
                return None

        hists = []
        for hist in gpx.histograms():
            # NOTE the name: this used to be `entry`, which SHADOWED the entry
            # policy set above, so `out["entry"]` came back as the last histogram
            # record instead of "hold"/"direct" (measured 2026-10-02).
            hrec = {"name": str(getattr(hist, "name", ""))}
            try:
                wR = _num(hist.get_wR())
            except Exception:
                wR = None
            if wR is not None:
                hrec["wR"] = wR
            try:
                r = _num(hist.residuals)
            except Exception:
                r = None
            if r is not None:
                hrec["residuals"] = r
            hists.append(hrec)

        lst_path = proj_used.replace(".gpx", ".lst")
        lst_tail = ""
        if os.path.exists(lst_path):
            try:
                with open(lst_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                lst_tail = "".join(lines[-160:])
            except Exception:
                lst_tail = ""

        # cells of each phase — G2Phase.get_cell() returns a DICT keyed by
        # length_a/length_b/length_c/angle_alpha/angle_beta/angle_gamma/volume
        # (iterating it yields the key strings, which is why a naive float()
        # loop reported "could not convert string to float: 'length_a'").
        CELL_KEYS = ("length_a", "length_b", "length_c",
                     "angle_alpha", "angle_beta", "angle_gamma", "volume")
        cells = []
        for p in gpx.phases():
            prec = {"name": str(p.name)}
            try:
                celld = p.get_cell()
                vals = [float(celld[k]) for k in CELL_KEYS if k in celld]
                if any(v != v or abs(v) == float("inf") for v in vals):
                    raise ValueError("non-finite cell parameter")
                prec["cell"] = vals
                prec["cell_keys"] = [k for k in CELL_KEYS if k in celld]
            except Exception as e:
                prec["cell_error"] = str(e)
            cells.append(prec)

        # C3 fix: detect divergence / stuck even though LS "finished".
        # wR at ~100% or .lst degenerate markers mean the fit is meaningless;
        # report ok:false so callers do not read a garbage "converged" state.
        warn_flags = []
        # element-table findings come first: they explain a later bare KeyError
        warn_flags.extend(table_checks)
        # 0.8.10 ladder findings. TWO TIERS by design: only a problem that leaves the
        # DELIVERED state compromised appears here (a failed step that was kept, a
        # rollback that failed, a best-state restore that failed), because `warning`
        # drives the ok verdict. A step that failed and WAS rolled back leaves a
        # perfectly good project behind — it is reported in `ladder`
        # (accepted:false / rolled_back:true / gsas_error) instead of failing the run.
        warn_flags.extend(_ladder_warn)
        for h in hists:
            wr = h.get("wR")
            if wr is not None and wr >= 99.0:
                warn_flags.append(f"{h.get('name')}: wR={wr:.1f}% (~100% = not converged)")
        low = lst_tail.lower()
        for marker in ("stuck", "divergence", "dropped", "matrix singular", "shift/esd"):
            if marker in low:
                warn_flags.append(f"lst marker: {marker}")
        # NOTE: the verdict is NOT taken here — the Uiso checks below add to
        # warn_flags too, and freezing the verdict before them is what let a
        # negative-Uiso run report ok:true. See "final verdict" further down.

        # ---- Uiso sanity of the FINAL state -----------------------------------
        # Measured, never assumed: report every Uiso that is still below the floor
        # (or negative) after everything ran, so "the floor is on" is never mistaken
        # for "no Uiso is below the floor".
        uiso_final = []
        for ph in gpx.phases():
            ptr = (ph.data.get("General") or {}).get("AtomPtrs") or [3, 1, 7, 9]
            ct, cia = ptr[1], ptr[3]
            for at in (ph.data.get("Atoms") or []):
                try:
                    u = float(at[cia + 1])
                except Exception:
                    continue
                uiso_final.append({"atom": str(at[ct - 1]), "Uiso": u,
                                   "flags": str(at[ct + 1])})
        neg = [r for r in uiso_final if r["Uiso"] < 0]
        below = [r for r in uiso_final if uf_enabled and r["Uiso"] < uf_min - 1e-12]
        if neg:
            warn_flags.append(
                "%d atom(s) have a NEGATIVE Uiso (unphysical): %s%s"
                % (len(neg), ", ".join("%s=%.4g" % (r["atom"], r["Uiso"]) for r in neg[:6]),
                   "" if uf_enabled else " - uiso_floor was OFF, so nothing clamped them"))
        if below:
            warn_flags.append(
                "%d atom(s) are still below the Uiso floor %.4g after clamping: %s"
                % (len(below), uf_min,
                   ", ".join("%s=%.4g" % (r["atom"], r["Uiso"]) for r in below[:6])))
            log("Uiso floor NOT fully satisfied: " + warn_flags[-1])

        # ---- final verdict, computed AFTER every check -------------------------
        # The negative-Uiso / below-floor findings above are part of the verdict:
        # the previous version froze `diverged` before those checks ran, so a run
        # whose ONLY problem was a negative Uiso still returned ok:true (and no
        # warning field), which is exactly the "reported success on a bad result"
        # failure the audit flagged (2026-09-21).
        diverged = bool(warn_flags)
        result_ok = not diverged
        if warn_flags:
            log("C3: refinement looks diverged/stuck:\n" + "\n".join(warn_flags))

        # #1 fix: build the result by ADDING only non-null fields. The tool
        # schema declares lst/summary/log_tail/lst_tail as string and warning as
        # array; putting Python None there made the host reject the whole result
        # ("value.lst must be a string", "value.warning must be an array").
        # 0.8.10: in rollback ("hold") mode a run that accepted NOTHING is not a
        # failure — it is the correct outcome for a project that already sits at a
        # local minimum — but it must not read as "the requested families were
        # refined": the delivered .gpx is exactly the state the run entered with.
        # Said in the summary so a caller does not have to diff the ladder to learn it.
        summary = ("GSAS-II refinement COMPLETE" if result_ok
                   else "GSAS-II refinement NOT DELIVERABLE — see warning/lst_tail: "
                        + "; ".join(warn_flags[:3]))
        if result_ok and rollback and ladder and not any(r.get("accepted") for r in ladder):
            summary += (" — 0 of %d step(s) accepted: the delivered project is the state "
                        "it entered with" % len(ladder))
        out = {
            "ok": result_ok,
            "histograms": hists,
            "phases": cells,
            "summary": summary,
        }
        if warn_flags:
            out["warning"] = warn_flags
        if proj_used and os.path.exists(proj_used):
            out["gpx"] = proj_used
        # Uiso floor provenance: was it on, what did it clamp, what is the final state
        out["uiso_floor"] = {
            "enabled": uf_enabled,
            "min": uf_min if uf_enabled else None,
            "rounds": uiso_floor_log,
            "n_clamped": sum(len(r.get("clamped") or []) for r in uiso_floor_log),
            "final": uiso_final,
            "n_negative_final": len(neg),
            "n_below_floor_final": len(below),
            "note": ("The floor is enforced AFTER each refinement round by pushing the "
                     "value up to `min` and dropping that atom's U flag (a clamped "
                     "parameter is no longer measured). It is a repair, not a hard "
                     "constraint inside the least-squares, so the final state is "
                     "re-checked and reported."),
        }
        if lst_path and os.path.exists(lst_path):
            out["lst"] = lst_path
        if lst_tail:
            out["lst_tail"] = lst_tail
        # 0.8.10: entry policy + the per-step accept/rollback trace, so a caller can
        # see WHICH family moved wR and which one was undone (never a silent state).
        out["entry"] = entry
        out["ladder"] = ladder
        if handoff_info is not None:
            out["handoff"] = handoff_info
        out["per_phase"] = bool(any_per_phase)
        return out
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        err = {
            "ok": False,
            "error": f"GSAS-II refinement failed: {e}",
            "log_tail": tb[-2000:],
        }
        try:
            if proj_used and os.path.exists(proj_used):
                err["gpx"] = proj_used
        except Exception:
            pass
        return err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--out")
    args = ap.parse_args()
    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)
    # P1/P2 (0.8.3): GSAS-II refining is one long in-process call, so ticks of
    # this wrapper alone would only prove that python is alive — GSAS can stall
    # inside its own loop. The heartbeat therefore watches what GSAS ITSELF
    # writes (.gpx / .lst): only a real write counts as progress, and the JS
    # layer kills the run when that stops. The guard also puts us in a
    # kill-on-close job, so nothing survives this process.
    out = args.out or os.path.join(os.getcwd(), "gsas_result.json")
    guard = start_guard(
        plan, backend='gsas', out_path=out,
        progress_paths=[os.path.join(plan.get('out_dir') or '.', 'gsas_proj.gpx'),
                        os.path.join(plan.get('out_dir') or '.', 'gsas_proj.lst'),
                        os.path.join(plan.get('out_dir') or '.', 'gsas_proj_continued.gpx'),
                        os.path.join(plan.get('out_dir') or '.', 'gsas_proj_continued.lst')])
    result = run(plan, guard=guard)
    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own
    # (default=float is a safety net: any numpy scalar that slipped through
    # becomes a plain float instead of aborting the whole run at dump time).
    write_result(out, result, plan)
    print("RESULT:", json.dumps({k: v for k, v in result.items() if k != "lst_tail"},
                                ensure_ascii=False, default=str)[:900])


if __name__ == "__main__":
    run_guarded_main(main)
