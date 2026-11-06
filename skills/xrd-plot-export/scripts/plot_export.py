# -*- coding: utf-8 -*-
"""Export a refined structure/fit into the TWO CSV families a third-party plotter needs.

    <sample>_rietveld.csv    2Theta,Iobs,Icalc,Iback,Idiff        (the whole pattern)
    <sample>_reflections.csv Phase,h,k,l,Mult,d_spacing,2Theta,Icorr  (the tick marks)
    _stats_summary.csv       one row per histogram: Sample,Rwp_wR_pct,...,n_observations

Why two families and not one: a Rietveld figure is (a) four curves sharing one X axis
and (b) one short list of Bragg positions per phase. Merging them into one file forces
every plotter to re-derive the second from the first, which is exactly the work this
export exists to remove.

Sources (all three were needed on the real dataset):
  gpx            a GSAS-II project  — the normal path (histograms carry obs/calc/bg/resid)
  components     a manual-engine result.json/components.json — x,yobs,ycalc,bg arrays
  amorphous-work a hump fit work dir — hump_model.json + W_twocol.dat, 4 pseudo-Voigt bands

Design rules taken from the session that produced the first batch:
  * Iobs is the RAW observed intensity, Icalc the TOTAL calculated pattern, Iback the
    background, Idiff = Iobs - Icalc (not Iobs - Iback).
  * Icorr in the reflections file is the GSAS-II `Icorr` (already corrected for
    multiplicity/LP); it is what sets the tick height. Never re-derive it.
  * Non-finite values become EMPTY cells, not "nan"/"inf" — a plotter treats an empty
    cell as a gap and a text token as a parse error.
  * 2Theta keeps 5 decimals for laboratory data (0.02 deg step) — 3 decimals visibly
    quantises a 0.01 deg synchrotron step into a staircase.
  * Multiple histograms in one project get a `_<hist-name>` suffix; the stats file keys
    on the SAME suffixed name, so nothing ever collides.
  * `--split-phases` additionally writes one reflections file per phase with the Phase
    column dropped — the usual reason is "each phase needs its own colour/tick row".

Usage
  python plot_export.py --src <x.gpx|dir> --out <dir> [--recursive] [--split-phases]
  python plot_export.py --components <result.json|components.json> --out <dir> [--name X]
  python plot_export.py --amorphous-work <work_dir> --out <dir> [--name X]

The input project/artifact is only READ. Nothing is written next to it.
"""
import argparse
import glob
import json
import math
import os
import re
import sys


# --- GSAS-II location (this script is invoked directly, not through the plugin) ---
def gsas_root():
    env = os.environ.get('DSH_GSAS_ROOT')
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, '..', '..', '..', 'xrd.local.json'),
                 os.path.join(here, '..', '..', 'xrd.local.json')):
        if os.path.isfile(cand):
            try:
                with open(cand, encoding='utf-8') as fh:
                    root = json.load(fh).get('gsas_root')
                if root and os.path.isdir(root):
                    return root
            except Exception:
                pass
    # Last resort: GSAS-II already importable (it is on sys.path for this interpreter).
    try:
        import importlib.util
        if importlib.util.find_spec('GSASII') is not None:
            return ''
    except Exception:
        pass
    raise SystemExit('plot_export: cannot find GSAS-II; set DSH_GSAS_ROOT to the GSAS-II install '
                     'root, or make GSASII importable by this interpreter')


