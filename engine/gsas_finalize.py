#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_finalize engine - package a CONVERGED GSAS-II project into deliverables.

Protocol: plan JSON in (argv[1]), result JSON out (--out <file>), same shape as
gsas_engine.py / xrd_engine.py.

Deliverables written into <out_dir>/<sample>_final/:
  <sample>_final.gpx     clean working project (stale reflection lists pruned)
  <sample>_final.lst     refinement log (copied when a same-stem .lst exists)
  <sample>_<phase>.cif   GSAS-generated CIF per phase (G2Phase.export_CIF)
  <sample>_fit.png       fit figure: obs / calc / background / per-phase
                         component + residual panel + per-phase reflection
                         ticks, with automatic occlusion avoidance
  <sample>_report.md     metrics / phases / atoms / strong-reflection tables
  <sample>_finalize.json machine-readable manifest

This engine NEVER refines and NEVER edits structural parameters.  Uiso /
degeneracy repair is a planned FUTURE feature and is deliberately not done
here (user decision 2026-09).

All numbers returned are finite floats or omitted (host output schema rejects
null/NaN for declared non-null types).
"""
import os
import sys
import json

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard
import math
import re
import shutil
import datetime
import traceback

import numpy as np


def log(*a):
    print(*a, flush=True)


def _num(v):
    """float(v) or None when not finite/convertible."""
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
    t = ''.join(out).strip('_')
    return t or 'phase'


def _arr(v):
    """MaskedArray -> plain float array with masked points as NaN."""
    a = np.asarray(v, dtype=float)
    if isinstance(v, np.ma.MaskedArray):
        a = np.ma.filled(v.astype(float), np.nan)
    return np.asarray(a, dtype=float)


# ---------------------------------------------------------------------------
# figure helpers
# ---------------------------------------------------------------------------
def _pick_font(need_cjk):
    """Best available font family; returns (family, ok)."""
    try:
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
    except Exception:
        have = set()
    for cand in ('Microsoft YaHei', 'SimHei', 'SimSun', 'Noto Sans CJK SC',
                 'Source Han Sans SC', 'WenQuanYi Zen Hei'):
        if cand in have:
            return cand, True
    return 'DejaVu Sans', not need_cjk


def _emptiest_corner(x, y, ylo, yhi, exclude=()):
    """Pick the corner of the axes least occupied by data points.

    Occupancy of a corner block = mean normalised height of the data inside it
    (0 = empty block).  A block holding tall peaks near its own top wins the
    "occupied" contest and is avoided.
    """
    corners = ['upper right', 'upper left', 'lower right', 'lower left']
    x0, x1 = float(np.nanmin(x)), float(np.nanmax(x))
    span = max(yhi - ylo, 1e-9)
    w = 0.34 * (x1 - x0)
    best, best_score = None, None
    for c in corners:
        if c in exclude:
            continue
        xa, xb = (x0, x0 + w) if 'left' in c else (x1 - w, x1)
        if 'upper' in c:
            ya, yb = ylo + 0.60 * span, yhi
        else:
            ya, yb = ylo, ylo + 0.40 * span
        m = (x >= xa) & (x <= xb) & np.isfinite(y)
        if not np.any(m):
            score = 0.0
        else:
            yy = y[m]
            inside = np.clip((yy - ya) / max(yb - ya, 1e-9), 0.0, 1.0)
            score = float(np.mean(inside))
        if best_score is None or score < best_score:
            best, best_score = c, score
    return best or 'upper right'


def _top_reflections(pos, icorr, xmin, xmax, topn):
    """Indices of the strongest reflections inside the plotted range."""
    m = np.isfinite(pos) & (pos >= xmin) & (pos <= xmax)
    idx = np.where(m)[0]
    if idx.size == 0:
        return []
    order = idx[np.argsort(-np.nan_to_num(icorr[idx], nan=0.0))]
    return list(order[:topn])


def _hkl_str(h, k, l):
    def f(v):
        iv = int(round(float(v)))
        return f'{iv}' if iv >= 0 else f'-{abs(iv)}'
    return f'({f(h)}{f(k)}{f(l)})'


# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        raise SystemExit('plan JSON required')
    planf = sys.argv[1]
    outres = None
    for i, a in enumerate(sys.argv[2:]):
        if a == '--out':
            outres = sys.argv[3 + i]
    plan = json.load(open(planf, encoding='utf-8'))
    guard = start_guard(plan, backend='finalize', out_path=outres)   # P1 (0.8.4)
    outres = outres or os.path.splitext(planf)[0] + '_result.json'
    outdir = os.path.dirname(os.path.abspath(outres))
    os.makedirs(outdir, exist_ok=True)

    gsas_root = plan.get('gsas_root') or os.environ.get('DSH_GSAS_ROOT') or ''
    if not gsas_root:
        raise RuntimeError(
            'GSAS-II root not configured: pass "gsas_root" in the plan, set DSH_GSAS_ROOT, '
            'or add "gsas_root" to xrd.local.json')
    sys.path.insert(0, gsas_root)
    warnings = []
    result = {'ok': False}
    # delivery-status flags (audit 2026-09-21): a package that cannot be reopened,
    # that lost phases in the copy, or whose CIF export failed, is NOT a finished
    # deliverable — the artifacts are still written and reported, but ok must say so.
    reopen_failed = False
    cif_failed = False
    diverged_pkg = False
    try:
        gpx_src = plan['gpx']
        if not os.path.isfile(gpx_src):
            raise RuntimeError('gpx not found: ' + str(gpx_src))
        if os.path.getsize(gpx_src) <= 0:
            raise RuntimeError('gpx is empty: ' + str(gpx_src))
        sample = plan.get('sample') or os.path.splitext(os.path.basename(gpx_src))[0]
        sample = _safe(sample)
        pkg = os.path.join(outdir, sample + '_final')
        os.makedirs(pkg, exist_ok=True)

        from GSASII import GSASIIscriptable as G2sc
        # A safe point before the (long, read-only) packaging work (0.8.5): this engine
        # previously had no checkpoint at all, so a stop request could only be answered
        # by a tree kill.
        _g = active_guard()
        if _g is not None:
            _g.checkpoint('finalize %s' % (sample or ''))
        proj = G2sc.G2Project(gpxfile=gpx_src)
        phases = proj.phases()
        hists = proj.histograms()
        if not phases:
            raise RuntimeError('no phases in gpx (nothing to package)')
        if not hists:
            raise RuntimeError('no histograms in gpx (nothing to package)')

        # ---- prune stale reflection lists (phases already deleted) --------
        valid = {p.name for p in phases}
        for h0 in hists:
            rl = h0.data.get('Reflection Lists') or {}
            for k in list(rl.keys()):
                if k not in valid:
                    del rl[k]
                    warnings.append(
                        "pruned stale reflection list '%s' in histogram '%s' "
                        '(phase no longer in project)' % (k, h0.name))

        # ---- deliverable hygiene: stale constraint entries ----------------
        # Constraints built from text variable names are stored by ranId; once
        # atoms are added/removed those rows become garbage and the GSAS GUI
        # raises "ranId not found". The standard deliverable recipe (same one as
        # PB/XRD/mono2phase/clean_gpx.py + rebuild_clean_gpx.py) is to clear the
        # three buckets and then NOT refine - refining after clearing would throw
        # away the cached converged wR.
        cons = (proj.data.get('Constraints') or {}).get('data') or {}
        cons_before = {k: len(cons.get(k) or []) for k in ('Hist', 'HAP', 'Phase')}
        cons_total = sum(cons_before.values())
        clean_cons = plan.get('clean_constraints', True) is not False
        cons_cleared = False
        if cons_total and clean_cons:
            for k in ('Hist', 'HAP', 'Phase'):
                cons[k] = []
            cons_cleared = True
            warnings.append(
                'cleared %d stale constraint entr%s (%s) - do NOT re-refine this '
                'project afterwards (refining would drop the cached converged wR)'
                % (cons_total, 'y' if cons_total == 1 else 'ies',
                   ', '.join('%s=%d' % (k, v) for k, v in cons_before.items())))
        elif cons_total:
            warnings.append('%d constraint entries kept (clean_constraints=false): %s'
                            % (cons_total,
                               ', '.join('%s=%d' % (k, v) for k, v in cons_before.items())))

        # ---- clean working project ---------------------------------------
        clean_gpx = os.path.join(pkg, sample + '_final.gpx')
        if os.path.abspath(clean_gpx) == os.path.abspath(gpx_src):
            raise RuntimeError('refusing to overwrite the source gpx in place')
        proj.save(clean_gpx)
        if not (os.path.isfile(clean_gpx) and os.path.getsize(clean_gpx) > 0):
            raise RuntimeError('failed to write clean gpx: ' + clean_gpx)

        # GSAS version that wrote the packaged project. A .gpx is a pickle: an
        # older GSAS-II reading a newer file reports "not a current GSAS-II .gpx",
        # and the file must never be renamed or edited as text.
        try:
            gsas_ver = str((proj.data.get('Controls') or {}).get('data', {})
                           .get('LastSavedUsing') or '')
        except Exception:
            gsas_ver = ''

        # ---- delivery validation: reopen the packaged project -------------
        # A project that cannot be reopened, or that lost its cached wR / phases
        # / histograms, is not deliverable.
        reopen = {}
        try:
            chk = G2sc.G2Project(gpxfile=clean_gpx)
            reopen['phases'] = len(chk.phases())
            reopen['histograms'] = len(chk.histograms())
            chk_cons = (chk.data.get('Constraints') or {}).get('data') or {}
            reopen['constraints'] = sum(len(chk_cons.get(k) or [])
                                        for k in ('Hist', 'HAP', 'Phase'))
            if chk.histograms():
                rw = _num(chk.histograms()[0].get_wR())
                if rw is not None:
                    reopen['wR'] = rw
        except Exception as e:
            reopen_failed = True
            warnings.append('packaged gpx failed to reopen: %s' % e)
            reopen['error'] = str(e)
        if 'phases' in reopen and reopen['phases'] != len(phases):
            reopen_failed = True
            warnings.append('packaged gpx holds %d phase(s), the source had %d'
                            % (reopen['phases'], len(phases)))
        if reopen.get('constraints'):
            warnings.append('packaged gpx still holds %d constraint entries'
                            % reopen['constraints'])
        if 'wR' not in reopen and 'error' not in reopen:
            warnings.append('packaged gpx carries no cached wR after reopen')

        h = hists[0]
        if len(hists) > 1:
            warnings.append('project has %d histograms; the package reports all '
                            'but plots the first one (%s)' % (len(hists), h.name))

        # ---- refinement log ----------------------------------------------
        lst_src = plan.get('lst') or os.path.splitext(gpx_src)[0] + '.lst'
        lst_out = None
        lst_metrics = {}
        if os.path.isfile(lst_src):
            lst_out = os.path.join(pkg, sample + '_final.lst')
            shutil.copyfile(lst_src, lst_out)
            try:
                txt = open(lst_src, encoding='utf-8', errors='replace').read()
                # The .lst logs EVERY refinement cycle, so the reported wR must be the
                # LAST one (the final state). `re.search` took the FIRST match — i.e.
                # the oldest, often much better-looking, cycle — and that value then
                # OVERRODE the project's own wR (audit 2026-09-21). Take the last, and
                # say which one it was so the number can be checked.
                ms = re.findall(r'wR\s*=\s*([\d.]+)%\s*,\s*chi\*\*2\s*=\s*([\d.]+)'
                                r'\s*,\s*GOF\s*=\s*([\d.]+)', txt)
                if ms:
                    last = ms[-1]
                    lst_metrics = {'wR': float(last[0]),
                                   'chi2': float(last[1]),
                                   'gof': float(last[2]),
                                   'n_cycles_in_log': len(ms),
                                   'wR_source': 'last of %d cycle(s) in the .lst' % len(ms)}
                    if len(ms) > 1 and float(ms[0][0]) != float(last[0]):
                        warnings.append(
                            'the .lst holds %d refinement cycle(s); the FINAL wR '
                            '(%.2f%%) is used (the first cycle read %.2f%% - taking '
                            'that one would have reported the oldest, best-looking '
                            'value)' % (len(ms), float(last[0]), float(ms[0][0])))
                mfrac = re.findall(r'Phase fraction\s*:\s*([\d.]+)', txt)
                if mfrac:
                    lst_metrics['phase_fraction'] = [float(v) for v in mfrac]
            except Exception as e:
                warnings.append('could not parse refinement log: %s' % e)
        else:
            warnings.append('no refinement log (.lst) found next to the gpx; '
                            'chi**2/GOF are omitted')

        # ---- per-phase CIF (GSAS official exporter) ----------------------
        cif_files = []
        for ph in phases:
            target = os.path.join(pkg, '%s_%s.cif' % (sample, _safe(ph.name)))
            try:
                ph.export_CIF(target)
                if os.path.isfile(target) and os.path.getsize(target) > 0:
                    cif_files.append(target)
                else:
                    cif_failed = True
                    warnings.append('CIF export produced no file for phase %s' % ph.name)
            except Exception as e:
                cif_failed = True
                warnings.append('CIF export failed for phase %s: %s' % (ph.name, e))

        # ---- metrics ------------------------------------------------------
        try:
            resid_d = dict(h.residuals or {})
        except Exception:
            resid_d = {}
        wR = _num(h.get_wR())
        if lst_metrics.get('wR') is not None:
            wR = lst_metrics['wR']
        metrics = {}
        for key, src in (('wR', wR), ('R', resid_d.get('R')), ('Rb', resid_d.get('Rb')),
                         ('wRb', resid_d.get('wRb')), ('wRmin', resid_d.get('wRmin')),
                         ('chi2', lst_metrics.get('chi2')), ('gof', lst_metrics.get('gof'))):
            v = _num(src)
            if v is not None:
                metrics[key] = v
        # honesty guard: an unconverged/diverged project must never be packaged
        # silently as a final deliverable. The final wR can come from the .lst OR
        # from the gpx itself (a crashed refinement writes no R-factor block into
        # the .lst but does store its last wR in the project), so judge the value
        # actually reported rather than one source.
        if metrics.get('wR') is not None and metrics['wR'] >= 50.0:
            diverged_pkg = True
            warnings.append('reported wR = %.2f%% - this project looks '
                            'unconverged/diverged; do not deliver it as a final '
                            'result' % metrics['wR'])

        # ---- phase / atom tables -----------------------------------------
        phase_info = []
        negative_uiso = []          # audit 2026-09-21: the ok verdict ignored these
        for i, ph in enumerate(phases):
            gen = ph.data.get('General') or {}
            sg = gen.get('SGData') or {}
            try:
                cell, sig = ph.get_cell_and_esd()
            except Exception:
                cell, sig = ph.get_cell(), {}
            keys = [k for k in ('length_a', 'length_b', 'length_c', 'angle_alpha',
                                'angle_beta', 'angle_gamma', 'volume') if k in cell]
            cell_v = [_num(cell[k]) for k in keys]
            sig_v = [_num((sig or {}).get(k)) for k in keys]
            cell_v = [v for v in cell_v if v is not None]
            sig_v = [v for v in sig_v if v is not None]
            ptr = gen.get('AtomPtrs') or [3, 1, 7, 9]
            cx, ct, cs, cia = ptr[0], ptr[1], ptr[2], ptr[3]
            atoms = []
            for at in (ph.data.get('Atoms') or []):
                try:
                    uiso = _num(at[cia + 1]) if str(at[cia]) == 'I' else None
                except Exception:
                    uiso = None
                entry = {'label': str(at[ct - 1]), 'element': str(at[ct]),
                         'refine': str(at[ct + 1])}
                # A value that is absent/non-finite is OMITTED: the host output
                # schema rejects null for declared non-null types (null used to
                # make the whole tool result invalid).
                for key, val in (('x', _num(at[cx])), ('y', _num(at[cx + 1])),
                                 ('z', _num(at[cx + 2])), ('occ', _num(at[cx + 3])),
                                 ('uiso', uiso)):
                    if val is not None:
                        entry[key] = val
                if isinstance(at[cs + 1], (int, float)):
                    entry['mult'] = int(at[cs + 1])
                if uiso is not None and uiso < 0:
                    negative_uiso.append({'phase': str(ph.name),
                                          'atom': entry['label'], 'Uiso': uiso})
                atoms.append(entry)
            info = {
                'name': str(ph.name),
                'space_group': str(sg.get('SpGrp') or '').strip(),
                'crystal_system': str(sg.get('SGSys') or '').strip(),
                'cell_keys': keys,
                'cell': cell_v,
            }
            if sig_v and len(sig_v) == len(cell_v):
                info['cell_sigma'] = sig_v
            bragg = _num(resid_d.get('%d:0:Rf' % i))
            if bragg is not None:
                info['bragg_r'] = bragg
            if str(gen.get('Type') or '') != 'nuclear':
                warnings.append('phase %s is not a nuclear phase (%s); CIF export may be unsupported'
                                % (ph.name, gen.get('Type')))
            info['atoms'] = atoms
            phase_info.append(info)

        # ---- histogram arrays --------------------------------------------
        x = _arr(h.getdata('X'))
        yobs = _arr(h.getdata('Yobs'))
        ycalc = _arr(h.getdata('Ycalc'))
        ybkg = _arr(h.getdata('Background'))
        resid = _arr(h.getdata('Residual'))
        xmin, xmax = float(np.nanmin(x)), float(np.nanmax(x))
        if isinstance(plan.get('xlim'), list) and len(plan['xlim']) == 2:
            xmin = float(plan['xlim'][0])
            xmax = float(plan['xlim'][1])

        refl = {}
        try:
            refl = h.reflections() or {}
        except Exception as e:
            warnings.append('reflection lists unavailable: %s' % e)
        phase_ticks = []
        for ph in phases:
            ent = refl.get(ph.name)
            if not isinstance(ent, dict):
                continue
            arr = ent.get('RefList')
            if arr is None:
                continue
            a = np.asarray(arr, dtype=float)
            if a.ndim != 2 or a.shape[1] < 6:
                continue
            pos = a[:, 5]
            icorr = a[:, 11] if a.shape[1] > 11 else np.zeros(a.shape[0])
            keep = np.isfinite(pos) & (pos >= xmin) & (pos <= xmax)
            if not np.any(keep):
                continue
            phase_ticks.append((ph.name, pos[keep], np.nan_to_num(icorr[keep], nan=0.0),
                                a[keep][:, 0:3]))
        if not phase_ticks:
            warnings.append('no reflection lists found; the reflection-tick panel is empty')

        # ---- figure -------------------------------------------------------
        mplcache = os.path.join(pkg, '.mplcache')
        os.makedirs(mplcache, exist_ok=True)
        os.environ['MPLCONFIGDIR'] = mplcache
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.transforms import blended_transform_factory

        title = plan.get('title') or ('%s - GSAS-II Rietveld refinement' % sample)
        formula = plan.get('formula') or ''
        need_cjk = any(ord(c) > 127 for c in (title + formula + sample))
        family, ok_font = _pick_font(need_cjk)
        if need_cjk and not ok_font:
            warnings.append('no CJK font found; non-ASCII characters may not render '
                            'in the figure')
        plt.rcParams['font.family'] = family
        plt.rcParams['axes.unicode_minus'] = False

        lo = float(np.nanmin([np.nanmin(ybkg), np.nanmin(ycalc), np.nanmin(yobs)]))
        hi = float(np.nanmax([np.nanmax(ycalc), np.nanmax(yobs)]))
        span = max(hi - lo, 1e-9)
        ylo, yhi = lo - 0.06 * span, hi + 0.24 * span

        nph = max(1, len(phase_ticks))
        fig = plt.figure(figsize=(10.0, 6.4 + 0.34 * nph))
        gs = fig.add_gridspec(3, 1, height_ratios=[3.2, 1.0, 0.42 * nph], hspace=0.07)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        ax3 = fig.add_subplot(gs[2], sharex=ax1)

        ax1.plot(x, ybkg, '-', color='#2ca02c', lw=1.1, label='Background', zorder=2)
        ax1.plot(x, ycalc - ybkg, '-', color='#d62728', lw=1.0,
                 label='Calculated (phase sum)', zorder=3)
        ax1.plot(x, yobs, '.', color='#1f4fd8', ms=2.1, label='Observed', zorder=4)
        ax1.plot(x, ycalc, '-', color='#111111', lw=0.8, alpha=0.85,
                 label='Calculated (total)', zorder=5)
        ax1.set_ylim(ylo, yhi)
        ax1.set_ylabel('Intensity (counts)')
        ax1.tick_params(labelbottom=False)

        # de-occlusion: legend in the emptiest corner, stats box in the opposite
        clean_y = np.where(np.isfinite(yobs), yobs, np.nan)
        corner = _emptiest_corner(x, clean_y, ylo, yhi)
        ax1.legend(loc=corner, fontsize=8, ncol=2, framealpha=0.92)
        stats_corner = 'upper right' if corner.startswith('upper left') else 'upper left'
        sxa = 0.985 if 'right' in stats_corner else 0.015
        sha = 'right' if 'right' in stats_corner else 'left'
        ylab = 0.975 if 'upper' in stats_corner else 0.03
        vlab = 'top' if 'upper' in stats_corner else 'bottom'

        stats = []
        if metrics.get('wR') is not None:
            stats.append('R$_w$ = %.2f%%' % metrics['wR'])
        for key, lab in (('R', 'R'), ('Rb', 'R$_b$'), ('gof', 'GOF')):
            v = metrics.get(key)
            if v is not None:
                stats.append('%s = %.2f' % (lab, v))
        if formula:
            stats.append(formula)
        if stats:
            lines = ['   '.join(stats[:3])]
            if len(stats) > 3:
                lines.append('   '.join(stats[3:]))
            ax1.text(sxa, ylab, '\n'.join(lines), transform=ax1.transAxes,
                     va=vlab, ha=sha, fontsize=9,
                     bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='0.6',
                               alpha=0.88))

        # strongest-reflection labels, staggered to avoid mutual overlap
        topn = int(plan.get('top_peaks') or 6)
        if plan.get('list_ticks', True) is not False and topn > 0 and phase_ticks:
            pool = []
            for nm, pos, icorr, hkl in phase_ticks:
                for j in _top_reflections(pos, icorr, xmin, xmax, topn):
                    pool.append((float(icorr[j]), float(pos[j]),
                                 _hkl_str(hkl[j][0], hkl[j][1], hkl[j][2])))
            pool.sort(reverse=True)
            placed = []
            for _, p, lbl in pool:
                if len(placed) >= topn:
                    break
                if any(abs(p - q) < 1.2 for q, _ in placed):
                    continue
                sel = (x > p - 0.35) & (x < p + 0.35)
                if not np.any(sel):
                    continue
                yv = float(np.nanmax(ycalc[sel]))
                if not np.isfinite(yv):
                    continue
                level = len(placed) % 2
                ax1.annotate(lbl, xy=(p, yv), xytext=(0, 6 + 13 * level),
                             textcoords='offset points', ha='center', fontsize=7,
                             color='0.15',
                             bbox=dict(boxstyle='round,pad=0.12', fc='white',
                                       ec='none', alpha=0.75),
                             arrowprops=dict(arrowstyle='-', lw=0.5, color='0.5'))
                placed.append((p, lbl))

        rmax = float(np.nanmax(np.abs(resid)))
        rmax = rmax if np.isfinite(rmax) and rmax > 0 else 1.0
        ax2.plot(x, resid, '-', color='#333333', lw=0.7)
        ax2.axhline(0, color='0.55', lw=0.6)
        ax2.set_ylim(-1.25 * rmax, 1.25 * rmax)
        ax2.set_ylabel('Obs - Calc', fontsize=9)
        ax2.tick_params(labelbottom=False)

        ax3.set_yticks([])
        ax3.set_ylim(nph - 0.5, -0.5)
        colors = ['#1f77b4', '#d62728', '#2ca02c', '#9467bd', '#ff7f0e', '#8c564b']
        for i, (nm, pos, icorr, hkl) in enumerate(phase_ticks):
            ax3.vlines(pos, i + 0.16, i + 0.84, color=colors[i % len(colors)], lw=0.9)
            ax3.text(-0.007, i, nm,
                     transform=blended_transform_factory(ax3.transAxes, ax3.transData),
                     ha='right', va='center', fontsize=8)
        ax3.set_xlabel('2-theta (deg)')
        for ax in (ax2, ax3):
            ax.spines['top'].set_visible(False)
            ax.tick_params(axis='x', labelsize=9)
        for ax in (ax1, ax2, ax3):
            ax.set_xlim(xmin, xmax)
        fig.suptitle(title, fontsize=12)
        png = os.path.join(pkg, sample + '_fit.png')
        fig.savefig(png, dpi=int(plan.get('dpi') or 300), bbox_inches='tight')
        plt.close(fig)
        if not (os.path.isfile(png) and os.path.getsize(png) > 0):
            raise RuntimeError('figure was not written: ' + png)

        # ---- report -------------------------------------------------------
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            g2ver = str(G2sc.GSASIIpath.GetVersionNumber())
        except Exception:
            g2ver = 'unknown'
        rep = []
        rep.append('# %s - final refinement deliverable' % sample)
        rep.append('')
        rep.append('- generated: %s' % now)
        rep.append('- engine: GSAS-II %s (scripting interface, not GUI)' % g2ver)
        rep.append('- source project: `%s`' % gpx_src)
        rep.append('- histogram: `%s`' % h.name)
        if lst_out:
            rep.append('- refinement log: `%s`' % os.path.basename(lst_out))
        rep.append('')
        rep.append('## 1. Goodness of fit')
        rep.append('')
        rep.append('| metric | value |')
        rep.append('| --- | --- |')
        for k, lab in (('wR', 'Rwp (%)'), ('R', 'R (%)'), ('Rb', 'Rb (%)'),
                       ('wRb', 'wRb (%)'), ('wRmin', 'wRmin (%)'),
                       ('chi2', 'chi**2'), ('gof', 'GOF')):
            if metrics.get(k) is not None:
                rep.append('| %s | %.4g |' % (lab, metrics[k]))
        rep.append('')
        rep.append('## 2. Phases and cells')
        rep.append('')
        rep.append('| phase | space group | a | b | c | alpha | beta | gamma | V |')
        rep.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
        for info in phase_info:
            d = dict(zip(info['cell_keys'], info['cell']))
            sd = dict(zip(info.get('cell_keys', []), info.get('cell_sigma') or []))

            def cellf(k, nd=4):
                v = d.get(k)
                if v is None:
                    return '-'
                s = sd.get(k)
                return ('%.*f(%.*f)' % (nd, v, nd, s)) if s else ('%.*f' % (nd, v))
            rep.append('| %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
                info['name'], info['space_group'], cellf('length_a'), cellf('length_b'),
                cellf('length_c'), cellf('angle_alpha', 3), cellf('angle_beta', 3),
                cellf('angle_gamma', 3), cellf('volume', 3)))
        rep.append('')
        rep.append('## 3. Atom parameters')
        rep.append('')
        rep.append('| phase | label | element | x | y | z | Occ | Mult | Uiso | flag |')
        rep.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
        for info in phase_info:
            for at in info['atoms']:
                rep.append('| %s | %s | %s | %.5f | %.5f | %.5f | %.4f | %s | %s | %s |' % (
                    info['name'], at['label'], at['element'],
                    at.get('x', 0.0), at.get('y', 0.0), at.get('z', 0.0),
                    at.get('occ', 0.0), at.get('mult', '-'),
                    ('%.4f' % at['uiso']) if at.get('uiso') is not None else '-',
                    at['refine']))
        rep.append('')
        rep.append('## 4. Strongest reflections (top 15 by Icorr)')
        rep.append('')
        for nm, pos, icorr, hkl in phase_ticks:
            idx = _top_reflections(pos, icorr, xmin, xmax, 15)
            if not idx:
                continue
            rep.append('**%s**' % nm)
            rep.append('')
            rep.append('| hkl | 2-theta (deg) | Icorr |')
            rep.append('| --- | --- | --- |')
            for j in idx:
                rep.append('| %s | %.4f | %.2f |' % (
                    _hkl_str(hkl[j][0], hkl[j][1], hkl[j][2]), pos[j], icorr[j]))
            rep.append('')
        rep.append('## 5. Deliverable validation')
        rep.append('')
        rep.append('| check | result |')
        rep.append('| --- | --- |')
        if gsas_ver:
            rep.append('| written by GSAS-II | %s |' % gsas_ver)
        rep.append('| source constraints (Hist/HAP/Phase) | %d / %d / %d |' % (
            cons_before['Hist'], cons_before['HAP'], cons_before['Phase']))
        rep.append('| constraints in package | %s |' % (
            'cleared' if cons_cleared else ('kept' if cons_total else 'none')))
        if 'phases' in reopen:
            rep.append('| reopen: phases / histograms | %d / %d |' % (
                reopen['phases'], reopen['histograms']))
        if 'wR' in reopen:
            rep.append('| reopen: cached wR | %.2f%% |' % reopen['wR'])
        if 'error' in reopen:
            rep.append('| reopen | FAILED: %s |' % str(reopen['error']).replace('|', '/'))
        rep.append('')
        rep.append('> A .gpx is a GSAS-II pickle: never rename it, never edit it as '
                   'text, and never open it with an OLDER GSAS-II than the version '
                   'above (older builds report "not a current GSAS-II .gpx").')
        rep.append('> Stale constraints were cleared as deliverable hygiene - do NOT '
                   're-refine this project afterwards, or the cached converged wR is lost.')
        rep.append('')
        rep.append('## 6. Package contents')
        rep.append('')
        for f in [clean_gpx, lst_out, png, os.path.join(pkg, sample + '_report.md')] + cif_files:
            if f:
                rep.append('- `%s`' % os.path.basename(f))
        rep.append('')
        rep.append('## 7. Notes')
        rep.append('')
        rep.append('- Packaging only: no refinement parameter was changed and no '
                   'refinement was run.')
        rep.append('- CIF files are produced by the official GSAS-II exporter '
                   '`G2Phase.export_CIF` (never hand-written).')
        rep.append('- One row of reflection ticks per phase (peak assignment); the '
                   'strongest peaks carry hkl labels whose heights are staggered '
                   'to avoid mutual overlap.')
        rep.append('- Legend and statistics box are placed by data occupancy in '
                   'the four corners, and the top of the main panel is padded, so '
                   'peaks are never covered.')
        for w in warnings:
            rep.append('- WARNING: %s' % w)
        rep.append('')
        report_path = os.path.join(pkg, sample + '_report.md')
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rep))

        files = [clean_gpx, png, report_path] + cif_files
        if lst_out:
            files.insert(1, lst_out)

        # machine-readable views of the delivery checks (declared in the tool
        # output schema; the reopen 'error' key is never exported)
        cons_out = {'hist': cons_before['Hist'], 'hap': cons_before['HAP'],
                    'phase': cons_before['Phase']}
        if cons_total:
            cons_out['cleared'] = bool(cons_cleared)
        reopen_out = {k: v for k, v in reopen.items()
                      if k in ('phases', 'histograms', 'constraints', 'wR')}

        not_deliverable = reopen_failed or cif_failed or diverged_pkg or bool(negative_uiso)
        if negative_uiso:
            warnings.append(
                '%d atom(s) carry a NEGATIVE Uiso (%s) - unphysical, and a delivery must '
                'not be presented as a finished structure while it does; the package is '
                'still written so the problem can be diagnosed'
                % (len(negative_uiso),
                   ', '.join('%s:%s=%.4g' % (r['phase'], r['atom'], r['Uiso'])
                             for r in negative_uiso[:5])))
        _reasons = [n for n, f in (('gpx did not reopen', reopen_failed),
                                   ('CIF export failed', cif_failed),
                                   ('wR too high', diverged_pkg),
                                   ('negative Uiso', bool(negative_uiso))) if f]
        _summary = 'packaged %d phase(s), %d CIF(s), wR=%s' % (
            len(phase_info), len(cif_files),
            ('%.2f%%' % metrics['wR']) if metrics.get('wR') is not None else 'n/a')
        if not_deliverable:
            _summary += ' — NOT DELIVERABLE (%s)' % ', '.join(_reasons)
        manifest = {
            'ok': not not_deliverable,
            'package_dir': pkg,
            'gpx': clean_gpx,
            'plot': png,
            'report': report_path,
            'cifs': cif_files,
            'files': files,
            'metrics': metrics,
            'phases': phase_info,
            'histogram': h.name,
            'source_gpx': gpx_src,
            'constraints': cons_out,
            'reopen': reopen_out,
            'warning': warnings,
            'summary': _summary,
        }
        if gsas_ver:
            manifest['gsas_version'] = gsas_ver
        if lst_out:
            manifest['lst'] = lst_out
        with open(os.path.join(pkg, sample + '_finalize.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1, default=float)
        result = manifest
        log('FINALIZE_OK', pkg)
    except Exception as e:
        tb = traceback.format_exc()
        log('FINALIZE_FAILED:', e)
        log(tb[-2500:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-2000:]}
        if warnings:
            result['warning'] = warnings

    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


if __name__ == '__main__':
    run_guarded_main(main)
