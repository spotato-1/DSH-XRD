#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_cif_edit engine - READ / EDIT / BUILD / FETCH CIF files.

User requirement (2026-09-17): "xrd_cif_edit 这个是关键拟合方法" - editing a known
structure is what actually cracked the PB case (dropping Sb from the literature
monoclinic P2_1/c cell took the single-phase fit from 19.4% to 14.5%), and the
AI must also be able to CREATE a CIF from structures it obtains from a crystal
database (Materials Project / COD) or from parameters read out of a paper.

ACTIONS (plan JSON, argv[1]; result JSON via --out):
  {"type":"read","cif":"x.cif"}
      parse and report space group / cell / sites with occupancies
  {"type":"fetch_cod","query":"Na2MoO4","max":10,"elements":["Na","Mo","O"]}
      search the Crystallography Open Database and download the CIFs
  {"type":"fetch_mp","chemsys":"Na-Mo-O"} or {"type":"fetch_mp","formula":"Na2MoO4"}
      search the Materials Project (needs MP_API_KEY / .pmgrc.yaml) and write CIFs
  {"type":"edit_text","cif":"in.cif","out":"out.cif", <changes>}
      line-level edit that PRESERVES the original symmetry block (the PB recipe)
  {"type":"edit_structure","cif":"in.cif","out":"out.cif", <changes>}
      structure-level edit through pymatgen, re-symmetrised on output
  {"type":"build","spacegroup":"P 21/c","cell":{...},"sites":[...],"out":"new.cif"}
      create a CIF from parameters (paper / nominal composition / model)
  {"type":"symmetrize","cif":"in.cif","symprec":0.01,"out":"symm.cif"}
  {"type":"validate","cif":"x.cif","declared_spacegroup":"optional override",
   "contact_limit":1.0}
      professional audit of a hand-built CIF BEFORE it is trusted: is the file
      consistent with the symmetry it declares?  Use when the crystal database
      and the literature search both fail and the structure has to be written
      by hand (user requirement 2026-09-19).
  {"type":"build_orbit","spacegroup":"R -3 m","cell":{"a":2.87,"c":14.3,...},
   "sites":[{"element":"K","x":0,"y":0,"z":0,"occ":0.8}],"out":"new.cif"}
      build a CIF the professional way: give the space group + cell + the
      Wyckoff REPRESENTATIVE of each site, and let the space group's own orbit
      generate every equivalent position.  Reports the Wyckoff letter and
      multiplicity pymatgen assigns (3a/3b/6c ...), so the model can be checked
      against the literature before any refinement.

<changes> keys (shared by edit_text / edit_structure):
  replace_element : {"Sb":"Mo"}                 element-wide substitution
  rename_sites    : {"Sb1":"Mo1"}               relabel sites
  set_occ         : {"Sb1":0.5}                 occupancy
  set_site_composition : {"TM1":{"Mn":0.6,"Ni":0.4}}   multi-element site
  set_coord       : {"O1":[0.1,0.2,0.3]}        fractional coordinates
  set_cell        : {"a":..,"b":..,"c":..,"alpha":..,"beta":..,"gamma":..}
  drop_sites      : ["Sb1"] / [2]
  add_sites       : [{"label":"Mo1","element":"Mo","x":0,"y":0,"z":0,"occ":1.0}]

