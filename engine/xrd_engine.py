#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_engine — generic powder-XRD Rietveld-style fitting engine (executor).

Pure executor: reads a *plan JSON* describing data, structure, parameters to
vary/fix, optional extra broad peaks and optional second phases; runs lmfit
leastsq; writes a result JSON (params + metrics) and, when requested, a full
component model JSON (yobs/ycalc/bg/main/extra) for plotting.

The "brain" (which stage, which params, red lines) lives in skills / the
xrd_refine_manual tool wrapper - NOT here. This file never hard-codes a
sample or a stage ladder.

=============== AI-CUSTOMIZATION GUIDE (model section) ===============
This file is a MUTABLE TEMPLATE. When a material needs model features the
plan JSON cannot express (custom peak shape, special background, rigid-body
constraints, preferred-orientation correction, anisotropic broadening, ...),
DO NOT edit this bundled copy. Instead:
  1. call xrd_refine_manual with init_engine:true  -> copies this file to
     <workdir>/xrd_engine_custom.py,
  2. read & edit that copy - the editable seams are marked with
     "== EDITABLE ==" below,
  3. call xrd_refine_manual with engine=<that copy> to run your version.
Keep edits inside the model section; the I/O protocol (plan JSON in, result
JSON + components JSON out) must stay unchanged so the tool wrapper still
understands the output.
========================================================================

Usage:
    python xrd_engine.py plan.json [--out result.json] [--components]

