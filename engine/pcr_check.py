"""pcr_check.py - post-conversion audit of a CIF -> PCR pair (occupancy focus).

WHAT THIS CHECKS (and what it deliberately does NOT claim)
----------------------------------------------------------
The official converter (FullProfAPP's CIFs_to_PCR.exe) writes site occupancies in
the standard FullProf convention

        pcr_occ  =  cif_occ  x  site_multiplicity / general_position_multiplicity

verified on two independent conversions (2026-09-18):

    Fm-3m (general position = 192)
        K  0.80 -> 0.03333  (x 8/192)      Fe 1.00 -> 0.02083  (x 4/192)
        C  1.00 -> 0.12500  (x 24/192)
    P 63/m m c (general position = 24; multiplicity read from the CIF's own
    _atom_site_site_symmetry_multiplicity column)
        Mn 0.62 -> 0.05167  (x 2/24)       O  1.00 -> 0.16667  (x 4/24)

which reproduces every observed value exactly. So a small ratio is NOT by itself
evidence of a broken pcr - an earlier version of this audit assumed
`pcr_occ == cif_occ` and therefore reported perfectly correct pcr files as
"fail" (including on files that AutoFP could refine). This version compares
against the multiplicity-normalised expectation instead, using the CIF's declared
site multiplicity when present and computing it from the space-group operations
otherwise.

The .pcr is only ever READ here - it is never rewritten.

Usage:
    pcr_check.py <cif> <pcr> [--out result.json]
"""
import json
import os
import re
import sys
import traceback

TOL_PASS = 0.02          # <=2% relative difference -> pass
TOL_WARN = 0.06          # <=6% -> warn, above -> fail
COORD_TOL = 0.02         # fractional-coordinate match tolerance


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _num(v):
    try:
        f = float(v)
        return f if f == f else None       # drop NaN
    except (TypeError, ValueError):
        return None


def _element_of(label):
    m = re.match(r'([A-Za-z]{1,2})', str(label) or '')
    return m.group(1).capitalize() if m else ''


# --- CIF side ---------------------------------------------------------------
def _norm(xyz):
    return tuple(round(v % 1.0, 4) for v in xyz)


def _cell_and_sg(text):
    """(cell params dict, space-group symbol) from the CIF header."""
    cell, sg = {}, ''
    pats = {
        'a': r'_cell_length_a', 'b': r'_cell_length_b', 'c': r'_cell_length_c',
        'alpha': r'_cell_angle_alpha', 'beta': r'_cell_angle_beta',
        'gamma': r'_cell_angle_gamma',
    }
    for ln in text.splitlines():
        s = ln.strip()
        for k, p in pats.items():
            if re.match(p + r'\s', s):
                v = _num(re.split(r'\s+', s, maxsplit=1)[1].split('(')[0])
                if v is not None:
                    cell[k] = v
        if '_symmetry_space_group_name_h-m' in s.lower() or '_space_group_name_h-m' in s.lower():
            sg = s.split(None, 1)[1].strip().strip('"').strip("'")
    return cell, sg


