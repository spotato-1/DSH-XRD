#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_phase_screen engine - residual peaks -> candidate-phase scoring.

This is the "which phase explains the leftover peaks?" method that the PB and
layer-oxide campaigns did by hand (extract_residual_engine.py +
score_candidates_engine.py + mp_residual_screen.py + the many *_check.py theory
scripts). It is now one declarative engine:

  {"type":"residuals","gpx":...}          leftover (obs-calc) peaks from a
                                          converged GSAS project
  {"type":"peaks","file":...,"smooth":15} peaks of a raw pattern (no model)
  {"type":"fetch","mp":{"chemsys":...}}   pull candidates from MP / COD
  {"type":"fetch","cod":{"query":...,"elements":[...]}}
  {"type":"screen","candidates":[...]|"dir":...,"tol":0.25}
                                          score every candidate CIF against the
                                          residual peak list

Scoring (same recipe as score_candidates_engine.py): every theoretical
reflection with relative intensity >= min_rel is matched to the nearest residual
peak within `tol` degrees; the match contributes
`rel_intensity * (residual_height / max_residual)`. Candidates are ranked by the
total. Nothing is judged - the ranking is evidence for a phase decision, and a
weak/no-match result is reported as such.
"""
import os
import sys
import json

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard
import glob
import math
import traceback

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import cif_edit as ce
except Exception:  # pragma: no cover - cif_edit is shipped alongside
    ce = None


def log(*a):
    print(*a, flush=True)


def _num(v):
    try:
        f = float(v)
    except Exception:
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _safe(s):
    out = []
    for ch in str(s):
        out.append(ch if (ch.isalnum() or ch in '._-') else '_')
    return ''.join(out).strip('_') or 'x'


def residual_peaks_from_gpx(gpx_path, gsas_root, min_rel=0.03, merge=0.2, top=30):
    sys.path.insert(0, gsas_root)
    from GSASII import GSASIIscriptable as G2sc
    gpx = G2sc.G2Project(gpxfile=gpx_path)
    hist = gpx.histograms()[0]
    x = np.asarray(hist.getdata('X'), dtype=float)
    ycalc = np.asarray(hist.getdata('Ycalc'), dtype=float)
    yobs = np.asarray(hist.getdata('Yobs'), dtype=float)
    resid = yobs - ycalc
    thr = max(1.0, float(np.nanmax(resid)) * float(min_rel))
    cand = []
    for i in range(1, len(resid) - 1):
        if resid[i] > resid[i - 1] and resid[i] >= resid[i + 1] and resid[i] > thr:
            cand.append((float(x[i]), float(resid[i])))
    cand.sort(key=lambda p: -p[1])
    merged = []
    for p in cand:
        if all(abs(p[0] - q[0]) > float(merge) for q in merged):
            merged.append(p)
        if len(merged) >= int(top):
            break
    merged.sort(key=lambda p: p[0])
    return ({'gpx': gpx_path, 'wR': _num(hist.get_wR()),
             'residual_peaks': [{'two_theta': round(p[0], 3), 'residual': round(p[1], 2)}
                                for p in merged]}, x)


def peaks_from_data(path, smooth=15, min_rel=0.03, top=40):
    """Peak picking on a raw pattern (smoothed local maxima)."""
    x, y = _two_col(path)
    w = max(3, int(smooth) | 1)
    ker = np.ones(w) / w
    ys = np.convolve(y, ker, mode='same')
    thr = float(np.nanmax(ys)) * float(min_rel)
    cand = []
    for i in range(1, len(ys) - 1):
        if ys[i] > ys[i - 1] and ys[i] >= ys[i + 1] and ys[i] > thr:
            cand.append((float(x[i]), float(ys[i])))
    cand.sort(key=lambda p: -p[1])
    merged = []
    for p in cand:
        if all(abs(p[0] - q[0]) > 0.25 for q in merged):
            merged.append(p)
        if len(merged) >= int(top):
            break
    merged.sort(key=lambda p: p[0])
    return {'file': path, 'n_points': int(len(x)),
            'peaks': [{'two_theta': round(p[0], 3), 'intensity': round(p[1], 1)}
                      for p in merged]}


def _two_col(path):
    """Tolerant 2-column reader (also handles Rigaku '[Data]' blocks)."""
    xs, ys = [], []
    in_data = False
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if s.startswith('[Data]'):
                in_data = True
                continue
            if not s or s[0] in '#!;':
                continue
            if s.startswith('['):
                if xs:
                    break
                continue
            parts = s.replace(',', ' ').split()
            if len(parts) < 2:
                continue
            try:
                xs.append(float(parts[0]))
                ys.append(float(parts[1]))
            except ValueError:
                continue
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def score_candidates(cif_paths, resid_peaks, wavelength=1.54056, tol=0.25,
                     min_rel=0.02, elements=None):
    from pymatgen.io.cif import CifParser
    from pymatgen.analysis.diffraction.xrd import XRDCalculator
    calc = XRDCalculator(wavelength=float(wavelength))
    rx = np.asarray([p['two_theta'] for p in resid_peaks], dtype=float)
    rw = np.asarray([p['residual'] for p in resid_peaks], dtype=float)
    rmax = float(np.max(rw)) if len(rw) else 1.0
    out = []
    for cif in cif_paths:
        try:
            st = CifParser(cif).parse_structures(primitive=False)[0]
        except Exception as e:
            out.append({'cif': cif, 'error': str(e), 'score': 0.0, 'n_matches': 0})
            continue
        if elements:
            formula = str(st.composition.reduced_formula)
            if not all(el in formula for el in elements):
                continue
        try:
            pat = calc.get_pattern(st, two_theta_range=(float(rx.min()) - 2,
                                                        float(rx.max()) + 2))
        except Exception as e:
            out.append({'cif': cif, 'error': 'pattern failed: %s' % e,
                        'score': 0.0, 'n_matches': 0})
            continue
        xp = np.asarray(pat.x, dtype=float)
        ip = np.asarray(pat.y, dtype=float)
        imax = float(ip.max()) if ip.size and ip.max() > 0 else 1.0
        matches = []
        score = 0.0
        for t, inten in zip(xp, ip):
            rel = float(inten) / imax
            if rel < float(min_rel) or rx.size == 0:
                continue
            d = np.abs(rx - t)
            j = int(np.argmin(d))
            if d[j] <= float(tol):
                w = min(1.0, rel) * (float(rw[j]) / rmax)
                matches.append({'pred_2theta': round(float(t), 3),
                                'pred_rel_intensity': round(rel, 3),
                                'residual_2theta': round(float(rx[j]), 3),
                                'residual_height': round(float(rw[j]), 1),
                                'delta': round(float(d[j]), 3)})
                score += w
        out.append({
            'cif': cif,
            'formula': str(st.composition.reduced_formula),
            'space_group': ce._sg_of(st) if ce is not None else '',
            'lattice': [_num(st.lattice.a), _num(st.lattice.b), _num(st.lattice.c),
                        _num(st.lattice.alpha), _num(st.lattice.beta), _num(st.lattice.gamma)],
            'score': round(float(score), 3),
            'n_matches': len(matches),
            'matches': matches,
        })
    out.sort(key=lambda s: -s.get('score', 0.0))
    return out


# ---------------------------------------------------------------------------
def resolve_gsas_root(plan_gsas, action, gsas_env):
    """The GSAS-II root, resolved LAZILY (0.8.5).

    It used to be demanded before ANY action ran, so `peaks` — a pure numpy peak
    picker that never opens GSAS — failed with "GSAS-II root not configured" on a
    machine without GSAS, contradicting its own "raw pattern, no model needed"
    purpose. Only the actions that really read a .gpx ask for it now.
    """
    root = (action or {}).get('gsas_root') or plan_gsas or gsas_env or ''
    if not root:
        raise RuntimeError(
            'GSAS-II root not configured: pass "gsas_root" in the plan, set DSH_GSAS_ROOT, '
            'or add "gsas_root" to xrd.local.json (this action reads a .gpx)')
    return root


def main():
    if len(sys.argv) < 2:
        raise SystemExit('plan JSON required')
    planf = sys.argv[1]
    outres = None
    for i, a in enumerate(sys.argv[2:]):
        if a == '--out':
            outres = sys.argv[3 + i]
    plan = json.load(open(planf, encoding='utf-8'))
    guard = start_guard(plan, backend='phase_screen', out_path=outres)   # P1 (0.8.4)
    outres = outres or os.path.splitext(planf)[0] + '_result.json'
    out_dir = plan.get('out_dir') or os.path.dirname(os.path.abspath(outres))
    os.makedirs(out_dir, exist_ok=True)
    ref_dir = os.path.join(out_dir, 'ref')
    gsas_env = os.environ.get('DSH_GSAS_ROOT') or ''
    plan_gsas = plan.get('gsas_root') or ''

    warnings = []
    actions_log = []
    residuals = None
    candidates = []
    failed_actions = []
    result = {'ok': False}
    try:
        acts = plan.get('actions') or []
        if not acts:
            raise RuntimeError('need a non-empty "actions" list')
        for i, a in enumerate(acts):
            atype = str(a.get('type') or '')
            rec = {'action': atype}
            # Stop at a safe point between actions (0.8.5); EngineStopped is a
            # BaseException, so the broad handler below cannot swallow the request.
            _g = active_guard()
            if _g is not None:
                _g.checkpoint('action %s' % atype)
            try:
                if atype == 'residuals':
                    data, _x = residual_peaks_from_gpx(
                        a['gpx'], resolve_gsas_root(plan_gsas, a, gsas_env),
                        a.get('min_rel', 0.03), a.get('merge', 0.2), a.get('top', 30))
                    residuals = data
                    rec['detail'] = '%d residual peak(s) (wR=%s)' % (
                        len(data['residual_peaks']), data.get('wR'))
                elif atype == 'peaks':
                    data = peaks_from_data(a['file'], a.get('smooth', 15),
                                           a.get('min_rel', 0.03), a.get('top', 40))
                    residuals = {'file': a['file'],
                                 'residual_peaks': [{'two_theta': p['two_theta'],
                                                     'residual': p['intensity']}
                                                    for p in data['peaks']]}
                    rec['detail'] = '%d peak(s) picked from raw pattern' % len(data['peaks'])
                elif atype == 'fetch':
                    os.makedirs(ref_dir, exist_ok=True)
                    hits = []
                    # A fetch that cannot run must FAIL, not report "0 candidates"
                    # (0.8.6): engine/cif_edit.py carries the MP/COD fetchers, and when
                    # its import failed this branch silently returned an empty hit list
                    # with ok:true — the third review's finding.
                    if ce is None:
                        raise RuntimeError(
                            'the candidate fetchers are unavailable: engine/cif_edit.py could not be '
                            'imported (it needs the same site-packages as the other engines), so the '
                            'fetch action cannot run')
                    if a.get('mp') and ce is not None:
                        spec = dict(a['mp'])
                        h, _f, w = ce.fetch_mp(spec, spec.get('max', 10), ref_dir)
                        hits += h
                        warnings.extend(w)
                    if a.get('cod') and ce is not None:
                        spec = dict(a['cod'])
                        h, _f, w = ce.fetch_cod(spec.get('query'), spec.get('max', 10),
                                                ref_dir, spec.get('elements'))
                        hits += h
                        warnings.extend(w)
                    for h in hits:
                        candidates.append(h['path'])
                    rec['detail'] = 'fetched %d candidate(s)' % len(hits)
                elif atype == 'screen':
                    cifs = list(a.get('candidates') or [])
                    if a.get('dir'):
                        cifs += sorted(glob.glob(os.path.join(a['dir'], '*.cif')))
                    if not cifs and candidates:
                        cifs = list(candidates)
                    if not cifs:
                        raise RuntimeError('screen needs "candidates", "dir" or a prior fetch')
                    rp = a.get('residuals')
                    if rp is None:
                        if a.get('gpx'):
                            d, _x = residual_peaks_from_gpx(
                                a['gpx'], resolve_gsas_root(plan_gsas, a, gsas_env),
                                a.get('min_rel', 0.03), a.get('merge', 0.2), a.get('top', 30))
                            rp = d['residual_peaks']
                            residuals = d
                        elif residuals:
                            rp = residuals['residual_peaks']
                    scored = score_candidates(cifs, rp, a.get('wavelength', 1.54056),
                                              a.get('tol', 0.25), a.get('min_rel', 0.02),
                                              a.get('elements'))
                    for s in scored:
                        if s.get('error'):
                            warnings.append('%s: %s' % (os.path.basename(s['cif']), s['error']))
                    rec['detail'] = 'scored %d candidate(s); best=%s' % (
                        len(scored),
                        ('%.3f (%s)' % (scored[0]['score'], scored[0].get('formula')))
                        if scored else 'none')
                    rec['ranked'] = [{'cif': s['cif'], 'formula': s.get('formula'),
                                      'score': s.get('score'), 'n_matches': s.get('n_matches')}
                                     for s in scored[:20]]
                    result['candidates'] = scored
                    if residuals:
                        result['residual_peaks'] = residuals['residual_peaks']
                else:
                    # An UNKNOWN action is a FAILURE, not a warning (0.8.6): a typo
                    # used to leave ok:true with nothing done and only an "ignored"
                    # note, so a batch that did nothing could be read as a success.
                    msg = 'unknown action type: %s' % atype
                    rec['detail'] = 'unknown action IGNORED'
                    rec['error'] = msg
                    warnings.append(msg)
                    failed_actions.append({'index': i, 'type': atype, 'error': msg})
            except Exception as e:
                rec['error'] = str(e)
                warnings.append('action %s failed: %s' % (atype, e))
                failed_actions.append({'index': i, 'type': atype, 'error': str(e)})
                log(traceback.format_exc()[-1000:])
            actions_log.append(rec)

        result.update({
            'ok': not failed_actions,
            'actions': actions_log,
            'warning': warnings,
            'summary': 'xrd_phase_screen: %d action(s), %d candidate(s) scored%s' % (
                len(actions_log), len(result.get('candidates') or []),
                ('' if not failed_actions else '; %d action(s) FAILED' % len(failed_actions))),
        })
        if failed_actions:
            result['failed_actions'] = failed_actions
            # partial means SOME action succeeded (0.8.6): the old test asked whether
            # any artifact existed, so a successful peak pick followed by a failed
            # screening step reported partial=false — the third review's finding.
            result['partial'] = (len(actions_log) - len(failed_actions)) > 0
            result['error'] = ('%d of %d action(s) failed: %s' % (
                len(failed_actions), len(actions_log),
                '; '.join('%s(#%d): %s' % (f['type'], f['index'], f['error'])
                          for f in failed_actions[:5])))
        if residuals and 'residual_peaks' not in result:
            result['residual_peaks'] = residuals['residual_peaks']
        log('PHASE_SCREEN_OK')
    except Exception as e:
        tb = traceback.format_exc()
        log('PHASE_SCREEN_FAILED:', e)
        log(tb[-2000:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-1500:]}
        if warnings:
            result['warning'] = warnings

    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


if __name__ == '__main__':
    run_guarded_main(main)