def fmt(v, nd):
    """Empty cell for anything non-finite — never the strings 'nan'/'inf'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ''
    if math.isnan(f) or math.isinf(f):
        return ''
    return '%.*f' % (nd, f)


def safe(text):
    """File-name-safe phase label: spaces/dots/slashes would break the split files."""
    t = re.sub(r'\s+', '_', str(text)).replace('.', '_').replace('/', '_')
    return re.sub(r'_+', '_', t).strip('_') or 'phase'


# --- source 1: a GSAS-II project -------------------------------------------------
def export_gpx(gpx_path, out_dir, sample=None, split_phases=False):
    import numpy as np
    sys.path.insert(0, gsas_root())
    from GSASII import GSASIIscriptable as G2sc
    from GSASII import GSASIIfiles as G2fil
    G2fil.G2SetPrintLevel('error')

    def unmask(a):
        a = np.asarray(a)
        if np.ma.isMaskedArray(a):
            a = np.where(np.ma.getmaskarray(a), np.nan, np.asarray(a.data).astype(float))
        return np.asarray(a).astype(float)

    proj = G2sc.G2Project(gpx_path)
    stem = sample or os.path.splitext(os.path.basename(gpx_path))[0]
    hists = proj.histograms()
    multi = len(hists) > 1
    rows = []

    for h in hists:
        tag = h.name.replace(' ', '_').replace(':', '') if multi else ''
        name = stem + (('_' + tag) if tag else '')

        x = unmask(h.getdata('x'))
        yobs = unmask(h.getdata('yobs'))
        ycalc = unmask(h.getdata('ycalc'))
        ybkg = unmask(h.getdata('background'))
        yres = unmask(h.getdata('residual'))

        d0 = h.data['data'][0]
        wr = float(d0.get('wR', float('nan')))
        rp = float(d0.get('R', float('nan')))
        rb = float(d0.get('Rb', float('nan')))
        wre = float(d0.get('wRmin', float('nan')))
        gof = (wr / wre) if wre else float('nan')
        rows.append((name, wr, rp, rb, wre, gof, int(x.size)))

        n = x.size
        path = os.path.join(out_dir, name + '_rietveld.csv')
        with open(path, 'w', newline='\n') as fh:
            fh.write('2Theta,Iobs,Icalc,Iback,Idiff\n')
            for k in range(n):
                fh.write('%s,%s,%s,%s,%s\n' % (fmt(x[k], 5), fmt(yobs[k], 2), fmt(ycalc[k], 2),
                                               fmt(ybkg[k], 2), fmt(yres[k], 2)))

        refl = h.reflections()
        per_phase = {}
        nref = 0
        path = os.path.join(out_dir, name + '_reflections.csv')
        with open(path, 'w', newline='\n') as fh:
            fh.write('Phase,h,k,l,Mult,d_spacing,2Theta,Icorr\n')
            for ph, d in refl.items():
                rl = np.asarray(d.get('RefList', []))
                if rl.size == 0:
                    continue
                bucket = per_phase.setdefault(ph, [])
                for r in rl:
                    line = '%s,%d,%d,%d,%d,%.6f,%.5f,%.3f\n' % (
                        ph, int(r[0]), int(r[1]), int(r[2]), int(r[3]),
                        float(r[4]), float(r[5]), float(r[11]))
                    fh.write(line)
                    bucket.append(line)
                    nref += 1

        if split_phases:
            for ph, lines in per_phase.items():
                sp = os.path.join(out_dir, '%s_reflections_%s.csv' % (name, safe(ph)))
                with open(sp, 'w', newline='\n') as fh:
                    # Keep h,k,l,Mult: a per-phase tick file without the indices cannot be
                    # checked against anything. (The FIRST batch was produced by two one-off
                    # scripts and came out inconsistent — 7 columns for one sample, 3 for
                    # another — so there is no older convention worth preserving here; the
                    # 7-column form is the documented one.)
                    fh.write('h,k,l,Mult,d_spacing,2Theta,Icorr\n')
                    for line in lines:
                        fh.write(','.join(line.split(',')[1:]))
        print('  %-26s n=%-6d n_ref=%-5d wR=%.2f%%' % (name, n, nref, wr))
    return rows


# --- source 2: a manual-engine result -------------------------------------------
def export_components(path, out_dir, sample=None):
    """components.json (preferred) or result.json carrying a components block."""
    with open(path, encoding='utf-8') as fh:
        doc = json.load(fh)
    comp = doc
    if 'components' not in doc and 'x' not in doc:
        # a result.json: the arrays live in the sibling components.json
        sib = os.path.join(os.path.dirname(path), 'components.json')
        if os.path.isfile(sib):
            with open(sib, encoding='utf-8') as fh:
                comp = json.load(fh)
        else:
            raise SystemExit('plot_export: %s carries no arrays and has no sibling components.json' % path)

    x = comp['x']
    yobs = comp['yobs']
    ycalc = comp.get('ycalc') or comp.get('yc')
    bg = comp.get('bg') or comp.get('ybkg') or [0.0] * len(x)
    name = sample or os.path.splitext(os.path.basename(os.path.dirname(path)))[0] or 'sample'
    path_out = os.path.join(out_dir, name + '_rietveld.csv')
    with open(path_out, 'w', newline='\n') as fh:
        fh.write('2Theta,Iobs,Icalc,Iback,Idiff\n')
        for k in range(len(x)):
            fh.write('%s,%s,%s,%s,%s\n' % (fmt(x[k], 5), fmt(yobs[k], 2), fmt(ycalc[k], 2),
                                           fmt(bg[k], 2), fmt(float(yobs[k]) - float(ycalc[k]), 2)))
    print('  %-26s n=%-6d (engine components)' % (name, len(x)))
    return []


# --- source 3: an amorphous hump fit --------------------------------------------
def export_amorphous(work_dir, out_dir, sample=None):
    """hump_model.json + W_twocol.dat -> rietveld.csv with one column per band.

    Every Band<i> column is background + that component ALONE (the convention the first
    batch used): stacked on top of each other they reproduce Icalc exactly, which is what
    a stacked-component figure needs. Do not emit the bare component or the stack breaks.
    """
    import numpy as np
    with open(os.path.join(work_dir, 'hump_model.json'), encoding='utf-8') as fh:
        model = json.load(fh)
    xs, ys = [], []
    with open(os.path.join(work_dir, 'W_twocol.dat'), 'r', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            p = line.replace(',', ' ').split()
            if len(p) < 2:
                continue
            try:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
            except ValueError:
                continue
    x = np.asarray(xs)
    y = np.asarray(ys)
    bg = np.polyval(model['bg_coeffs'], (x - x.mean()) / (x.max() - x.min()))
    comps = model['components']

    def pv(xx, amp, cen, h, eta):
        hw = h / 2.0
        g = np.exp(-4.0 * np.log(2.0) * ((xx - cen) / h) ** 2)
        l = 1.0 / (1.0 + ((xx - cen) / hw) ** 2)
        return amp * (eta * l + (1.0 - eta) * g)

    curves = [pv(x, c['amp'], c['center'], c['FWHM'], c['eta']) for c in comps]
    calc = bg + np.sum(curves, axis=0)
    res = y - calc
    rwp = float(np.sqrt(np.sum(res ** 2) / np.sum(y ** 2)) * 100.0)

    name = sample or 'Amorphous'
    head = '2Theta,Iobs,Icalc,Iback,Idiff' + ''.join(',Band%d' % (i + 1) for i in range(len(comps)))
    with open(os.path.join(out_dir, name + '_rietveld.csv'), 'w', newline='\n') as fh:
        fh.write(head + '\n')
        for k in range(x.size):
            bands = ','.join(fmt(bg[k] + c[k], 2) for c in curves)
            fh.write('%s,%s,%s,%s,%s,%s\n' % (fmt(x[k], 5), fmt(y[k], 2), fmt(calc[k], 2),
                                              fmt(bg[k], 2), fmt(res[k], 2), bands))
    with open(os.path.join(out_dir, name + '_components.csv'), 'w', newline='\n') as fh:
        fh.write('Band,center_2th,FWHM,eta,amp,area,d_A,Lc_nm\n')
        for c in comps:
            fh.write('%d,%.4f,%.4f,%.4f,%.2f,%.1f,%.4f,%.4f\n' % (
                c['n'], c['center'], c['FWHM'], c['eta'], c['amp'], c['area'], c['d'], c['Lc_nm']))
    with open(os.path.join(out_dir, name + '_stats.csv'), 'w', newline='\n') as fh:
        fh.write('metric,value\n')
        fh.write('Rwp,%.4f\n' % rwp)
        # chi2/dof was present in the first batch — keep it when the model carries chi2/npar.
        if model.get('chi2') is not None and model.get('npar') is not None:
            dof = int(x.size) - int(model['npar'])
            if dof > 0:
                fh.write('chi2_dof,%.4f\n' % (float(model['chi2']) / dof))
        fh.write('n_components,%d\n' % len(comps))
        fh.write('npar,%s\n' % model.get('npar', ''))
        fh.write('NOTE,not a Rietveld fit - Rwp is not comparable with crystalline samples\n')
        # §5 requires the APPARENT qualifier to travel WITH the number, not only in the prose.
        fh.write('NOTE,components.csv Lc_nm is an APPARENT coherence length (Scherrer on a broad\n')
        fh.write('NOTE,hump assumes a single effective size); it is not a crystallite size and no\n')
        fh.write('NOTE,strain separation was performed\n')
    print('  %-26s n=%-6d bands=%d  Rwp=%.4f%%' % (name, x.size, len(comps), rwp))
    return []


# --- the shared stats file ------------------------------------------------------
def write_stats(out_dir, rows):
    if not rows:
        return None
    path = os.path.join(out_dir, '_stats_summary.csv')
    head = 'Sample,Rwp_wR_pct,Rp_pct,Rb_pct,Rexp_wRmin_pct,gof,n_observations'

    def f4(v):
        """Empty cell for a non-finite metric — a literal 'nan' would contradict §8.2 and
        every plotter would choke on it."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return ''
        if math.isnan(f) or math.isinf(f):
            return ''
        return '%.4f' % f

    keep = []
    if os.path.isfile(path):                     # merge: a new row for a known sample REPLACES it
        with open(path, encoding='utf-8') as fh:
            old = fh.read().splitlines()
        new_names = set(r[0] for r in rows)
        keep = [l for l in old[1:] if l.split(',')[0] not in new_names]
    with open(path, 'w', newline='\n') as fh:
        fh.write(head + '\n')
        for line in keep:
            fh.write(line + '\n')
        for r in rows:
            fh.write('%s,%s,%s,%s,%s,%s,%d\n' % (r[0], f4(r[1]), f4(r[2]), f4(r[3]), f4(r[4]), f4(r[5]), r[6]))
    return path