def _multiplicity_fn(text):
    """(f(coords) -> site multiplicity, general multiplicity) from the CIF header.

    Returns (None, None) when the space group cannot be determined, in which case
    the caller reports 'unknown' rather than guessing.

    WHY THE SYMBOL NORMALISATION: CIFs spell Hermann-Mauguin symbols the human way
    ("P 21/c", "P 63/m m c"), while pymatgen's SpaceGroup() requires the screw-axis
    subscript written with an underscore ("P2_1/c") and *also* rejects the compact
    form ("P21/c"). Without normalising, an ordinary CIF degrades the whole audit
    to verdict=unknown - measured 2026-09-18 on a synthetic P 21/c probe, while the
    two real regression fixtures happen to be spelled in a form pymatgen accepts,
    which is exactly why the tests did not catch it.
    """
    cell, sg_raw = _cell_and_sg(text)
    if not sg_raw or len(cell) < 6:
        return None, None
    try:
        from pymatgen.symmetry.groups import SpaceGroup
    except Exception as e:
        log('multiplicity: pymatgen unavailable (%s)' % e)
        return None, None

    s = re.sub(r'\s+', '', str(sg_raw).replace('_', ''))
    parts = s.split('/')
    head = parts[0]
    m = re.match(r'^([A-Za-z])(\d+)$', head)
    if m and len(m.group(2)) >= 2:                 # "P21" -> "P2_1"
        head = m.group(1) + m.group(2)[:-1] + '_' + m.group(2)[-1]
    cands = ['/'.join([head] + parts[1:]), s]
    if len(parts) > 1:
        cands.append(head + ''.join(parts[1:]))
    group = None
    for cand in cands:
        try:
            group = SpaceGroup(cand)
            break
        except Exception:
            continue
    if group is None:
        log('multiplicity: space group %r unusable by pymatgen (tried %s)' % (sg_raw, cands))
        return None, None

    try:
        from pymatgen.core import Lattice
        Lattice.from_parameters(cell['a'], cell['b'], cell['c'],
                                cell['alpha'], cell['beta'], cell['gamma'])
    except Exception as e:
        log('multiplicity: cell unusable (%s)' % e)
        return None, None
    ops = group.symmetry_ops
    general = len(ops)

    def mult_at(coords):
        seen = set()
        for op in ops:
            seen.add(_norm(op.operate(coords)))
        return len(seen)

    return mult_at, general


def cif_sites(path):
    """[(label, element, occ, declared_multiplicity or None)] from the CIF's
    _atom_site_ loop, AS WRITTEN.

    Deliberately a TEXT parse rather than pymatgen: CifParser symmetry-expands
    the site list AND merges species that share coordinates (in the P2 phase
    Mn/Ni/Mo all sit at 0,0,0), which destroys the per-site occupancy the pcr has
    to be compared against. The .pcr is a per-row table, so the CIF is read
    per-row too.
    """
    text = open(path, encoding='utf-8', errors='replace').read()
    lines = text.splitlines()
    rows, i = [], 0
    raw = []
    while i < len(lines):
        if lines[i].strip().lower() == 'loop_':
            tags, j = [], i + 1
            while j < len(lines) and lines[j].strip().startswith('_'):
                tags.append(lines[j].strip())
                j += 1
            low = [t.lower() for t in tags]
            if any(t.startswith('_atom_site_') for t in low):
                idx = {t: k for k, t in enumerate(low)}
                li = idx.get('_atom_site_label')
                oi = idx.get('_atom_site_occupancy')
                ti = idx.get('_atom_site_type_symbol')
                mi = (idx.get('_atom_site_site_symmetry_multiplicity')
                      if '_atom_site_site_symmetry_multiplicity' in idx
                      else idx.get('_atom_site_symmetry_multiplicity'))
                xi, yi, zi = (idx.get('_atom_site_fract_x'), idx.get('_atom_site_fract_y'),
                              idx.get('_atom_site_fract_z'))
                while j < len(lines):
                    s = lines[j].strip()
                    if not s or s.startswith('#') or s.startswith('_') or s.lower() == 'loop_':
                        break
                    j += 1
                    p = s.replace("'", ' ').replace('"', ' ').split()
                    if len(p) < len(tags):
                        continue
                    label = p[li] if li is not None else ''
                    el = p[ti] if (ti is not None and ti < len(p)) else _element_of(label)
                    occ = _num(p[oi]) if oi is not None else None
                    dm = _num(p[mi]) if (mi is not None and mi < len(p)) else None
                    coords = None
                    if None not in (xi, yi, zi) and max(xi, yi, zi) < len(p):
                        try:
                            coords = (float(p[xi]), float(p[yi]), float(p[zi]))
                        except (ValueError, TypeError):
                            coords = None
                    if label:
                        raw.append({'label': label, 'element': _element_of(el),
                                    'occ': 1.0 if occ is None else occ,
                                    'declared_mult': int(dm) if dm else None,
                                    'coords': coords})
                i = j
                continue
        i += 1

    mult_fn, general = _multiplicity_fn(text)
    for r in raw:
        mult = r['declared_mult']
        if mult is None and mult_fn and r['coords']:
            try:
                mult = mult_fn(r['coords'])
            except Exception:
                mult = None
        r['mult'] = mult
        r['mult_source'] = ('cif_column' if r['declared_mult'] is not None
                            else ('space_group' if mult else None))
        rows.append(r)
    return rows, general


