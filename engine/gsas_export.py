#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_export_cif engine - GSAS-II FINAL OUTPUT with physical sanity checks
(tool #3 of the GSAS trio).

User's tool trio (2026-09-17):
  #1 `xrd_refine_gsas`  - quick ladder
  #2 `xrd_gsas_ops`     - every parameter / constraint reachable
  #3 `xrd_export_cif`   - THIS tool: export the converged structure to CIF and
                          immediately CHECK it (bond lengths, occupancies, Uiso)

WHY: the PB campaign showed that a refinement can lower wR while producing
physically impossible structures (C/N free coordinates drove the C#N bond to
0.37 A; K split-sites collapsed to 0.49 A). "Every improvement must pass a bond
check" was the lesson - so the export step is also the CHECK step, not a
formality. Recipe taken from PB/XRD/mono2phase/{export_s3_cif,phys2b_save,
phys3_save,fix_final_cif}.py, made declarative.

Plan protocol (argv[1] = plan JSON, --out <result.json>):
{
  "gsas_root": "<GSAS-II install root; or set DSH_GSAS_ROOT>",
  "gpx": "<converged .gpx>",
  "out_dir": "...",                     # default: dir of the result json
  "sample": "<prefix>",                 # default: gpx stem
  "mode": "structured|official|both",   # default structured (+official if asked)
  "uiso": {"mode":"keep"},              # or {"mode":"physical",
                                        #     "default":0.01,
                                        #     "by_element":{"K":0.02,"Fe":0.005},
                                        #     "by_atom":{"O6":0.01}}
  "occ_check": true,                    # assert 0 < occ <= 1.001
  "min_distance": 1.0,                  # contacts shorter than this FAIL
  "bond_expect": {"Fe-C":[1.8,2.05], "C-N":[1.1,1.2]},   # optional windows
  "phases": ["all"]                     # or explicit phase names
}

Result: {ok, cifs[], sanity[{phase, file, space_group, n_atoms, bonds[], 
         bad_contacts[], bond_violations[], occ_violations[]}], verdict,
         warning[], error?, log_tail?}
`verdict` is "pass" / "warn" / "fail" - a FAIL must never be presented as a
finished structure.
"""
import os
import re
import sys
import json

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard
import math
import traceback
from collections import defaultdict

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
    return ''.join(out).strip('_') or 'phase'


def _element_of(label):
    """Element symbol a GSAS site label starts with ('Fe1_N' -> 'Fe', 'K2_1' -> 'K').

    Taking every letter and capitalising produced 'Fen'/'FeN' for the composite labels
    this port itself writes (`<site>_<element>` from set_site_composition, `<site>_<k>`
    from split_site), so `by_element` never matched and those atoms silently fell back
    to the default Uiso (audit 2026-09-21 third pass).
    """
    m = re.match(r'\s*([A-Za-z][a-z]?)', str(label))
    return m.group(1).capitalize() if m else ''


def _element_pair(pair_str):
    """'Y-O' / 'P-O' / 'O2--Pb2+' -> ('O','Pb'), a sorted ELEMENT pair. None if unusable.

    Exists so a bond window is matched by element identity, never by string containment:
    the old lookup (`all(e in k2 for e in els)`) matched the request "P-O" against a
    "Pb-O" contact and "C-N" against "C-Na", i.e. the window was enforced on the WRONG
    atom pair and could report a pass or a fail that belongs to another chemistry
    (codex review 2026-09-27). Species decorations (charge "O2-", occupancy "Mn0.62")
    and label suffixes dissolve through _element_of, which every other element-level
    decision in this file already uses.
    """
    parts = [p for p in str(pair_str).split('-') if p.strip()]
    if len(parts) != 2:
        return None
    a, b = _element_of(parts[0]), _element_of(parts[1])
    return tuple(sorted([a, b])) if a and b else None


# ---------------------------------------------------------------------------
def phase_cell(ph):
    """(a,b,c,alpha,beta,gamma) from the phase General['Cell'] record."""
    cell = (ph.data.get('General') or {}).get('Cell') or []
    vals = [_num(v) for v in cell[1:7]]
    if len(vals) != 6 or any(v is None for v in vals):
        raise RuntimeError('phase %s has no usable cell' % ph.name)
    return vals


def phase_atoms(ph):
    gen = ph.data.get('General') or {}
    ptr = gen.get('AtomPtrs') or [3, 1, 7, 9]
    cx, ct, cs, cia = ptr[0], ptr[1], ptr[2], ptr[3]
    rows = []
    for at in (ph.data.get('Atoms') or []):
        flags = str(at[ct + 1])
        try:
            uiso = _num(at[cia + 1]) if str(at[cia]) == 'I' else None
        except Exception:
            uiso = None
        rows.append({
            'label': str(at[ct - 1]),
            'element': str(at[ct]),
            'flags': flags,
            'x': _num(at[cx]), 'y': _num(at[cx + 1]), 'z': _num(at[cx + 2]),
            'occ': _num(at[cx + 3]),
            'mult': int(at[cs + 1]) if isinstance(at[cs + 1], (int, float)) else None,
            'uiso': uiso,
        })
    return rows


def _atom_element(row):
    """The atom's REAL element, preferring GSAS's own field over its label.

    `phase_atoms` already records `element` straight out of the atom record (`at[ct]`); the
    label is just a name someone chose. Reading the label instead (the original code) made
    a mixed site labelled `M1` whose element is Fe miss `by_element['Fe']` and silently take
    the default Uiso, and made a site labelled `Fe1` whose element is Mn pick up Fe's value
    (audit 2026-09-21, cloud end-to-end run). Both directions are wrong, so the element
    field wins and the label is only a fallback for records that carry no usable element.
    """
    el = _element_of(row.get('element') or '')
    return el or _element_of(row.get('label'))


def choose_uiso(row, ucfg):
    """Physical Uiso for one atom (recipe: by_atom > by_element > default)."""
    if not ucfg or (ucfg.get('mode') or 'keep') == 'keep':
        return row.get('uiso')
    by_atom = ucfg.get('by_atom') or {}
    if row['label'] in by_atom:
        return _num(by_atom[row['label']])
    by_el = ucfg.get('by_element') or {}
    el = _atom_element(row)
    if el in by_el:
        return _num(by_el[el])
    d = _num(ucfg.get('default'))
    return d if d is not None else row.get('uiso')


def write_structured_cif(path, data_name, ph, rows, sg_ops, ucfg, comment=None):
    """Structured CIF (explicit symmetry operations) - same layout as the
    PB scripts, so it round-trips through pymatgen/CifParser."""
    a, b, c, al, be, ga = phase_cell(ph)
    sg = (ph.data.get('General') or {}).get('SGData') or {}
    spgrp = str(sg.get('SpGrp') or '').strip()
    L = []
    L.append('data_%s' % data_name)
    L.append('')
    if comment:
        for line in comment.splitlines():
            L.append('# ' + line)
    L.append('_cell_length_a  %.6f' % a)
    L.append('_cell_length_b  %.6f' % b)
    L.append('_cell_length_c  %.6f' % c)
    L.append('_cell_angle_alpha  %.6f' % al)
    L.append('_cell_angle_beta  %.6f' % be)
    L.append('_cell_angle_gamma  %.6f' % ga)
    L.append("_space_group_name_H-M_alt  '%s'" % spgrp)
    L.append('')
    L.append('loop_')
    L.append('_space_group_symop_operation_xyz')
    for op in sg_ops:
        L.append("   '%s'" % op)
    L.append('')
    L.append('loop_')
    for tag in ('_atom_site_label', '_atom_site_type_symbol',
                '_atom_site_fract_x', '_atom_site_fract_y', '_atom_site_fract_z',
                '_atom_site_occupancy', '_atom_site_adp_type',
                '_atom_site_U_iso_or_equiv'):
        L.append('   ' + tag)
    for r in rows:
        x = r['x'] if r['x'] is not None else 0.0
        y = r['y'] if r['y'] is not None else 0.0
        z = r['z'] if r['z'] is not None else 0.0
        occ = r['occ'] if r['occ'] is not None else 0.0
        u = choose_uiso(r, ucfg)
        if u is None:
            u = 0.0
        L.append('   %-10s %-4s %12.6f %12.6f %12.6f %8.4f  Uiso %8.5f' % (
            r['label'], r['element'], x, y, z, occ, u))
    L.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    return path


def symmetry_ops(ph):
    """Explicit symmetry operation strings for the CIF (GSASII G2spc.AllOps)."""
    try:
        from GSASII import GSASIIspc as G2spc
        sgdata = (ph.data.get('General') or {}).get('SGData') or {}
        ops = G2spc.AllOps(sgdata)[0]
        return [str(o).lower() for o in ops]
    except Exception as e:
        log('symmetry ops unavailable: %s' % e)
        return []


# ---------------------------------------------------------------------------
def sanity_check(cif_path, opts):
    """Parse the written CIF with pymatgen and check bond lengths / occupancies.

    Returns dict with per-pair bond statistics, sub-1A contacts and violations
    against the optional expected windows."""
    out = {'file': cif_path, 'bonds': [], 'bad_contacts': [], 'bond_violations': [],
           'occ_violations': []}
    try:
        from pymatgen.io.cif import CifParser
        st = CifParser(cif_path).parse_structures(primitive=False)[0]
    except Exception as e:
        out['parse_error'] = str(e)
        return out
    out['n_atoms'] = len(st)
    try:
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
        out['space_group'] = str(SpacegroupAnalyzer(st).get_space_group_symbol())
    except Exception as e:
        out['space_group_error'] = str(e)
    try:
        out['formula'] = str(st.composition.reduced_formula)
    except Exception:
        pass

    coords = np.array([s.coords for s in st])
    els = [str(s.species_string) for s in st]
    occs = [float(getattr(s, 'occupancy', 1.0) or 1.0) for s in st]
    mind = float(_pget(opts, 'min_distance', 1.0))
    bond_map = defaultdict(list)
    # Element-level twin of bond_map, used ONLY by the window lookups below. `bond_map`
    # keeps its species-level keys because that is what `out['bonds']` has always
    # reported; this index answers the different question "does this ELEMENT pair have
    # contacts", by exact identity (see _element_pair).
    bond_el = defaultdict(list)
    for i in range(len(st)):
        d = np.linalg.norm(coords - coords[i], axis=1)
        d[i] = 99.0
        for j in np.argsort(d)[:4]:
            if d[j] < 2.6:
                pair = '-'.join(sorted([els[i], els[j]]))
                bond_map[pair].append(round(float(d[j]), 3))
                ea, eb = _element_of(els[i]), _element_of(els[j])
                if ea and eb:
                    bond_el[tuple(sorted([ea, eb]))].append(round(float(d[j]), 3))
            if d[j] < mind:
                out['bad_contacts'].append({'atoms': [els[i], els[j]],
                                            'd': round(float(d[j]), 3)})
    for pair in sorted(bond_map):
        v = bond_map[pair]
        out['bonds'].append({'pair': pair, 'n': len(v),
                             'min': round(min(v), 3), 'max': round(max(v), 3)})

    requested = set(dict(opts.get('bond_expect') or {}).keys())  # caller-supplied only
    expect = dict(opts.get('bond_expect') or {})   # copy: defaults must not mutate opts
    # Default windows: the audit noted that "bond windows require the caller to supply
    # them" left the output check inert unless someone remembered. These defaults cover
    # bonds that actually appear in these refinements and are applied ONLY when the
    # contact exists — an absent default pair is NOT a violation (measured 2026-09-21:
    # without this distinction, Y2O3 produced 22 "no such contact found" entries and the
    # verdict collapsed to fail on a perfectly good structure). Caller keys still win,
    # key by key; bond_expect_defaults:false disables the defaults entirely.
    if _pget(opts, 'bond_expect_defaults', True) is not False:
        for k, v in _DEFAULT_BOND_WINDOWS.items():
            expect.setdefault(k, v)
    for pair, win in expect.items():
        key = '-'.join(sorted(str(pair).split('-')))
        # EXACT element identity first (the species-level keys carry charge/occupancy
        # decorations, so a window key like "Y-O" is an element request, not a species one).
        want = _element_pair(pair)
        v = bond_el.get(want) if want else None
        if not v:
            # Exact species key as a fallback — and NOTHING ELSE. The previous substring
            # fallback (`all(e in k2 for e in els)`) let "P-O" be satisfied by a "Pb-O"
            # contact and "C-N" by "C-Na": the window then judged the wrong atom pair and
            # could report pass/fail for a chemistry the caller never asked about
            # (codex review 2026-09-27). A pair that truly is absent must reach the
            # "no such contact found" branch below, which is the honest answer.
            v = bond_map.get(key) or bond_map.get(pair)
        if not v:
            # Only a CALLER-supplied window may complain about a missing contact. A
            # default window that does not apply to this chemistry is silent by design
            # (a missing default pair is not evidence of anything).
            if pair in requested:
                out['bond_violations'].append({'pair': pair, 'issue': 'no such contact found'})
            continue
        lo, hi = float(win[0]), float(win[1])
        bad = [x for x in v if x < lo or x > hi]
        if bad:
            out['bond_violations'].append({
                'pair': pair, 'expected': [lo, hi], 'n_out': len(bad),
                'range': [round(min(bad), 3), round(max(bad), 3)]})
    for i, o in enumerate(occs):
        if _pget(opts, 'occ_check', True) and not (0.0 < o <= 1.001):
            out['occ_violations'].append({'atom': els[i], 'occ': o})

    # ---- charge neutrality (audit 2026-09-21: the output check had no such test) --
    # Deliberately CONSERVATIVE: a charge imbalance is only reported when every
    # element in the cell has a usable oxidation state. Reporting "not neutral" for
    # a mixed-valence or unknown-state compound would be noise, and this check exists
    # to catch real slips (a missing atom, a wrong occupancy), not to judge chemistry.
    if _pget(opts, 'charge_check', True):
        try:
            comp = st.composition
            total, unknown = 0.0, []
            for el, amt in comp.items():
                oxi = _oxidation_state(el)
                if oxi is None:
                    unknown.append(str(el))
                    continue
                total += float(oxi) * float(amt)
            out['charge'] = {
                'sum': round(total, 4),
                'n_elements': len(list(comp.items())),
                'unknown_elements': unknown,
            }
            if not unknown and abs(total) > 0.05:
                out['charge_violations'] = [{
                    'sum': round(total, 4),
                    'note': ('the cell is not charge-neutral for the unambiguous '
                             'oxidation states of these elements — check for a missing '
                             'atom, a wrong occupancy, or a site that should be mixed-'
                             'valence (this is a FLAG, not a verdict: the model may be '
                             'right and the assumed states wrong)')}]
            elif unknown:
                out['charge']['note'] = ('not judged: no unambiguous oxidation state for '
                                         + ', '.join(unknown))
        except Exception as e:
            out['charge'] = {'error': str(e)}
    return out


# Oxidation states used ONLY for the charge-neutrality flag above. Deliberately
# restricted to elements whose state in an ionic oxide/fluoride is not in doubt.
# MULTIVALENT elements are absent on purpose — Fe, Mn, Co, Cu, Cr, V, Nb, Mo, W, Sn,
# Pb, S, N, Ti, Ce, U … : for those the check must decline to judge, because a false
# "not neutral" on a perfectly good mixed-valence model is worse than no check.
_OXI = {
    'Li': 1, 'Na': 1, 'K': 1, 'Rb': 1, 'Cs': 1, 'Ag': 1,
    'Mg': 2, 'Ca': 2, 'Sr': 2, 'Ba': 2, 'Zn': 2, 'Cd': 2,
    'Al': 3, 'Ga': 3, 'In': 3, 'Y': 3, 'La': 3, 'Sc': 3,
    'Zr': 4, 'Hf': 4, 'Si': 4, 'Ge': 4,
    'P': 5, 'As': 5, 'Ta': 5,
    'F': -1, 'Cl': -1, 'Br': -1, 'I': -1,
    'O': -2,
}


def _oxidation_state(el):
    """Charge to use for `el` in the neutrality check, or None when it must not be judged.

    Two sources, in order:
      1. the charge pymatgen already carries on the species string (`'Y3+'`, `'O2-'`) —
         the model's own declaration, and the better basis;
      2. a small table of unambiguous ionic states for BARE element symbols.
    `el` must be the charge suffix stripped before the table lookup: measured
    2026-09-21, without stripping, Y2O3 was reported as "not judged: no unambiguous
    oxidation state for Y3+, O2-" — the check silently declined on exactly the simple
    oxide it was written for.

    Multivalent bare elements (Fe, Mn, Co, Cu, Cr, V, …) are absent from the table on
    purpose: None makes the caller decline to judge instead of inventing a state.
    """
    s = str(el).strip()
    declared = _declared_charge(s)
    if declared is not None:
        return declared
    m = re.match(r'^([A-Z][a-z]?)', s)
    return _OXI.get(m.group(1)) if m else None


def _declared_charge(el):
    """Oxidation state written into the species string itself (`Fe3+`, `O2-`), or None."""
    m = re.match(r'^[A-Z][a-z]?(\d+(?:\.\d+)?)([+-])$', str(el).strip())
    if not m:
        return None
    v = float(m.group(1))
    return v if m.group(2) == '+' else -v


# Default expected bond windows (Å), applied only to contacts that exist. Ranges are
# deliberately generous — they exist to catch a gross slip (an atom swapped or a site
# collapsed), not to judge fine chemistry; a refinement is free to sit anywhere inside.
_DEFAULT_BOND_WINDOWS = {
    # M-O of common ionic oxides
    'Mg-O': [1.9, 2.3], 'Al-O': [1.7, 2.1], 'Ca-O': [2.2, 2.7],
    'Sr-O': [2.4, 2.9], 'Ba-O': [2.6, 3.1], 'Zn-O': [1.8, 2.2],
    'Y-O': [2.1, 2.6], 'La-O': [2.3, 2.9], 'Zr-O': [1.9, 2.4],
    'Si-O': [1.5, 1.8], 'P-O': [1.4, 1.7], 'Ti-O': [1.7, 2.2],
    # alkali / halide / hydride-ish
    'Li-O': [1.8, 2.3], 'Na-O': [2.2, 2.7], 'K-O': [2.5, 3.2],
    'Li-F': [1.8, 2.2], 'Na-F': [2.1, 2.5],
    # bonds that appear in the PB / coordination-chemistry cases this plugin is used on
    'C-N': [1.05, 1.25], 'C-O': [1.15, 1.45], 'C-C': [1.30, 1.65],
    'N-O': [1.10, 1.35], 'S-O': [1.35, 1.60], 'P-Cl': [1.90, 2.15],
}


def _load_components(path):
    """components.json written by the manual python engine."""
    d = json.load(open(path, encoding='utf-8'))
    x = np.asarray(d['x'], dtype=float)
    yobs = np.asarray(d['yobs'], dtype=float)
    ycalc = np.asarray(d.get('ycalc', d['yobs']), dtype=float)
    bg = np.asarray(d.get('bg', np.zeros_like(x)), dtype=float)
    return x, yobs, ycalc, bg


def _strip_nulls(obj):
    """Drop null values recursively: the host rejects null keys in a tool payload,
    so a single None inside a declared property fails the whole result."""
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


def _cif_from_result(result_path, out_dir, base, plan):
    """Manual-engine result.json -> refined CIF (plus its components.json path).

    This closes the relay. gpx -> CIF -> manual engine already worked, but a
    manual fit could not be handed to GSAS-II because result.json carried no
    structure; it now carries a "structure" block and this turns it into a CIF.

    Returns (cif_path, components_path_or_None, info_dict).
    """
    from pymatgen.core import Lattice, Structure
    from pymatgen.io.cif import CifWriter

    d = json.load(open(result_path, encoding='utf-8'))
    st_in = d.get('structure')
    if not st_in:
        raise RuntimeError('%s has no "structure" block - it was written by an older '
                           'engine run; re-run xrd_refine_manual to get one'
                           % os.path.basename(result_path))
    lat = st_in.get('lattice') or {}
    latt = Lattice.from_parameters(
        float(lat.get('a')), float(lat.get('b')), float(lat.get('c')),
        float(lat.get('alpha', 90.0)), float(lat.get('beta', 90.0)),
        float(lat.get('gamma', 90.0)))
    species, coords, labels = [], [], []
    for s in (st_in.get('sites') or []):
        # Prefer the explicit site composition: a disordered site has several
        # elements and its "element" field is a composite string that pymatgen
        # cannot parse as an element.
        comp = s.get('composition')
        if isinstance(comp, dict) and comp:
            spec = {str(k): float(v) for k, v in comp.items() if str(k).strip()}
        else:
            el = str(s.get('element') or s.get('label') or '').strip()
            spec = {el: float(s.get('occ', 1.0))} if el else {}
        if not spec:
            continue
        species.append(spec)
        coords.append([float(s.get('x', 0.0)), float(s.get('y', 0.0)),
                       float(s.get('z', 0.0))])
        labels.append(str(s.get('label') or next(iter(spec))))
    if not species:
        raise RuntimeError('result structure block has no usable sites')
    st = Structure(latt, species, coords, coords_are_cartesian=False)

    symprec = float(plan.get('symprec', 0.1))
    cif_out = os.path.join(out_dir, base + '_refined.cif')
    detected = 'P1'
    try:
        CifWriter(st, symprec=symprec).write_file(cif_out)
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
        detected = str(SpacegroupAnalyzer(st, symprec=symprec).get_space_group_symbol())
    except Exception:
        CifWriter(st).write_file(cif_out)
    info = {'cif': cif_out, 'space_group': detected, 'symprec': symprec,
            'n_sites': len(species), 'source_cif': st_in.get('source_cif'),
            'metrics': d.get('metrics') or {}}
    bg = d.get('background') or {}
    if bg:
        info['background'] = bg
    return cif_out, (d.get('components') or None), info


def _ticks_from_phases(hist, phases, xmin, xmax, want=None):
    """[(phase,pos,icorr,hkl)] from GSAS reflection lists (15-column layout)."""
    out = []
    try:
        refl = hist.reflections() or {}
    except Exception:
        return out
    for ph in phases:
        if want and ph.name not in want:
            continue
        ent = refl.get(ph.name)
        if not isinstance(ent, dict):
            continue
        a = np.asarray(ent.get('RefList'), dtype=float) if ent.get('RefList') is not None else None
        if a is None or a.ndim != 2 or a.shape[1] < 6:
            continue
        pos = a[:, 5]
        icorr = a[:, 11] if a.shape[1] > 11 else np.zeros(a.shape[0])
        keep = np.isfinite(pos) & (pos >= xmin) & (pos <= xmax)
        if np.any(keep):
            out.append((str(ph.name), pos[keep],
                        np.nan_to_num(icorr[keep], nan=0.0), a[keep][:, 0:3]))
    return out


def _hkl(h, k, l):
    def f(v):
        iv = int(round(float(v)))
        return '%d' % iv if iv >= 0 else '-%d' % abs(iv)
    return '(%s%s%s)' % (f(h), f(k), f(l))


def _plot_fit(x, yobs, ycalc, ybkg, png_path, opts, ticks=None):
    """Fit figure: obs / calc total / background / phase sum + residual panel +
    one row of reflection ticks per phase, with de-occlusion (legend in the
    emptiest corner, padded top, staggered hkl labels)."""
    warns = []
    ticks = ticks or []
    xmin, xmax = float(np.nanmin(x)), float(np.nanmax(x))
    xl = opts.get('xlim')
    if isinstance(xl, (list, tuple)) and len(xl) == 2:
        xmin, xmax = float(xl[0]), float(xl[1])
    resid = yobs - ycalc

    mplcache = os.path.join(os.path.dirname(os.path.abspath(png_path)), '.mplcache')
    os.makedirs(mplcache, exist_ok=True)
    os.environ['MPLCONFIGDIR'] = mplcache
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.transforms import blended_transform_factory
    from matplotlib import font_manager

    title = opts.get('title') or 'XRD result'
    formula = opts.get('formula') or ''
    need_cjk = any(ord(c) > 127 for c in (title + formula))
    have = set()
    try:
        have = {f.name for f in font_manager.fontManager.ttflist}
    except Exception:
        pass
    family = 'DejaVu Sans'
    ok_font = not need_cjk
    for cand in ('Microsoft YaHei', 'SimHei', 'SimSun', 'Noto Sans CJK SC'):
        if cand in have:
            family, ok_font = cand, True
            break
    if need_cjk and not ok_font:
        warns.append('no CJK font found; non-ASCII characters may not render')
    plt.rcParams['font.family'] = family
    plt.rcParams['axes.unicode_minus'] = False

    lo = float(np.nanmin([np.nanmin(ybkg), np.nanmin(ycalc), np.nanmin(yobs)]))
    hi = float(np.nanmax([np.nanmax(ycalc), np.nanmax(yobs)]))
    span = max(hi - lo, 1e-9)
    ylo, yhi = lo - 0.06 * span, hi + 0.24 * span

    # emptiest-corner search (avoid drawing the legend over peaks)
    corner, best = 'upper right', None
    w = 0.34 * (xmax - xmin)
    for c in ('upper right', 'upper left', 'lower right', 'lower left'):
        xa, xb = (xmin, xmin + w) if 'left' in c else (xmax - w, xmax)
        ya, yb = (ylo + 0.60 * span, yhi) if 'upper' in c else (ylo, ylo + 0.40 * span)
        m = (x >= xa) & (x <= xb) & np.isfinite(yobs)
        score = 0.0 if not np.any(m) else float(np.mean(np.clip(
            (yobs[m] - ya) / max(yb - ya, 1e-9), 0.0, 1.0)))
        if best is None or score < best:
            corner, best = c, score

    nph = len(ticks)
    if nph:
        fig = plt.figure(figsize=(10.0, 6.4 + 0.34 * nph))
        gs = fig.add_gridspec(3, 1, height_ratios=[3.2, 1.0, 0.42 * nph], hspace=0.07)
    else:
        # no reflection list (e.g. a manual-engine result) => do NOT leave an
        # empty tick panel behind
        fig = plt.figure(figsize=(10.0, 6.4))
        gs = fig.add_gridspec(2, 1, height_ratios=[3.2, 1.0], hspace=0.07)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1) if nph else None

    ax1.plot(x, ybkg, '-', color='#2ca02c', lw=1.1, label='Background', zorder=2)
    ax1.plot(x, ycalc - ybkg, '-', color='#d62728', lw=1.0,
             label='Calculated (phase sum)', zorder=3)
    ax1.plot(x, yobs, '.', color='#1f4fd8', ms=2.1, label='Observed', zorder=4)
    ax1.plot(x, ycalc, '-', color='#111111', lw=0.8, alpha=0.85,
             label='Calculated (total)', zorder=5)
    ax1.set_ylim(ylo, yhi)
    ax1.set_ylabel('Intensity (counts)')
    ax1.tick_params(labelbottom=False)
    ax1.legend(loc=corner, fontsize=8, ncol=2, framealpha=0.92)

    stats = []
    m = opts.get('stats') or {}
    if m.get('wR') is not None:
        stats.append('R$_w$ = %.2f%%' % m['wR'])
    for key, lab in (('R', 'R'), ('Rb', 'R$_b$'), ('gof', 'GOF')):
        if m.get(key) is not None:
            stats.append('%s = %.2f' % (lab, m[key]))
    if formula:
        stats.append(formula)
    if stats:
        sc = 'upper right' if corner.startswith('upper left') else 'upper left'
        lines = ['   '.join(stats[:3])]
        if len(stats) > 3:
            lines.append('   '.join(stats[3:]))
        ax1.text(0.985 if 'right' in sc else 0.015,
                 0.975 if 'upper' in sc else 0.03, '\n'.join(lines),
                 transform=ax1.transAxes, va='top' if 'upper' in sc else 'bottom',
                 ha='right' if 'right' in sc else 'left', fontsize=9,
                 bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='0.6', alpha=0.88))

    topn = int(opts.get('top_peaks') or 6)
    if opts.get('list_ticks', True) is not False and topn > 0 and ticks:
        pool = []
        for nm, pos, icorr, hkl in ticks:
            m2 = np.isfinite(pos) & (pos >= xmin) & (pos <= xmax)
            idx = np.where(m2)[0]
            order = idx[np.argsort(-np.nan_to_num(icorr[idx], nan=0.0))][:topn]
            for j in order:
                pool.append((float(icorr[j]), float(pos[j]), _hkl(*hkl[j])))
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
            ax1.annotate(lbl, xy=(p, yv), xytext=(0, 6 + 13 * (len(placed) % 2)),
                         textcoords='offset points', ha='center', fontsize=7,
                         color='0.15',
                         bbox=dict(boxstyle='round,pad=0.12', fc='white', ec='none',
                                   alpha=0.75),
                         arrowprops=dict(arrowstyle='-', lw=0.5, color='0.5'))
            placed.append((p, lbl))

    rmax = float(np.nanmax(np.abs(resid)))
    rmax = rmax if np.isfinite(rmax) and rmax > 0 else 1.0
    ax2.plot(x, resid, '-', color='#333333', lw=0.7)
    ax2.axhline(0, color='0.55', lw=0.6)
    ax2.set_ylim(-1.25 * rmax, 1.25 * rmax)
    ax2.set_ylabel('Obs - Calc', fontsize=9)
    ax2.tick_params(labelbottom=False)

    if ax3 is not None:
        ax3.set_yticks([])
        ax3.set_ylim(nph - 0.5, -0.5)
        colors = ['#1f77b4', '#d62728', '#2ca02c', '#9467bd', '#ff7f0e', '#8c564b']
        for i, (nm, pos, icorr, hkl) in enumerate(ticks):
            ax3.vlines(pos, i + 0.16, i + 0.84, color=colors[i % len(colors)], lw=0.9)
            ax3.text(-0.007, i, str(nm),
                     transform=blended_transform_factory(ax3.transAxes, ax3.transData),
                     ha='right', va='center', fontsize=8)
        ax3.set_xlabel('2-theta (deg)')
        for ax in (ax2, ax3):
            ax.spines['top'].set_visible(False)
            ax.tick_params(axis='x', labelsize=9)
        for ax in (ax1, ax2, ax3):
            ax.set_xlim(xmin, xmax)
    else:
        ax2.set_xlabel('2-theta (deg)')
        ax2.spines['top'].set_visible(False)
        ax2.tick_params(axis='x', labelsize=9, labelbottom=True)
        for ax in (ax1, ax2):
            ax.set_xlim(xmin, xmax)
    fig.suptitle(title, fontsize=12)
    fig.savefig(png_path, dpi=int(opts.get('dpi') or 300), bbox_inches='tight')
    plt.close(fig)
    if not (os.path.isfile(png_path) and os.path.getsize(png_path) > 0):
        raise RuntimeError('figure was not written: ' + png_path)
    return {'figure': png_path, 'corner': corner, 'warning': warns}


def _pget(d, key, default):
    """Value or default, treating an explicit null as "not set".

    The tool layer sends null for optional numbers (e.g. uiso_flag_above,
    min_distance); dict.get(key, default) returns that None instead of the
    default, which crashed float(None) on the first real tool call."""
    try:
        v = d.get(key)
    except Exception:
        return default
    return default if v is None else v


def scan_anomalies(gpx, phases, plan):
    """Collect physically impossible / suspicious values WITHOUT judging them.

    User requirement (2026-09-17): when a refinement fails, the unscientific
    values themselves ARE the diagnostic - so they are always reported, their
    presence never blocks a CIF from being written, and this scan is the only
    thing that runs when sanity checking is switched off.
    """
    out = []
    umax = float(_pget(plan, 'uiso_flag_above', 0.2))
    for ph in phases:
        rows = phase_atoms(ph)
        for r in rows:
            u = r.get('uiso')
            if u is not None:
                if u <= 0.0:
                    out.append({'phase': str(ph.name), 'kind': 'uiso_nonpositive',
                                'atom': r['label'], 'value': u})
                elif u > umax:
                    out.append({'phase': str(ph.name), 'kind': 'uiso_large',
                                'atom': r['label'], 'value': u})
            occ = r.get('occ')
            if occ is not None and (occ < 0.0 or occ > 1.0):
                out.append({'phase': str(ph.name), 'kind': 'occupancy_out_of_range',
                            'atom': r['label'], 'value': occ})
            for ax in ('x', 'y', 'z'):
                v = r.get(ax)
                if v is not None and (v < -0.5 or v > 1.5):
                    out.append({'phase': str(ph.name), 'kind': 'coord_out_of_cell',
                                'atom': r['label'], 'axis': ax, 'value': v})
        try:
            a, b, c, al, be, ga = phase_cell(ph)
            for name, v in (('a', a), ('b', b), ('c', c)):
                if v <= 0.5 or v > 100.0:
                    out.append({'phase': str(ph.name), 'kind': 'cell_length_implausible',
                                'axis': name, 'value': v})
            for name, v in (('alpha', al), ('beta', be), ('gamma', ga)):
                if v < 5.0 or v > 175.0:
                    out.append({'phase': str(ph.name), 'kind': 'cell_angle_implausible',
                                'axis': name, 'value': v})
        except Exception as e:
            out.append({'phase': str(ph.name), 'kind': 'cell_unreadable',
                        'detail': str(e)})
        try:
            for hn, hp in (ph.data.get('Histograms') or {}).items():
                sc = None
                try:
                    sc = _num(hp['Scale'][0])
                except Exception:
                    pass
                if sc is not None and (sc <= 0.0 or sc > 1e6):
                    out.append({'phase': str(ph.name), 'kind': 'phase_scale_extreme',
                                'histogram': str(hn), 'value': sc})
        except Exception:
            pass
    try:
        res = dict(gpx.histograms()[0].residuals or {})
        wR = _num(res.get('wR'))
        if wR is not None and wR >= 50.0:
            out.append({'kind': 'wR_high', 'value': wR})
    except Exception:
        pass

    # ---- phase weight / vanishing phase (measured on a real two-phase project) ---
    # A two-phase refinement may legitimately converge with one phase at ZERO weight —
    # that IS a solution (user, 2026-09-21: "一相为 0 也是解"). But then the delivered
    # model is effectively single-phase, and nothing in the previous output said so:
    # measured on the SampleA cubic+monoclinic project, all three degenerate start points
    # reported only wR_high + coord_out_of_cell. GSAS also DROPS the scale parameter
    # ("Parameter(s) dropped: 0:0:Scale"), i.e. it stops refining it without failing.
    # This is reported, never blocked: it is a finding about the model, not an error.
    try:
        _ph_scales = []
        for ph in (phases or []):
            sd = (ph.data or {}).get('Histograms') or {}
            for _hn, blk in sd.items():
                try:
                    s = _num((blk.get('Scale') or [None])[0])
                except Exception:
                    s = None
                if s is not None:
                    _ph_scales.append((str(ph.name), float(s)))
                break
        if len(_ph_scales) >= 2:
            total = sum(max(s, 0.0) for _n, s in _ph_scales)
            zero = [n for n, s in _ph_scales if total > 0 and (max(s, 0.0) / total) < 1e-6]
            out.append({'kind': 'phase_weights', 'value': {n: s for n, s in _ph_scales},
                        'fraction': {n: (max(s, 0.0) / total if total > 0 else None)
                                     for n, s in _ph_scales}})
            if zero and len(zero) < len(_ph_scales):
                out.append({'kind': 'phase_vanished', 'phases': zero,
                            'note': ('one or more phases carry no weight after the '
                                     'refinement, so the delivered model is effectively '
                                     'single-phase. This can be a legitimate solution, but '
                                     'it must be stated: a two-phase model that collapsed '
                                     'to one phase is not evidence that the second phase '
                                     'is present.')})
            elif zero:
                out.append({'kind': 'all_phases_zero',
                            'note': ('EVERY phase carries zero weight - the model '
                                     'explains nothing; treat the refinement as failed '
                                     'rather than as a single-phase result.')})
    except Exception as e:
        out.append({'kind': 'phase_weight_scan_failed', 'detail': str(e)})
    return out


# ---------------------------------------------------------------------------
def _scan_components(x, yobs, ycalc, plan):
    """Diagnostics for a manual-engine fit (no structure to bond-check)."""
    out = []
    try:
        resid = yobs - ycalc
        rms = float(np.sqrt(np.nanmean(resid ** 2)))
        out.append({'kind': 'residual_rms', 'value': round(rms, 4)})
        n = int(plan.get('worst_points') or 5)
        idx = np.argsort(-np.abs(np.nan_to_num(resid)))[:n]
        for i in idx:
            out.append({'kind': 'worst_residual',
                        'value': round(float(resid[i]), 3),
                        'position': round(float(x[i]), 4)})
    except Exception as e:
        out.append({'kind': 'residual_scan_failed', 'detail': str(e)})
    return out


def _main_other(plan, outres):
    """Output path for data that has NO GSAS project.

    User requirement (2026-09-17): the SAME output port must serve data that
    cannot be Rietveld-refined at all (hard carbon / amorphous-dominated /
    impurity-only patterns) as well as refined results. Accepted inputs:
      * components : manual-engine components.json (x/yobs/ycalc/bg)
      * data       : raw 2theta-intensity file (plotted without a model)
      * cif        : a bare structure file to check (no project needed)
    """
    out_dir = plan.get('out_dir') or os.path.dirname(os.path.abspath(outres))
    os.makedirs(out_dir, exist_ok=True)
    warnings = []
    figures = []
    cifs = []
    sanity = []
    anomalies = []
    result = {'ok': False}
    try:
        comp = plan.get('components')
        data = plan.get('data')
        cif_in = plan.get('cif')
        res_in = plan.get('result')
        stem = os.path.basename(comp or data or cif_in or res_in or 'output')
        base = _safe(plan.get('sample') or os.path.splitext(stem)[0])
        opts = {'title': plan.get('title') or base,
                'formula': plan.get('formula') or '',
                'dpi': plan.get('dpi') or 300,
                'top_peaks': plan.get('top_peaks'),
                'list_ticks': plan.get('list_ticks'),
                'xlim': plan.get('xlim'),
                'stats': plan.get('stats') or {}}
        source = 'cif' if cif_in else 'unknown'
        result_meta = None
        if res_in and os.path.isfile(res_in):
            # manual-engine result.json -> refined CIF (+ figure from its
            # components.json). This is the relay the manual engine was missing:
            # it can now hand its refined structure to GSAS-II and to the
            # delivery path, the same way a converged .gpx can.
            source = 'result'
            cif_path, comp_from_result, result_meta = _cif_from_result(
                res_in, out_dir, base, plan)
            cifs.append(cif_path)
            if plan.get('sanity', True) is not False:
                s = sanity_check(cif_path, plan)
                s['phase'] = 'from-manual-result'
                s['mode'] = 'manual-result'
                sanity.append(s)
            if comp_from_result and os.path.isfile(comp_from_result):
                x, yobs, ycalc, bg = _load_components(comp_from_result)
                anomalies.extend(_scan_components(x, yobs, ycalc, plan))
                if plan.get('figure', True) is not False:
                    f = _plot_fit(x, yobs, ycalc, bg,
                                  os.path.join(out_dir, base + '_fit.png'), opts, [])
                    figures.append(f['figure'])
                    warnings.extend(f.get('warning') or [])
            else:
                warnings.append('manual result carries no components.json, so only the '
                                'CIF was written (no fit figure)')
        elif comp and os.path.isfile(comp):
            source = 'components'
            x, yobs, ycalc, bg = _load_components(comp)
            anomalies.extend(_scan_components(x, yobs, ycalc, plan))
            if plan.get('figure', True) is not False:
                f = _plot_fit(x, yobs, ycalc, bg,
                              os.path.join(out_dir, base + '_fit.png'), opts, [])
                figures.append(f['figure'])
                warnings.extend(f.get('warning') or [])
        elif data and os.path.isfile(data):
            source = 'data'
            # tolerant two-column reader: skips headers, comments, blank lines
            # and any non-numeric row (vendor text exports often carry a column
            # header line that makes np.loadtxt fail outright)
            xs, ys, skipped = [], [], 0
            with open(data, encoding='utf-8', errors='replace') as fh:
                for line in fh:
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
                    xs.append(xv)
                    ys.append(yv)
            if len(xs) < 10:
                raise RuntimeError('could not read >=10 numeric (2theta, intensity) rows from '
                                   '%s (%d non-numeric line(s) skipped)' % (data, skipped))
            x = np.asarray(xs, dtype=float)
            yobs = np.asarray(ys, dtype=float)
            keep = np.isfinite(x) & np.isfinite(yobs)
            x, yobs = x[keep], yobs[keep]
            if skipped:
                warnings.append('skipped %d non-numeric line(s) (header/comment) in %s'
                                % (skipped, os.path.basename(data)))
            warnings.append('raw data only: no fit model exists, so the calculated trace '
                            'coincides with the observations (run xrd_refine_auto / '
                            'xrd_refine_manual / xrd_gsas_ops first for a model)')
            anomalies.append({'kind': 'no_fit_model',
                              'detail': 'raw pattern plotted without a calculation'})
            if plan.get('figure', True) is not False:
                f = _plot_fit(x, yobs, yobs.copy(), np.zeros_like(yobs),
                              os.path.join(out_dir, base + '_fit.png'), opts, [])
                figures.append(f['figure'])
                warnings.extend(f.get('warning') or [])
        if cif_in and os.path.isfile(cif_in):
            cifs.append(cif_in)
            if plan.get('sanity', True) is not False:
                s = sanity_check(cif_in, plan)
                s['phase'] = 'from-file'
                s['mode'] = 'file'
                sanity.append(s)
        if not figures and not cifs:
            raise RuntimeError('nothing to output: pass gpx, result, components, data or cif')
        if plan.get('sanity', True) is False:
            verdict = 'skipped'
        else:
            verdict = 'pass'
            for s in sanity:
                if s.get('parse_error'):
                    verdict = 'warn'
                if s.get('bad_contacts') or s.get('bond_violations') or s.get('occ_violations'):
                    verdict = 'fail'
                if s.get('charge_violations'):
                    # A charge imbalance is a FLAG, not a proof of a bad model (the
                    # assumed oxidation states can be the wrong ones), so it downgrades
                    # to warn instead of fail — but it must be visible.
                    verdict = 'fail' if verdict == 'fail' else 'warn'
            if verdict == 'pass' and anomalies:
                verdict = 'warn'
        result = {
            'ok': True,
            'verdict': verdict,
            'source': source,
            'figures': figures,
            'cifs': cifs,
            'sanity': sanity,
            'anomalies': anomalies,
            'warning': warnings,
            'summary': 'xrd_export_cif (%s): %d figure(s), %d CIF(s), verdict=%s%s' % (
                source, len(figures), len(cifs), verdict,
                (', %d anomalous value(s)' % len(anomalies)) if anomalies else ''),
        }
        if result_meta:
            result['manual_result'] = _strip_nulls(result_meta)
        log('EXPORT_OK', source, verdict, figures, cifs)
    except Exception as e:
        tb = traceback.format_exc()
        log('EXPORT_FAILED:', e)
        log(tb[-1500:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-1500:]}
        if warnings:
            result['warning'] = warnings
    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


def main():
    if len(sys.argv) < 2:
        raise SystemExit('plan JSON required')
    planf = sys.argv[1]
    outres = None
    for i, a in enumerate(sys.argv[2:]):
        if a == '--out':
            outres = sys.argv[3 + i]
    plan = json.load(open(planf, encoding='utf-8'))
    guard = start_guard(plan, backend='export', out_path=outres)   # P1 (0.8.4)
    outres = outres or os.path.splitext(planf)[0] + '_result.json'
    out_dir = plan.get('out_dir') or os.path.dirname(os.path.abspath(outres))
    os.makedirs(out_dir, exist_ok=True)
    gsas_root = plan.get('gsas_root') or os.environ.get('DSH_GSAS_ROOT') or ''
    if not gsas_root:
        raise RuntimeError(
            'GSAS-II root not configured: pass "gsas_root" in the plan, set DSH_GSAS_ROOT, '
            'or add "gsas_root" to xrd.local.json')
    sys.path.insert(0, gsas_root)

    # ONE output port, several sources (user requirement 2026-09-17): data that
    # cannot be Rietveld-refined must still be outputtable. A .gpx refined
    # project goes down the project path below; components.json (manual engine),
    # raw two-column data and a bare CIF go to _main_other().
    if not plan.get('gpx'):
        return _main_other(plan, outres)

    warnings = []
    result = {'ok': False}
    result_calibration = ''
    try:
        from GSASII import GSASIIscriptable as G2sc
        gpx_path = plan['gpx']
        if not (os.path.isfile(gpx_path) and os.path.getsize(gpx_path) > 0):
            raise RuntimeError('gpx not found or empty: %s' % gpx_path)
        sample = _safe(plan.get('sample') or
                       os.path.splitext(os.path.basename(gpx_path))[0])
        # A safe point before the export work (0.8.5): no checkpoint existed here, so a
        # stop request could only be answered by a tree kill.
        _g = active_guard()
        if _g is not None:
            _g.checkpoint('export %s' % sample)
        gpx = G2sc.G2Project(gpxfile=gpx_path)
        phases = gpx.phases()
        if not phases:
            raise RuntimeError('project has no phases')
        pick = plan.get('phases') or ['all']
        if pick != ['all']:
            phases = [p for p in phases if p.name in pick]
            if not phases:
                raise RuntimeError('none of the requested phases exist')

        mode = str(plan.get('mode') or 'structured')
        ucfg = plan.get('uiso') or {'mode': 'keep'}
        # Sanity / diagnostics are OPTIONAL (user requirement 2026-09-17): a
        # failed refinement is diagnosed BY reading its unscientific values, so
        # the CIF is always written first and the checks never block output.
        #   sanity: false      -> skip pymatgen bond/occupancy validation
        #   diagnostics: false -> skip the anomaly scan as well
        do_sanity = plan.get('sanity', True) is not False
        anomalies = (scan_anomalies(gpx, phases, plan)
                     if plan.get('diagnostics', True) is not False else [])
        cifs = []
        sanity = []
        for ph in phases:
            rows = phase_atoms(ph)
            base = '%s_%s' % (sample, _safe(ph.name))
            files = []
            if mode in ('structured', 'both'):
                p = os.path.join(out_dir, base + '.cif')
                comment = plan.get('comment')
                if not comment:
                    try:
                        wR = _num(gpx.histograms()[0].get_wR())
                    except Exception:
                        wR = None
                    comment = '%s / %s (GSAS-II)' % (ph.name, sample)
                    if wR is not None:
                        comment += ', wR = %.2f%%' % wR
                write_structured_cif(p, base, ph, rows, symmetry_ops(ph), ucfg,
                                     comment=comment)
                files.append(p)
            if mode in ('official', 'both'):
                p = os.path.join(out_dir, base + '_gsas.cif')
                try:
                    ph.export_CIF(p)
                    if os.path.isfile(p) and os.path.getsize(p) > 0:
                        files.append(p)
                    else:
                        warnings.append('official export produced no file for %s' % ph.name)
                except Exception as e:
                    warnings.append('official export failed for %s: %s' % (ph.name, e))
            if not files:
                warnings.append('no CIF written for phase %s' % ph.name)
            cifs.extend(files)
            if do_sanity:
                for f in files:
                    s = sanity_check(f, plan)
                    s['phase'] = str(ph.name)
                    s['mode'] = 'official' if f.endswith('_gsas.cif') else 'structured'
                    sanity.append(s)

        # ---- figure: always produced, so this tool is a COMPLETE output port
        # (figure + GSAS project + CIF + verdict), not just a CIF writer ------
        figures = []
        if plan.get('figure', True) is not False:
            try:
                def _A(v):
                    if isinstance(v, np.ma.MaskedArray):
                        return np.asarray(np.ma.filled(v.astype(float), np.nan), dtype=float)
                    return np.asarray(v, dtype=float)

                h0 = gpx.histograms()[0]
                x_all = _A(h0.getdata('X'))
                xmin0, xmax0 = float(np.nanmin(x_all)), float(np.nanmax(x_all))
                ticks = _ticks_from_phases(h0, phases, xmin0, xmax0,
                                           pick if pick != ['all'] else None)
                st = {}
                try:
                    res = dict(h0.residuals or {})
                    for k in ('wR', 'R', 'Rb'):
                        v = _num(res.get(k))
                        if v is not None:
                            st[k] = v
                except Exception:
                    pass
                f = _plot_fit(x_all, _A(h0.getdata('Yobs')), _A(h0.getdata('Ycalc')),
                              _A(h0.getdata('Background')),
                              os.path.join(out_dir, sample + '_fit.png'),
                              {'title': plan.get('title') or
                                        ('%s - GSAS-II Rietveld refinement' % sample),
                               'formula': plan.get('formula') or '',
                               'dpi': plan.get('dpi') or 300,
                               'top_peaks': plan.get('top_peaks'),
                               'list_ticks': plan.get('list_ticks'),
                               'xlim': plan.get('xlim'),
                               'stats': st}, ticks)
                figures.append(f['figure'])
                warnings.extend(f.get('warning') or [])
            except Exception as e:
                warnings.append('figure generation failed: %s' % e)

        # ---- GSAS project copy: the port hands over the COMPLETE structure
        # (figure + .gpx + CIF), so the caller never has to assemble the set ---
        gpx_copy = None
        if plan.get('gpx_copy', True) is not False and gpx_path:
            try:
                gpx_copy = os.path.join(out_dir, sample + '_exported.gpx')
                if os.path.abspath(gpx_copy) != os.path.abspath(gpx_path):
                    with open(gpx_path, 'rb') as fi, open(gpx_copy, 'wb') as fo:
                        fo.write(fi.read())
                    if not (os.path.isfile(gpx_copy) and os.path.getsize(gpx_copy) > 0):
                        raise RuntimeError('copy is empty')
                else:
                    gpx_copy = gpx_path
            except Exception as e:
                warnings.append('gpx copy failed: %s' % e)
                gpx_copy = None

        verdict = 'pass'
        if not do_sanity:
            verdict = 'skipped'
        for s in sanity:
            if s.get('parse_error'):
                verdict = 'warn'
                warnings.append('%s: CIF could not be re-parsed (%s)'
                                % (s.get('file'), s['parse_error']))
            if s.get('bad_contacts'):
                verdict = 'fail'
            if s.get('bond_violations'):
                verdict = 'fail'
                warnings.append('%s: bond window violation(s): %s'
                                % (s.get('phase'), json.dumps(s['bond_violations'])))
            if s.get('occ_violations'):
                verdict = 'fail'
                warnings.append('%s: occupancy outside (0,1]: %s'
                                % (s.get('phase'), json.dumps(s['occ_violations'])))
            # Charge balance was collected but never consulted on THIS path, so a CIF
            # whose only defect is a charge imbalance reported verdict:pass here while
            # the CIF path (line ~925) downgraded it — the same input judging
            # differently depending on which route produced it
            # (audit 2026-09-21 third pass).
            if s.get('charge_violations'):
                verdict = 'fail' if verdict == 'fail' else 'warn'
                warnings.append('%s: charge imbalance: %s'
                                % (s.get('phase'), json.dumps(s['charge_violations'])))
        if anomalies:
            # The verdict must reflect the anomalies (audit 2026-09-21): negative Uiso
            # and charge imbalance were collected here but the verdict stayed 'pass',
            # so a run could report ok:true + verdict:pass while carrying them.
            # Severity is deliberate: only structural violations fail; wR and residual
            # statistics are DIAGNOSTIC (the whole point of this port is to hand back
            # unscientific values for diagnosis), so they downgrade to 'warn'.
            _fatal_kinds = {'uiso_nonpositive', 'occupancy_out_of_range',
                            'coord_out_of_cell', 'cell_unreadable'}
            _kinds = {str(a.get('kind')) for a in anomalies}
            if _kinds & _fatal_kinds:
                verdict = 'fail'
                warnings.append(
                    'anomalous structural value(s) present (%s) - reported, never blocked, '
                    'but this structure must not be reported as a finished result'
                    % ', '.join(sorted(_kinds & _fatal_kinds)))
            elif _kinds and verdict == 'pass':
                verdict = 'warn'
            warnings.append('%d anomalous value(s) detected - they are REPORTED, never '
                            'blocked: unscientific values are the evidence used to '
                            'diagnose a failed refinement; fix them by hand-refining '
                            'with xrd_gsas_ops' % len(anomalies))

        # verdict summary LAST, so it reflects every contribution above (the sanity
        # loop AND the anomalies) instead of only the first one.
        if verdict == 'fail':
            warnings.append('SANITY FAILED - the figure, the CIF and the anomalies were '
                            'STILL produced (kept for diagnosis); do not report this '
                            'structure as a finished result until the violation is explained')
        elif verdict == 'warn':
            warnings.append('sanity returned warnings - review before reporting')

        # ---- calibration of what the figure and the wR actually describe -------
        # (user decision 2026-09-21: (b) is the implementation — rewrite the Uiso in
        # the CIF and LABEL the provenance clearly. (a) "re-run the refinement so the
        # figure/wR describe the rewritten model" is NOT done by this tool: claiming
        # it would be a false statement, and an unused switch that only declares the
        # intent is interface bloat. The contract is therefore: this field says which
        # model the numbers belong to, and the warning says how to get the other one.)
        uiso_mode = str(((plan.get('uiso') or {}).get('mode') or 'keep')).lower()
        _wrote_structured = mode in ('structured', 'both')
        if uiso_mode == 'physical' and not _wrote_structured:
            # mode=official only calls ph.export_CIF(), which writes GSAS-II's own CIF
            # and never goes through write_structured_cif — the ONLY place the Uiso is
            # clamped. Claiming "the CIF was rewritten" here was false: the official
            # CIF still carried the unclamped (possibly negative) values (audit
            # 2026-09-21, reproduced in isolation).
            warnings.append(
                'CALIBRATION: uiso.mode=physical was requested but NO structured CIF was '
                'written (mode=%s), and only the structured writer applies the Uiso '
                'clamp — the official GSAS-II CIF(s) here still carry the UNCLAMPED '
                'values. Re-run with mode=structured or mode=both to get a clamped CIF, '
                'and do not describe these CIFs as physically clamped.' % mode)
            result_calibration = ('uiso clamp NOT applied: mode=%s writes only the official '
                                  'GSAS CIF, which bypasses the clamp; figure/wR are the '
                                  'original gpx model' % mode)
        elif uiso_mode == 'physical' and mode == 'both':
            warnings.append(
                'CALIBRATION: uiso.mode=physical with mode=both — the STRUCTURED CIF '
                'carries the clamped Uiso, while the OFFICIAL *_gsas.cif does NOT (it '
                'bypasses the clamp). The figure and the wR belong to the ORIGINAL gpx '
                'model, which is the project to refine in GSAS-II for the final fit '
                'result; report the clamped CIF and the statistics separately.')
            result_calibration = ('structured CIF clamped; official *_gsas.cif NOT clamped; '
                                  'figure/wR from the ORIGINAL gpx (the GSAS-II refinement '
                                  'project that reaches the final fit result)')
        elif uiso_mode == 'physical':
            warnings.append(
                'CALIBRATION: uiso.mode=physical rewrote the Uiso values in the CIF '
                'ONLY — re-running the refinement to match is NOT performed by this '
                'tool (user decision 2026-09-21: not practical). Consequences, stated '
                'plainly: (1) the figure and the wR above belong to the ORIGINAL gpx '
                'model, NOT to the clamped CIF, so never present them as the result of '
                'the clamped CIF; (2) the ORIGINAL gpx is the refinement project to '
                'hand to GSAS-II — refining it there is what reaches the final fit '
                'result; (3) report the clamped CIF and the refinement statistics '
                'separately, or export the gpx after refining it.')
            result_calibration = ('uiso rewritten in the structured CIF only (no '
                                  're-refinement); figure/wR from the ORIGINAL gpx, which '
                                  'is the GSAS-II refinement project that reaches the final '
                                  'fit result')
        else:
            result_calibration = 'figure/wR and CIF describe the same gpx model'

        result = {
            'ok': True,
            'verdict': verdict,
            'source': 'gpx',
            'figures': figures,
            'cifs': cifs,
            'sanity': sanity,
            'anomalies': anomalies,
            'warning': warnings,
            'calibration': result_calibration,
            'summary': 'xrd_export_cif (gpx): %d figure(s), %d CIF(s), verdict=%s, '
                       '%d anomalous value(s)' % (len(figures), len(cifs), verdict,
                                                  len(anomalies)),
        }
        if gpx_copy:
            result['gpx'] = gpx_copy
        log('EXPORT_OK', verdict, cifs)
    except Exception as e:
        tb = traceback.format_exc()
        log('EXPORT_FAILED:', e)
        log(tb[-2500:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-2000:]}
        if warnings:
            result['warning'] = warnings

    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


if __name__ == '__main__':
    run_guarded_main(main)