def sample_for(gpx_path):
    """`<...>/<sample>/GPX/<sample>_final.gpx` is the layout the report tree uses, so the
    SAMPLE is the grandparent name, not the file stem. Applying the rule in every branch
    (single file too) is what makes a re-export line up with the first batch's file names."""
    parent = os.path.basename(os.path.dirname(gpx_path))
    grand = os.path.basename(os.path.dirname(os.path.dirname(gpx_path)))
    if parent.lower() == 'gpx' and grand:
        return grand
    return os.path.splitext(os.path.basename(gpx_path))[0]


def main():
    ap = argparse.ArgumentParser(description='export the two plotting CSV families')
    ap.add_argument('--src', help='.gpx file or a directory of them')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--recursive', action='store_true', help='descend into subdirectories under --src')
    ap.add_argument('--split-phases', action='store_true', help='also write one reflections file per phase')
    ap.add_argument('--components', help='manual-engine components.json / result.json')
    ap.add_argument('--amorphous-work', help='work dir holding hump_model.json + W_twocol.dat')
    ap.add_argument('--name', help='sample name override (components / amorphous modes)')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    rows = []
    if args.amorphous_work:
        export_amorphous(args.amorphous_work, args.out, args.name)
    elif args.components:
        rows += export_components(args.components, args.out, args.name)
    elif args.src:
        if os.path.isdir(args.src):
            pat = os.path.join(args.src, '**', '*.gpx') if args.recursive else os.path.join(args.src, '*.gpx')
            gpxs = sorted(glob.glob(pat, recursive=args.recursive))
            for g in gpxs:
                try:
                    rows += export_gpx(g, args.out, args.name or sample_for(g), args.split_phases)
                except Exception as e:                                  # noqa: BLE001
                    print('  FAILED %s: %r' % (g, e))
        else:
            rows += export_gpx(args.src, args.out, args.name or sample_for(args.src), args.split_phases)
    else:
        ap.error('give one of --src / --components / --amorphous-work')

    stats = write_stats(args.out, rows)
    if stats:
        print('wrote %s (%d row(s))' % (stats, len(rows)))
    print('DONE')


if __name__ == '__main__':
    main()