Plan JSON schema:
{
  "data":   {"file": "abs/path.txt", "th_min": 10.0, "th_max": 70.0},
  "cif":    "abs/path.cif",                  # main phase (preferred)
  "atoms":  null,                            # or nominal: [{el,xyz,occ}...]
  "lattice": {"a": 10.17, "cubic": true},    # required when cif absent
  "spacegroup": null,                        # reserved (nominal SG use)
  "scale":  0.0,                             # 0 => auto-init
  "params": {                                # lmfit-style initial/bounds
      "zero": {"value": -0.13, "min": -0.35, "max": 0.1},
      "a":    {"value": 10.105, "min": 9.9, "max": 10.4},
      "U":    {"value": 0.55, "min": 0.0, "max": 2.0},
      ...
  },
  "vary": ["scale","zero","a","U","V","W","eta","bg0",...],
  "fix":   [...],                             # optional explicit fixes
  "occ":   {"Fe1": {"value": 1.0, "min":0.5, "max":1.0, "vary":true}},  # site-label -> occupancy multiplier
  "background": {"n_cheb": 6, "center": 40.0, "halfwidth": 30.0},
  "bg_scan": {"enabled": false,   # background iteration is opt-in (see below)
              "orders": [6,8,10,12,14,16,18,20], "stop_tol": 0.05},
  "extras": [{"center":25.4,"H":0.6,"amp":100,"eta":0.9,"vary_c":false}],
  "second": [{"cif":"...", "k":1.0, "scale":1e-6, "Y":0.5, "vary_scale":true, "vary_k":true, "vary_Y":true}],
  "lambda": {"kalpha1": 1.54056, "kalpha2": 1.54439, "ratio2": 0.497},
  "dw":     {"Fe":0.006, "Mn":0.006, "C":0.006, "N":0.006, "K":0.013},
  "seed_restarts": [{"occ": {...}, ...}]      # optional multi-start
}
"""
import os, sys, json, math, time, copy, traceback

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard
import numpy as np

def log(*a):
    print(*a, flush=True)

def load_data(path, th_min=None, th_max=None):
    x, y = [], []
    # ENCODING (2026-10-03): plain open() uses the PLATFORM default, which is GBK on
    # a Chinese Windows - and our own two-column TXT files carry a UTF-8 '#' header
    # with Chinese text, so this reader died with
    # "UnicodeDecodeError: 'gbk' codec can't decode byte 0x80" before reading a
    # single data row (measured on the test set's Y2O3 file). Read UTF-8 with
    # errors='replace': the header is comment text, the numbers are ASCII.
    with open(path, encoding='utf-8', errors='replace') as f:
        for ln in f:
            p = ln.split()
            if len(p) >= 2:
                try:
                    xv, yv = float(p[0]), float(p[1])
                except ValueError:
                    continue
                if th_min is not None and xv < th_min: continue
                if th_max is not None and xv > th_max: continue
                x.append(xv); y.append(yv)
    x = np.array(x); y = np.array(y)
    if len(x) < 10:
        raise SystemExit(f"data too short: {len(x)} pts in {path}")
    return x, y

def load_structure(cif_path, atoms, lattice, dw):
    """Return (struct, site_labels) via pymatgen; atoms nominal fallback."""
    from pymatgen.core import Structure, Lattice
    if cif_path and os.path.exists(cif_path):
        try:
            st = Structure.from_file(cif_path)
        except Exception as e:
            raise SystemExit(f"CIF parse failed {cif_path}: {e}")
        # Prefer the CIF site label (N1, Fe1, ...) and fall back to the element
        # symbol. `occ` and `coord_vary` are written with site labels, and the
        # old element-only list silently failed to match them (occupancy
        # multipliers were therefore ignored in that case).
        labels = []
        for s in st:
            lab = getattr(s, "label", None)
            labels.append(str(lab) if lab else s.species_string)
        return st, labels
    if atoms:
        lc = lattice or {}
        if lc.get("cubic"):
            latt = Lattice.cubic(lc["a"])
        elif lc.get("a") and lc.get("b") and lc.get("c"):
            latt = Lattice.from_parameters(lc["a"], lc["b"], lc["c"],
                                           lc.get("alpha",90), lc.get("beta",90), lc.get("gamma",90))
        else:
            raise SystemExit("nominal atoms need lattice a (cubic) or a/b/c")
        sp = [a["el"] for a in atoms]
        coords = [a["xyz"] for a in atoms]
        st = Structure(latt, sp, coords)
        return st, [a.get("label") or a["el"] for a in atoms]
    raise SystemExit("plan needs either cif or atoms")

def _json_safe(obj):
    """Replace every non-finite float with None, recursively.

    `inf`/`nan` are not representable in JSON: Python writes them as bare
    `Infinity`/`NaN` tokens and every strict parser (including the tool layer's
    JSON.parse) rejects the file outright. Converting them to null keeps the
    result readable while making the "no value" explicit.
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _write_journal_md(jsonl_path, md_path):
    """Regenerate the human-readable journal from the authoritative .jsonl.

    Fixed table so a later reader can scan progress and the sticking point in one
    glance: 轮次 | 引擎 | 段/档 | 模型 | Rwp(基线) | 判读 | 为什么 | 产物.
    Regenerated, never appended — the .jsonl stays the single source of truth.
    """
    rows = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    # 表头双语（2026-09-27，用户要求；同批还改了 review.js 注入行与工具面那句警示句）。
    # 边界：**只做显示层** —— 表头是给人看的渲染；行内的记录值（model / trigger / verdict）
    # 原样不动，因为 journal.jsonl 是机器可读记录，双语化会让任何按值匹配的下游断掉。
    head = ("# XRD 精修日志 · XRD refinement journal"
            "（由 journal.jsonl 自动生成 / generated from journal.jsonl，"
            "勿手改 / do not edit by hand）\n\n"
            "| 轮次 / round | 引擎 / engine | 段·档 / stage | 模型 / model | Rwp | "
            "基线R / baseline | 判读 / verdict | 为什么 / why | 产物 / artifacts |\n"
            "|---|---|---|---|---|---|---|---|---|\n")
    body = []
    for r in rows:
        flags = (r.get("fit_status") or {}).get("flags") or []
        verdict = r.get("verdict") or ""
        if flags:
            verdict += "；⚠" + "；".join(flags)
        art = r.get("artifacts") or {}
        body.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("round"), r.get("engine"), r.get("stage"), r.get("model"),
            r.get("Rwp"), r.get("baseline_Rwp"), verdict, r.get("why") or "",
            art.get("result") or ""))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(head + "\n".join(body) + "\n")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    planf = sys.argv[1]
    outres = None
    want_comp = False
    for i, a in enumerate(sys.argv[2:]):
        if a == "--out": outres = sys.argv[3+i]
        elif a == "--components": want_comp = True
    plan = json.load(open(planf, encoding="utf-8"))
    outres = outres or (os.path.splitext(planf)[0] + "_result.json")
    os.makedirs(os.path.dirname(os.path.abspath(outres)), exist_ok=True)
    # P1/P2 (0.8.3): this run guards itself and PUBLISHES progress. The manual
    # engine is one of OUR processes, so "alive and working" is observable —
    # the JS layer keeps it running while ticks advance and only falls back on
    # its hard fuse. (fp2k is judged differently on purpose: it is a black box
    # that writes its outputs a couple of times per run, so nothing about it can
    # be inferred from ticks of the driver that is merely waiting for it.)
    guard = start_guard(plan, out_dir=os.path.dirname(os.path.abspath(outres)),
                        backend='manual', out_path=outres)

    from pymatgen.analysis.diffraction.xrd import XRDCalculator
    from lmfit import Parameters, minimize
    # P0 fix: Structure must be importable in main() scope (second-phase loop
    # L~188 uses Structure.from_file). load_structure() imports it locally,
    # which does NOT reach main() → NameError when a second phase is present.
    from pymatgen.core import Structure

    data = plan["data"]
    x, y = load_data(data["file"], data.get("th_min"), data.get("th_max"))
    sig = np.sqrt(np.maximum(y, 1.0))
    log(f"data: {len(x)} pts  {x[0]:.4f}-{x[-1]:.4f} deg")

    lam1 = plan.get("lambda", {}).get("kalpha1", 1.54056)
    lam2 = plan.get("lambda", {}).get("kalpha2", 1.54439)
    rat2 = plan.get("lambda", {}).get("ratio2", 0.497)
    # Monochromatic when no K-alpha2 is declared (synchrotron, or K-alpha2 already
    # stripped): lam2 = 0 or ratio = 0 means "one line only" (user 2026-10-03).
    # Declaring λ here is the ONLY way it enters this engine - there is no hidden
    # default beyond the Cu K-alpha values above, and the log line below is what
    # the report quotes.
    mono = (not lam2) or (not rat2)
    if mono:
        lam2, rat2 = 0.0, 0.0
    log("wavelength: kalpha1=%.5f %s [%s]" % (
        lam1, "(monochromatic)" if mono else "kalpha2=%.5f ratio=%.4f" % (lam2, rat2),
        "explicit" if plan.get("lambda_explicit") else "engine default"))
    dw = plan.get("dw") or {}
    bgcfg = plan.get("background") or {"n_cheb": 6, "center": 40.0, "halfwidth": 30.0}
    NBG = int(bgcfg.get("n_cheb", 6))
    BG0 = float(bgcfg.get("center", 40.0))
    BGH = float(bgcfg.get("halfwidth", 30.0))

    # ---- background order scan (user rule 2026-09-18) -----------------------
    # The background is fitted HERE, in the BOUNDED engine, and the fitted curve
    # is then handed to GSAS-II as its background (lin-interpolate import).
    # Rules: default 6, never above 20, raised in steps of 2, ONE step at a time
    # (never a blind jump to a high order), and stopped as soon as the R value
    # shows its plateau - the "boundary effect" the manual engine must confirm.
    bgscan = plan.get("bg_scan") or {}
    scan_orders = sorted({int(o) for o in (bgscan.get("orders") or [6, 8, 10, 12, 14, 16, 18, 20])
                          if 2 <= int(o) <= 20})
    scan_stop = float(bgscan.get("stop_tol", 0.05))   # Rwp percentage points
    scan_patience = int(bgscan.get("patience", 1))
    NBG_TOP = max([NBG] + scan_orders) if len(scan_orders) > 1 else NBG

    st, labels = load_structure(plan.get("cif"), plan.get("atoms"), plan.get("lattice"), dw)
    # element-symbol view of the same sites — occ/coord_vary may be written with
    # either the CIF label (N1) or the plain element (N)
    labels_el = [s.species_string for s in st]
    xrd = XRDCalculator(wavelength=lam1, debye_waller_factors=dw or None)

    # ---- params ----
    P = Parameters()
    pdef = plan.get("params") or {}
    def addp(name, **kw):
        d = pdef.get(name) or {}
        P.add(name, value=kw.get("value", d.get("value", 0.0)),
              min=d.get("min", kw.get("min")), max=d.get("max", kw.get("max")),
              vary=d.get("vary", kw.get("vary", name in (plan.get("vary") or []))))
    # scale: auto-init guess from data/10000
    init_scale = plan.get("scale")
    if not init_scale:
        init_scale = max(float(np.max(y)) / 5000.0, 1e-9)
    pdef.setdefault("scale", {})
    pdef["scale"].setdefault("value", init_scale)
    # P1 fix: default scale max was 1.0 — with a second phase present the main
    # phase hit the cap (scale=0.99999999875) and degraded (stderr null, a/zero
    # drift). Same order as second-phase scale_max (default 50).
    pdef["scale"].setdefault("min", 1e-12); pdef["scale"].setdefault("max", 50.0)
    addp("scale")
    addp("zero", value=-0.1, min=-0.5, max=0.5)
    # Lattice params: NEVER assume cubic. A hexagonal/tetragonal/orthorhombic
    # cell needs its own c (and b) — the old engine only refined `a` and
    # rebuilt every non-cubic cell as cubic (a,a,a,90,90,90), silently losing c
    # (observed on the hexagonal 极片 phase: c was frozen at the CIF value).
    _cell0 = st.lattice.parameters  # (a, b, c, alpha, beta, gamma)
    _a0, _b0, _c0, _al0, _be0, _ga0 = [float(v) for v in _cell0]

    def _close(x, y, tol=1e-4):
        return abs(x - y) <= tol * max(1.0, abs(y))

    CRYSTAL = ("cubic" if (_close(_a0, _b0) and _close(_b0, _c0) and _close(_al0, 90) and _close(_be0, 90) and _close(_ga0, 90))
               else "hex" if (_close(_a0, _b0) and _close(_al0, 90) and _close(_be0, 90) and _close(_ga0, 120))
               else "tet" if (_close(_a0, _b0) and _close(_al0, 90) and _close(_be0, 90) and _close(_ga0, 90))
               else "ortho" if (_close(_al0, 90) and _close(_be0, 90) and _close(_ga0, 90))
               else "other")
    log(f"lattice: {CRYSTAL}  a={_a0:.5f} b={_b0:.5f} c={_c0:.5f} "
        f"angles=({_al0:.3f},{_be0:.3f},{_ga0:.3f})")

    def _cellparam(name, v0, pct=0.03, force_vary=None):
        d = pdef.get(name) or {}
        P.add(name, value=d.get("value", v0),
              min=d.get("min", v0 * (1 - pct)), max=d.get("max", v0 * (1 + pct)),
              vary=d.get("vary", force_vary if force_vary is not None
                         else name in (plan.get("vary") or [])))

    _vary = plan.get("vary") or []
    _cellparam("a", _a0, force_vary=("a" in _vary))
    if CRYSTAL != "cubic":
        # non-cubic always exposes c (and b for orthorhombic/triclinic-like)
        _cellparam("c", _c0, force_vary=("c" in _vary))
    if CRYSTAL in ("ortho", "other"):
        _cellparam("b", _b0, force_vary=("b" in _vary))
    if CRYSTAL == "other":
        # Low symmetry (monoclinic / triclinic): the angles carry real
        # information - monoclinic beta in particular - so expose them as
        # refinable parameters with a tight +-2% window. They stay FIXED unless
        # named in `vary` (the PB lesson: on low-resolution data the angles are
        # strongly correlated with the profile terms, so opening them is a
        # deliberate choice, not a default).
        _cellparam("alpha", _al0, pct=0.02, force_vary=("alpha" in _vary))
        _cellparam("beta", _be0, pct=0.02, force_vary=("beta" in _vary))
        _cellparam("gamma", _ga0, pct=0.02, force_vary=("gamma" in _vary))
        log("low-symmetry cell: alpha/beta/gamma exposed as refinable parameters "
            "(add \"beta\" etc. to vary to use them; default fixed).")
    addp("U", value=0.5, min=0.0, max=5.0)
    addp("V", value=0.1, min=-1.0, max=1.0)
    addp("W", value=0.03, min=0.0, max=0.5)
    addp("eta", value=0.6, min=0.05, max=0.98)
    # Background Chebyshev terms. Listing only SOME bgN in `vary` used to leave
    # the remaining terms fixed, so raising background.n_cheb changed nothing
    # (nvar and Rwp stayed identical - a silent no-op). If ANY bgN is requested,
    # refine the whole set; an explicit per-term "vary" still wins.
    _vary_bg = any(str(v).startswith('bg') for v in _vary)
    # USER RULE (2026-09-18, RESTATED): background iteration is NOT a default step.
    # The manual engine and GSAS-II both default to a 6th-order polynomial, and
    # the order iteration is switched on ONLY when every other parameter has been
    # optimised and the fit still misses the target. So it must be asked for
    # explicitly (bg_scan.enabled = true), never run automatically.
    do_scan = bool(_vary_bg and len(scan_orders) > 1
                   and bgscan.get("enabled") is True)
    NBG_cur = [NBG]          # live order seen by model();  NBG = requested/default
    for i in range(NBG_TOP):
        nm = f"bg{i}"
        d = pdef.get(nm) or {}
        P.add(nm, value=d.get("value", 0.0),
              vary=bool(d.get("vary", _vary_bg or nm in (plan.get("vary") or []))))

    def _set_bg_order(n):
        """Open exactly bg0..bg(n-1) and close every term above it. model() only
        evaluates up to NBG_cur[0], so the closed terms cannot leak in."""
        NBG_cur[0] = int(n)
        for i in range(NBG_TOP):
            nm = 'bg%d' % i
            if nm in P:
                P[nm].set(vary=bool(_vary_bg and i < NBG_cur[0]))

    if NBG_TOP > NBG:
        # Only bg0..bg(NBG-1) may be open OUTSIDE the iteration. The higher terms
        # exist so the iteration can reach them, but leaving them open put
        # zero-gradient parameters into the least-squares: the fit stopped
        # converging (nfev hit the cap) and Rwp was meaningless.
        _set_bg_order(NBG)
    # occupancy multipliers per site label
    occ_cfg = plan.get("occ") or {}
    occ_pars = {}
    for lbl, cfg in occ_cfg.items():
        nm = "occ_" + lbl
        occ_pars[lbl] = nm
        P.add(nm, value=cfg.get("value", 1.0), min=cfg.get("min", 0.0),
              max=cfg.get("max", 1.0), vary=cfg.get("vary", False))
    # NOTE (boundary, user decision 2026-09-19): TOTAL-occupancy conservation is
    # NOT implemented here. Independent per-site `occ` multipliers cannot hold a
    # total, but the fix belongs to the GSAS side, where `add_EquivConstr` with
    # `0::Afrac:<idx>` ties occupancies (the project's own scripts already do this
    # for the [Fe(CN)6] linkage and for split K sub-sites). Use `xrd_gsas_ops` for
    # that; this engine stays a bounded-LM fitter with no conservation machinery.
    # optional per-atom coordinate refinement (OFF by default — the PB lesson is
    # that free C/N coordinates on low-resolution data drift to nonsense, so this
    # must be asked for explicitly) e.g. coord_vary: {"O6": ["z"]}
    coord_cfg = plan.get("coord_vary") or {}
    coord_pars = {}
    _lbl_idx = {str(l): i for i, l in enumerate(labels or [])}
    for lbl, axes in coord_cfg.items():
        i = _lbl_idx.get(str(lbl))
        if i is None:
            log("coord_vary: site %s not found — available labels: %s"
                % (lbl, ", ".join(str(x) for x in (labels or [])[:20])))
            continue
        for ax in (axes or []):
            ax = str(ax).lower()
            if ax not in ("x", "y", "z"):
                continue
            nm = "c%s_%s" % (ax, lbl)
            v0 = float(st[i].frac_coords[{"x": 0, "y": 1, "z": 2}[ax]])
            win = float((plan.get("coord_window") or {}).get(lbl, 0.05))
            coord_pars[(str(lbl), ax)] = (nm, i, {"x": 0, "y": 1, "z": 2}[ax])
            P.add(nm, value=v0, min=v0 - win, max=v0 + win, vary=True)
    if coord_pars:
        log("coordinate refinement enabled for %d parameter(s)" % len(coord_pars))
    # bond-length soft constraints: [{pair:["Fe","C"], d:1.90, sigma:0.03, weight:1.0}]
    # They are appended to the residual vector (so Rwp itself is NOT inflated)
    # and keep ligand bonds physical without freezing the coordinates outright.
    bond_targets = []
    for t in (plan.get("bond_targets") or []):
        try:
            pair = t.get("pair") or []
            if len(pair) != 2 or t.get("d") is None:
                continue
            bond_targets.append({"pair": (str(pair[0]), str(pair[1])),
                                 "d": float(t["d"]),
                                 "sigma": float(t.get("sigma", 0.03)),
                                 "weight": float(t.get("weight", 1.0))})
        except Exception as e:
            log("bond_targets entry skipped: %s (%s)" % (t, e))
    if bond_targets:
        log("bond-length soft constraints: %d" % len(bond_targets))
    # extras (empirical broad peaks)
    extras = plan.get("extras") or []
    for j, e in enumerate(extras):
        for q, (v0, lo, hi, vr) in {
            "c": (e.get("center", 25.0), e.get("center", 25.0)-2, e.get("center", 25.0)+2, e.get("vary_c", True)),
            "H": (e.get("H", 0.6), 0.1, 4.0, True),
            "a": (e.get("amp", 100), 0, None, True),
        }.items():
            kw = dict(value=v0, min=lo)
            if hi: kw["max"] = hi
            P.add(f"ex{j}_{q}", vary=vr, **kw)
    # second phases
    second = plan.get("second") or []
    S2 = []
    for si, sc in enumerate(second):
        st2 = Structure.from_file(sc["cif"]) if os.path.exists(sc["cif"]) else None
        if st2 is None: raise SystemExit("second-phase cif missing: " + sc["cif"])
        x2 = XRDCalculator(wavelength=lam1, debye_waller_factors=dw or None)
        pat2 = x2.get_pattern(st2, two_theta_range=(data.get("th_min",0)-1, data.get("th_max",100)+1))
        # B1/B2 fix: store d_hkls (d-spacing, NOT 2theta) and scale second-phase
        # intensities to the SAME order as the main phase (main uses pymatgen
        # scaled pattern, max ~ O(100)); scale_max default 50 (was 1) so the
        # second phase is not silently ~100x too weak (LS gradient ~0).
        S2.append({"d": np.array(pat2.d_hkls),
                   "I": np.array(pat2.y) / (np.max(pat2.y) or 1.0) * 100.0,
                   "k": sc.get("k", 1.0), "Y": sc.get("Y", 0.5)})
        P.add(f"s2_{si}_scale", value=sc.get("scale", 1e-6), min=0,
              max=sc.get("scale_max", 50.0), vary=sc.get("vary_scale", True))
        # B3: physical constraints readable from plan (e.g. k in [0.985,1.015])
        P.add(f"s2_{si}_k", value=sc.get("k", 1.0), min=sc.get("k_min", 0.9),
              max=sc.get("k_max", 1.1), vary=sc.get("vary_k", False))
        P.add(f"s2_{si}_Y", value=sc.get("Y", 0.5), min=sc.get("Y_min", 0.02),
              max=sc.get("Y_max", 5.0), vary=sc.get("vary_Y", False))
    # B4 self-check: warn loudly when this engine copy is an OLD buggy version
    # (d table stores 2theta, I unscaled ~max 1, scale_max<=1) — stale copies
    # were the source of "second phase excluded" false conclusions.
    for si, s2tab in enumerate(S2):
        d_arr = s2tab.get("d")
        if d_arr is not None and len(d_arr) and float(np.max(d_arr)) > 170.0:
            log(f"WARN(B4): second-phase {si} reflection table looks like 2theta "
                f"(dmax={float(np.max(d_arr)):.1f}) not d-spacing — stale engine copy? "
                f"Should be d_hkls. Fix by using the canonical engine.")
        imax = float(np.max(s2tab.get("I", [0])))
        smax = float(P[f"s2_{si}_scale"].max if f"s2_{si}_scale" in P else 1.0)
        if imax < 10.0 or smax <= 1.0:
            log(f"WARN(B4): second-phase {si} I-scaled max={imax:.1f} (<10) or "
                f"scale_max={smax:.1f} (<=1) — intensity scale bug present in this copy.")
    for k in plan.get("fix") or []:
        if k in P: P[k].set(vary=False)

    # ---- cached pattern ----
    _cache = {}
    def pattern_struct(par):
        a = par["a"].value
        b = par["b"].value if "b" in P else None
        c = par["c"].value if "c" in P else None
        al = par["alpha"].value if "alpha" in P else None
        be = par["beta"].value if "beta" in P else None
        ga = par["gamma"].value if "gamma" in P else None
        occmap = {lbl: par[occ_pars[lbl]].value for lbl in occ_pars}
        # Refined fractional coordinates, as (site_index, axis_index) -> value.
        # These MUST take part in the calculation: the previous version rebuilt the
        # cell and the occupancies but always passed `st.frac_coords`, so a refined
        # coordinate changed the reported structure while the pattern — and therefore
        # chi2 — stayed exactly as it was. The parameters were silently not fitted to
        # the data (found by audit 2026-09-21).
        coordmap = {}
        for (lbl, ax), (nm, i, j) in coord_pars.items():
            coordmap[(i, j)] = round(par[nm].value, 6)
        # cache key MUST include b/c, the angles, the occupancies AND the coordinates —
        # otherwise refining any of them returns a stale pattern (the coordinate entry
        # was missing here even after the cell/angle fix).
        # PRECISION (audit 2026-09-21): the occupancies were rounded to 4 decimals, so
        # a finite-difference probe that moved an occupancy by less than ~1e-4 landed
        # on the SAME cache key and got the old pattern back — the optimizer then saw
        # a flat gradient and the parameter never moved. 10 decimals keeps the cache
        # effective while making that impossible for any realistic step.
        key = (round(a,6), round(b,6) if b is not None else None,
               round(c,6) if c is not None else None,
               round(al,4) if al is not None else None,
               round(be,4) if be is not None else None,
               round(ga,4) if ga is not None else None) \
            + tuple(round(occmap[l],10) for l in occ_pars) \
            + tuple(sorted(coordmap.items()))
        if key in _cache: return _cache[key]
        stx = st
        # Rebuild the cell when any refined lattice parameter moved, keeping the
        # crystal system's symmetry (angles + a/b relation) intact.
        moved = (abs(a - _a0) > 1e-8) or (c is not None and abs(c - _c0) > 1e-8) \
                or (b is not None and abs(b - _b0) > 1e-8) \
                or (al is not None and abs(al - _al0) > 1e-8) \
                or (be is not None and abs(be - _be0) > 1e-8) \
                or (ga is not None and abs(ga - _ga0) > 1e-8)
        if moved or coordmap:
            from pymatgen.core import Lattice
            latt = st.lattice
            if moved:
                if CRYSTAL == "cubic":
                    latt = Lattice.from_parameters(a, a, a, 90, 90, 90)
                elif CRYSTAL == "hex":
                    latt = Lattice.from_parameters(a, a, (c if c is not None else _c0), 90, 90, 120)
                elif CRYSTAL == "tet":
                    latt = Lattice.from_parameters(a, a, (c if c is not None else _c0), 90, 90, 90)
                elif CRYSTAL == "ortho":
                    latt = Lattice.from_parameters(a, (b if b is not None else _b0),
                                                   (c if c is not None else _c0), 90, 90, 90)
                else:  # low symmetry: a/b/c refined AND the angles are parameters too
                    latt = Lattice.from_parameters(a, (b if b is not None else _b0),
                                                   (c if c is not None else _c0),
                                                   (al if al is not None else _al0),
                                                   (be if be is not None else _be0),
                                                   (ga if ga is not None else _ga0))
            # refined coordinates, applied the same way current_structure() does it
            coords = [list(st[i].frac_coords) for i in range(len(st))]
            for (i, j), v in coordmap.items():
                coords[i][j] = v
            # species_and_occu keeps partial-occupancy/disordered sites valid
            stx = st.__class__(latt, st.species_and_occu, coords)
        # occupancy: rebuild species list with occ multiplier (partial occupancy)
        # NB: build from site.species (a pymatgen Composition), NOT from
        # species_string. For a partially occupied site species_string is a
        # composite such as "K0.8" or "Fe0.324 Mn0.4", and using it as a dict key
        # makes pymatgen raise IndexError inside Species.from_str -> ANY fit that
        # carries an `occ` entry crashed here (found 2026-09-19; the same fix had
        # already been applied to current_structure() but this copy of the pattern
        # builder was missed).
        if occmap:
            sp2 = []
            for i, site in enumerate(stx):
                comp = site.species
                lbl = labels[i] if (labels and i < len(labels)) else ""
                mult = 1.0
                _why = ""
                if lbl in occmap:
                    mult = occmap[lbl]
                    _why = 'occ[%s]' % lbl
                else:
                    for el in comp:
                        if el.symbol in occmap:
                            mult = occmap[el.symbol]
                            _why = 'occ[%s]' % el.symbol
                            break
                spec = {el: float(amt) * mult for el, amt in comp.items()}
                # An occupancy MULTIPLIER above the value that makes a site sum to >1
                # makes pymatgen raise "Species occupancies sum to more than 1!"
                # deep inside Structure(), and the engine then died with NO result
                # file at all — from the outside that looks like "the engine produced
                # nothing". Name the site and the multiplier instead (found
                # 2026-09-21 while measuring epsfcn; the same ceiling the tool
                # description warns about).
                _tot = sum(spec.values())
                if _tot > 1.0 + 1e-9:
                    raise RuntimeError(
                        'occupancy multiplier out of range at site %s: %s = %.6g makes the '
                        'site occupancies sum to %.6g (> 1). A multiplier scales the CIF '
                        'occupancy, so it cannot push a site above full occupancy — lower '
                        'the bound, or use GSAS-II (xrd_gsas_ops) where occupancies are '
                        'constrained properly.' % (lbl or '?', _why or 'occ', mult, _tot))
                sp2.append(spec)
            stx = stx.__class__(stx.lattice, sp2, stx.frac_coords)
        pat = xrd.get_pattern(stx, two_theta_range=(data.get("th_min",0)-1, data.get("th_max",100)+1))
        out = (np.array(pat.x), np.array(pat.y))
        _cache[key] = out
        return out

    def current_structure(par):
        """Same cell/site reconstruction as pattern_struct, but returns the
        pymatgen Structure — used by the coordinate params and bond constraints."""
        from pymatgen.core import Lattice
        a = par["a"].value
        b = par["b"].value if "b" in P else None
        c = par["c"].value if "c" in P else None
        al = par["alpha"].value if "alpha" in P else None
        be = par["beta"].value if "beta" in P else None
        ga = par["gamma"].value if "gamma" in P else None
        if CRYSTAL == "cubic":
            latt = Lattice.from_parameters(a, a, a, 90, 90, 90)
        elif CRYSTAL == "hex":
            latt = Lattice.from_parameters(a, a, (c if c is not None else _c0), 90, 90, 120)
        elif CRYSTAL == "tet":
            latt = Lattice.from_parameters(a, a, (c if c is not None else _c0), 90, 90, 90)
        elif CRYSTAL == "ortho":
            latt = Lattice.from_parameters(a, (b if b is not None else _b0),
                                           (c if c is not None else _c0), 90, 90, 90)
        else:
            latt = Lattice.from_parameters(a, (b if b is not None else _b0),
                                           (c if c is not None else _c0),
                                           (al if al is not None else _al0),
                                           (be if be is not None else _be0),
                                           (ga if ga is not None else _ga0))
        coords = [list(st[i].frac_coords) for i in range(len(st))]
        for (lbl, ax), (nm, i, j) in coord_pars.items():
            coords[i][j] = par[nm].value
        # NB: build the species from site.species (a pymatgen Composition), NOT
        # from species_string. A disordered site's species_string is a composite
        # such as "Na0.72 Ni0.33", and passing that back as a dict key makes
        # pymatgen call Element.from_str("Na0.72 Ni0.33") -> IndexError (hit on
        # the layered-oxide P2 CIF). Keeping the composition also preserves mixed
        # occupancies instead of collapsing the site into one element.
        sp = []
        for i, site in enumerate(st):
            comp = site.species
            key = labels[i] if (labels and i < len(labels)) else ''
            mult = 1.0
            if key in occ_pars:
                mult = par[occ_pars[key]].value
            else:
                for el in comp:
                    if el.symbol in occ_pars:
                        mult = par[occ_pars[el.symbol]].value
                        break
            spec = {el: float(amt) * mult for el, amt in comp.items()}
            # same ceiling as pattern_struct: an occupancy multiplier that pushes a
            # site above full occupancy makes pymatgen raise deep inside Structure();
            # name the site instead of dying without a result file.
            _tot = sum(spec.values())
            if _tot > 1.0 + 1e-9:
                raise RuntimeError(
                    'occupancy multiplier out of range at site %s: makes the site '
                    'occupancies sum to %.6g (> 1). A multiplier scales the CIF '
                    'occupancy and cannot exceed full occupancy.' % (key or '?', _tot))
            sp.append(spec)
        return st.__class__(latt, sp, coords)

    def bond_residuals(par):
        """Weighted bond-length deviations, appended to the residual vector so
        that Rwp itself is not inflated. Keeps ligand bonds physical without
        freezing the coordinates outright."""
        rows = []
        if not bond_targets:
            return rows
        try:
            sx = current_structure(par)
        except Exception:
            return rows
        for t in bond_targets:
            e1, e2 = t["pair"]
            i1 = [i for i, s in enumerate(sx) if e1 in str(s.species_string)]
            i2 = [i for i, s in enumerate(sx) if e2 in str(s.species_string)]
            if not i1 or not i2:
                continue
            best = None
            for i in i1:
                for j in i2:
                    if i == j:
                        continue
                    d = sx.lattice.get_distance_and_image(sx.frac_coords[i],
                                                          sx.frac_coords[j])[0]
                    if best is None or d < best:
                        best = float(d)
            if best is None:
                continue
            rows.append(float(np.sqrt(max(t["weight"], 0.0))) *
                        (best - t["d"]) / max(t["sigma"], 1e-9))
        return rows

    def _is_cubic(stl):
        p = stl.lattice.parameters
        return abs(p[0]-p[1])<1e-6 and abs(p[1]-p[2])<1e-6 and abs(p[3]-90)<1e-6 and abs(p[4]-90)<1e-6 and abs(p[5]-90)<1e-6

    # ---- model ----
    # == EDITABLE == peak-shape / model section: adjust for your material
    # (e.g. add asymmetry, anisotropic broadening, strain model, preferred
    # orientation, instrument correction). The two functions below compute
    # per-reflection shape; model() assembles the full profile.
    def g_area(X, c, HG):
        d = X - c
        return np.sqrt(4*math.log(2)/math.pi)/HG*np.exp(-4*math.log(2)*d*d/HG/HG)
    def l_area(X, c, HL):
        d = X - c
        return 2.0/(math.pi*HL)/(1.0+(2*d/HL)**2)
    def pv_area(X, c, H, eta):
        return (1-eta)*g_area(X, c, H) + eta*l_area(X, c, H)

    # == EDITABLE == per-reflection width model (Caglioti U tan^2+V tan+W,
    # Gaussian; Lorentzian HL = X*tan + Y/cos if needed)
    def hwidth(thc_deg, prm):
        U = prm["U"].value; V = prm["V"].value; W = prm["W"].value
        tt = math.tan(math.radians(thc_deg/2))
        return math.sqrt(max(U*tt*tt + V*tt + W, 0.002))

    def model(xa, prm, comps=None):
        a = prm["a"].value; zero = prm["zero"].value; sc = prm["scale"].value
        U = prm["U"].value; V = prm["V"].value; W = prm["W"].value; eta = prm["eta"].value
        th, I1 = pattern_struct(prm)
        t = (xa - BG0)/BGH
        bg = np.zeros_like(xa)
        T0 = np.ones_like(xa); T1 = t.copy()
        bg = bg + prm["bg0"].value*T0 + prm["bg1"].value*T1
        for i in range(2, NBG_cur[0]):
            Tn = 2*t*T1 - T0
            bg = bg + prm[f"bg{i}"].value*Tn
            T0, T1 = T1, Tn
        m = bg.copy(); main_part = bg.copy()
        for th1, iv in zip(th, I1):
            for lam, rr in (((1.0, 1.0),) if mono else ((1.0, 1.0), (lam2/lam1, rat2))):
                # K-alpha2 position from Bragg's law, NOT by scaling 2theta linearly:
                # sin(theta2) = (lambda2/lambda1) * sin(theta1). The old `th1*lam` put
                # the K-alpha2 component ~0.005 deg (at 30 deg 2theta) to ~0.014 deg
                # (at 60 deg) away from where it belongs — small, but a systematic
                # error independent of the model (audit 2026-09-21).
                if lam == 1.0:
                    thc = th1 + zero
                else:
                    s2 = lam * math.sin(math.radians(th1 / 2.0))
                    thc = 2.0 * math.degrees(math.asin(min(s2, 0.999))) + zero
                tt = math.tan(math.radians(thc/2))
                H = hwidth(thc, prm)
                m = m + sc*iv*rr*pv_area(xa, thc, H, eta)
                main_part = main_part + sc*iv*rr*pv_area(xa, thc, H, eta)
        extra_part = np.zeros_like(xa)
        for j in range(len(extras)):
            c = prm[f"ex{j}_c"].value; H = prm[f"ex{j}_H"].value; A = prm[f"ex{j}_a"].value
            exeta = extras[j].get("eta", 0.9)
            m = m + A*pv_area(xa, c, H, exeta)
            extra_part = extra_part + A*pv_area(xa, c, H, exeta)
        for si in range(len(S2)):
            sc2 = prm[f"s2_{si}_scale"].value; k2 = prm[f"s2_{si}_k"].value; Y2 = prm[f"s2_{si}_Y"].value
            D2, I2v = S2[si]["d"], S2[si]["I"]
            for d0, iv in zip(D2, I2v):
                th1 = 2*math.degrees(math.asin(min(lam1/(2*d0*k2), 0.999)))
                for lam, rr in ((1.0,1.0),(lam2/lam1,rat2)):
                    # same strict Bragg relation as the main phase above
                    if lam == 1.0:
                        thc = th1 + zero
                    else:
                        s2 = lam * math.sin(math.radians(th1 / 2.0))
                        thc = 2.0 * math.degrees(math.asin(min(s2, 0.999))) + zero
                    tt = math.tan(math.radians(thc/2))
                    Hg = math.sqrt(max(U*tt*tt+V*tt+W, 0.002))
                    HL = Y2/math.cos(math.radians(thc/2))
                    Ht = math.sqrt(Hg*Hg+HL*HL)
                    m = m + sc2*iv*rr*pv_area(xa, thc, Ht, 0.9)
                    extra_part = extra_part + sc2*iv*rr*pv_area(xa, thc, Ht, 0.9)
        if comps is not None:
            comps["bg"] = bg; comps["main"] = main_part; comps["extra"] = extra_part
        return m

    _nfev = [0]

    def resid(prm):
        # The residual is the hot loop: one tick per evaluation is the finest
        # liveness signal this engine can produce (guard.tick rate-limits itself
        # to tick_s, so this is a counter increment plus an occasional write).
        _nfev[0] += 1
        # progress=True is the whole point: one residual evaluation IS the fit
        # moving. The JS layer keeps this run alive only while such ticks arrive,
        # so a fit that stalls inside numpy is still caught (the ticker thread's
        # own heartbeats deliberately claim no progress).
        guard.tick('fitting', progress=True, nfev=_nfev[0])
        if guard.stop_requested():
            # P1 (0.8.3): a graceful stop lands HERE, between iterations — the
            # fit is not killed mid-write, and the caller gets a structured
            # "stopped" result instead of a truncated file.
            raise EngineStopped('stop requested (checked at the residual)')
        r = (model(x, prm) - y)/sig
        # Bond-length soft constraints enter as EXTRA residuals: calc_rw() below
        # stays a pure profile factor (Rwp is not inflated by the penalty), while
        # the least-squares is pulled towards the target bond lengths.
        pen = bond_residuals(prm)
        if pen:
            r = np.concatenate([r, np.asarray(pen, dtype=float)])
        return r

    def calc_rw(prm):
        return float(np.sqrt(np.sum(((model(x,prm)-y)/sig)**2)/np.sum((y/sig)**2))*100)

    # ---- BASELINE R: every parameter still at its starting value -------------
    # Measured BEFORE any optimisation, so a caller can tell "the fit improved
    # from 30% to 12%" apart from "the fit never moved". Without it a result of
    # 12% says nothing about whether the starting model was already 12%.
    try:
        baseline_rwp = calc_rw(P)
    except Exception as e:
        baseline_rwp = None
        log("baseline Rwp unavailable (%s)" % e)

    t0 = time.time()
    seeds = plan.get("seed_restarts") or [None]

    def do_fit():
        best_local = None
        for sd in seeds:
            if sd:
                for grp in sd.values():
                    for k, v in grp.items():
                        if k in P: P[k].set(value=v)
            # epsfcn: finite-difference step for the Jacobian. lmfit's default is tiny
            # (about 1.5e-8); an audit reported occupancy parameters not moving with an
            # explicit 1e-10, i.e. a step so small the numerical derivative vanishes
            # against the rounding of the pattern calculation. It is EXPOSED here
            # (default = library default) rather than changed blindly: the value gets
            # chosen by measurement on real data, not by guesswork.
            _mk = {"max_nfev": plan.get("max_nfev", 8000)}
            if plan.get("epsfcn") is not None:
                _mk["epsfcn"] = float(plan["epsfcn"])
            rr = minimize(resid, P, method="leastsq", **_mk)
            rw = calc_rw(rr.params)
            log(f"fit nfev={rr.nfev}  Rwp={rw:.2f}%  ({time.time()-t0:.1f}s)")
            if best_local is None or rw < best_local[0]:
                best_local = (rw, rr)
        return best_local

    # ---- PHASE 1: the main fit, background held at its STARTING order --------
    # USER RULE (2026-09-18, hard-coded): the background polynomial is the LAST
    # parameter family to be fitted - it is only optimised after everything else
    # has been tried. Optimising it first lets it absorb structural misfit, so
    # the structure "converges" against a background that has eaten its signal.
    # Stop at a safe point BEFORE the main fit (0.8.5): the fit itself is one lmfit call
    # that cannot be interrupted without corrupting the model, so the boundary between
    # stages is the honest place to honour a stop request.
    _g = active_guard()
    if _g is not None:
        _g.checkpoint('main fit')
    best = do_fit()
    r = best[1]

    # ---- PHASE 2 (LAST): background order iteration --------------------------
    # USER RULE (2026-09-18, RESTATED): the iteration exports ONE BACKGROUND CURVE
    # PER ORDER so each can be imported into GSAS-II directly, and the R-value
    # BOUNDARY EFFECT is judged on THIS engine's Rwp (bounded LM), never on the
    # GSAS wR - GSAS uses an unbounded least-squares whose wR can look better
    # while the model is worse. The scan therefore stops at the first order whose
    # R gain falls below the tolerance, and that order is the boundary.
    # Runs only now, with every non-background parameter FROZEN at its phase-1
    # value, so the trials differ solely in the background order.
    bg_scan_table = []
    bg_curves = []
    boundary_n = None
    scan_only_rw = None
    if do_scan:

        def _freeze_non_bg():
            frozen = []
            for nm, p in P.items():
                if not nm.startswith('bg') and p.vary:
                    frozen.append(nm)
                    p.set(vary=False)
            return frozen

        def _unfreeze(names):
            for nm in names:
                if nm in P:
                    P[nm].set(vary=True)

        held = _freeze_non_bg()
        _curve_dir = os.path.dirname(os.path.abspath(outres))
        try:
            prev_rw, flat = None, 0
            for n in scan_orders:
                # Between background-order trials is a safe point (0.8.5).
                _g2 = active_guard()
                if _g2 is not None:
                    _g2.checkpoint('bg_scan order %d' % n)
                _set_bg_order(n)
                rw_n, res_n = do_fit()
                # Export THIS order's background curve so it can be handed to
                # GSAS-II directly: two columns (2theta, background), which is
                # exactly what xrd_gsas_ops import_background reads. The iteration
                # therefore produces one importable curve PER order, not just a
                # winner chosen inside the engine.
                try:
                    _c = {}
                    model(x, res_n.params, _c)
                    _curve = _c.get("bg")
                    if _curve is not None:
                        _p = os.path.join(_curve_dir, "bg_n%02d.txt" % n)
                        with open(_p, "w", encoding="utf-8") as _fh:
                            _fh.write("# background order %d  Rwp=%.3f%%\n"
                                      "# 2theta  background\n" % (n, rw_n))
                            for _xi, _yi in zip(x, _curve):
                                _fh.write("%.5f %.6g\n" % (_xi, _yi))
                        bg_curves.append({"n_cheb": n, "Rwp": round(float(rw_n), 4),
                                          "path": _p})
                except Exception as _e:
                    log("background curve export failed for n=%d: %s" % (n, _e))
                gain = None if prev_rw is None else prev_rw - rw_n
                bg_scan_table.append({"n_cheb": n, "Rwp": round(float(rw_n), 4),
                                      "gain": None if gain is None else round(float(gain), 4)})
                # boundary effect, judged on THIS engine's Rwp:
                if boundary_n is None and gain is not None and gain < scan_stop:
                    boundary_n = n
                log("bg_scan n_cheb=%d Rwp=%.3f%% gain=%s"
                    % (n, rw_n, "n/a" if gain is None else "%.3f" % gain))
                flat = flat + 1 if (gain is not None and gain < scan_stop) else 0
                prev_rw = rw_n
                if flat >= scan_patience:
                    log("bg_scan STOP at n_cheb=%d: R plateaued (gain<%.3f for %d step(s))"
                        % (n, scan_stop, flat))
                    break
            # The R-value BOUNDARY EFFECT decides how far to go, and it is judged
            # on THIS engine's Rwp (bounded LM) - never on a downstream GSAS wR.
            # The first order that buys less than stop_tol is the edge; going past
            # it would spend background terms on a gain R says is not there.
            # Within the boundary keep the best R (an order past the edge can
            # still show a marginally lower R by noise, and must not be picked).
            _limit = boundary_n if boundary_n is not None else max(
                row["n_cheb"] for row in bg_scan_table)
            _cands = [row for row in bg_scan_table if row["n_cheb"] <= _limit]
            best_n = min(_cands, key=lambda row: row["Rwp"])["n_cheb"]
            _set_bg_order(best_n)
            log("bg_scan chosen n_cheb=%d (boundary=%s, tried %s) on manual-engine Rwp"
                % (best_n, boundary_n, [row["n_cheb"] for row in bg_scan_table]))
            scan_only_rw = do_fit()[0]
            log("bg_scan background-only Rwp=%.2f%% at n_cheb=%d" % (scan_only_rw, best_n))
        finally:
            _unfreeze(held)
        # The background ORDER was settled last, with every other parameter frozen
        # so the trials differed only in the background. The final state is then a
        # joint fit at that order: leaving the structure frozen against a
        # background it was never fitted with reports an R far worse than the
        # model can actually reach (observed: 10.76% -> 16.25% on the electrode).
        best = do_fit()
        r = best[1]
    rwp = calc_rw(r.params)
    nvar = len([p for p in r.params.values() if p.vary])
    rexp = float(np.sqrt((len(x)-nvar)/np.sum((y/sig)**2))*100)
    chi2 = float(np.sum(((model(x,r.params)-y)/sig)**2)/(len(x)-nvar))
    log(f"Rwp={rwp:.2f}%  Rexp={rexp:.2f}%  chi2={chi2:.2f}  nvar={nvar}")

    comps = {}
    if want_comp:
        ym = model(x, r.params, comps)
    else:
        ym = model(x, r.params)
    # result json
    res = {"metrics": {"Rwp": rwp, "Rexp": rexp, "chi2": chi2, "nvar": nvar,
                       "npts": len(x), "nfev": r.nfev,
                       "baseline_Rwp": baseline_rwp},
           "params": {k: {"value": float(p.value), "stderr": float(p.stderr) if p.stderr is not None else None,
                          "vary": bool(p.vary)} for k, p in r.params.items()}}

    # ---- FIT STATUS: did the optimiser actually finish, and where is it stuck? --
    # The caller (and the workflow) needs to distinguish three very different
    # situations that all used to look like "Rwp = 12%":
    #   (a) the fit converged and Rwp is a real minimum;
    #   (b) the fit ran out of evaluations - Rwp is a snapshot, not a minimum;
    #   (c) the fit pushed parameters onto their bounds - Rwp is bound-limited,
    #       so the "best" value is an artefact of the box, not of the data.
    try:
        msg = str(getattr(r, "message", "") or "")
        ier = int(getattr(r, "ier", 0) or 0)
        max_nfev = int(plan.get("max_nfev", 8000))
        # leastsq reports ier in {1,2,3,4}; 0/negative means it did not finish
        success = bool(ier in (1, 2, 3, 4))
        hit_cap = bool(r.nfev >= max_nfev)
        at_bound = []
        open_bounds = 0
        for k, p in r.params.items():
            if not p.vary:
                continue
            lo, hi = getattr(p, "min", None), getattr(p, "max", None)
            lo_f = float(lo) if lo is not None and np.isfinite(lo) else None
            hi_f = float(hi) if hi is not None and np.isfinite(hi) else None
            if lo_f is None or hi_f is None:
                # lmfit reports an open bound as +/-inf. NEVER let that reach the
                # JSON: `Infinity` is not valid JSON, and json.dump writes it as
                # `-Infinity`, which makes the tool layer's JSON.parse throw and
                # the whole result look like "the engine produced nothing"
                # (measured 2026-09-20).
                open_bounds += 1
            span = (hi_f - lo_f) if (lo_f is not None and hi_f is not None) else 0.0
            for lim, tag in ((lo_f, "min"), (hi_f, "max")):
                if lim is None:
                    continue
                tol = max(1e-9, 1e-3 * span) if span else 1e-9
                if abs(float(p.value) - lim) <= tol:
                    at_bound.append({"param": k, "bound": tag, "limit": lim})
        stderr_missing = [k for k, p in r.params.items() if p.vary and p.stderr is None]
        res["fit_status"] = {
            "success": success,
            "ier": ier,
            "message": msg,
            "nfev": int(r.nfev),
            "max_nfev": max_nfev,
            "hit_max_nfev": hit_cap,
            "n_at_bound": len(at_bound),
            "at_bound": at_bound[:20],
            "n_open_bounds": open_bounds,
            "n_stderr_missing": len(stderr_missing),
            "stderr_missing": stderr_missing[:20],
            "covariance_available": bool(not stderr_missing),
        }
        flags = []
        if not success:
            flags.append("the optimiser did not report convergence (%s)" % (msg or "no message"))
        if hit_cap:
            flags.append("stopped at max_nfev=%d - Rwp is a snapshot, not a minimum" % max_nfev)
        if at_bound:
            flags.append("%d parameter(s) sit on a bound (%s) - the value is box-limited"
                         % (len(at_bound), ", ".join(sorted({b["param"] for b in at_bound})[:6])))
        if baseline_rwp is not None and rwp is not None:
            if abs(baseline_rwp - rwp) < 1e-6:
                flags.append("Rwp equals the baseline exactly - nothing was optimised")
        res["fit_status"]["flags"] = flags
    except Exception as e:
        res["fit_status"] = {"error": "fit_status unavailable: %s" % e, "flags": []}

    # ---- R-VALUE PLATEAU ("R 值洼地") ---------------------------------------
    # This engine runs a BOUNDED least-squares whose Rexp/chi2 are *not* a GSAS wR,
    # so the plateau cannot be derived from chi2 alone. Two independent signals are
    # reported and the caller decides:
    #   * nvar/npts        - degrees of freedom left. A fit with nvar ~ npts has no
    #                        freedom left, so a low Rwp means overfitting, not fit.
    #   * fit_status.flags - a non-converged / bound-limited / snapshot Rwp is not a
    #                        plateau at all.
    # A plateau is reported only when the fit genuinely finished AND the parameter
    # count is not eating the data. The refinement ladder (xrd_refine_auto) refines
    # the same sample across several families, so a true plateau is judged there
    # from the per-step Rwp trace, not from one run.
    try:
        dof = int(len(x) - nvar)
        overfit = bool(nvar > 0 and dof > 0 and (nvar / float(len(x))) > 0.2)
        fs = res.get("fit_status", {})
        clean = bool(fs.get("success")) and not fs.get("hit_max_nfev") and not fs.get("n_at_bound")
        plateau = bool(clean and not overfit)
        # `basis` is the sentence a reader (or an AI) uses to justify the verdict, so it has
        # to be DERIVED from the flags that produced it. It used to be a fixed string
        # claiming "fit converged, no parameter on a bound" even when the fit had hit
        # max_nfev or sat on a bound -- i.e. it contradicted is_plateau and fit_status in
        # exactly the cases where the distinction matters (audit 2026-09-21).
        ratio = (nvar / float(len(x))) if len(x) else 0.0
        blockers = []
        if not fs.get("success"):
            blockers.append("the optimiser did not report success")
        if fs.get("hit_max_nfev"):
            blockers.append("it hit max_nfev (%s) so this Rwp is a snapshot, not a minimum"
                            % (fs.get("nfev") if fs.get("nfev") is not None else '?'))
        if fs.get("n_at_bound"):
            blockers.append("%s parameter(s) sit on a bound" % fs.get("n_at_bound"))
        if overfit:
            blockers.append("nvar/npts = %.3f exceeds 0.2" % ratio)
        if plateau:
            basis = ("fit_status.success and no max_nfev hit and no parameter on a bound, "
                     "with nvar/npts = %.3f below 0.2" % ratio)
        else:
            basis = ("NOT a plateau" +
                     (": " + "; ".join(blockers) if blockers else ": no clean-fit signal"))
        res["r_plateau"] = {
            "is_plateau": plateau,
            "basis": basis,
            "blockers": blockers,
            "dof": dof,
            "nvar_over_npts": round(ratio, 4) if len(x) else None,
            "overfit_risk": overfit,
            "note": ("a plateau means THIS model+data combination is exhausted at this "
                     "engine's Rwp; judge the ladder's plateau from the per-step Rwp trace "
                     "in xrd_refine_auto's ladder field, and hand over to GSAS-II once "
                     "Rwp stops improving"),
        }
    except Exception as e:
        res["r_plateau"] = {"error": "r_plateau unavailable: %s" % e}


    # Background provenance: which order was requested, which one was chosen, and
    # the scan that led there (the curve itself is in components.json under "bg").
    #
    # TRUTHFUL `fitted_last` (0.8.5): it used to be hard-coded True, but on the
    # DEFAULT path (no bg_scan) the background coefficients are part of the same
    # bounded least-squares as the cell/profile parameters — one joint fit — so
    # "the background was fitted last" was simply not what happened. The fitting
    # itself is deliberately UNCHANGED (user decision 2026-09-28: "如果是手动引擎的
    # 部分就不要动了"); only the record now says which of the two things occurred:
    #   * do_scan  → the background ORDER was settled last, with every other family
    #                frozen, and the run then closed with a joint fit at that order;
    #   * no scan  → ONE joint fit, background included from the start.
    _bg_varied = [nm for nm in (r.params or {})
                  if nm.startswith("bg") and getattr(r.params[nm], "vary", False)]
    _other_varied = [nm for nm in (r.params or {})
                     if not nm.startswith("bg") and not nm.startswith("__")
                     and getattr(r.params[nm], "vary", False)]
    res["background"] = {"n_cheb_requested": NBG, "n_cheb_final": NBG_cur[0],
                         "cheb_center": BG0, "cheb_halfwidth": BGH,
                         "fitted_last": bool(bg_scan_table),
                         "order_settled_last": bool(bg_scan_table),
                         "final_fit": ("joint fit at the chosen order, AFTER a background-only "
                                       "order scan with every other family frozen"
                                       if bg_scan_table else
                                       "ONE joint bounded least-squares: the background was "
                                       "refined TOGETHER with the other families, not after them"),
                         "fitted_jointly_with": ([] if bg_scan_table else _other_varied),
                         "n_bg_terms_varied": len(_bg_varied),
                         "iterated": bool(bg_scan_table),
                         "default_order": 6, "max_order": 20, "step": 2}
    if bg_scan_table:
        res["background"]["scan"] = bg_scan_table
        res["background"]["stop_tol"] = scan_stop
        res["background"]["scanned"] = [row["n_cheb"] for row in bg_scan_table]
        # the boundary effect is judged HERE, on the bounded engine's Rwp
        res["background"]["boundary_r_basis"] = "manual engine Rwp (bounded LM)"
        if boundary_n is not None:
            res["background"]["boundary_n_cheb"] = boundary_n
            res["background"]["boundary_note"] = (
                'R-value boundary effect (manual-engine Rwp): from order %d the gain fell '
                'below %.3f percentage points, so higher orders buy nothing and were not '
                'used to pick the curve.' % (boundary_n, scan_stop))
        if scan_only_rw is not None:
            res["background"]["scan_only_Rwp"] = round(float(scan_only_rw), 4)
    if bg_curves:
        # one importable 2theta/background file per order (for GSAS-II)
        res["background"]["curves"] = bg_curves
    # ---- conservation report: PROVE the total actually held after the fit -------
    # A constraint that silently fails to apply is worse than none (it would let a
    # "conserved" run be reported as conserved when it is not), so the realised sum
    # is measured against the target and surfaced, not assumed.
    # ---- judgment packet (user rule: R alone is NOT the whole story) ---------
    # A parameter pinned at a bound, or with no/huge stderr, is NOT a measurement.
    # Observed on a real sample: monoclinic `a` refined to 9.919, exactly its
    # default +-3% bound, and nothing in the result said the value was truncated.
    def _health(prm):
        rows, flags = [], []
        # internal bookkeeping parameters (names starting with "__") are not physical
        # parameters and are not reported as such.
        book = {nm for nm in prm if nm.startswith("__")}
        varying = [(nm, p) for nm, p in prm.items() if p.vary and nm not in book]
        # stderr is computed for ALL parameters or for NONE: when leastsq stops at
        # max_nfev the covariance matrix is never built. That is ONE global
        # condition, not N per-parameter findings (reporting it per parameter
        # buried the real signal under 14 identical lines).
        any_stderr = any(p.stderr is not None for _nm, p in varying)
        for nm, p in prm.items():
            if nm in book:
                continue
            try:
                v = float(p.value)
            except Exception:
                continue
            row = {'name': nm, 'value': v, 'vary': bool(p.vary)}
            if getattr(p, "expr", None):
                # this parameter is COMPUTED from the anchor of its conservation
                # group: its value is not an independent measurement, and its stderr
                # is the anchor's propagated through the relation.
                row['determined_by'] = 'constraint: %s' % p.expr
            if not p.vary:
                rows.append(row)
                continue
            lo, hi = p.min, p.max
            lo_ok = lo is not None and np.isfinite(lo)
            hi_ok = hi is not None and np.isfinite(hi)
            if lo_ok and hi_ok and hi > lo:
                span = hi - lo
                head = min(v - lo, hi - v)
                row['headroom'] = float(head)
                if head <= 0.01 * span:
                    side = 'lower' if (v - lo) <= (hi - v) else 'upper'
                    row['at_bound'] = 'min' if side == 'lower' else 'max'
                    row['bound'] = float(lo if side == 'lower' else hi)
                    flags.append(
                        '%s = %g is within 1%% of its %s bound %g -> effectively PINNED: the value '
                        'is set by the constraint, not measured'
                        % (nm, v, side, row['bound']))
            elif lo_ok and abs(v - lo) <= 1e-6 * max(1.0, abs(lo)):
                row['at_bound'] = 'min'
                row['bound'] = float(lo)
                flags.append('%s = %g is pinned at its lower bound -> not measured' % (nm, v))
            elif hi_ok and abs(v - hi) <= 1e-6 * max(1.0, abs(hi)):
                row['at_bound'] = 'max'
                row['bound'] = float(hi)
                flags.append('%s = %g is pinned at its upper bound -> not measured' % (nm, v))
            se = p.stderr
            if se is None:
                if any_stderr:
                    row['stderr_note'] = 'no stderr although the covariance matrix exists'
                    flags.append('%s: no stderr while other parameters have one -> this parameter '
                                 'is not determined by the data' % nm)
            else:
                se = float(se)
                row['stderr'] = se
                if se <= 0.0:
                    row['stderr_note'] = 'stderr = 0'
                    flags.append('%s: stderr is 0 -> not determined by the data' % nm)
                elif abs(v) > 1e-12 and se / abs(v) > 0.5:
                    row['stderr_note'] = 'stderr/|value| = %.2f' % (se / abs(v))
                    flags.append('%s: stderr/|value| = %.2f -> weakly determined' % (nm, se / abs(v)))
            rows.append(row)
        if varying and not any_stderr:
            flags.append(
                'the covariance matrix was NOT computed (the fit stopped at max_nfev=%s), so no '
                'stderr exists for any of the %d varying parameters -> no esd can be quoted from '
                'this run; raise max_nfev and re-run if esd is required'
                % (plan.get("max_nfev", 8000), len(varying)))
        return rows, flags

    _hrows, _hflags = _health(r.params)
    res["health"] = {
        "n_varying": int(nvar),
        "flags": _hflags,
        "params": _hrows,
        "note": ('Judgment packet: a parameter pinned at a bound, or with no/huge stderr, is '
                 'NOT a measurement - report it as fixed-by-constraint instead of as a refined '
                 'value. Read this together with the R values, not instead of them.'),
    }
    # Refined-structure dump: the output port (xrd_export_cif with source="result")
    # turns this into a CIF, which is how a manual-engine result is handed over to
    # GSAS-II - the missing half of the relay (gpx -> CIF -> manual already worked,
    # manual -> CIF -> GSAS did not, because result.json carried no structure).
    try:
        st_fin = current_structure(r.params)
        _lp = [float(v) for v in st_fin.lattice.parameters]
        _sites = []
        for _i, _s in enumerate(st_fin):
            # NB: PeriodicSite.occupancy was removed in newer pymatgen, so the
            # total occupancy is summed from the site composition instead.
            _comp = {str(_el): float(_amt) for _el, _amt in _s.species.items()}
            _sites.append({
                "label": (labels[_i] if (labels and _i < len(labels))
                          else str(_s.species_string)),
                "element": str(_s.species_string),
                "composition": _comp,
                "x": float(_s.frac_coords[0]),
                "y": float(_s.frac_coords[1]),
                "z": float(_s.frac_coords[2]),
                "occ": float(sum(_comp.values())),
            })
        res["structure"] = {
            "lattice": {"a": _lp[0], "b": _lp[1], "c": _lp[2],
                        "alpha": _lp[3], "beta": _lp[4], "gamma": _lp[5]},
            "crystal_system": CRYSTAL,
            "debye_waller": {str(k): float(v) for k, v in (dw or {}).items()},
            "source_cif": plan.get("cif"),
            "sites": _sites,
        }
    except Exception as e:
        log("structure dump failed:", e)
        log(traceback.format_exc()[-900:])
    outdir = os.path.dirname(os.path.abspath(outres))
    if want_comp:
        comp_path = os.path.join(outdir, "components.json")
        json.dump({"x": x.tolist(), "yobs": y.tolist(), "ycalc": ym.tolist(),
                   "bg": comps.get("bg",[]).tolist(), "main": comps.get("main",[]).tolist(),
                   "extra": comps.get("extra",[]).tolist()}, open(comp_path, "w"))
        res["components"] = comp_path
        log("components ->", comp_path)
    # LAST LINE OF DEFENCE against non-finite numbers: Python's json module writes
    # Infinity/NaN as the bare tokens `Infinity`/`NaN`, which are NOT valid JSON.
    # The tool layer parses this file with JSON.parse, so one stray inf kills the
    # whole result and the caller only sees "the engine produced nothing"
    # (measured 2026-09-20: a bound of -inf from lmfit reached the file).
    res = _json_safe(res)
    # P0 (0.8.3): atomic write + run identity (ensure_ascii kept at its original
    # value here so this path's bytes do not change).
    write_result(outres, res, plan, ensure_ascii=True, allow_nan=False)
    log("result ->", outres)
    # ---- unified workflow journal (user design 2026-09-19) ------------------
    # APPEND-ONLY, machine-readable, one line per refinement round, in the working
    # directory: <cwd>/xrd_workflow/journal.jsonl. The derived journal.md is
    # REGENERATED from the jsonl (never appended) so the two can never disagree.
    # Written by the ENGINE, not by hand: a journal that depends on the AI
    # remembering to write it is not a journal.
    try:
        wf_dir = os.path.join(os.getcwd(), "xrd_workflow")
        os.makedirs(wf_dir, exist_ok=True)
        jl = os.path.join(wf_dir, "journal.jsonl")
        n_prev = 0
        if os.path.exists(jl):
            with open(jl, "r", encoding="utf-8") as fh:
                n_prev = sum(1 for line in fh if line.strip())
        fs = res.get("fit_status") or {}
        rp = res.get("r_plateau") or {}
        rec = {
            "round": n_prev + 1,
            "engine": "xrd_refine_manual",
            "stage": plan.get("stage_hint") or "(explicit vary)",
            "model": plan.get("model") or "物理",
            "trigger": plan.get("trigger") or "AI 判读",
            "checklist_item": plan.get("checklist_item"),
            "plan_digest": ",".join(str(v) for v in (plan.get("vary") or [])) or "(by stage)",
            "Rwp": res["metrics"]["Rwp"],
            "baseline_Rwp": res["metrics"].get("baseline_Rwp"),
            "fit_status": {
                "success": fs.get("success"),
                "hit_max_nfev": fs.get("hit_max_nfev"),
                "n_at_bound": fs.get("n_at_bound"),
                "flags": fs.get("flags") or [],
            },
            "verdict": ("R 值洼地" if rp.get("is_plateau") else "未到洼地"),
            "why": plan.get("why") or "",
            "artifacts": {"result": outres, "components": res.get("components")},
            "snapshot": plan.get("snapshot"),
            "from": plan.get("from"),
        }
        with open(jl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_json_safe(rec), ensure_ascii=False) + "\n")
        _write_journal_md(jl, os.path.join(wf_dir, "journal.md"))
        log("journal ->", jl)
    except Exception as e:
        log("journal not written (%s)" % e)

if __name__ == "__main__":
    # P1 (0.8.3/0.8.4): a graceful stop is a normal outcome, not a crash — the
    # shared handler writes the structured "stopped" result and exits 97.
    run_guarded_main(main)