# --- PCR side ---------------------------------------------------------------
def pcr_atom_block(text):
    """Return (space_group, [atom rows]) from a FullProf .pcr.

    Atom rows sit after the '<--Space group symbol' line and the '!Atom' header;
    each row is  label type x y z biso occ  (then per-atom code fields), followed
    by an optional code line that starts with a number.
    """
    lines = text.splitlines()
    sg = ''
    start = None
    for i, ln in enumerate(lines):
        if '<--Space group symbol' in ln or 'Space group symbol' in ln:
            sg = ln.split('<--')[0].strip().lstrip('>').strip()
            start = i + 1
            break
    rows = []
    if start is None:
        return sg, rows
    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('!'):
            # the atom block ends at the next comment banner once rows have begun
            if rows:
                break
            continue
        parts = s.split()
        if len(parts) < 7:
            continue
        # an atom row is  LABEL  ELEM  x y z biso occ ...  - the two leading
        # tokens must be word-like, otherwise the numeric-only lines of the
        # Profile Parameters block are mistaken for atoms.
        if not re.match(r'^[A-Za-z]{1,3}\d*$', parts[0]):
            continue
        if not re.match(r'^[A-Za-z]{1,2}$', parts[1]):
            continue
        if _num(parts[6]) is None:
            continue
        if _num(parts[2]) is None or _num(parts[3]) is None or _num(parts[4]) is None:
            continue
        rows.append({'label': parts[0], 'element': parts[1],
                     'occ': _num(parts[6]),
                     'coords': (_num(parts[2]), _num(parts[3]), _num(parts[4]))})
    return sg, rows


# --- comparison -------------------------------------------------------------
def _coords_delta(a, b):
    """Largest periodic fractional-coordinate difference, or None when unusable."""
    if not a or not b or any(v is None for v in a) or any(v is None for v in b):
        return None
    try:
        return max(abs((u - v + 0.5) % 1.0 - 0.5) for u, v in zip(a, b))
    except (TypeError, ValueError):
        return None


def _coords_match(a, b):
    d = _coords_delta(a, b)
    return d is not None and d <= COORD_TOL