Nothing is ever validated here on purpose: a structure that is chemically wrong
is still written out, because the point is to try candidate models and let the
fit decide (see the tool red line: unscientific values must stay outputtable).
"""
import os
import sys
import json

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard
import math
import time
import shutil
import urllib.parse
import urllib.request
import traceback


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


def _http_get(url, timeout=30, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'dsh-xrd/1.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    log('GET failed %s : %s' % (url, last))
    return None


# ---------------------------------------------------------------------------
# structure view helpers
# ---------------------------------------------------------------------------
def _sg_of(st):
    try:
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
        return str(SpacegroupAnalyzer(st).get_space_group_symbol())
    except Exception as e:
        return ''


def site_rows(st):
    rows = []
    for i, site in enumerate(st):
        comp = site.species
        parts = []
        tot = 0.0
        for el, occ in comp.items():
            parts.append('%s%.3f' % (el.symbol, occ))
            tot += occ
        el = max(comp.items(), key=lambda kv: kv[1])[0].symbol if comp else '?'
        rows.append({
            'label': str(getattr(site, 'label', '') or ('%s%d' % (el, i + 1))),
            'index': i,
            'element': el,
            'composition': ''.join(parts),
            'occ': _num(tot),
            'x': _num(site.frac_coords[0]),
            'y': _num(site.frac_coords[1]),
            'z': _num(site.frac_coords[2]),
        })
    return rows


def structure_summary(st, path, source):
    cell = {'a': _num(st.lattice.a), 'b': _num(st.lattice.b), 'c': _num(st.lattice.c),
            'alpha': _num(st.lattice.alpha), 'beta': _num(st.lattice.beta),
            'gamma': _num(st.lattice.gamma), 'volume': _num(st.lattice.volume)}
    return {'path': path, 'source': source, 'space_group': _sg_of(st),
            'formula': str(st.composition.reduced_formula),
            'n_sites': len(st), 'cell': {k: v for k, v in cell.items() if v is not None},
            'sites': site_rows(st)}


def load_structure(cif):
    from pymatgen.io.cif import CifParser
    st = CifParser(cif).parse_structures(primitive=False)[0]
    return st


def write_structure(st, out, symprec=0.01):
    from pymatgen.io.cif import CifWriter
    try:
        CifWriter(st, symprec=symprec).write_file(out)
    except Exception as e:
        log('CifWriter symprec failed (%s), retrying without symmetry' % e)
        CifWriter(st).write_file(out)
    return out


# ---------------------------------------------------------------------------
# line-level CIF editing (keeps the original symmetry block intact)
# ---------------------------------------------------------------------------
ATOM_TAGS = ['_atom_site_label', '_atom_site_type_symbol',
             '_atom_site_fract_x', '_atom_site_fract_y', '_atom_site_fract_z',
             '_atom_site_occupancy', '_atom_site_U_iso_or_equiv']


def _read_lines(cif):
    with open(cif, encoding='utf-8', errors='replace') as f:
        return f.read().splitlines()


def _atom_table(lines):
    """Locate the _atom_site loop: returns (header_idx, col_index, row_idx[])."""
    cols, header_idx, rows = [], None, []
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if s == 'loop_':
            j = i + 1
            tags = []
            while j < n and lines[j].strip().startswith('_'):
                tags.append(lines[j].strip().split()[0].lower())
                j += 1
            if any(t.startswith('_atom_site_') for t in tags):
                cols = tags
                header_idx = j - 1
                k = j
                while k < n:
                    t = lines[k].strip()
                    if not t or t.startswith('#') or t.startswith('_') or t == 'loop_':
                        break
                    rows.append(k)
                    k += 1
                return header_idx, {c: idx for idx, c in enumerate(cols)}, rows
            i = j
            continue
        i += 1
    return None, {}, []


def _col(cols, *names):
    for nm in names:
        if nm in cols:
            return cols[nm]
    return None


def edit_text(cif, out, ch):
    lines = _read_lines(cif)
    hidx, cols, rows = _atom_table(lines)
    if hidx is None:
        raise RuntimeError('no _atom_site loop found in %s' % cif)
    ci_label = _col(cols, '_atom_site_label')
    ci_type = _col(cols, '_atom_site_type_symbol')
    ci_x = _col(cols, '_atom_site_fract_x')
    ci_y = _col(cols, '_atom_site_fract_y')
    ci_z = _col(cols, '_atom_site_fract_z')
    ci_occ = _col(cols, '_atom_site_occupancy')
    rep = ch.get('replace_element') or {}
    ren = {str(k): str(v) for k, v in (ch.get('rename_sites') or {}).items()}
    occ = {str(k): float(v) for k, v in (ch.get('set_occ') or {}).items()}
    coord = ch.get('set_coord') or {}
    dropped = set(str(x) for x in (ch.get('drop_sites') or []))
    touched, removed = 0, 0
    keep_rows = []
    for ri in rows:
        parts = lines[ri].split()
        if len(parts) < len(cols):
            keep_rows.append(ri)
            continue
        label = parts[ci_label] if ci_label is not None else ''
        if label in dropped or str(ri) in dropped:
            removed += 1
            continue
        if ci_type is not None:
            el = parts[ci_type]
            if el in rep:
                parts[ci_type] = rep[el]
                touched += 1
        if label in ren and ci_label is not None:
            parts[ci_label] = ren[label]
            touched += 1
        if label in occ and ci_occ is not None:
            parts[ci_occ] = '%.4f' % occ[label]
            touched += 1
        if label in coord:
            c = coord[label]
            for idx, val in ((ci_x, c[0] if len(c) > 0 else None),
                             (ci_y, c[1] if len(c) > 1 else None),
                             (ci_z, c[2] if len(c) > 2 else None)):
                if idx is not None and val is not None:
                    parts[idx] = '%.6f' % float(val)
                    touched += 1
        lines[ri] = '   ' + '  '.join(parts)
        keep_rows.append(ri)
    # add new sites at the end of the atom loop
    adds = ch.get('add_sites') or []
    if adds:
        insert_at = (max(keep_rows) + 1) if keep_rows else (hidx + 1)
        newlines = []
        for a in adds:
            row = ['?'] * len(cols)
            if ci_label is not None:
                row[ci_label] = str(a.get('label') or (str(a.get('element', 'X')) + '1'))
            if ci_type is not None:
                row[ci_type] = str(a.get('element', 'X'))
            if ci_x is not None:
                row[ci_x] = '%.6f' % float(a.get('x', 0.0))
            if ci_y is not None:
                row[ci_y] = '%.6f' % float(a.get('y', 0.0))
            if ci_z is not None:
                row[ci_z] = '%.6f' % float(a.get('z', 0.0))
            if ci_occ is not None:
                row[ci_occ] = '%.4f' % float(a.get('occ', 1.0))
            newlines.append('   ' + '  '.join(row))
            touched += 1
        lines[insert_at:insert_at] = newlines
    # cell values
    cell = ch.get('set_cell') or {}
    if cell:
        keymap = {'a': '_cell_length_a', 'b': '_cell_length_b', 'c': '_cell_length_c',
                  'alpha': '_cell_angle_alpha', 'beta': '_cell_angle_beta',
                  'gamma': '_cell_angle_gamma'}
        for li, line in enumerate(lines):
            t = line.strip()
            for k, tag in keymap.items():
                if t.startswith(tag) and cell.get(k) is not None:
                    lines[li] = '%s  %.6f' % (tag, float(cell[k]))
                    touched += 1
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return {'path': out, 'mode': 'text', 'fields_changed': touched,
            'sites_removed': removed, 'sites_added': len(adds)}


# ---------------------------------------------------------------------------
# structure-level editing
# ---------------------------------------------------------------------------
def edit_structure(cif, out, ch, symprec=0.01):
    from pymatgen.core import Composition, Species
    from pymatgen.core.periodic_table import Element
    st = load_structure(cif)
    rep = ch.get('replace_element') or {}
    ren = {str(k): str(v) for k, v in (ch.get('rename_sites') or {}).items()}
    occ = ch.get('set_occ') or {}
    scomp = ch.get('set_site_composition') or {}
    coord = ch.get('set_coord') or {}
    dropped = [str(x) for x in (ch.get('drop_sites') or [])]
    touched = 0

    if rep:
        for i in range(len(st) - 1, -1, -1):
            sym = st[i].species.elements[0].symbol
            if sym in rep:
                st.replace(i, rep[sym])
                touched += 1
    if scomp:
        for i, site in enumerate(st):
            lbl = str(getattr(site, 'label', '') or '')
            key = lbl if lbl in scomp else None
            if key is None and str(i) in scomp:
                key = str(i)
            if key is None:
                continue
            spec = scomp[key]
            comp = Composition({Species(sym): float(v) for sym, v in spec.items()})
            st.replace(i, comp)
            touched += 1
    if occ:
        for i, site in enumerate(st):
            lbl = str(getattr(site, 'label', '') or '')
            if lbl in occ:
                comp = site.species
                tot = sum(comp.values()) or 1.0
                newc = Composition({sp: v * (float(occ[lbl]) / tot) for sp, v in comp.items()})
                st.replace(i, newc)
                touched += 1
    if coord:
        for i, site in enumerate(st):
            lbl = str(getattr(site, 'label', '') or '')
            if lbl in coord:
                c = list(site.frac_coords)
                for j, v in enumerate(coord[lbl][:3]):
                    if v is not None:
                        c[j] = float(v)
                st.replace(i, st[i].species, coords=c, coords_are_cartesian=False)
                touched += 1
    if dropped:
        idx = []
        for i, site in enumerate(st):
            lbl = str(getattr(site, 'label', '') or '')
            if lbl in dropped or str(i) in dropped:
                idx.append(i)
        if idx:
            st.remove_sites(idx)
            touched += len(idx)
    for a in (ch.get('add_sites') or []):
        el = str(a.get('element', 'X'))
        o = float(a.get('occ', 1.0))
        sp = Species(el) if abs(o - 1.0) < 1e-9 else Composition({Species(el): o})
        st.append(sp, [float(a.get('x', 0.0)), float(a.get('y', 0.0)), float(a.get('z', 0.0))],
                  coords_are_cartesian=False)
        touched += 1
    cell = ch.get('set_cell') or {}
    if cell:
        p = list(st.lattice.parameters)
        for j, k in enumerate(('a', 'b', 'c', 'alpha', 'beta', 'gamma')):
            if cell.get(k) is not None:
                p[j] = float(cell[k])
        st.lattice = st.lattice.__class__(*p)
        touched += 1
    write_structure(st, out, symprec)
    return {'path': out, 'mode': 'structure', 'fields_changed': touched,
            'n_sites': len(st)}


def build_cif(spec, out):
    """Create a CIF from parameters (space group + cell + sites)."""
    from pymatgen.core import Lattice, Structure
    from pymatgen.core.periodic_table import Element
    cell = spec.get('cell') or {}
    if spec.get('lattice_from'):
        st0 = load_structure(spec['lattice_from'])
        p = list(st0.lattice.parameters)
        for j, k in enumerate(('a', 'b', 'c', 'alpha', 'beta', 'gamma')):
            if cell.get(k) is not None:
                p[j] = float(cell[k])
        latt = Lattice.from_parameters(*p)
    else:
        latt = Lattice.from_parameters(float(cell.get('a', 1)), float(cell.get('b', 1)),
                                       float(cell.get('c', 1)), float(cell.get('alpha', 90)),
                                       float(cell.get('beta', 90)), float(cell.get('gamma', 90)))
    sites = spec.get('sites') or []
    if not sites:
        raise RuntimeError('build needs "sites"')
    sg = spec.get('spacegroup')
    if sg:
        from pymatgen.core import Composition, Species
        species, coords = [], []
        for a in sites:
            o = float(a.get('occ', 1.0))
            # Species WITHOUT an oxidation state: Species(el, 0) stringifies as
            # "O0+", which CifWriter writes into _atom_site_type_symbol and
            # pymatgen then refuses to re-read ("Invalid CIF file with no
            # structures") — observed when build was verified end-to-end.
            species.append(Species(str(a['element'])) if abs(o - 1.0) < 1e-9
                           else Composition({Species(str(a['element'])): o}))
            coords.append([float(a.get('x', 0.0)), float(a.get('y', 0.0)), float(a.get('z', 0.0))])
        st = Structure.from_spacegroup(sg, latt, species, coords)
    else:
        st = Structure(latt, [str(a['element']) for a in sites],
                       [[float(a.get('x', 0.0)), float(a.get('y', 0.0)), float(a.get('z', 0.0))]
                        for a in sites])
    write_structure(st, out, float(spec.get('symprec') or 0.01))
    return {'path': out, 'mode': 'build', 'spacegroup_input': sg or '(P1)',
            'n_sites': len(st)}


def symmetrize_cif(cif, out, symprec=0.01):
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    st = load_structure(cif)
    sga = SpacegroupAnalyzer(st, symprec=symprec)
    st2 = sga.get_symmetrized_structure()
    write_structure(st2, out, symprec)
    return {'path': out, 'mode': 'symmetrize', 'found_spacegroup': sga.get_space_group_symbol(),
            'n_sites': len(st2)}


# ---------------------------------------------------------------------------
# PROFESSIONAL CIF AUDIT + WYCKOFF-ORBIT BUILD  (added 2026-09-19)
#
# WHY THIS EXISTS (user requirement, verbatim intent): "将先前的 PYTHON 晶体学
# 工具加入到 CIF 生成中，避免出现复杂材料晶体库找不到，自己根据网络检索信息
# 手动 cif 生成失败的情况。要求调用 PYTHON 晶体学工具尝试更专业化的生成 CIF。"
# It must stay a TOOL SCRIPT, not a fixed engine, and - like the manual
# refinement engine - it is meant to be EDITABLE by the AI for the material at
# hand.  Extend these two actions rather than writing a throw-away script.
#
# WHAT THE LIBRARIES CAN AND CANNOT DO (all measured, 2026-09-19):
#   * gemmi 0.7.5 resolves EVERY spelling of a space group correctly
#     ('R -3 m', 'R-3m', 'P21/c', 'P 63/m m c' -> 166 / 14 / 194) and can read
#     the raw site loop WITHOUT expanding symmetry
#     (gemmi.make_small_structure_from_block). Its Python binding has NO
#     Wyckoff table in this version, so Wyckoff letters come from pymatgen.
#   * pymatgen's CifParser SILENTLY expands the listed sites with the declared
#     operators, so any "is the structure in the declared group?" check done
#     after parsing is circular - it always says yes. The real defects are only
#     visible (a) per site against the declared group's orbits, or (b) as
#     collisions in the expanded cell.
#   * NOTHING in the installed stack can decide whether a structure is
#     CHEMICALLY right: a file whose O sits at z=0.26 instead of 0.23 is
#     perfectly self-consistent and no library can flag it. Only a refinement
#     (R value) can. This audit therefore checks SELF-CONSISTENCY ONLY, by
#     decision: no bond-length tables, no chemistry heuristics.
#   * Calibration (positive + negative controls, so the verdict is trustworthy):
#     correct asu CIF -> pass; intentionally wrong asu CIF -> warn with the
#     measured overlap (0.115 A for a 6c site typed as x=0.04; 0.572 A for a
#     cell labelled P 6_3/m m c while the coordinates obey R-3m).
#
# A criterion that was TRIED AND REJECTED, so it is not re-invented: "is the
# declared group a subgroup of the highest symmetry the listed sites support?"
# A correct asymmetric-unit CIF lists only representatives, so it never closes
# under the declared group and the test flags GOOD files (measured: both
# correct controls were reported SUSPECT). A "site is near a special position"
# heuristic failed the same way. Only the OVERLAP test passed both controls.
# ---------------------------------------------------------------------------
def _resolve_symbol(sym):
    """Return (pymatgen SpaceGroup, gemmi SpaceGroup, canonical hm, number).

    pymatgen accepts 'P2_1/c' but throws on 'P21/c' or 'P 21/c'; gemmi accepts
    all of them. So gemmi is the resolver and pymatgen is the orbit engine.
    """
    import gemmi
    from pymatgen.symmetry.groups import SpaceGroup
    gem = gemmi.SpaceGroup(str(sym))
    if gem.number == 0:
        raise RuntimeError('unrecognised space group symbol: %r' % sym)
    hm = gem.hm
    pg = None
    err = None
    for cand in (hm.replace(' ', ''), hm,
                 hm.replace(' ', '').replace('21', '2_1').replace('31', '3_1')
                   .replace('32', '3_2').replace('41', '4_1').replace('42', '4_2')
                   .replace('43', '4_3').replace('61', '6_1').replace('62', '6_2')
                   .replace('63', '6_3').replace('64', '6_4').replace('65', '6_5')):
        try:
            pg = SpaceGroup(cand)
            break
        except Exception as e:
            err = e
    if pg is None:
        raise RuntimeError('pymatgen cannot use space group %r (%s): %s' % (hm, hm, err))
    return pg, gem, hm, gem.number


def _orbit(pg, pt, atol=1e-4):
    """Distinct images of one point under the full group (the Wyckoff orbit)."""
    import numpy as _numpy
    pts = []
    p = _numpy.array([float(v) for v in pt], dtype=float)
    for op in pg.symmetry_ops:
        q = _numpy.mod(op.operate(p), 1.0)
        if not any(_numpy.linalg.norm((lambda d: d - _numpy.round(d))(q - u)) < atol
                   for u in pts):
            pts.append(q)
    return pts


def _read_raw_sites(cif):
    """The file as it is written: declared symbol + the listed sites, read
    WITHOUT symmetry expansion (that is the whole point - CifParser would
    expand them and hide the defect)."""
    import gemmi
    doc = gemmi.cif.read(cif)
    blk = doc.sole_block()
    ss = gemmi.make_small_structure_from_block(blk)
    mult = None
    try:
        vals = blk.find_loop('_atom_site_symmetry_multiplicity')
        mult = [float(v) for v in vals] if vals else None
    except Exception:
        mult = None
    sites = []
    for i, s in enumerate(ss.sites):
        sites.append({'label': s.label, 'element': s.type_symbol,
                      'x': float(s.fract.x), 'y': float(s.fract.y), 'z': float(s.fract.z),
                      'occ': float(s.occ),
                      'file_multiplicity': (mult[i] if mult and i < len(mult) else None)})
    return {'block': blk.name, 'spacegroup_hm': ss.spacegroup_hm,
            'cell': list(ss.cell.parameters), 'sites': sites,
            'n_ops_in_file': len(blk.find_loop('_symmetry_equiv_pos_as_xyz') or [])}


def _frac_dist(a, b, lattice=None):
    """Minimum-image distance in CARTESIAN space (A).

    Two traps, both hit while building this (2026-09-19), so both are handled:
      1. Measuring in fractional coordinates is wrong for a non-orthogonal
         cell (a hexagonal (1/3,2/3,2/3) displacement is ~9.5 A, not 0.577).
      2. Even in cartesian space, "subtract, then round each component" is NOT
         the minimum image for a skewed or centred lattice - the closest copy
         can sit in a neighbouring cell. Searching the 27 surrounding cells is
         what makes this correct (e.g. the R-centring offset (1/3,2/3,2/3)
         minus (1,1,1) is the same point; without the search the distance comes
         back as 5.05 A instead of 0).
    Without a lattice the fractional norm is returned, which only makes sense
    for a cubic cell.
    """
    import numpy as _numpy
    d = _numpy.array(a, dtype=float) - _numpy.array(b, dtype=float)
    if lattice is None:
        d -= _numpy.round(d)
        return float(_numpy.linalg.norm(d))
    best = None
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            for k in (-1, 0, 1):
                v = d + _numpy.array([i, j, k], dtype=float)
                r = float(_numpy.linalg.norm(_numpy.dot(v, lattice.matrix)))
                if best is None or r < best:
                    best = r
    return best


def validate_cif(cif, declared=None, contact_limit=1.0):
    """Audit a hand-built CIF for consistency with its own symmetry header."""
    import numpy as _numpy
    raw = _read_raw_sites(cif)
    hm_raw = declared or raw['spacegroup_hm']
    pg, gem, hm, number = _resolve_symbol(hm_raw)
    cell = raw['cell']
    out = {
        'file': cif, 'declared_symbol_raw': hm_raw, 'declared_symbol': hm,
        'declared_number': number, 'n_ops_in_group': len(pg.symmetry_ops),
        'n_sites_listed': len(raw['sites']), 'contact_limit': contact_limit,
        'cell': [round(v, 4) for v in cell],
        'warnings': [], 'checks': {},
    }
    # (1) per site: orbit size under the DECLARED group
    per = []
    for s in raw['sites']:
        orb = _orbit(pg, [s['x'], s['y'], s['z']])
        n = len(orb)
        per.append({'label': s['label'], 'element': s['element'],
                    'pos': [round(s['x'], 5), round(s['y'], 5), round(s['z'], 5)],
                    'occ': round(s['occ'], 5), 'orbit_size': n,
                    'on_special_position': n < len(pg.symmetry_ops),
                    'file_multiplicity': s['file_multiplicity']})
        if s['file_multiplicity'] is not None and abs(s['file_multiplicity'] - n) > 0.5:
            out['warnings'].append(
                '%s: the file declares _atom_site_symmetry_multiplicity=%g but this '
                'position has an orbit of %d under %s'
                % (s['label'], s['file_multiplicity'], n, hm))
    out['per_site'] = per

    # (2) the expanded cell: collisions are the signature of a coordinate that
    #     is NOT on the special position the author intended
    try:
        import warnings as _w
        from pymatgen.io.cif import CifParser
        with _w.catch_warnings():
            _w.simplefilter('ignore')
            st = CifParser(cif, occupancy_tolerance=100.0).parse_structures(primitive=False)[0]
        dm = st.distance_matrix
        _numpy.fill_diagonal(dm, 9.9)
        pairs = _numpy.argwhere(dm < contact_limit)
        worst = float(dm.min())
        out['checks']['expanded_cell'] = {
            'n_atoms': len(st), 'min_contact_A': round(worst, 3),
            'n_pairs_under_limit': int(len(pairs)),
            'examples': [{'i': int(a), 'j': int(b), 'd': round(float(dm[a, b]), 3)}
                         for a, b in pairs[:5]],
        }
        if len(pairs):
            out['warnings'].append(
                '%d atom pair(s) closer than %g A (closest %.3f A): the listed coordinates '
                'do not obey the declared symmetry - most likely a special position was '
                'typed with a small offset, which makes the parser fabricate extra atoms'
                % (len(pairs), contact_limit, worst))
    except Exception as e:
        out['checks']['expanded_cell'] = {'error': str(e)}

    # (3) what the declared group generates vs what the parser built
    gen, gen_uniq = [], []
    for s in raw['sites']:
        for c in _orbit(pg, [s['x'], s['y'], s['z']]):
            if not any(s['element'] == e2 and
                       _frac_dist(c, c2) < 1e-3
                       for e2, c2 in gen_uniq):
                gen_uniq.append((s['element'], c))
    exp = out['checks']['expanded_cell']
    out['checks']['generated_vs_parsed'] = {
        'generated_by_declared_group': len(gen_uniq),
        'parsed': exp.get('n_atoms'),
        'agree': exp.get('n_atoms') == len(gen_uniq),
    }
    out['asu_atoms_per_cell'] = round(sum(p['occ'] * p['orbit_size'] for p in per), 4)
    out['verdict'] = 'pass' if not out['warnings'] else 'warn'
    out['note'] = ('Consistency with the file\'s own symmetry header only. Whether the '
                   'structure is CHEMICALLY right cannot be decided by any library - a '
                   'cell with a wrong layer spacing is self-consistent; only a refinement '
                   'R value can rule on that.')
    return out


# WHY THE FILE IS WRITTEN BY HAND AND NOT VIA CifWriter(symprec):
# measured 2026-09-19 on Y2O3 (Ia-3, the published ICSD cell). CifWriter wrote a
# correct-looking 3-row asymmetric-unit CIF, but the representative it printed
# for the 24d orbit was (0, 0.25, 0.4673) while the real site is (0.9673, 0,
# 0.25) -- pymatgen had changed the ORIGIN/setting inside
# get_symmetrized_structure(). FullProf then reported Rwp 84.5% on data whose
# reference fit is 7.04%, i.e. the CIF was silently the wrong structure.
# The representative the caller supplied is by construction a member of the
# orbit, so it is the one that gets written. Same lesson as "read the declared
# data filename, never the pcr stem": the authority is the input, not a library's
# choice of setting.
def _cif_text(pg, hm, number, cell, entries, block='built'):
    """entries: [{'element','label','x','y','z','occ','mult'}] -- asymmetric unit."""
    def fmt(v):
        return ('%.8f' % float(v)).rstrip('0').rstrip('.') if abs(float(v)) > 1e-12 else '0'
    lines = ['data_%s' % block,
             "_symmetry_space_group_name_H-M   '%s'" % hm,
             '_symmetry_Int_Tables_number   %d' % number,
             '_cell_length_a   %s' % fmt(cell[0]),
             '_cell_length_b   %s' % fmt(cell[1]),
             '_cell_length_c   %s' % fmt(cell[2]),
             '_cell_angle_alpha   %s' % fmt(cell[3]),
             '_cell_angle_beta    %s' % fmt(cell[4]),
             '_cell_angle_gamma   %s' % fmt(cell[5]),
             'loop_',
             '_symmetry_equiv_pos_site_id',
             '_symmetry_equiv_pos_as_xyz']
    for i, op in enumerate(pg.symmetry_ops, 1):
        lines.append("  %d  '%s'" % (i, _op_triplet(op)))
    lines += ['loop_',
              '_atom_site_label',
              '_atom_site_type_symbol',
              '_atom_site_fract_x',
              '_atom_site_fract_y',
              '_atom_site_fract_z',
              '_atom_site_occupancy']
    for e in entries:
        lines.append('  %s  %s  %s  %s  %s  %s' % (
            e['label'], e['element'],
            fmt(e['x']), fmt(e['y']), fmt(e['z']), fmt(e['occ'])))
    return '\n'.join(lines) + '\n'


# WHY THE SPACE-GROUP SYMBOL IS QUOTED, AND WHY THE MULTIPLICITY COLUMN WAS NOT
# THE CULPRIT (both established by isolation, 2026-09-19):
# The official CIFs_to_PCR.exe wrote occ = 2.0 for EVERY site of an Ia-3 Y2O3
# cell whose real occupancies are 1,1,1 (expected 0.16667 / 0.5 / 1.0 by the
# multiplicity convention), and FullProf then refined to Rwp 84.5% on data whose
# reference fit is 7.04%. I first blamed the _atom_site_symmetry_multiplicity
# column -- WRONG: removing it changed nothing, and a controlled series (good
# file + one added feature at a time: symmetry-operation block, Int_Tables line,
# both) all converted correctly. The real cause was the UNQUOTED space-group
# name: "_symmetry_space_group_name_H-M   I a -3" is truncated at the first space
# to "I", and "I" is the ITA symbol for space group 2 (P-1), whose sites all have
# multiplicity 2 -- hence occ=2 everywhere. Quoting it ("'I a -3'") fixes it. This
# is the same class of mistake as "read the declared data filename, not the pcr
# stem": the authority is the file's literal content, and a bare word with spaces
# is not a valid CIF value. The multiplicity column is still omitted, because the
# converter derives the multiplicity from the space group and does it correctly --
# supply nothing it can get wrong.


def _op_triplet(op):
    """pymatgen SymmOp -> CIF string 'x, -y, z+1/2'."""
    import fractions
    import numpy as _numpy
    R = _numpy.rint(_numpy.array(op.rotation_matrix)).astype(int)
    t = _numpy.array(op.translation_vector, dtype=float)

    def frac(v):
        f = fractions.Fraction(float(v)).limit_denominator(12)
        return f

    out = []
    for i in range(3):
        parts = []
        for j, ax in enumerate('xyz'):
            c = int(R[i, j])
            if c:
                parts.append(('%s%s' % ('-' if c < 0 else ('+' if parts else ''), ax)))
        f = frac(t[i] % 1.0)
        if f:
            parts.append(('+' if parts else '') + str(f))
        out.append(''.join(parts) if parts else '0')
    return ', '.join(out)


def build_orbit(spec, out):
    """Build a CIF from space group + cell + Wyckoff representatives.

    The output is a genuine ASYMMETRIC-UNIT CIF: the site loop holds the
    representatives that were supplied, and the symmetry block holds the group's
    operations, which is the form a refinement backend is built to read. The
    Wyckoff letter each representative lands on is reported back so the plan can
    be checked against the paper before any refinement.
    """
    from pymatgen.core import Lattice
    pg, gem, hm, number = _resolve_symbol(spec.get('spacegroup'))
    cell = spec.get('cell') or {}
    if spec.get('lattice_from'):
        st0 = load_structure(spec['lattice_from'])
        p = list(st0.lattice.parameters)
        for j, k in enumerate(('a', 'b', 'c', 'alpha', 'beta', 'gamma')):
            if cell.get(k) is not None:
                p[j] = float(cell[k])
        latt = Lattice.from_parameters(*p)
    else:
        latt = Lattice.from_parameters(float(cell.get('a', 1)), float(cell.get('b', 1)),
                                       float(cell.get('c', 1)), float(cell.get('alpha', 90)),
                                       float(cell.get('beta', 90)), float(cell.get('gamma', 90)))
    sites = spec.get('sites') or []
    if not sites:
        raise RuntimeError('build_orbit needs "sites" (one Wyckoff representative each)')

    entries, plan = [], []
    used_labels = set()
    for idx, a in enumerate(sites):
        el = str(a.get('element') or a.get('label'))
        pt = [float(a.get('x', 0.0)), float(a.get('y', 0.0)), float(a.get('z', 0.0))]
        o = float(a.get('occ', 1.0))
        orb = _orbit(pg, pt)
        label = str(a.get('label') or ('%s%d' % (el, idx + 1)))
        while label in used_labels:
            label += 'b'
        used_labels.add(label)
        entries.append({'element': el, 'label': label, 'x': pt[0], 'y': pt[1], 'z': pt[2],
                        'occ': o, 'mult': len(orb)})
        plan.append({'element': el, 'label': label, 'input': pt, 'occ': o,
                     'multiplicity': len(orb), 'wyckoff_letter': None,
                     'on_special_position': len(orb) < len(pg.symmetry_ops)})

    par = latt.parameters
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(_cif_text(pg, hm, number, par, entries,
                           block=_safe(spec.get('sample') or 'built') or 'built'))

    # physical cell, for verification only (the CIF itself stays asymmetric-unit)
    from pymatgen.core import Structure as _Structure
    sp, co = [], []
    for e in entries:
        for c in _orbit(pg, [e['x'], e['y'], e['z']]):
            sp.append(e['element'])
            co.append(c)
    st = _Structure(latt, sp, co)

    # Which Wyckoff orbit does each representative belong to? Look the
    # representative up as a cell site, then read the orbit it falls in -- never
    # ask pymatgen for "the representative of my orbit", because it answers in
    # its own setting (the Y2O3 84.5% trap above).
    orbits = []
    try:
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
        sga = SpacegroupAnalyzer(st, symprec=float(spec.get('symprec') or 0.01))
        sym = sga.get_symmetrized_structure()
        for k, idxs in enumerate(sym.equivalent_indices):
            c = sym[idxs[0]]
            orbits.append({'letter': sym.wyckoff_symbols[k], 'multiplicity': len(idxs),
                           'representative': [round(float(v), 4) for v in c.frac_coords],
                           'element': c.species_string})
        owner = {}
        for k, idxs in enumerate(sym.equivalent_indices):
            for i in idxs:
                owner[i] = k
        for q in plan:
            hits = [i for i, s in enumerate(sym)
                    if s.species_string == q['element']
                    and _frac_dist(s.frac_coords, q['input'], latt) < 1e-3]
            if not hits:
                hits = [i for i, s in enumerate(sym)
                        if _frac_dist(s.frac_coords, q['input'], latt) < 1e-3]
            if hits and owner.get(hits[0]) is not None:
                k = owner[hits[0]]
                q['wyckoff_letter'] = orbits[k]['letter']
        found = sga.get_space_group_symbol()
    except Exception as e:
        found = None
        log('build_orbit: wyckoff labelling skipped (%s)' % e)
    return {'path': out, 'mode': 'build_orbit', 'spacegroup_input': spec.get('spacegroup'),
            'spacegroup_used': hm, 'spacegroup_number': number,
            'found_spacegroup': found, 'n_sites': len(entries),
            'n_atoms_in_cell': len(st), 'plan': plan, 'orbits': orbits,
            'style': 'asymmetric unit (representatives + symmetry operations)'}


# ---------------------------------------------------------------------------
# crystal-database fetches
# ---------------------------------------------------------------------------
def fetch_cod(query, nmax, out_dir, elements=None):
    hits, files = [], []
    url = ('https://www.crystallography.net/cod/result.php?format=json&text=' +
           urllib.parse.quote(str(query)) + '&max=%d' % int(nmax))
    raw = _http_get(url)
    if not raw:
        return hits, files, ['COD search failed (network?)']
    try:
        docs = json.loads(raw.decode('utf-8', 'replace'))
    except Exception as e:
        return hits, files, ['COD response not JSON: %s' % e]
    warn = []
    for d in docs[:int(nmax)]:
        cid = str(d.get('file') or d.get('id') or '')
        if not cid:
            continue
        formula = str(d.get('formula') or '')
        if elements:
            if not all(el in formula for el in elements):
                continue
        target = os.path.join(out_dir, 'cod_%s.cif' % _safe(cid))
        if not os.path.isfile(target):
            # COD serves a CIF at /cod/<id>.cif - the older /cod/CIF/<xxxx>/<id>.cif
            # form now returns 404, and the payload begins with a comment banner,
            # so look for CIF landmarks anywhere in the head instead of requiring
            # 'data_' at byte 0.
            for u in ('https://www.crystallography.net/cod/%s.cif' % cid,
                      'https://www.crystallography.net/cod/%s.cif?format=cif' % cid):
                b = _http_get(u, timeout=40)
                if not b or len(b) < 200:
                    continue
                head = b[:1500]
                if b'data_' in head or b'_cell_' in head or b'loop_' in head:
                    with open(target, 'wb') as f:
                        f.write(b)
                    break
        if os.path.isfile(target):
            files.append(target)
            rec = {'source': 'COD', 'id': cid, 'formula': formula, 'path': target}
            for k in ('sg', 'a', 'b', 'c'):
                if d.get(k) is not None:
                    rec[k] = str(d[k])
            hits.append(rec)
    if not hits:
        warn.append('COD returned no usable entry for %r' % query)
    return hits, files, warn


def fetch_mp(spec, nmax, out_dir):
    hits, files, warn = [], [], []
    try:
        from pymatgen.ext.matproj import MPRester
        from pymatgen.io.cif import CifWriter
    except Exception as e:
        return hits, files, ['pymatgen MPRester unavailable: %s' % e]
    criteria = {}
    if spec.get('chemsys'):
        criteria['chemsys'] = str(spec['chemsys'])
    if spec.get('formula'):
        criteria['formula'] = str(spec['formula'])
    if not criteria:
        return hits, files, ['fetch_mp needs "chemsys" or "formula"']
    try:
        with MPRester() as mpr:
            docs = mpr.summary.search(**criteria)
    except Exception as e:
        return hits, files, ['MP search failed (API key / network?): %s' % e]
    picked = 0
    for d in docs:
        if picked >= int(nmax):
            break
        st = d.get('structure') if hasattr(d, 'get') else getattr(d, 'structure', None)
        if st is None:
            continue
        mid = str(d.get('material_id') if hasattr(d, 'get') else getattr(d, 'material_id', 'x'))
        target = os.path.join(out_dir, 'mp_%s.cif' % _safe(mid))
        try:
            CifWriter(st).write_file(target)
        except Exception as e:
            warn.append('CIF write failed for %s: %s' % (mid, e))
            continue
        files.append(target)
        hits.append({'source': 'MP', 'id': mid, 'formula': str(st.composition.reduced_formula),
                     'path': target, 'space_group': _sg_of(st)})
        picked += 1
    if not hits:
        warn.append('MP returned no structure for %s' % json.dumps(criteria))
    return hits, files, warn


def fetch_mp_polymorphs(spec, nmax, out_dir, per=1):
    """Search MP and keep one representative CIF per DISTINCT structure type.

    Grouping key = (reduced formula, space group): two entries in the same space
    group but different compositions are different phases, and different space
    groups are different polymorphs. Within a group the most stable entry
    (lowest energy_above_hull) wins, so the CIFs returned are the plausible
    polymorphs rather than forty near-duplicates of one.
    """
    hits, files, warn = [], [], []
    try:
        from pymatgen.ext.matproj import MPRester
        from pymatgen.io.cif import CifWriter
    except Exception as e:
        return hits, files, ['pymatgen MPRester unavailable: %s' % e]
    criteria = {}
    if spec.get('chemsys'):
        criteria['chemsys'] = str(spec['chemsys'])
    if spec.get('formula'):
        criteria['formula'] = str(spec['formula'])
    if not criteria:
        return hits, files, ['polymorphs needs "formula" or "chemsys"']
    try:
        with MPRester() as mpr:
            docs = mpr.summary.search(**criteria)
    except Exception as e:
        return hits, files, ['MP search failed (API key / network?): %s' % e]

    def g(d, k, dflt=None):
        return d.get(k) if hasattr(d, 'get') else getattr(d, k, dflt)

    groups = {}
    for d in docs:
        st = g(d, 'structure')
        if st is None:
            continue
        sg = _sg_of(st)
        key = (str(st.composition.reduced_formula), sg or 'unknown')
        groups.setdefault(key, []).append({
            'id': str(g(d, 'material_id', 'x')), 'structure': st,
            'energy_above_hull': _num(g(d, 'energy_above_hull')),
        })
    for (formula, sg), entries in sorted(groups.items()):
        entries.sort(key=lambda e: (e['energy_above_hull'] is None,
                                    e['energy_above_hull'] if e['energy_above_hull']
                                    is not None else 1e9))
        for e in entries[:max(1, int(per))]:
            target = os.path.join(
                out_dir, 'poly_%s_%s.cif' % (_safe(sg.replace(' ', '')), _safe(e['id'])))
            try:
                CifWriter(e['structure']).write_file(target)
            except Exception as ex:
                warn.append('CIF write failed for %s: %s' % (e['id'], ex))
                continue
            files.append(target)
            cell = e['structure'].lattice
            hits.append({
                'source': 'MP', 'id': e['id'], 'space_group': sg, 'formula': formula,
                'path': target, 'n_in_group': len(entries),
                'energy_above_hull': e['energy_above_hull'],
                'cell': {'a': _num(cell.a), 'b': _num(cell.b), 'c': _num(cell.c),
                         'alpha': _num(cell.alpha), 'beta': _num(cell.beta),
                         'gamma': _num(cell.gamma)},
            })
    if not hits:
        warn.append('MP returned no structure for %s' % json.dumps(criteria))
    return hits, files, warn


def polymorphs(spec, out_dir):
    """Return the CIFs of the DIFFERENT polymorphs of a composition.

    USER RULE (2026-09-18): for highly defective or doped crystals the actual
    phase type is often undecidable from the pattern alone, so instead of
    guessing a single structure the search must surface the several plausible
    polymorphs as separate CIFs. Those candidates are then adjudicated by
    actually refining them (AutoFP), never by theory-score ranking alone.
    """
    per = max(1, int(spec.get('per_polymorph') or 1))
    nmax = int(spec.get('max') or 40)
    src = str(spec.get('source') or 'mp').lower()
    if src == 'mp':
        return fetch_mp_polymorphs(spec, nmax, out_dir, per)
    raw, _files, warn = fetch_cod(spec.get('query') or spec.get('formula') or '',
                                  nmax, out_dir, spec.get('elements'))
    groups = {}
    for h in raw:
        groups.setdefault((h.get('formula'), h.get('space_group')), []).append(h)
    hits, files = [], []
    for (formula, sg), entries in sorted(groups.items(), key=lambda kv: str(kv[0])):
        for e in entries[:per]:
            h = dict(e)
            h['n_in_group'] = len(entries)
            hits.append(h)
            if h.get('path'):
                files.append(h['path'])
    if not hits:
        warn.append('COD returned no structure')
    return hits, files, warn


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
    guard = start_guard(plan, backend='cif_edit', out_path=outres)   # P1 (0.8.4)
    outres = outres or os.path.splitext(planf)[0] + '_result.json'
    out_dir = plan.get('out_dir') or os.path.dirname(os.path.abspath(outres))
    os.makedirs(out_dir, exist_ok=True)
    ref_dir = os.path.join(out_dir, 'ref')
    os.makedirs(ref_dir, exist_ok=True)

    warnings = []
    actions_log = []
    structures = []
    fetched = []
    # P0 (0.8.5): a failing action must not leave the call claiming success — the old
    # code wrote `ok: True` after the loop no matter how many actions raised, so
    # "cif not found" came back as ok:true with nothing done. `ok` = all actions
    # succeeded; `partial` = some did; `failed_actions` names the ones that did not.
    failed_actions = []
    validations = []
    last_cif = None
    keep = plan.get('keep_intermediate', True)
    result = {'ok': False}
    try:
        acts = plan.get('actions') or []
        if not acts:
            raise RuntimeError('need a non-empty "actions" list')
        base = _safe(plan.get('sample') or 'cif') if plan.get('sample') else None
        seq = 0
        for i, a in enumerate(acts):
            atype = str(a.get('type') or '')
            rec = {'action': atype}
            # Stop at a safe point between actions (0.8.5): without it a stop request in
            # this engine could only be answered by a tree kill. EngineStopped derives
            # from BaseException, so the `except Exception` below cannot swallow it.
            _g = active_guard()
            if _g is not None:
                _g.checkpoint('action %s' % atype)
            try:
                if atype == 'read':
                    cif = a.get('cif')
                    if not (cif and os.path.isfile(cif)):
                        raise RuntimeError('cif not found: %s' % cif)
                    st = load_structure(cif)
                    summary = structure_summary(st, cif, a.get('source') or 'file')
                    structures.append(summary)
                    rec['detail'] = '%s | %s | %d site(s)' % (
                        summary['space_group'] or '?', summary['formula'], summary['n_sites'])
                    last_cif = cif
                elif atype == 'fetch_cod':
                    hits, files, warn = fetch_cod(a.get('query'), a.get('max') or 10,
                                                  ref_dir, a.get('elements'))
                    fetched.extend(hits)
                    warnings.extend(warn)
                    for f in files:
                        try:
                            structures.append(structure_summary(load_structure(f), f, 'COD'))
                        except Exception as e:
                            warnings.append('could not parse %s: %s' % (f, e))
                    rec['detail'] = 'COD: %d entr(ies)' % len(hits)
                elif atype == 'fetch_mp':
                    hits, files, warn = fetch_mp(a, a.get('max') or 10, ref_dir)
                    fetched.extend(hits)
                    warnings.extend(warn)
                    for f in files:
                        try:
                            structures.append(structure_summary(load_structure(f), f, 'MP'))
                        except Exception as e:
                            warnings.append('could not parse %s: %s' % (f, e))
                    rec['detail'] = 'MP: %d structure(s)' % len(hits)
                elif atype == 'polymorphs':
                    hits, files, warn = polymorphs(a, ref_dir)
                    fetched.extend(hits)
                    warnings.extend(warn)
                    for f in files:
                        try:
                            structures.append(structure_summary(load_structure(f), f,
                                                                'polymorph'))
                        except Exception as e:
                            warnings.append('could not parse %s: %s' % (f, e))
                    rec['detail'] = ('polymorphs: %d distinct structure type(s) from %d '
                                     'candidate(s)' % (len(hits), sum(
                                         h.get('n_in_group', 1) for h in hits)))
                    rec['polymorphs'] = [{'id': h.get('id'), 'space_group': h.get('space_group'),
                                          'formula': h.get('formula'), 'path': h.get('path'),
                                          'n_in_group': h.get('n_in_group')} for h in hits]
                elif atype in ('edit_text', 'edit_structure'):
                    cif = a.get('cif') or last_cif
                    if not (cif and os.path.isfile(cif)):
                        raise RuntimeError('%s needs an existing "cif"' % atype)
                    seq += 1
                    out = a.get('out') or os.path.join(
                        out_dir, '%s_%s_%02d.cif' % (base or _safe(os.path.splitext(
                            os.path.basename(cif))[0]), atype.replace('edit_', 'edit'), seq))
                    if atype == 'edit_text':
                        info = edit_text(cif, out, a)
                    else:
                        info = edit_structure(cif, out, a, float(a.get('symprec') or 0.01))
                    rec['detail'] = '%s -> %s (%s)' % (cif, out, json.dumps(
                        {k: info[k] for k in ('fields_changed',) if k in info}))
                    rec['out'] = out
                    last_cif = out
                    structures.append(structure_summary(load_structure(out), out,
                                                        'edited(%s)' % atype))
                elif atype == 'build':
                    seq += 1
                    out = a.get('out') or os.path.join(out_dir, '%s_build_%02d.cif' % (base or 'new', seq))
                    info = build_cif(a, out)
                    rec['detail'] = 'built %s (%s, %d sites)' % (
                        out, info['spacegroup_input'], info['n_sites'])
                    rec['out'] = out
                    last_cif = out
                    structures.append(structure_summary(load_structure(out), out, 'built'))
                elif atype == 'build_orbit':
                    seq += 1
                    out = a.get('out') or os.path.join(
                        out_dir, '%s_orbit_%02d.cif' % (base or 'new', seq))
                    info = build_orbit(a, out)
                    wy = ', '.join('%s x%d%s' % (p['element'], p['multiplicity'],
                                                 ('(%s)' % p['wyckoff_letter'])
                                                 if p.get('wyckoff_letter') else '')
                                   for p in info['plan'])
                    rec['detail'] = ('built %s via %s (%d representative(s) -> %d atoms in the '
                                     'cell; %s)' % (out, info['spacegroup_used'],
                                                    info['n_sites'],
                                                    info.get('n_atoms_in_cell', '?'), wy))
                    rec['out'] = out
                    rec['plan'] = info['plan']
                    last_cif = out
                    structures.append(structure_summary(load_structure(out), out,
                                                        'built_orbit'))
                elif atype == 'validate':
                    cif = a.get('cif') or last_cif
                    if not (cif and os.path.isfile(cif)):
                        raise RuntimeError('validate needs an existing "cif"')
                    info = validate_cif(cif, a.get('declared_spacegroup'),
                                        float(a.get('contact_limit') or 1.0))
                    validations.append(info)
                    rec['detail'] = 'validate %s: %s (%s, %d site(s), %d warning(s))' % (
                        os.path.basename(cif), info['verdict'],
                        info.get('declared_symbol') or '?', info['n_sites_listed'],
                        len(info['warnings']))
                    rec['validation'] = info
                    warnings.extend('validate %s: %s' % (os.path.basename(cif), w)
                                    for w in info['warnings'])
                elif atype == 'symmetrize':
                    cif = a.get('cif') or last_cif
                    if not (cif and os.path.isfile(cif)):
                        raise RuntimeError('symmetrize needs an existing "cif"')
                    seq += 1
                    out = a.get('out') or os.path.join(out_dir, '%s_symm_%02d.cif' % (base or 'symm', seq))
                    info = symmetrize_cif(cif, out, float(a.get('symprec') or 0.01))
                    rec['detail'] = 'symmetrized -> %s (%s)' % (out, info['found_spacegroup'])
                    rec['out'] = out
                    last_cif = out
                    structures.append(structure_summary(load_structure(out), out, 'symmetrized'))
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
                log(traceback.format_exc()[-1200:])
            actions_log.append(rec)

        result = {
            'ok': not failed_actions,
            'structures': structures,
            'fetched': fetched,
            'actions': actions_log,
            'warning': warnings,
            'summary': 'xrd_cif_edit: %d action(s), %d structure(s) (re)built, %d fetched%s' % (
                len(actions_log), len(structures), len(fetched),
                ('' if not failed_actions else '; %d action(s) FAILED' % len(failed_actions))),
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
        if validations:
            result['validations'] = validations
        if last_cif:
            result['cif'] = last_cif
        log('CIF_EDIT_OK', last_cif)
    except Exception as e:
        tb = traceback.format_exc()
        log('CIF_EDIT_FAILED:', e)
        log(tb[-2500:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-2000:]}
        if warnings:
            result['warning'] = warnings

    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


if __name__ == '__main__':
    run_guarded_main(main)
