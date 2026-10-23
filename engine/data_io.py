#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_data_io engine - read/convert/cut/aligne powder-XRD data files.

Closes the biggest capability gap (user 2026-09-17): the plugin used to accept
only data already in GSAS format, so every vendor raw file, two-column text or
Rigaku export needed a fresh one-off script (parse_sampleA.py,
convert_electrode_engine.py, cut_electrode_engine.py, align_dat.py, fix_step.py,
measure_fwhm.py, set_uvw.py ... - 166 self-built scripts existed in total).

ACTIONS (plan JSON via argv[1]; result JSON via --out):
  {"type":"read","file":...,"format":"auto"}
      parse and report n_points / 2theta range / step / intensity range
  {"type":"convert","file":...,"format":"auto","to":"twocol|fxye|dat","out":...,
      "pad_lines":6}
      convert;  fxye = BANK 1 FXY with 2theta*100 (centidegrees)
  {"type":"cut","file":...,"ranges":[[lo,hi],...],"out":...,"format":"auto",
      "auto_al":false}
      drop 2theta ranges (data-level cutting - GSAS scripted masking does not
      work, so cutting has to happen here); auto_al adds the usual Al-foil peaks
  {"type":"fwhm","file":...,"peaks":[[lo,hi],...],"format":"auto"}
      measure FWHM of the given peak windows; with >=3 peaks also solve the
      Caglioti U/V/W initial guesses (H^2 = U tan^2 + V tan + W)
  {"type":"scherrer","file":...,"peaks":[[lo,hi],...],"wavelength":1.54056,
      "K":0.9,"inst_broadening":<deg>,"caglioti":{"U":..,"V":..,"W":..},
      "inst_mode":"gaussian|lorentzian","format":"auto"}
      Scherrer size D = K*lambda/(beta*cos(theta)) per window (beta in radians).
      With NO instrumental broadening supplied the value is flagged APPARENT;
      supply inst_broadening (a width in degrees) or caglioti {U,V,W} to
      subtract it - gaussian = sqrt(b_obs^2-b_inst^2), lorentzian = b_obs-b_inst.
      BOUNDARY: micro-strain separation (Williamson-Hall) is NOT implemented
      here; Kalpha2 is not stripped; and on an amorphous hump D is the apparent
      coherence length of that hump, not a crystallite size.
  {"type":"align","file":...,"drop_lines":6,"out":...}
      drop the leading N lines so FullProf's fixed 6-comment skip lines up
  {"type":"head","file":...,"n":12}
      show the first lines (format sniffing)

Loaded formats (auto-detected):
  rigaku  - Rigaku RAW/txt: "[Data]" section, "2theta, count" rows
  fxye    - GSAS FXY: "BANK n FXY" then "2theta*100  intensity [esd]"
  twocol  - any whitespace/comma separated numeric 2-column text
