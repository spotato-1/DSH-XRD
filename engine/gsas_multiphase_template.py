#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gsas_multiphase_template — MULTIPHASE (>=3 phases) GSAS-II refinement driver.

THIS IS AN AI-EDITABLE TEMPLATE. The canonical driver for 1-2 phases is
`gsas_engine.py` (xrd_refine_gsas). This file exists because the multiphase case
needs a place where the AI can write orchestration that no fixed ladder can
express (phase-fraction couplings, per-phase profile models, phase-specific
constraints, adding/removing phases mid-run).

WORKFLOW CONTRACT (user, 2026-10-02) — do not skip it:
  1. the TWO most abundant phases are settled with the BOUNDED MANUAL engine
     first (xrd_refine_manual + its `second` block);
  2. that result is handed over (`"handoff": "<manual result_*.json>"`), and only
     then does the remaining phase chemistry happen here;
  3. everything else in the project keeps the delivery rules of the workflow
     (GSAS-II numbers are the delivered ones; physical limits at delivery).
  Consequently a FRESH build (no .gpx, no handoff) is REFUSED: a 3+ phase fit
  started from several unrelated CIFs is exactly the "too many free parameters at
  once" failure this entry is meant to avoid. Pass `gpx` to continue an existing
  project, or `handoff` (plus `cifs`) to build one from the manual solution.

HARD GATE: >=3 phases. 1-2 phases must go through xrd_refine_gsas.
  The gate is enforced twice: by the tool layer when it already knows the phase
  count, and HERE at run time (that is the one that cannot be bypassed by an
  AI-edited copy of this file).

EDITABLE SECTIONS are marked `# == EDITABLE ==`. The I/O protocol (plan JSON in,
result JSON out, the gates) should stay intact: the tool layer parses this file's
result and reports `ok/error/ladder` to the caller.

Usage: python gsas_multiphase_template.py <plan.json> --out <result.json>
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from result_io import write_result
except Exception:                                    # pragma: no cover
    def write_result(path, payload, plan=None, **kw):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)

try:
    from proc_guard import run_guarded_main
except Exception:                                    # pragma: no cover
    def run_guarded_main(fn, **kw):
        return fn()

MIN_PHASES = 3          # == EDITABLE == hard gate; 1-2 phases => xrd_refine_gsas
LOG = []


def log(msg):
    LOG.append(str(msg))
    print(msg, flush=True)


# --------------------------------------------------------------------------- handoff
def _param_lookup(params):
    """Manual-engine result "params" -> {name: value} (dict or health-row list)."""
    out = {}
    if isinstance(params, dict):
        for k, v in params.items():
            out[str(k)] = v.get("value") if isinstance(v, dict) else v
    elif isinstance(params, list):
        for row in params:
            if isinstance(row, dict) and "name" in row:
                out[str(row["name"])] = row.get("value")
    return out