def compare(cif_rows, pcr_rows, general):
    by_label = {}
    for r in pcr_rows:
        by_label.setdefault(r['label'], r)
    sites, warnings, unmatched = [], [], []
    for c in cif_rows:
        p = by_label.get(c['label'])
        if p is not None and p.get('_used'):
            p = None
        if p is None:
            for q in pcr_rows:
                if q.get('_used'):
                    continue
                if _coords_match(c.get('coords'), q.get('coords')):
                    p = q
                    break
        if p is None:
            for q in pcr_rows:
                if q.get('_used'):
                    continue
                if _element_of(q['label']) == c['element']:
                    p = q
                    break
        if p is None:
            unmatched.append(c['label'])
            note = 'no matching row in the pcr'
            # A co-located partly-occupied pair (e.g. Fe 0.45 / Mn 0.55 on the same
            # position) can legitimately end up as ONE pcr row. When the partner row
            # has already claimed it, saying just "no matching row" hides why: the
            # occupancy may simply be shared with the co-located site. Name that.
            for other in cif_rows:
                if other is c:
                    continue
                d = _coords_delta(c.get('coords'), other.get('coords'))
                if d is not None and d <= COORD_TOL:
                    note = ('no separate row in the pcr: this site shares its position with %s, '
                            'and a co-located pair may be carried as a single pcr row - the '
                            'occupancies cannot be attributed from these two files alone'
                            % other['label'])
                    break
            sites.append({'label': c['label'], 'element': c['element'],
                          'cif_occ': round(float(c['occ']), 5), 'note': note})
            continue
        p['_used'] = True
        co, po = c['occ'], p['occ']
        mult = c.get('mult')
        expected = None
        if mult and general:
            expected = float(mult) / float(general)
        rec = {'label': c['label'], 'element': c['element'],
               'cif_occ': round(float(co), 5), 'pcr_occ': round(float(po), 5)}
        if mult:
            rec['site_multiplicity'] = int(mult)
        if c.get('mult_source'):
            rec['multiplicity_source'] = c['mult_source']
        if general:
            rec['general_multiplicity'] = int(general)
        if co and co > 0:
            ratio = po / co
            rec['factor'] = round(ratio, 6)
            if expected:
                rec['expected_factor'] = round(expected, 6)
                rel = abs(ratio - expected) / expected
                rec['relative_deviation'] = round(rel, 5)
                if rel > TOL_WARN:
                    warnings.append(
                        '%s: pcr occupancy %.5g does not match CIF %.5g x %.5g (site '
                        'multiplicity %d / general %d); the pcr factor is %.5g where %.5g was '
                        'expected, so the site scatters wrongly.'
                        % (c['label'], po, co, expected, mult, general, ratio, expected))
                elif rel > TOL_PASS:
                    warnings.append(
                        '%s: pcr occupancy %.5g vs expected %.5g (CIF %.5g x %.5g), a small but '
                        'non-zero difference.' % (c['label'], po, co * expected, co, expected))
            else:
                # no multiplicity available: report the raw ratio, but do NOT
                # declare failure - a small factor is the normal convention
                rec['note'] = ('site multiplicity unknown, so only the raw factor is reported; '
                               'a factor well below 1 is expected (FullProf writes occ/site '
                               'multiplicity)')
        sites.append(rec)
    for r in pcr_rows:
        if not r.get('_used'):
            unmatched.append('pcr-only:%s' % r['label'])
    for r in pcr_rows:
        r.pop('_used', None)

    worst = 0.0
    judged = 0
    for s in sites:
        if 'relative_deviation' in s:
            judged += 1
            worst = max(worst, s['relative_deviation'])
    if judged == 0:
        verdict = 'unknown'
    else:
        verdict = 'pass' if worst <= TOL_PASS else ('warn' if worst <= TOL_WARN else 'fail')
    return sites, warnings, unmatched, verdict


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    out = None
    if '--out' in sys.argv:
        i = sys.argv.index('--out')
        if i + 1 < len(sys.argv):
            out = sys.argv[i + 1]
    result = {'ok': False}
    try:
        if len(args) < 2:
            raise SystemExit(__doc__)
        cif_path, pcr_path = args[0], args[1]
        if not os.path.isfile(cif_path):
            raise RuntimeError('cif not found: %s' % cif_path)
        if not os.path.isfile(pcr_path):
            raise RuntimeError('pcr not found: %s' % pcr_path)
        text = open(pcr_path, encoding='utf-8', errors='replace').read()
        sg, pcr_rows = pcr_atom_block(text)
        if not pcr_rows:
            raise RuntimeError('no atom rows found in %s (unexpected pcr layout)' % pcr_path)
        cif_rows, general = cif_sites(cif_path)
        sites, warnings, unmatched, verdict = compare(cif_rows, pcr_rows, general)
        n_bad = sum(1 for w in warnings if 'expected' in w and 'small but' not in w)
        result = {
            'ok': True,
            'verdict': verdict,
            'space_group': sg or None,
            'general_multiplicity': general,
            'n_cif_sites': len(cif_rows),
            'n_pcr_sites': len(pcr_rows),
            'sites': sites,
            'warning': warnings,
            'unmatched': unmatched,
            'summary': ('occupancy audit: %d site(s) compared, %d disagree with the CIF '
                        '(multiplicity-normalised)' % (len(sites), n_bad)),
            'note': ('CIFs_to_PCR.exe writes occ x site_multiplicity / general_multiplicity '
                     '(FullProf convention; verified on Fm-3m and P 63/m m c, 2026-09-18), so a '
                     'pcr occupancy far below the CIF value is EXPECTED, not an error. '
                     'verdict=fail means the pcr does not match that expectation and the '
                     'occupancies must be reconciled before the pcr is trusted.'),
        }
        result = {k: v for k, v in result.items() if v is not None}
        log('PCR_CHECK', verdict, len(sites), 'sites')
    except SystemExit:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log('PCR_CHECK_FAILED:', e)
        log(tb[-1200:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-1200:]}
    if out:
        with open(out, 'w', encoding='utf-8') as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