Nothing is judged here: values pass through unchanged.
"""
import os
import sys
import json

from result_io import write_result
from proc_guard import start_guard, run_guarded_main
import math
import traceback

import numpy as np


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
    return ''.join(out).strip('_') or 'data'


# ---------------------------------------------------------------------------
def read_head(path, n=12):
    with open(path, encoding='utf-8', errors='replace') as f:
        return [next(f, '').rstrip('\n') for _ in range(int(n))]


def parse_rigaku(path):
    """Rigaku RAW/txt: everything after '[Data]' until the next '[...]' block."""
    x, y = [], []
    in_data = False
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if s.startswith('[Data]'):
                in_data = True
                continue
            if not in_data:
                continue
            if s.startswith('[') or s == '':
                if x:
                    break
                continue
            parts = [p.strip() for p in s.split(',') if p.strip()]
            if len(parts) >= 2:
                try:
                    x.append(float(parts[0]))
                    y.append(float(parts[1]))
                except ValueError:
                    continue
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


def parse_fxye(path):
    """GSAS FXY: skip BANK/comment lines, x is in centidegrees."""
    x, y = [], []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#') or s.upper().startswith('BANK'):
                continue
            parts = s.replace(',', ' ').split()
            if len(parts) < 2:
                continue
            try:
                xv = float(parts[0])
                yv = float(parts[1])
            except ValueError:
                continue
            if xv > 180.0:          # centidegrees -> degrees
                xv = xv / 100.0
            x.append(xv)
            y.append(yv)
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


def parse_twocol(path):
    """Tolerant two-column reader: skips headers/comments/non-numeric rows."""
    x, y, skipped = [], [], 0
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if not s or s[0] in '#!;':
                continue
            parts = s.replace(',', ' ').split()
            if len(parts) < 2:
                skipped += 1
                continue
            try:
                xv = float(parts[0])
                yv = float(parts[1])
            except ValueError:
                skipped += 1
                continue
            x.append(xv)
            y.append(yv)
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float), skipped


def detect_format(path):
    """Sniff the format: Rigaku RAW (';RAW4.00' / '[RawHeader]' / '[Data]'),
    GSAS FXY ('BANK'), otherwise a tolerant two-column text.

    The '[Data]' block of a Rigaku export sits after ~120 header lines, so the
    sniff window has to be wide (40 lines was not enough - it reported 'twocol')."""
    try:
        head = read_head(path, 250)
    except Exception:
        return 'twocol'
    for line in head:
        s = line.strip()
        if s.startswith('[Data]'):
            return 'rigaku'
        if s.upper().startswith(';RAW') or s.startswith('[RawHeader]'):
            return 'rigaku'
        if s.upper().startswith('BANK'):
            return 'fxye'
    return 'twocol'


def load_data(path, fmt='auto'):
    fmt = (fmt or 'auto').lower()
    if fmt == 'auto':
        fmt = detect_format(path)
    skipped = 0
    if fmt == 'rigaku':
        x, y = parse_rigaku(path)
    elif fmt == 'fxye':
        x, y = parse_fxye(path)
    else:
        x, y, skipped = parse_twocol(path)
    if len(x) < 10:
        raise RuntimeError('only %d numeric row(s) read from %s (format=%s)'
                           % (len(x), path, fmt))
    keep = np.isfinite(x) & np.isfinite(y)
    return x[keep], y[keep], fmt, skipped


def dataset_info(x, y, fmt, skipped):
    step = float(np.median(np.diff(x))) if len(x) > 2 else 0.0
    return {
        'format': fmt,
        'n_points': int(len(x)),
        'x_min': _num(x[0]), 'x_max': _num(x[-1]),
        'step': _num(step),
        'y_min': _num(np.min(y)), 'y_max': _num(np.max(y)),
        'skipped_lines': int(skipped),
    }


def write_twocol(path, x, y, header='2theta intensity'):
    with open(path, 'w', encoding='utf-8') as f:
        if header:
            f.write(header + '\n')
        for xv, yv in zip(x, y):
            f.write('%.5f %.4f\n' % (xv, yv))
    return path


def write_fxye(path, x, y, title=''):
    with open(path, 'w', encoding='utf-8') as f:
        if title:
            f.write('# %s\n' % title)
        f.write('BANK 1 FXY\n')
        for xv, yv in zip(x, y):
            f.write('%.4f %.4f\n' % (xv * 100.0, yv))
    return path


def write_dat(path, x, y, pad_lines=0):
    """FullProf two-column .dat; pad_lines prepends dummy rows (fp2k skips a
    fixed number of leading comment lines - the align trick from the PB work)."""
    with open(path, 'w', encoding='utf-8') as f:
        for i in range(int(pad_lines)):
            f.write('%.5f %.4f\n' % (x[0], y[0]))
        for xv, yv in zip(x, y):
            f.write('%.5f %.4f\n' % (xv, yv))
    return path


AL_PEAKS = [[26.2, 26.9], [38.3, 38.8], [44.5, 45.0], [64.8, 65.6], [77.9, 78.7]]


def read_text_lines(path):
    """The file as text lines, or None when it is not a text file.

    Binary vendor containers (Rigaku .raw) must not be round-tripped line by line, so
    a NUL byte or a decode failure returns None and the caller falls back to the
    parsed-points writer.
    """
    try:
        with open(path, 'rb') as fh:
            blob = fh.read()
    except Exception:
        return None
    if b'\x00' in blob[:4096]:
        return None
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        try:
            text = blob.decode('latin-1')
        except Exception:
            return None
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def strip_leading_comments(lines, n_drop):
    """Drop up to `n_drop` leading comment/blank lines; return (kept, dropped).

    Only the leading block is touched: once a real data line is seen nothing else is
    removed, so a comment further down (or a ';RAW4.00' header in the middle of a
    vendor file) survives. FullProf skips a FIXED number of comment lines, which is
    what this alignment is for.
    """
    kept = list(lines)
    dropped = 0
    want = max(0, int(n_drop))
    while dropped < want and kept:
        s = kept[0].strip()
        if s == '' or s.startswith('!') or s.startswith('#') or s.startswith(';'):
            kept.pop(0)
            dropped += 1
        else:
            break
    return kept, dropped


def cut_ranges(x, y, ranges):
    keep = np.ones(len(x), dtype=bool)
    for r in ranges:
        lo, hi = float(r[0]), float(r[1])
        keep &= ~((x >= lo) & (x <= hi))
    return x[keep], y[keep], int(keep.sum())


def measure_fwhm(x, y, windows):
    """FWHM per window by half-maximum crossing on a local linear background."""
    out = []
    for w in windows:
        lo, hi = float(w[0]), float(w[1])
        m = (x >= lo) & (x <= hi)
        if not np.any(m):
            continue
        xs, ys = x[m], y[m]
        i0 = int(np.argmax(ys))
        ypk = float(ys[i0])
        xpk = float(xs[i0])
        # local background = median of the window edges
        edge = np.concatenate([ys[:max(1, len(ys) // 10)], ys[-max(1, len(ys) // 10):]])
        ybg = float(np.median(edge))
        half = ybg + (ypk - ybg) / 2.0
        left = None
        for i in range(i0, 0, -1):
            if ys[i - 1] <= half <= ys[i] or ys[i - 1] >= half >= ys[i]:
                span = (ys[i] - ys[i - 1]) or 1e-12
                left = xs[i - 1] + (half - ys[i - 1]) * (xs[i] - xs[i - 1]) / span
                break
        right = None
        for i in range(i0, len(ys) - 1):
            if ys[i] >= half >= ys[i + 1] or ys[i] <= half <= ys[i + 1]:
                span = (ys[i + 1] - ys[i]) or 1e-12
                right = xs[i] + (half - ys[i]) * (xs[i + 1] - xs[i]) / span
                break
        if left is None or right is None:
            out.append({'center': _num(xpk), 'height': _num(ypk),
                        'fwhm': None, 'note': 'half-maximum crossing not found'})
            continue
        out.append({'center': _num(xpk), 'height': _num(ypk),
                    'fwhm': _num(abs(right - left)),
                    'x_left': _num(left), 'x_right': _num(right),
                    'background': _num(ybg)})
    return out


def caglioti_fit(fwhms, wavelength=1.54056):
    """Solve U/V/W from >=3 (2theta, FWHM) pairs: H^2 = U tan^2 + V tan + W."""
    pts = [(f['center'], f['fwhm']) for f in fwhms
           if f.get('center') and f.get('fwhm') and f['fwhm'] > 0]
    if len(pts) < 3:
        return None
    th = np.radians(np.asarray([p[0] for p in pts], dtype=float) / 2.0)
    h2 = np.asarray([p[1] for p in pts], dtype=float) ** 2
    A = np.column_stack([np.tan(th) ** 2, np.tan(th), np.ones_like(th)])
    try:
        sol, *_ = np.linalg.lstsq(A, h2, rcond=None)
    except Exception:
        return None
    return {'U': _num(sol[0]), 'V': _num(sol[1]), 'W': _num(sol[2]), 'n_peaks': len(pts)}


def _inst_fwhm_deg(center, inst_broadening=None, caglioti=None):
    """Instrumental FWHM (degrees) at this 2theta, from an explicit width or from
    Caglioti U/V/W (H^2 = U tan^2 + V tan + W). Caglioti wins when both are given."""
    if caglioti:
        try:
            th = math.radians(float(center) / 2.0)
            h2 = (float(caglioti['U']) * math.tan(th) ** 2
                  + float(caglioti['V']) * math.tan(th)
                  + float(caglioti['W']))
            if h2 > 0:
                return math.sqrt(h2), 'caglioti U/V/W'
        except Exception:
            pass
    if inst_broadening is not None:
        try:
            return float(inst_broadening), 'given value'
        except Exception:
            pass
    return None, None


def scherrer_assumptions(wavelength, K, inst_mode, caglioti, inst_broadening):
    """The assumption block that travels with every Scherrer number."""
    return {
        'formula': 'D = K*lambda / (beta*cos(theta)), beta = FWHM in radians',
        'wavelength_A': float(wavelength or 1.54056),
        'K': float(K if K is not None else 0.9),
        'inst_mode': str(inst_mode or 'gaussian').lower(),
        'inst_source': ('caglioti U/V/W' if caglioti else
                        ('given value' if inst_broadening is not None
                         else 'NONE - values are APPARENT (instrumental broadening not subtracted)')),
        'kalpha2': 'not stripped - an unresolved doublet inflates the width and lowers D',
        'strain': ('not separated - D is a coherence length; Williamson-Hall / size-strain '
                   'separation is out of scope here (boundary, user decision 2026-09-21)'),
        'scope': ('on an amorphous hump D is the apparent coherence length of that hump, '
                  'NOT a crystallite size; a crystallite size requires strain-free, '
                  'monodisperse, size-only broadening'),
    }


def scherrer_sizes(fwhms, wavelength=1.54056, K=0.9, inst_broadening=None,
                   inst_mode='gaussian', caglioti=None):
    """Scherrer size per peak window (ACTION: scherrer).

        D = K * lambda / (beta * cos(theta)),   beta = FWHM in RADIANS

    The formula is one line; the assumptions are the whole risk, so each one is
    echoed back next to the number and the value is explicitly labelled:

      * **apparent vs corrected** - with no instrumental broadening the result is
        an APPARENT value and says so. Over-subtraction (b_inst >= b_obs) is
        reported as `over_subtracted` rather than silently returning 0.
      * **D is a coherence length, not necessarily a crystallite size** - see the
        BOUNDARY in the module header. Micro-strain separation is deliberately
        not implemented; for that use GSAS-II's size/strain broadening.
      * **Kalpha2 is not stripped**, and K (shape factor) is echoed back.
    """
    lam = float(wavelength or 1.54056)
    kk = float(K if K is not None else 0.9)
    mode = str(inst_mode or 'gaussian').lower()
    out = []
    for f in fwhms:
        c = _num(f.get('center'))
        w = _num(f.get('fwhm'))
        row = {'center': c, 'fwhm_deg': w}
        if c is None or not w or w <= 0:
            row['D_nm'] = None
            row['note'] = 'no usable half-maximum width in this window'
            out.append(row)
            continue
        th = math.radians(c / 2.0)
        b_obs = math.radians(w)
        row['theta_deg'] = _num(math.degrees(th))
        row['d_spacing'] = _num(lam / (2.0 * math.sin(th))) if th > 0 else None
        b_inst_deg, src = _inst_fwhm_deg(c, inst_broadening, caglioti)
        beta = b_obs
        if b_inst_deg is None:
            row['corrected'] = False
            row['inst_broadening_deg'] = None
            row['note'] = ('APPARENT value: instrumental broadening NOT subtracted '
                           '(pass inst_broadening or caglioti to subtract it)')
        else:
            b_inst = math.radians(b_inst_deg)
            row['corrected'] = True
            row['inst_broadening_deg'] = _num(b_inst_deg)
            row['inst_source'] = src
            if mode.startswith('lorentz'):
                row['inst_mode'] = 'lorentzian (linear subtraction)'
                beta = b_obs - b_inst
                if beta <= 0:
                    beta = None
                    row['note'] = ('over_subtracted: instrumental width (%.4f deg) >= observed '
                                   'width (%.4f deg)' % (b_inst_deg, w))
            else:
                row['inst_mode'] = 'gaussian (quadrature subtraction)'
                d2 = b_obs ** 2 - b_inst ** 2
                if d2 <= 0:
                    beta = None
                    row['note'] = ('over_subtracted: instrumental width (%.4f deg) >= observed '
                                   'width (%.4f deg)' % (b_inst_deg, w))
                else:
                    beta = math.sqrt(d2)
            if beta is not None:
                row['note'] = ('instrumental broadening subtracted (%s); Kalpha2 not stripped; '
                               'strain not separated' % row['inst_mode'])
        row['beta_rad'] = _num(beta) if beta else None
        if beta:
            dA = kk * lam / (beta * math.cos(th))
            row['D_A'] = _num(dA)
            row['D_nm'] = _num(dA / 10.0)
        else:
            row['D_A'] = None
            row['D_nm'] = None
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# --------------------------------------------------------------- file header
# Positional header extraction (user 2026-10-03): "真实文件的文件头与我们编写的并不
# 一致，需要截取两列数据出现前的所有部分，而不是特定表述截取".
# So: the header is EVERYTHING BEFORE the data starts, located POSITIONALLY -
# never by keyword matching, because real vendor headers do not look like ours.
HEADER_COMMENT_CHARS = '#!;'


def _is_data_row(s, min_cols=2):
    """A data row: >= min_cols numeric tokens (whitespace or comma separated) and
    not a comment line. 3-column files (.fxye, CIF data loops) qualify as well."""
    if not s or s[0] in HEADER_COMMENT_CHARS:
        return False
    parts = s.replace(',', ' ').split()
    if len(parts) < min_cols:
        return False
    try:
        for p in parts[:min_cols]:
            float(p)
    except ValueError:
        return False
    return True


def _row_ncols(s):
    """How many leading tokens of this row are numeric."""
    n = 0
    for p in s.replace(',', ' ').split():
        try:
            float(p)
            n += 1
        except ValueError:
            break
    return n


def find_data_start(lines, run=3, min_cols=2):
    """Index of the first line of the first run of `run` consecutive data rows.

    A single numeric-looking header line (a date, a version, a count) must not be
    mistaken for the data start, hence the run requirement. Returns None when the
    file contains no such run (then the whole file counts as header).
    """
    n = len(lines)
    i = 0
    while i < n:
        if _is_data_row(lines[i].strip(), min_cols):
            j, got = i, 0
            while j < n and _is_data_row(lines[j].strip(), min_cols):
                got += 1
                j += 1
                if got >= run:
                    return i
            i = j
            continue
        i += 1
    return None


def dump_header(path, max_lines=2000):
    """Everything before the data starts, verbatim, with 1-based line numbers.

    Nothing is interpreted here: this is evidence for the caller (the AI) to read.
    """
    with open(path, 'rb') as fh:
        blob = fh.read()
    try:
        blob.decode('utf-8')
        enc_note = 'utf-8 clean'
    except UnicodeDecodeError:
        enc_note = ('non-UTF-8 bytes present - decoded with errors="replace" '
                    '(vendor headers are often cp932/latin-1)')
    text = blob.decode('utf-8', errors='replace')
    crlf = '\r\n' in text
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if lines and lines[-1] == '':
        lines = lines[:-1]
    ds = find_data_start(lines)
    found = ds is not None
    cut = ds if found else len(lines)
    head = lines[:cut]
    shown = head[:max_lines]
    return {
        'file': path,
        'byte_size': len(blob),
        'total_lines': len(lines),
        'line_ending': 'CRLF' if crlf else 'LF',
        'encoding_note': enc_note,
        'data_found': found,
        'data_start_line': (ds + 1) if found else None,
        'data_first_row': lines[ds].strip() if found else None,
        'data_ncols': _row_ncols(lines[ds].strip()) if found else None,
        'header_lines': len(head),
        'returned_lines': len(shown),
        'truncated': len(head) > max_lines,
        'lines': ['%6d| %s' % (i + 1, ln) for i, ln in enumerate(shown)],
    }


def collect_evidence(paths, max_lines=400):
    """Header regions of several files, one block each.

    RAW EVIDENCE ONLY - nothing here decides what any value means, which one is
    authoritative, or whether two files agree. That judgement belongs to the
    caller (the AI reads these blocks and passes an explicit value downstream).
    """
    out = []
    for p in (paths or []):
        try:
            out.append(dump_header(p, max_lines=max_lines))
        except Exception as e:
            out.append({'file': p, 'error': '%s: %s' % (type(e).__name__, e)})
    return out


def main():
    if len(sys.argv) < 2:
        raise SystemExit('plan JSON required')
    planf = sys.argv[1]
    outres = None
    for i, a in enumerate(sys.argv[2:]):
        if a == '--out':
            outres = sys.argv[3 + i]
    plan = json.load(open(planf, encoding='utf-8'))
    guard = start_guard(plan, backend='data_io', out_path=outres)   # P1 (0.8.3/0.8.4)
    outres = outres or os.path.splitext(planf)[0] + '_result.json'
    out_dir = plan.get('out_dir') or os.path.dirname(os.path.abspath(outres))
    os.makedirs(out_dir, exist_ok=True)

    warnings = []
    actions_log = []
    datasets = []
    files = []
    # P0 (0.8.5): an action that fails must NOT leave the whole call claiming success.
    # Until now `ok` was written True unconditionally after the loop, so a batch whose
    # every action raised (a missing file, say) came back as ok:true + an empty result
    # set, and only a warning text hinted otherwise — measured by the review as
    # "缺失文件实测返回 ok:true、0 个结果". `ok` now means "every action succeeded";
    # `partial` says whether anything did, and `failed_actions` names what did not.
    failed_actions = []
    result = {'ok': False}
    try:
        acts = plan.get('actions') or []
        if not acts:
            raise RuntimeError('need a non-empty "actions" list')
        base = _safe(plan.get('sample') or 'data')
        for i, a in enumerate(acts):
            guard.checkpoint('action %s' % str(a.get('type') or ''))   # 0.8.4
            atype = str(a.get('type') or '')
            rec = {'action': atype}
            try:
                if atype == 'head':
                    f = a['file']
                    rec['detail'] = 'first %d line(s) of %s' % (a.get('n', 12), os.path.basename(f))
                    rec['lines'] = read_head(f, a.get('n', 12))
                elif atype == 'header':
                    # positional: EVERYTHING BEFORE THE DATA STARTS (user 2026-10-03)
                    d = dump_header(a['file'], int(a.get('max_lines') or 2000))
                    rec['header'] = {k: v for k, v in d.items() if k != 'lines'}
                    rec['lines'] = d['lines']
                    where = ('data starts at line %d with %d column(s)'
                             % (d['data_start_line'], d['data_ncols'])) if d['data_found'] \
                        else 'no data run found - the whole file counts as header'
                    rec['detail'] = ('header = everything before the data starts: %s '
                                     '(%d header line(s)%s)'
                                     % (where, d['header_lines'],
                                        ', TRUNCATED at max_lines' if d['truncated'] else ''))
                elif atype == 'evidence':
                    paths = a.get('files') or ([a['file']] if a.get('file') else [])
                    if not paths:
                        raise RuntimeError('evidence needs "files" (or a single "file")')
                    ev = collect_evidence(paths, int(a.get('max_lines') or 400))
                    rec['evidence'] = ev
                    n_ok = sum(1 for e in ev if not e.get('error'))
                    rec['detail'] = ('header regions of %d file(s) (%d read, %d failed) - '
                                     'raw evidence, no judgement made; read them and pass an '
                                     'explicit wavelength downstream' % (len(ev), n_ok, len(ev) - n_ok))
                elif atype == 'read':
                    x, y, fmt, sk = load_data(a['file'], a.get('format'))
                    info = dataset_info(x, y, fmt, sk)
                    info['file'] = a['file']
                    datasets.append(info)
                    rec['detail'] = '%s: %d pts %.3f-%.3f step %.4f' % (
                        fmt, info['n_points'], info['x_min'], info['x_max'], info['step'] or 0)
                elif atype == 'convert':
                    x, y, fmt, sk = load_data(a['file'], a.get('format'))
                    to = str(a.get('to') or 'twocol').lower()
                    stem = base if plan.get('sample') else _safe(
                        os.path.splitext(os.path.basename(a['file']))[0])
                    if to == 'fxye':
                        out = a.get('out') or os.path.join(out_dir, stem + '.fxye')
                        write_fxye(out, x, y, title=a.get('title') or os.path.basename(a['file']))
                    elif to == 'dat':
                        out = a.get('out') or os.path.join(out_dir, stem + '.dat')
                        write_dat(out, x, y, a.get('pad_lines') or 0)
                    else:
                        out = a.get('out') or os.path.join(out_dir, stem + '.txt')
                        write_twocol(out, x, y, a.get('header') or '2theta intensity')
                    files.append(out)
                    rec['detail'] = '%s -> %s (%s, %d pts)' % (fmt, out, to, len(x))
                    rec['out'] = out
                    if sk:
                        warnings.append('skipped %d non-numeric line(s) in %s'
                                        % (sk, os.path.basename(a['file'])))
                elif atype == 'cut':
                    # USER RULE (2026-09-18): cutting is DESTRUCTIVE - it really
                    # deletes data points, so every number downstream changes.
                    # It is therefore GATED: it only runs when the caller states
                    #   (a) confirm=true  -> the user asked for this cut in the
                    #                        conversation, and
                    #   (b) reason="..."  -> the declared use case.
                    # It must never be used as preventive clean-up, and never
                    # "just in case" - an undeclared cut is refused outright.
                    if a.get('confirm') is not True:
                        raise RuntimeError(
                            'cut is gated: it really deletes data points and changes every '
                            'downstream number, so it requires confirm=true (the user asked '
                            'for it in the conversation) plus reason="<use case>". '
                            'Declared use cases: (1) Al-foil / current-collector peaks in '
                            'electrode patterns, (2) a region dominated by an unrelated '
                            'artifact. Do NOT use it as preventive clean-up.')
                    _reason = (a.get('reason') or '').strip()
                    if not _reason:
                        raise RuntimeError(
                            'cut needs reason="<use case>" - one line stating why this cut '
                            'is wanted (it is recorded with the result).')
                    x, y, fmt, sk = load_data(a['file'], a.get('format'))
                    ranges = a.get('ranges') or []
                    if a.get('auto_al'):
                        ranges = list(ranges) + AL_PEAKS
                    if not ranges:
                        raise RuntimeError('cut needs "ranges" or auto_al=true')
                    x2, y2, nkeep = cut_ranges(x, y, ranges)
                    stem = base if plan.get('sample') else _safe(
                        os.path.splitext(os.path.basename(a['file']))[0])
                    out = a.get('out') or os.path.join(out_dir, stem + '_cut.txt')
                    write_twocol(out, x2, y2, a.get('header') or '2theta intensity')
                    files.append(out)
                    rec['detail'] = 'cut %d range(s): %d -> %d pts -> %s' % (
                        len(ranges), len(x), nkeep, out)
                    rec['reason'] = _reason
                    rec['out'] = out
                elif atype == 'fwhm':
                    x, y, fmt, sk = load_data(a['file'], a.get('format'))
                    wins = a.get('peaks') or []
                    if not wins:
                        raise RuntimeError('fwhm needs "peaks": [[lo,hi], ...]')
                    fw = measure_fwhm(x, y, wins)
                    rec['detail'] = 'FWHM for %d window(s)' % len(fw)
                    rec['fwhm'] = fw
                    cag = caglioti_fit(fw, float(a.get('wavelength') or 1.54056))
                    if cag:
                        rec['caglioti'] = cag
                        rec['detail'] += '; Caglioti U/V/W solved'
                    datasets.append({'file': a['file'], 'fwhm': fw, 'caglioti': cag})
                elif atype == 'scherrer':
                    # Scherrer size (user 2026-09-21: "谢乐公式对非晶体xrd谱线分析与
                    # 杂质尺寸判别很重要" - the width of an amorphous hump and the
                    # width of an impurity peak are read the same way here).
                    x, y, fmt, sk = load_data(a['file'], a.get('format'))
                    wins = a.get('peaks') or []
                    if not wins:
                        raise RuntimeError('scherrer needs "peaks": [[lo,hi], ...]')
                    fw = measure_fwhm(x, y, wins)
                    lam = float(a.get('wavelength') or 1.54056)
                    kk = 0.9 if a.get('K') is None else a.get('K')
                    size = scherrer_sizes(
                        fw, wavelength=lam, K=kk,
                        inst_broadening=a.get('inst_broadening'),
                        inst_mode=a.get('inst_mode'),
                        caglioti=a.get('caglioti'))
                    asum = scherrer_assumptions(
                        lam, kk, a.get('inst_mode'), a.get('caglioti'),
                        a.get('inst_broadening'))
                    rec['scherrer'] = size
                    rec['assumptions'] = asum
                    n_ok = sum(1 for r in size if r.get('D_nm'))
                    rec['detail'] = ('Scherrer size for %d/%d window(s); %s'
                                     % (n_ok, len(size), asum['inst_source']))
                    if not n_ok:
                        warnings.append('scherrer: no window produced a usable width')
                    datasets.append({'file': a['file'], 'scherrer': size,
                                     'assumptions': asum})
                elif atype == 'align':
                    # P1 (0.8.5): `drop_lines` did NOTHING — the code called
                    # write_dat(..., pad_lines=0) and then claimed in the report that it
                    # had padded n rows, so a 16-line input produced a 16-line output
                    # while the text said "pad_lines=6 prepends dummy rows". Now the
                    # parameter really drops (the FullProf comment block) and the
                    # separate `pad_lines` really pads, and both numbers are reported
                    # from what was actually written.
                    n_drop = int(a.get('drop_lines') if a.get('drop_lines') is not None else 6)
                    n_pad = int(a.get('pad_lines') or 0)
                    out = a.get('out') or os.path.join(out_dir, base + '_aligned.dat')
                    raw = read_text_lines(a['file'])
                    if raw is not None:
                        kept, dropped = strip_leading_comments(raw, n_drop)
                        pad = ['! pad for FullProf fixed %d-line skip' % n_pad] * max(0, n_pad)
                        with open(out, 'w', encoding='utf-8', newline='\n') as fh:
                            for ln in pad + kept:
                                fh.write(ln + '\n')
                        rec['detail'] = ('wrote %s: %d line(s) in, dropped %d leading comment/blank '
                                         'line(s), padded %d, %d line(s) out'
                                         % (out, len(raw), dropped, max(0, n_pad),
                                            len(pad) + len(kept)))
                        rec['dropped'] = dropped
                        rec['padded'] = max(0, n_pad)
                    else:
                        # Non-text (vendor raw / fxye): keep the parsed points and pad
                        # only, which is all that format allows.
                        x, y, fmt, sk = load_data(a['file'], a.get('format'))
                        write_dat(out, x, y, pad_lines=max(0, n_pad))
                        rec['detail'] = ('wrote %s (%d pts) by RE-SERIALISING the parsed data '
                                         '(not a text file); dropped 0, padded %d'
                                         % (out, len(x), max(0, n_pad)))
                        rec['dropped'] = 0
                        rec['padded'] = max(0, n_pad)
                        rec['assumptions'] = {'text_passthrough': False}
                    files.append(out)
                    rec['out'] = out
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

        result = {
            'ok': not failed_actions,
            'datasets': datasets,
            'files': files,
            'actions': actions_log,
            'warning': warnings,
            'summary': 'xrd_data_io: %d action(s), %d dataset(s), %d file(s) written%s' % (
                len(actions_log), len(datasets), len(files),
                ('' if not failed_actions else
                 '; %d action(s) FAILED' % len(failed_actions))),
        }
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
        if files:
            result['file'] = files[-1]
        log('DATA_IO_OK', files)
    except Exception as e:
        tb = traceback.format_exc()
        log('DATA_IO_FAILED:', e)
        log(tb[-2000:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-1500:]}
        if warnings:
            result['warning'] = warnings

    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


if __name__ == '__main__':
    run_guarded_main(main)