def seed_from_manual(gpx, path):
    """Seed phase ratio / main cell / instrument params from a manual result.

    Kept local ON PURPOSE: this file must keep working after the AI copies and
    edits it, so it may not import gsas_engine (a copied template sits alone).
    The canonical implementation with the full provenance report lives in
    gsas_engine.apply_handoff().
    """
    info = {"source": path, "transferred": [], "not_transferred": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            man = json.load(fh)
    except Exception as e:
        info["not_transferred"].append("handoff unreadable: %s" % e)
        return info
    pm = _param_lookup(man.get("params"))
    phs = gpx.phases()
    main_scale = pm.get("scale")
    if len(phs) > 1 and main_scale:
        for i, ph in enumerate(phs[1:]):
            s2 = pm.get("s2_%d_scale" % i)
            if s2 is None:
                info["not_transferred"].append("%s: manual result has no s2_%d_scale" % (ph.name, i))
                continue
            try:
                ratio = float(s2) / float(main_scale)
                for hn in (ph.data.get("Histograms") or {}):
                    ph.data["Histograms"][hn]["Scale"][0] = ratio
                info["transferred"].append("%s Scale=%.6g" % (ph.name, ratio))
            except Exception as e:
                info["not_transferred"].append("%s Scale: %s" % (ph.name, e))
    lat = ((man.get("structure") or {}).get("lattice") or {})
    if lat:
        try:
            cell = phs[0].data["General"]["Cell"]
            for j, k in enumerate(("a", "b", "c", "alpha", "beta", "gamma")):
                if lat.get(k) is not None:
                    cell[j + 1] = float(lat[k])
            info["transferred"].append("main cell")
        except Exception as e:
            info["not_transferred"].append("main cell: %s" % e)
    moved = []
    try:
        # Same three traps as gsas_engine.apply_handoff (measured 2026-10-02):
        # 'Instrument Parameters' is a LIST of per-bank dicts (a bare `key in ip`
        # never matches), the VALUE lives at index 1 of an entry
        # ([reference, value, refine-flag] - GSASIIstrIO reads inst['Lam'][1]),
        # and the units are mixed inside GSAS-II: the manual engine speaks DEGREES,
        # while GSAS-II wants U/V/W in CENTIDEGREES^2 (x1e4), X/Y in centidegrees
        # (x1e2) but Zero in DEGREES (x1, see G2lat.Dsp2pos).
        _ip_raw = gpx.histograms()[0].data.get("Instrument Parameters") or []
        ip = _ip_raw[0] if isinstance(_ip_raw, list) and _ip_raw else (
            _ip_raw if isinstance(_ip_raw, dict) else {})
        for key, fac, src in (("U", 1e4, "U"), ("V", 1e4, "V"), ("W", 1e4, "W"),
                              ("X", 1e2, "X"), ("Y", 1e2, "Y"), ("Zero", 1.0, "zero")):
            if pm.get(src) is None or key not in ip:
                continue
            try:
                val = float(pm[src]) * fac
            except Exception:
                continue
            if not (val == val) or abs(val) > 1e6:
                continue
            ent = ip[key]
            # Plausibility guard, IDENTICAL to gsas_engine.apply_handoff (measured
            # 2026-10-02): a manual value an order of magnitude away from the
            # calibration-derived one is a broken fit, and transferring it took a
            # real two-phase run from wR 25.53% to 71.82%. The instrument file wins.
            old = None
            if isinstance(ent, (list, tuple)) and len(ent) >= 2:
                try:
                    old = float(ent[1])
                except Exception:
                    old = None
            if old not in (None, 0) and abs(val) > 1e-9:
                big, small = max(abs(val), abs(old)), min(abs(val), abs(old))
                if small > 0 and big / small > 10:
                    info.setdefault("warnings", []).append(
                        "instrument '%s': manual %.4g -> %.4g is %.0fx the value this "
                        "project already carries (%.4g); REFUSED"
                        % (key, float(pm[src]), val, big / small, old))
                    info["not_transferred"].append("instrument %s: implausible seed (%.0fx)"
                                                   % (key, big / small))
                    log("handoff WARNING: instrument %s refused (%.0fx)" % (key, big / small))
                    continue
            if isinstance(ent, list) and len(ent) >= 2:
                ent[1] = val
            else:
                ent[0] = val
            moved.append("%s=%.4g(x%g)" % (key, val, fac))
    except Exception:
        pass
    if moved:
        info["transferred"].append("instrument " + ", ".join(moved))
    info["not_transferred"].append("Size/Mustrain + background + atom coordinates: never copied")
    return info


# --------------------------------------------------------------------------- ladder
def build_steps(gpx, plan):
    """One GSAS step per (family, phase).

    # == EDITABLE == this is the place to write multiphase orchestration the fixed
    ladder cannot express - phase-fraction couplings, one shared profile for two
    phases, a phase added halfway, D11/D22/D33 only on the minority phase, ...
    """
    refine = plan.get("refine") or ["background", "cell", "profile", "scale"]
    pspecs = plan.get("phases") or []
    nback = int(plan.get("n_back") or 6)
    inst = plan.get("inst") or []
    steps = []

    def spec(i, key, default=None):
        if i < len(pspecs) and isinstance(pspecs[i], dict) and key in pspecs[i]:
            return pspecs[i][key]
        return default

    if "background" in refine:
        steps.append(("background", {"set": {"Background": {"no. coeffs": nback, "refine": True}}}))
    if "inst" in refine and inst:
        steps.append(("inst", {"set": {"Instrument Parameters": list(inst)}}))
    for i, ph in enumerate(gpx.phases()):
        if "cell" in refine and spec(i, "cell", True):
            steps.append(("cell:" + ph.name, {"phases": [ph.name], "set": {"Cell": True}}))
        if "scale" in refine and spec(i, "scale", True):
            steps.append(("scale:" + ph.name, {"phases": [ph.name], "set": {"Scale": True}}))
        if "profile" in refine:
            size = spec(i, "size", True)
            must = spec(i, "mustrain", None)
            st = {}
            if size:
                st["Size"] = {"type": "isotropic", "refine": True}
            if must:
                st["Mustrain"] = {"type": must if isinstance(must, str) else "isotropic",
                                  "refine": True}
            if st:
                steps.append(("profile:" + ph.name, {"phases": [ph.name], "set": st}))
        if "atoms" in refine:
            flags = spec(i, "atoms", None)
            if flags:
                steps.append(("atoms:" + ph.name,
                              {"phases": [ph.name], "set": {"Atoms": {"all": flags}}}))
    return steps


def run(plan):
    # GSAS-II bootstrap — MUST stay identical in spirit to gsas_engine.py: the
    # interpreter that runs the engines is a plain CPython and GSAS-II is NOT on
    # its sys.path, so the root has to be resolved and inserted here. This was
    # missing in the first cut of this template, and the smoke test of 2026-10-02
    # showed the consequence in full: `ModuleNotFoundError: No module named
    # 'GSASII'` for every call, i.e. the entry was dead on arrival even though the
    # tool layer passes gsas_root in the plan.
    gsas_root = plan.get("gsas_root") or os.environ.get("DSH_GSAS_ROOT") or ""
    if not gsas_root:
        return {"ok": False,
                "error": ('GSAS-II root not configured: pass "gsas_root" in the plan, '
                          'set DSH_GSAS_ROOT, or add "gsas_root" to xrd.local.json')}
    if not os.path.exists(os.path.join(gsas_root, "GSASII", "GSASIIscriptable.py")):
        return {"ok": False,
                "error": "gsas_root missing GSASII package: %s" % gsas_root}
    if gsas_root not in sys.path:
        sys.path.insert(0, gsas_root)

    from GSASII import GSASIIscriptable as G2sc

    out_dir = plan.get("out_dir") or os.getcwd()
    gpx_in = plan.get("gpx")
    cifs = [c for c in (plan.get("cifs") or []) if c and os.path.exists(c)]
    data = plan.get("data") or {}
    proj = os.path.join(out_dir, "gsas_multiphase.gpx")
    proj_used = proj
    reopened = bool(gpx_in and os.path.exists(gpx_in))

    if reopened:
        gpx = G2sc.G2Project(gpxfile=gpx_in)
        proj_used = os.path.join(out_dir, "gsas_multiphase_continued.gpx")
        try:
            gpx.filename = proj_used
        except Exception:
            pass
    else:
        if not plan.get("handoff"):
            # WORKFLOW GATE (user 2026-10-02): for a 3+ phase fit, the two dominant
            # phases must already be settled by the bounded manual engine.
            return {"ok": False,
                    "error": ("multiphase entry needs a settled starting point: pass "
                              "gpx=<existing project> to continue, or handoff=<manual "
                              "engine result_*.json> after refining the TWO most "
                              "abundant phases with xrd_refine_manual. Refusing to "
                              "build a %d-phase fit from scratch." % len(cifs))}
        if len(cifs) < MIN_PHASES:
            return {"ok": False,
                    "error": ("this entry is for >=%d phases; got %d CIF(s). 1-2 phases "
                              "must go through xrd_refine_gsas." % (MIN_PHASES, len(cifs)))}
        gpx = G2sc.G2Project(newgpx=proj)
        if data.get("file") and os.path.exists(data["file"]):
            gpx.add_powder_histogram(data["file"], plan.get("instprm"), fmthint=data.get("fmthint"))
        hists = gpx.histograms()
        for c in cifs:
            gpx.add_phase(c, fmthint="CIF", histograms=hists)
        for h in hists:
            for p in gpx.phases():
                try:
                    if h.name not in (p.data.get("Histograms") or {}):
                        gpx.link_histogram_phase(h.name, p)
                except Exception as e:
                    log("link skipped %s<->%s: %s" % (h.name, p.name, e))

    # ---- HARD GATE, run-time copy (cannot be bypassed by an edited template) ----
    n_ph = len(gpx.phases())
    if n_ph < MIN_PHASES:
        return {"ok": False,
                "error": ("multiphase gate: the project carries %d phase(s); this entry "
                          "is for >=%d. Use xrd_refine_gsas for 1-2 phases." % (n_ph, MIN_PHASES))}

    handoff = None
    if plan.get("handoff") and os.path.exists(plan["handoff"]):
        handoff = seed_from_manual(gpx, plan["handoff"])
        log("handoff: %s" % handoff)

    maxcyc = int(plan.get("max_cycles") or 8)
    try:
        gpx.data["Controls"]["data"]["max cyc"] = maxcyc
    except Exception:
        pass

    steps = build_steps(gpx, plan)
    tol = float(plan.get("rollback_tol") or 0.0)

    def wr():
        vals = []
        for h in gpx.histograms():
            try:
                v = float(h.get_wR())
                if v == v:
                    vals.append(v)
            except Exception:
                pass
        return round(sum(vals) / len(vals), 6) if vals else None

    class _Tap(object):
        """Tee stdout/stderr into a buffer while letting everything through.

        GSAS-II reports a failed refinement on stdout ("Invalid cell metric tensor
        for phase #1 (...)") and then RETURNS NORMALLY, leaving the phase parameters
        partially updated. Without this tap such a step is indistinguishable from a
        good one, and acceptance is decided on wR alone - so a broken state could be
        crowned "best". (Same detector as gsas_engine.py, 0.8.10.)
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

    # GSAS-II's refine() is FILE-BASED (G2Project.refine -> GSASIIstrMain.Refine
    # (self.filename) + index_ids() auto-save), and `gpx.save(other_path)` REBINDS
    # self.filename - so a snapshot written with save() becomes the WORKING file and
    # the next refinement overwrites it. That made every "rolled_back" step restore
    # the state it was supposed to undo (measured in gsas_engine.py, 2026-10-02).
    # Snapshots are copied, and self.filename stays pinned to proj_used.
    ERR_MARKERS = ("invalid cell metric tensor", "refinement failed",
                   "matrix singular", "singular matrix", "****error")
    gpx.save(proj_used)              # materialise the working file + pin filename
    ladder = []
    snap = os.path.join(out_dir, ".xrd-multiphase-snap.gpx")
    best_snap = os.path.join(out_dir, ".xrd-multiphase-best.gpx")
    best = None
    state_is_best = True
    warn = []

    def keep_best(tag):
        try:
            gpx.save(proj_used)
            shutil.copyfile(proj_used, best_snap)
        except Exception as e:
            log("best-state snapshot failed (%s): %s" % (tag, e))

    for si, (label, st) in enumerate(steps, 1):
        before = wr()
        snapped = False
        try:
            gpx.save(proj_used)
            shutil.copyfile(proj_used, snap)
            snapped = True
        except Exception as e:
            log("snapshot failed: %s" % e)
        tap = _Tap()
        failed = None
        try:
            with tap:
                gpx.do_refinements([st])
        except Exception as e:
            failed = "do_refinements raised: %s" % e
        if failed is None:
            failed = tap.first_error(ERR_MARKERS)
        after = wr()
        rec = {"step": si, "label": label}
        if before is not None:
            rec["wR_before"] = before
        if after is not None:
            rec["wR_after"] = after
        if failed:
            rec["gsas_error"] = failed
            log("step %d/%d %s: GSAS-II reported a failure: %s" % (si, len(steps), label, failed))
        if best is None and not failed:
            best = after
            rec["accepted"] = True
            rec["note"] = "first step"
            state_is_best = True
            keep_best("step %d" % si)
        elif failed or after is None or after > best + tol:
            rec["accepted"] = False
            rec["rolled_back"] = False
            why = ("GSAS-II refinement failed" if failed
                   else "wR %s worse than best %s" % (after, best))
            if snapped:
                try:
                    gpx = G2sc.G2Project(gpxfile=snap)
                    gpx.filename = proj_used
                    gpx.save(proj_used)      # rolled-back state = working file
                    rec["rolled_back"] = True
                    rec["note"] = "%s - state restored" % why
                    state_is_best = True
                except Exception as e:
                    rec["note"] = "rollback FAILED (worse state kept): %s" % e
                    state_is_best = False
                    warn.append("step %d (%s): rollback failed - %s" % (si, label, e))
            else:
                rec["note"] = "%s - kept (rollback off)" % why
                state_is_best = False
                if failed:
                    warn.append("step %d (%s) FAILED inside GSAS-II and was kept: %s"
                                % (si, label, failed))
        else:
            rec["accepted"] = True
            if after < best:
                best = after
            state_is_best = True
            keep_best("step %d" % si)
        ladder.append(rec)
        log("step %d/%d %s: %s -> %s accepted=%s" % (si, len(steps), label, before, after,
                                                     rec.get("accepted")))

    # deliver the BEST accepted state, not merely the last one (see gsas_engine.py)
    if not state_is_best and best is not None:
        try:
            gpx = G2sc.G2Project(gpxfile=best_snap)
            gpx.filename = proj_used
            gpx.save(proj_used)
            state_is_best = True
            log("ladder: delivered state RESTORED to the best accepted step (wR %s)" % best)
        except Exception as e:
            warn.append("could not restore the best accepted state: %s" % e)
            log("ladder: best-state restore FAILED: %s" % e)

    gpx.save(proj_used)
    hists_out = []
    for h in gpx.histograms():
        e = {"name": str(getattr(h, "name", ""))}
        try:
            e["wR"] = float(h.get_wR())
        except Exception:
            pass
        hists_out.append(e)
    cells = []
    for p in gpx.phases():
        e = {"name": str(p.name)}
        try:
            c = p.get_cell()
            e["cell"] = [float(c[k]) for k in ("length_a", "length_b", "length_c",
                                               "angle_alpha", "angle_beta", "angle_gamma",
                                               "volume") if k in c]
        except Exception as ex:
            e["cell_error"] = str(ex)
        cells.append(e)
    n_acc = sum(1 for r in ladder if r.get("accepted"))
    # Divergence sentinel, same threshold as gsas_engine's C3 check: wR near 100%
    # means the fit is meaningless however clean the ladder looks (a >=3-phase
    # project can improve wR at every step and still sit at 60-80% when a phase in
    # the model does not belong to the sample — seen in the machinery smoke test).
    for _h in hists_out:
        _w = _h.get("wR")
        if _w is not None and _w >= 99.0:
            warn.append("%s: wR=%.1f%% (~100%% = not converged)" % (_h.get("name"), _w))
    summary = ("multiphase refinement: %d phase(s), %d step(s), %d accepted"
               % (n_ph, len(steps), n_acc))
    if n_acc == 0:
        # honest: with rollback in force a run that accepted nothing delivers exactly
        # the state it entered with - it must not read as "the families were refined"
        summary += " — 0 step(s) accepted: the delivered project is the state it entered with"
    out = {"ok": not warn,
           "phases_count": n_ph,
           "histograms": hists_out,
           "phases": cells,
           "ladder": ladder,
           "gpx": proj_used,
           "summary": summary}
    if warn:
        out["warning"] = warn
    if handoff:
        out["handoff"] = handoff
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    with open(a.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    try:
        res = run(plan)
    except Exception as e:
        import traceback
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
               "log_tail": traceback.format_exc()[-2000:]}
    res["log_tail"] = ("\n".join(LOG[-200:]) + "\n" + (res.get("log_tail") or ""))[-4000:]
    write_result(a.out, res, plan, allow_nan=False)
    print("RESULT:", json.dumps({k: v for k, v in res.items() if k != "log_tail"})[:2000])


if __name__ == "__main__":
    # NOTE: proc_guard.run_guarded_main(main_fn) takes ONE argument — the local
    # fallback shim below accepts **kw, which is exactly why a `backend=` keyword
    # here survived review: the shim swallowed it and the real module (imported on
    # any machine that has the plugin's engine/ dir on sys.path) raised
    # "TypeError: run_guarded_main() got an unexpected keyword argument 'backend'"
    # before a single line of the fit ran (found by the 0.8.10 smoke test,
    # 2026-10-02).
    run_guarded_main(main)
