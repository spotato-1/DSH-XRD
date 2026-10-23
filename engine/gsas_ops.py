#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""xrd_gsas_ops engine - GSAS-II FULL-PARAMETER control (tool #2 of the GSAS trio).

User's tool trio (2026-09-17):
  #1 `xrd_refine_gsas`  - quick ladder (background -> inst -> cell -> profile -> atoms)
  #2 `xrd_gsas_ops`     - THIS tool: every parameter reachable / settable /
                          constrainable (equivalent-Uiso groups, Uiso clamping,
                          occupancy control, site splitting, arbitrary GSAS recipe)
  #3 `xrd_export_cif`   - final output: CIF + bond-length / occupancy / Uiso sanity

WHY: the PB / layer-oxide campaigns accumulated ~44 one-off GSAS-II scripts
(gsas_s1..s13, gsas_uequiv, gsas_uclamp, phys*_refine, gsas_driver, ...). They are
all the same few operations - open/create project, set parameters, add
constraints, refine, save - so they become DECLARATIVE ACTIONS in a plan instead
of hand-edited copies of a script.

Plan protocol (argv[1] = plan JSON, --out <result.json>):
{
  "gsas_root": "<GSAS-II install root; or set DSH_GSAS_ROOT>",
  "gpx": "<existing .gpx>",           # open + continue
  "new_gpx": false,                   # true: build a new project from data/cif
  "data": {"file": "...", "fmthint": null},   # fmthint OPTIONAL: null = every powder
                                              # reader is tried (0.8.8). A wrong hint
                                              # hides readers GSAS-II would have used.
  "instprm": "...", "cif": "...", "cif2": "...",
  "out_dir": "...",
  "max_cycles": 15,
  "save_as": "<result .gpx>",         # default <out_dir>/gsas_ops_out.gpx
  "actions": [ ... ]                  # executed in order, see ACTIONS below
}

ACTIONS (declarative; unknown keys are ignored, each logs what it did):
  {"type":"snapshot"}                       record the full parameter table
  {"type":"open_uiso", "atoms":["all"]}     add the 'U' flag (start refining Uiso).
      ATOM SELECTOR (open_uiso / set_adp_mode): for "every atom" pass "all", ["all"],
      or omit "atoms" entirely - all three are equivalent and normalised; a list of
      labels selects just those atoms, and a label that matches nothing is reported.
  {"type":"set_adp_mode", "mode":"isotropic|anisotropic", "atoms":["all"],
      "open_uiso":true}                     which thermal parameter is refined:
      GSAS-II obeys the per-atom ADP mode MARKER ('I'/'A'), NOT the refine flags.
      isotropic also zeroes the six Uij; open_uiso (default true) adds the 'U' flag
  {"type":"set_atoms", "all":"XU", "atoms":{"O6":"X"},
      "uiso":{"O6":0.01}, "occ":{"Na3":0.2},
      "pos":{"O6":[null,null,0.11]}}        per-atom flags / value control
  {"type":"set_hist", "section":"Background",
      "value":{"no. coeffs":12,"refine":true}}   any GSAS histogram section:
      Background | Instrument Parameters | Size | Mustrain | HStrain |
      Extinction | Pref.Ori. | Limits | Sample Parameters
  {"type":"set_cell", "refine":true, "phases":["all"]}
  {"type":"set_cell", "params":{"length_c":{"value":11.15,"vary":true}}}  per-cell-param
      CAVEAT on set_hist / set_cell: installing a setting is done through
      do_refinements(), so ONE refinement step runs as a side effect and the wR they
      return is THAT step's value. It is a fresh fit of the current parameter state,
      not a continuation of the previous fit - consecutive set_* calls were measured
      jumping 16.56 -> 34.21 -> 19.31 -> 35.11 on a real project with no model change.
      Never read such a jump as "the model got worse"; each action record carries
      wR_kind to mark exactly this.
  {"type":"scale", "phase":"<name|2nd>", "value":1e-4, "vary":false}
  {"type":"equiv_uiso", "groups":{"Uiso_K":["K1","K2"], "Uiso_C":["C1","C2"]}}
  {"type":"equiv_afrac", "group":["Fe1","C4","N5"], "mult":[1,1,1]}     tie occupancies
  {"type":"conserve_afrac", "group":["K1a","K1b","K2a","K2b"]}          hold Σ occ (current total)
  {"type":"conserve_afrac", "group":[...], "total":1.6, "normalize":true}
  {"type":"check_conservation", "groups":[{"group":["K1a","K1b"],"total":1.6}]}
  {"type":"uiso_clamp", "min":0.01, "max_rounds":8, "clear_flag":true}
  {"type":"fix_uiso", "atoms":{"C1":0.01}}
  {"type":"clear_constraints"}
  {"type":"split_site", "site":"K2", "n":2, "frac":0.498, "axis":"x"}
  {"type":"refine", "raw":[ {...GSAS recipe...} ]}   arbitrary do_refinements()
  {"type":"save"}
"""
import os
import sys
import json

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard
import math
import re
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


# element symbols accepted as atom TYPES; a type outside this set means the file
# carries something that is not an element (see _element_symbol / the 'Fen_' bug)
try:
    from pymatgen.core import Element as _PmgElement
    _ELEMENTS = {str(e.symbol) for e in _PmgElement}
except Exception:                                   # pymatgen absent: the mass table will do
    _ELEMENTS = set()


_MASS = {
    'H': 1.008, 'D': 2.014, 'Li': 6.94, 'Be': 9.012, 'B': 10.81, 'C': 12.011,
    'N': 14.007, 'O': 15.999, 'F': 18.998, 'Na': 22.99, 'Mg': 24.305,
    'Al': 26.982, 'Si': 28.085, 'P': 30.974, 'S': 32.06, 'Cl': 35.45,
    'K': 39.098, 'Ca': 40.078, 'Sc': 44.956, 'Ti': 47.867, 'V': 50.942,
    'Cr': 51.996, 'Mn': 54.938, 'Fe': 55.845, 'Co': 58.933, 'Ni': 58.693,
    'Cu': 63.546, 'Zn': 65.38, 'Ga': 69.723, 'Ge': 72.63, 'As': 74.922,
    'Se': 78.97, 'Br': 79.904, 'Rb': 85.468, 'Sr': 87.62, 'Y': 88.906,
    'Zr': 91.224, 'Nb': 92.906, 'Mo': 95.95, 'Ru': 101.07, 'Rh': 102.91,
    'Pd': 106.42, 'Ag': 107.87, 'Cd': 112.41, 'In': 114.82, 'Sn': 118.71,
    'Sb': 121.76, 'Te': 127.6, 'I': 126.9, 'Cs': 132.91, 'Ba': 137.33,
    'La': 138.91, 'Ce': 140.12, 'Pr': 140.91, 'Nd': 144.24, 'Sm': 150.36,
    'Eu': 151.96, 'Gd': 157.25, 'Tb': 158.93, 'Dy': 162.5, 'Ho': 164.93,
    'Er': 167.26, 'Tm': 168.93, 'Yb': 173.05, 'Lu': 174.97, 'Hf': 178.49,
    'Ta': 180.95, 'W': 183.84, 'Re': 186.21, 'Os': 190.23, 'Ir': 192.22,
    'Pt': 195.08, 'Au': 196.97, 'Hg': 200.59, 'Tl': 204.38, 'Pb': 207.2,
    'Bi': 208.98,
}


def _mass_of(el):
    return _MASS.get(str(el).capitalize(), 1.0)


def _element_symbol(raw, fallback_label='', strict=False):
    """Element symbol for `raw`, '' when unknown, or '?' when DEFINITELY not an
    element.

    Needed because a CIF's atom TYPE can be something that is not an element: when
    the type-symbol column is missing, GSAS-II's CIF reader falls back to the atom
    LABEL, and a pymatgen-style label such as "FeN_1" is not an element. GSAS-II
    then silently substitutes Xe (or H) for the type.

    strict=True never falls back to the label and never salvages a leading symbol
    out of an arbitrary string - it answers "is this declared value a legitimate
    element?", where '?' means no and '' means unparseable.
    """
    cands = (raw,) if strict else (raw, fallback_label)
    for cand in cands:
        s = str(cand or '').strip()
        if not s:
            continue
        try:
            from pymatgen.core import Element
            return str(Element(s).symbol)          # clean single-symbol value
        except Exception:
            pass
        m = re.match(r'^([A-Za-z]{1,2})$', s)       # bare symbol
        if m and m.group(1).capitalize() in _MASS:
            return m.group(1).capitalize()
        m = re.match(r'^([A-Z][a-z]?)(?=[0-9+\-]|$)', s)   # "Fe1", "Fe3+"
        if m and m.group(1) in _MASS:
            return m.group(1)
        if strict:
            # a longer string: '?' marks DEFINITELY-not-an-element (e.g. "FeN_1",
            # whose first letter is uppercase, or "Fen", whose second is lowercase)
            return '?'
    return ''


def _declared_types_from_cif(path):
    """{rounded (x,y,z): [(declared type_symbol, label), ...]} as written in the CIF.

    A LIST per coordinate, not a single value: in a defective/mixed site two rows
    can share the exact same position (e.g. "FeN_1" and "Mn1" both at 0.5,0,0),
    and a single-value map would silently drop one of them.
    """
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except Exception:
        return {}
    lines = text.splitlines()
    out, i = {}, 0
    while i < len(lines):
        if lines[i].strip().lower() != 'loop_':
            i += 1
            continue
        tags, j = [], i + 1
        while j < len(lines) and lines[j].strip().startswith('_'):
            tags.append(lines[j].strip().lower())
            j += 1
        if not any(t.startswith('_atom_site_') for t in tags):
            i = j
            continue
        idx = {t: k for k, t in enumerate(tags)}
        xi, yi, zi = (idx.get('_atom_site_fract_x'), idx.get('_atom_site_fract_y'),
                      idx.get('_atom_site_fract_z'))
        ti = idx.get('_atom_site_type_symbol')
        li = idx.get('_atom_site_label')
        while j < len(lines):
            s = lines[j].strip()
            if not s or s.startswith('#') or s.startswith('_') or s.lower() == 'loop_':
                break
            j += 1
            p = s.replace("'", ' ').replace('"', ' ').split()
            if len(p) < len(tags) or xi is None or ti is None:
                continue
            try:
                key = (round(float(p[xi]), 3), round(float(p[yi]), 3), round(float(p[zi]), 3))
            except (ValueError, TypeError):
                continue
            out.setdefault(key, []).append((p[ti], p[li] if li is not None else ''))
        i = j
    return out


def _atom_index(ph, group, need_f=False):
    """Resolve site labels to Atoms-table indices, optionally ensuring the 'F'
    (occupancy) refinement flag is set.

    WHY need_f MATTERS: a constraint over parameters that are NOT being refined is
    silently DROPPED by GSAS ("Ignoring this constraint; contains no refined
    parameters"), so an occupancy constraint on atoms whose flags are e.g. "FU"
    does nothing at all while looking successful. Measured 2026-09-19: with F
    enabled the same equation held the occupancy sum exactly, without F the
    occupancy never moved.
    """
    ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
    ct = ptr[1]
    idx, missing, enabled = {}, [], []
    for at in (ph.data.get('Atoms') or []):
        idx[str(at[ct - 1])] = at
    for lab in group:
        at = idx.get(str(lab))
        if at is None:
            missing.append(str(lab))
            continue
        if need_f:
            fl = str(at[ct + 1])
            if 'F' not in fl:
                at[ct + 1] = 'F' + fl.replace('F', '')
                enabled.append(str(lab))
    return idx, missing, enabled


def _ensure_ids_indexed(gpx):
    """Build GSAS-II's phase/atom ID lookup tables before any variable name is used.

    MEASURED ON A REAL PROJECT (2026-09-21): without this call every variable name
    fails with `KeyError: '<prefix>'` raised at GSASIIobj.py:1007
    (`AtomIdLookup[lst[0]]`), because `AtomIdLookup`/`PhaseIdLookup` are EMPTY until
    `G2Project.index_ids()` runs — the tables are built from the USED histograms and
    phases. `add_EquivConstr(..., reloadIdx=False)` does NOT build them (its guard is
    `elif G2obj.TestIndexAll(): self.index_ids()`, and TestIndexAll() is False while
    the tables are empty), and the failure is swallowed into a short message that
    looked like a puzzling error for every constraint action.

    Side effect worth knowing: only phases ATTACHED TO A HISTOGRAM get indexed, so a
    phase added with add_phase() but not used by any histogram cannot take part in
    constraints.
    """
    try:
        gpx.index_ids()
        return True, ''
    except Exception as e:
        return False, str(e)


def _phase_index_of(phase, gpx):
    """The project-ordered index of `phase` — the prefix GSAS-II uses in variable names.

    GSAS-II constraint variables are named `<phase>[:<histogram>]:<variable>:<atom>`,
    where the first field is the PHASE INDEX (GSASIIscriptable docstring examples:
    `add_EquivConstr(('0::AUiso:0','0::AUiso:1'))`). `G2Project.phases()` returns the
    phases in project order (= index order) and `G2Project.phase(int)` resolves by
    id/ranId, so index order is authoritative.

    WHY THIS EXISTS (audit 2026-09-21): the constraint code hard-coded `0::` in every
    variable name. Once the phase resolution was fixed to pick the right phase, the
    action still SUBMITTED `0::Afrac:…` — so on a two-phase project it reported
    "applied to phase 'B'" while constraining phase 'A'. Measured in isolation:
    the record named the second phase, the variables pointed at phase 0.
    """
    try:
        ordered = list(gpx.phases())
    except Exception:
        return None, None
    for i, p in enumerate(ordered):
        if p is phase:
            return i, [str(q.name) for q in ordered]
    # object identity failed (a rebuilt wrapper) — fall back to a unique name match
    names = [str(p.name) for p in ordered]
    if names.count(str(getattr(phase, 'name', None))) == 1:
        return names.index(str(phase.name)), names
    return None, names


def _constraint_var_phases(gpx):
    """Phase INDICES actually referenced by the project's constraint variables.

    Read back AFTER adding a constraint so "I asked for phase N" can be checked
    against "GSAS-II stored phase M" instead of assumed.

    IMPORTANT (measured on a real two-phase project, 2026-09-21): GSAS-II stores the
    variable objects, and a stored var object reports the phase as its **ranId**, not
    the index used in the name string. An earlier version of this function only parsed
    the numeric prefix of `var.name` and therefore reported "no phase reference" for
    constraints that were stored PERFECTLY — three false alarms on a project whose
    constraints read back as `Constraints[Phase] = 3`. Both forms are accepted now and
    normalised to the index through PhaseIdLookup.
    """
    out = set()
    try:
        from GSASII import GSASIIobj as G2obj
        ranid_to_idx = {}
        for nm, val in (getattr(G2obj, 'PhaseIdLookup', {}) or {}).items():
            try:
                ranid_to_idx[val[1]] = int(nm)
            except (TypeError, ValueError, IndexError):
                pass
    except Exception:
        ranid_to_idx = {}
    try:
        data = (gpx.data.get('Constraints') or {}).get('data') or {}
        for key in ('Phase', 'Hist', 'HAP'):
            for row in (data.get(key) or []):
                if not isinstance(row, (list, tuple)):
                    continue
                for item in row:
                    if not (isinstance(item, (list, tuple)) and len(item) == 2):
                        continue
                    v = item[1]
                    ph = getattr(v, 'phase', None)
                    if ph is None and isinstance(v, dict):
                        ph = v.get('phase')
                    if isinstance(ph, int):
                        # stored ranId -> index when we can, else keep as-is (the caller
                        # compares against indices, so an unmapped ranId is reported too)
                        out.add(ranid_to_idx.get(ph, ph))
                    name = getattr(v, 'name', None)
                    if isinstance(name, str) and '::' in name:
                        try:
                            out.add(int(name.split('::', 1)[0]))
                        except ValueError:
                            pass
    except Exception:
        pass
    return sorted(out)


def _afrac_vars(ph, labels, phase_index=0):
    """['<phase>::Afrac:<row>', ...] in the order of `labels` (only resolvable ones)."""
    ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
    ct = ptr[1]
    idx = {str(at[ct - 1]): i for i, at in enumerate(ph.data.get('Atoms') or [])}
    return [('%d::Afrac:%d' % (phase_index, idx[l])) for l in labels if l in idx]


def _fresh_atom_ids(used, count):
    """`count` new atom random-ids that collide with nothing in `used`.

    GSAS-II keeps a random id as the LAST element of every atom record and repairs
    duplicates by drawing a new number at index time (GSASIIobj.AddAtom2Index prints
    "has repeated ranId. Fixing."). Copying a row therefore copies its IDENTITY as
    well; we draw fresh ids so the copied atoms are distinct by construction.
    """
    import random as _random
    out = []
    guard = 0
    while len(out) < count and guard < 10000:
        guard += 1
        cand = _random.randint(1, 2 ** 62)
        if cand in used or cand in out:
            continue
        out.append(cand)
    while len(out) < count:          # deterministic fallback, still unique
        out.append((max(used) if used else 0) + len(out) + 1)
    return out


def _occ_of(ph, labels):
    """{label: occupancy value} read from the Atoms table (col 6)."""
    ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
    ct = ptr[1]
    out = {}
    for at in (ph.data.get('Atoms') or []):
        lab = str(at[ct - 1])
        if lab in labels:
            out[lab] = _num(at[6])
    return out


def _consistency_check(ph, labels):
    """Can this group still absorb the equation?

    Entries live in Constraint['data']['Phase'] as
        [[mult, varObj], [mult, varObj], ..., None, None, 'e'|'c'|'f']
    where the trailing type char is 'e' (equivalence), 'c' (equation) or 'f' (hold).
    The project's split-site pattern is pairwise EQUIVALENCE, so if every member of
    the group is tied to another member of the same group, the sum is frozen and
    GSAS drops the equation - which we must say out loud instead of reporting success.
    """
    cons = ((ph.data.get('General') or {}).get('Constraints') or {})
    entries = (cons.get('data') or {}).get('Phase') or []
    labels = [str(l) for l in labels]
    var_of = {}
    for lab in labels:
        vs = _afrac_vars(ph, [lab])
        if vs:
            var_of[lab] = vs[0]
    tied = {lab: set() for lab in labels}
    for entry in entries:
        try:
            if str(entry[-1]) != 'e':
                continue
            names = [getattr(v, 'name', str(v)) for _m, v in entry[:-3]]
        except Exception:
            continue
        members = [lab for lab, v in var_of.items() if v in names]
        for a in members:
            for b in members:
                if a != b:
                    tied[a].add(b)
    return [lab for lab in labels if tied.get(lab)]


def act_equiv_afrac(gpx, a, rec):
    """Tie occupancies together with an official GSAS linear constraint.

    action: {"type":"equiv_afrac", "group":["Fe1","C4","N5"], "mult":[1,1,1]}
            {"type":"equiv_afrac", "group":["K1a","K1b"], "mult":[1,-1]}   # difference = 0

    Uses `add_EquivConstr` (Σ mᵢ·varᵢ = 0), i.e. the same primitive the project's
    own scripts used for the [Fe(CN)6] linkage and for split K sub-sites.
    """
    group = [str(l) for l in (a.get('group') or [])]
    if len(group) < 2:
        raise RuntimeError('equiv_afrac needs "group" with at least 2 site labels')
    mult = a.get('mult')
    if mult is not None and len(mult) != len(group):
        raise RuntimeError('equiv_afrac: "mult" must have the same length as "group"')
    need_f = a.get('enable_occupancy_refinement', True) is not False
    done = []
    # PHASE RESOLUTION (audit 2026-09-21). The old loop ran over EVERY phase and
    # applied the constraint to any phase that happened to carry the labels — on a
    # multi-phase project a label collision silently constrained the WRONG phase (the
    # audit's "phase 0" symptom), and there was no way to tell from the result.
    # No interface is added (user rule): the first phase that resolves the group is
    # used, and the resolved phase (plus any phase whose labels collided) is reported.
    hits, missing_by_phase = [], {}
    for ph in gpx.phases():
        _idx, missing, _en = _atom_index(ph, group, need_f=False)
        present = [l for l in group if l not in missing]
        if len(present) >= 2:
            hits.append(ph)
        elif present:
            missing_by_phase[ph.name] = [l for l in group if l in missing]
    if not hits:
        rec['detail'] = ('equiv_afrac: no phase carries at least 2 of %s - nothing '
                         'applied' % group)
        rec.setdefault('warnings', []).append(
            'equiv_afrac: group %s resolved in NO phase' % group)
        rec['phase_resolution'] = {'status': 'not-found', 'group': group}
        return
    if len(hits) > 1:
        rec.setdefault('warnings', []).append(
            'equiv_afrac: labels %s exist in %d phases (%s) - the constraint was '
            'applied to the FIRST one (%s). If that is the wrong phase, give unique '
            'site labels or split the action per phase.'
            % (group, len(hits), ', '.join(p.name for p in hits), hits[0].name))
    if missing_by_phase:
        rec.setdefault('warnings', []).append(
            'equiv_afrac: partial label overlap in other phase(s): %s'
            % json.dumps(missing_by_phase, ensure_ascii=False))
    rec['phase_resolution'] = {
        'status': 'applied-to-first-match' if len(hits) == 1 else 'ambiguous',
        'phase': hits[0].name, 'n_phases_matching': len(hits),
        'phases_matching': [p.name for p in hits],
        'group': group,
    }
    try:
        gpx.index_ids()
    except Exception as e:
        rec.setdefault('warnings', []).append(
            'equiv_afrac could not index the project ids (%s) - GSAS-II would reject '
            'every variable name' % e)
    for ph in hits[:1]:
        pidx, all_names = _phase_index_of(ph, gpx)
        if pidx is None:
            rec.setdefault('warnings', []).append(
                'equiv_afrac: could not determine the project index of phase %s '
                '(duplicate names?) - the constraint would point at the wrong phase, '
                'so it was NOT applied' % ph.name)
            rec['phase_resolution']['status'] = 'phase-index-unresolved'
            continue
        rec['phase_resolution']['phase_index'] = pidx
        _idx, missing, enabled = _atom_index(ph, group, need_f=need_f)
        if missing:
            rec.setdefault('warnings', []).append(
                'equiv_afrac: site(s) %s not found in phase %s' % (', '.join(missing), ph.name))
        labels = [l for l in group if l not in missing]
        vs = _afrac_vars(ph, labels, pidx)
        if len(vs) < 2:
            rec.setdefault('warnings', []).append(
                'equiv_afrac: fewer than 2 resolvable sites in phase %s - skipped' % ph.name)
            continue
        try:
            gpx.add_EquivConstr(tuple(vs), list(mult) if mult else [],
                                reloadIdx=False, override=True)
            # read back: did GSAS-II keep the phase we asked for?
            stored = set(_constraint_var_phases(gpx))
            if pidx not in stored:
                rec.setdefault('warnings', []).append(
                    'equiv_afrac: after adding the constraint the stored variables do '
                    'not reference phase %d (%s) - the phases present are %s. GSAS-II '
                    'may have dropped or rewritten it; verify before trusting this fit.'
                    % (pidx, ph.name, sorted(stored) or 'none'))
            done.append('%s@%s(%d)' % ('+'.join(labels), ph.name, len(vs)))
            if enabled:
                rec.setdefault('notes', []).append(
                    'equiv_afrac: enabled occupancy refinement (F) on %s' % ', '.join(enabled))
        except Exception as e:
            rec.setdefault('warnings', []).append('equiv_afrac failed: %s' % e)
    rec['detail'] = 'equiv_afrac: ' + (', '.join(done) if done else 'nothing applied')


def act_conserve_afrac(gpx, a, rec):
    """Hold the SUM of a group's occupancies with an official GSAS equation
    constraint - this is the fix for "equal Afrac does not constrain the total".

    action: {"type":"conserve_afrac", "group":["K1a","K1b","K2a","K2b"]}
            {"type":"conserve_afrac", "group":[...], "total":1.6}
            {"type":"conserve_afrac", "group":[...], "total":1.6, "normalize":true}

    total omitted  -> the CURRENT sum is kept ("only redistribute what you have")
    normalize      -> scale the members proportionally to the target first
    enable_occupancy_refinement (default true) sets the 'F' flag, without which
    GSAS drops the equation as "contains no refined parameters".
    """
    group = [str(l) for l in (a.get('group') or [])]
    if len(group) < 2:
        raise RuntimeError('conserve_afrac needs "group" with at least 2 site labels')
    need_f = a.get('enable_occupancy_refinement', True) is not False
    normalize = bool(a.get('normalize'))
    total_in = a.get('total')
    applied = []
    # PHASE RESOLUTION (audit 2026-09-21): ONE phase, chosen by which one actually
    # carries the labels, with the choice reported — the old loop constrained every
    # phase that shared the labels, i.e. the wrong phase on a multi-phase project.
    _hits = []
    for ph in gpx.phases():
        _i2, _m2, _e2 = _atom_index(ph, group, need_f=False)
        if len([l for l in group if l not in _m2]) >= 2:
            _hits.append(ph)
    if not _hits:
        rec['phase_resolution'] = {'status': 'not-found', 'group': group}
        rec.setdefault('warnings', []).append(
            'conserve_afrac: group %s resolved in NO phase - nothing applied' % group)
        rec['detail'] = 'conserve_afrac: nothing applied'
        return
    if len(_hits) > 1:
        rec.setdefault('warnings', []).append(
            'conserve_afrac: labels %s exist in %d phases (%s) - applied to the FIRST '
            '(%s). If that is the wrong phase, give unique labels or split the action.'
            % (group, len(_hits), ', '.join(p.name for p in _hits), _hits[0].name))
    rec['phase_resolution'] = {
        'status': 'applied-to-first-match' if len(_hits) == 1 else 'ambiguous',
        'phase': _hits[0].name, 'n_phases_matching': len(_hits),
        'phases_matching': [p.name for p in _hits], 'group': group}
    try:
        gpx.index_ids()
    except Exception as e:
        rec.setdefault('warnings', []).append(
            'conserve_afrac could not index the project ids (%s) - GSAS-II would reject '
            'every variable name' % e)
    for ph in _hits[:1]:
        pidx, _all_names = _phase_index_of(ph, gpx)
        if pidx is None:
            rec.setdefault('warnings', []).append(
                'conserve_afrac: could not determine the project index of phase %s - the '
                'sum equation would point at the wrong phase, so it was NOT applied' % ph.name)
            rec['phase_resolution']['status'] = 'phase-index-unresolved'
            continue
        rec['phase_resolution']['phase_index'] = pidx
        _idx, missing, enabled = _atom_index(ph, group, need_f=need_f)
        if missing:
            rec.setdefault('warnings', []).append(
                'conserve_afrac: site(s) %s not found in phase %s' % (', '.join(missing), ph.name))
        labels = [l for l in group if l not in missing]
        vs = _afrac_vars(ph, labels, pidx)
        if len(vs) < 2:
            rec.setdefault('warnings', []).append(
                'conserve_afrac: fewer than 2 resolvable sites in phase %s - skipped' % ph.name)
            continue
        occ = _occ_of(ph, labels)
        s0 = sum(v for v in occ.values() if v is not None)
        total = float(total_in) if total_in is not None else s0
        if need_f and _consistency_check(ph, labels):
            rec.setdefault('warnings', []).append(
                'conserve_afrac: %s are already tied to each other by an equivalence, so this sum '
                'is frozen - GSAS will ignore the equation. Free at least one member first.'
                % ', '.join(labels))
        if normalize and s0 > 0 and abs(s0 - total) > 1e-12:
            ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
            ct = ptr[1]
            for at in (ph.data.get('Atoms') or []):
                if str(at[ct - 1]) in labels and _num(at[6]) is not None:
                    at[6] = float(at[6]) * total / s0
            rec.setdefault('notes', []).append(
                'conserve_afrac: normalized %s from %.6g to %.6g before constraining'
                % ('+'.join(labels), s0, total))
        try:
            gpx.add_EqnConstr(total, tuple(vs), [1] * len(vs), reloadIdx=False, override=True)
            _stored = set(_constraint_var_phases(gpx))
            if pidx not in _stored:
                rec.setdefault('warnings', []).append(
                    'conserve_afrac: stored constraint variables do not reference phase %d '
                    '(%s); phases present: %s - verify before trusting this fit.'
                    % (pidx, ph.name, sorted(_stored) or 'none'))
            applied.append({'group': labels, 'total': total,
                            'total_before': s0,
                            'phase': ph.name, 'phase_index': pidx,
                            'entries': {l: occ.get(l) for l in labels},
                            'normalized': normalize})
            if enabled:
                rec.setdefault('notes', []).append(
                    'conserve_afrac: enabled occupancy refinement (F) on %s' % ', '.join(enabled))
        except Exception as e:
            rec.setdefault('warnings', []).append('conserve_afrac failed: %s' % e)
    rec['detail'] = ('conserve_afrac: ' + '; '.join(
        '%s held at %.6g' % ('+'.join(g['group']), g['total']) for g in applied)
        if applied else 'conserve_afrac: nothing applied')
    rec['conserve'] = applied
    return True


def act_check_conservation(gpx, a, rec):
    """Read the occupancies back and report whether each conserved group still
    sums to its target. Never assume a constraint held - measure it."""
    groups = a.get('groups') or []
    out = []
    for g in groups:
        labels = [str(l) for l in (g.get('group') or [])]
        total = g.get('total')
        for ph in gpx.phases():
            occ = _occ_of(ph, labels)
            if not occ:
                continue
            s = sum(v for v in occ.values() if v is not None)
            row = {'group': labels, 'total_after': s, 'entries': occ,
                   'target': total}
            if total is not None:
                row['residual'] = s - float(total)
                row['held'] = abs(row['residual']) <= 1e-4 * max(1.0, abs(float(total)))
            out.append(row)
    rec['detail'] = 'conservation check: ' + ('; '.join(
        '%s=%.6g%s' % ('+'.join(r['group']), r['total_after'],
                       '' if r.get('held') is None else (' HELD' if r['held'] else ' NOT HELD'))
        for r in out) if out else 'no groups')
    rec['conservation'] = out
    return True


def _same_element(a, b):
    """True when two names denote the same element once symbols are resolved.

    '?' (definitely not an element) never matches a genuine symbol — otherwise
    "FeN_1" would be salvaged into "Fe" and the mismatch this check exists to
    catch would be silently accepted.
    """
    ea, eb = _element_symbol(a, strict=True), _element_symbol(b, strict=True)
    if ea == '?' or eb == '?':
        return ea == eb
    if ea and eb:
        return ea == eb
    return str(a or '').strip() == str(b or '').strip()

def check_atom_types_vs_cif(ph, cif):
    """Compare each atom's GSAS-II TYPE against what the CIF declares.

    Why this exists: when a CIF's `_atom_site_type_symbol` is not an element,
    GSAS-II does not fail - it silently SUBSTITUTES an element (Xe, or e.g. H for
    "FeN_1"), so the site scatters wrongly while everything downstream looks
    healthy, and refinement later dies with a bare `KeyError: 'Fen_'`. Sites are
    matched by coordinates (atom order is not guaranteed), and a coordinate may
    carry several declared types.
    """
    checks = []
    if not (cif and os.path.isfile(cif)):
        return checks
    declared = _declared_types_from_cif(cif)
    if not declared:
        return checks
    gen = ph.data.get('General') or {}
    ptr = gen.get('AtomPtrs') or [3, 1, 7, 9]
    cx, ct = ptr[0], ptr[1]
    for at in (ph.data.get('Atoms') or []):
        try:
            key = (round(float(at[cx]), 3), round(float(at[cx + 1]), 3), round(float(at[cx + 2]), 3))
        except (ValueError, TypeError):
            continue
        cands = declared.get(key) or []
        if not cands:
            continue
        got = str(at[ct] or '')
        if any(_same_element(got, sym) for sym, _lbl in cands):
            continue                      # GSAS-II kept what the CIF declared
        bad = [c for c in cands if _element_symbol(c[0], strict=True) == '?']
        if not bad:
            continue                      # substitution, but the CIF type was sane
        sym, label = bad[0]
        checks.append(
            'site %r at %s: the CIF declares _atom_site_type_symbol %r, which is not an '
            'element - GSAS-II silently replaced the atom type with %r, so this site scatters '
            'wrongly and a later refinement fails with a bare KeyError. Write a real element in '
            '_atom_site_type_symbol, keeping the long name only in _atom_site_label.'
            % (label or '?', key, sym, got))
    return checks


def ensure_element_tables(gpx):
    """Fill per-phase General tables (Isotope/AtomMass/NoAtoms/AtomTypes).

    GSAS-II resolves scattering lengths from phase['General']['Isotope'] and uses
    General['AtomMass']/['NoAtoms'] for composition; an element missing there
    aborts refinement with KeyError('<El>') (seen as KeyError: 'Mo').

    NOTE: `AtomMass` is a LIST parallel to `AtomTypes` (while `Isotope` and
    `NoAtoms` are dicts). The earlier code treated it as a dict, so `ams[el] = …`
    raised "list indices must be integers or slices, not str" and the whole
    helper aborted silently — the tables were never filled at all (seen in the
    xrd_gsas_ops log on 2026-09-17).

    2026-09-18 REWRITE (the "'Fen_'" crash): the key must be the atom TYPE, never
    a string derived from the atom LABEL. GSAS-II reads the type from the CIF
    `_atom_site_type_symbol` column, while `GetBLtable` iterates
    General['AtomTypes'] and builds `AtmBlens[getElSym(El) + '_']`. Deriving the
    key from the label ("FeN_1" -> "Fen") wrote a useless table entry AND, as one
    failure aborted the whole loop, left the real tables unfilled — the refine
    action then failed with a bare `KeyError: 'Fen_'` (GSASIIElem.py:146). Each
    atom is now handled independently and anything unfillable is returned.

    Returns a list of warning strings (empty when everything was fine).
    """
    checks = []
    try:
        for ph in gpx.phases():
            gen = ph.data.setdefault('General', {})
            iso = gen.setdefault('Isotope', {})
            ams = gen.setdefault('AtomMass', [])
            nat = gen.setdefault('NoAtoms', {})
            atypes = gen.setdefault('AtomTypes', [])
            for atom in ph.atoms():
                try:
                    typ = str(getattr(atom, 'type', '') or '').strip()
                    lbl = str(getattr(atom, 'label', '') or '')
                    key = typ or lbl
                    if not key:
                        continue
                    el = _element_symbol(typ, lbl)
                    if el not in _ELEMENTS and el not in _MASS:
                        checks.append(
                            '%s: atom type %r (site %r) is not an element symbol - GSAS-II '
                            'replaces such a type with Xe/H, which scatters wrongly, and the '
                            'refinement then dies with a bare KeyError; the usual cause is a CIF '
                            'whose _atom_site_type_symbol column holds the site LABEL (e.g. '
                            "'FeN_1'). Regenerate the CIF with a real element in "
                            '_atom_site_type_symbol.' % (ph.name, typ or lbl, lbl or '?'))
                        continue
                    if key not in iso:
                        iso[key] = 'Nat'
                        log("%s: added Isotope[%s]='Nat'" % (ph.name, key))
                    if key not in nat:
                        nat[key] = 0.0
                    if key not in atypes:
                        atypes.append(key)
                        # keep the parallel AtomMass list aligned with AtomTypes
                        if isinstance(ams, list):
                            ams.append(_mass_of(el))
                            log('%s: added AtomMass[%s]=%s (list, appended)'
                                % (ph.name, key, _mass_of(el)))
                        else:
                            ams[key] = _mass_of(el)
                            log('%s: added AtomMass[%s]=%s' % (ph.name, key, ams[key]))
                except Exception as e:          # one bad site must not stop the rest
                    checks.append('%s: could not fill element tables for one site: %s'
                                  % (ph.name, e))
    except Exception as e:
        checks.append('element-table preparation skipped: %s' % e)
    return checks


# ---------------------------------------------------------------------------
# parameter snapshot (the "everything is reachable" read side)
# ---------------------------------------------------------------------------
CELL_KEYS = ['length_a', 'length_b', 'length_c', 'angle_alpha', 'angle_beta',
             'angle_gamma', 'volume']


def atom_table(ph):
    gen = ph.data.get('General') or {}
    ptr = gen.get('AtomPtrs') or [3, 1, 7, 9]
    cx, ct, cs, cia = ptr[0], ptr[1], ptr[2], ptr[3]
    out = []
    for at in (ph.data.get('Atoms') or []):
        # ADP MODE MARKER (atom[cia]): 'I' = isotropic (Uiso at cia+1), 'A' =
        # anisotropic (six Uij at cia+1..cia+6). GSAS-II refines according to THIS
        # marker, not according to the refine flags - so the mode has to be
        # reported, otherwise an anisotropic atom looks like an atom with no
        # thermal parameter at all and silently escapes every Uiso check
        # (user 2026-09-22; measured on a real project where six betas were being
        # refined while the Uiso column sat stale).
        mode = str(at[cia]) if cia < len(at) else 'I'
        uij = None
        if mode == 'A':
            try:
                vals = [_num(at[cia + k]) for k in range(1, 7)]
                if all(v is not None for v in vals):
                    uij = vals
            except Exception:
                uij = None
        try:
            uiso = _num(at[cia + 1]) if mode == 'I' else None
        except Exception:
            uiso = None
        rec = {
            'label': str(at[ct - 1]),
            'element': str(at[ct]),
            'flags': str(at[ct + 1]),
            'adp': mode,
            'x': _num(at[cx]), 'y': _num(at[cx + 1]), 'z': _num(at[cx + 2]),
            'occ': _num(at[cx + 3]),
        }
        if isinstance(at[cs + 1], (int, float)):
            rec['mult'] = int(at[cs + 1])
        if uiso is not None:
            rec['uiso'] = uiso
        if uij is not None:
            rec['uij'] = uij
        out.append(rec)
    return out


def hist_table(gpx):
    hists = []
    for h in gpx.histograms():
        inst = []
        try:
            for k, v in (h.data.get('Instrument Parameters') or [{}])[0].items():
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    val = _num(v[1])
                    if val is None:
                        val = _num(v[0])
                    rec = {'name': str(k)}
                    if val is not None:
                        rec['value'] = val
                    if len(v) >= 3 and isinstance(v[2], bool):
                        rec['vary'] = bool(v[2])
                    inst.append(rec)
        except Exception as e:
            log('instrument table skipped for %s: %s' % (h.name, e))
        bkg = {'coeffs': []}
        try:
            b = h.data.get('Background')
            # GSAS layout: Background = [[bakType, refineFlag, nBak, c0, c1, ...],
            #   {nDebye, debyeTerms, nPeaks, peaksList, 'background PWDR'}, ...]
            # The old code searched for a dict key named 'background', which never
            # exists, so this table was silently always empty.
            if b and isinstance(b[0], (list, tuple)) and b[0]:
                head = b[0]
                bkg['type'] = str(head[0])
                if len(head) > 1 and isinstance(head[1], bool):
                    bkg['refine'] = bool(head[1])
                if len(head) > 3:
                    raw = head[3:]
                    bkg['coeffs'] = [x for x in (_num(v) for v in raw) if x is not None]
                    if len(bkg['coeffs']) != len(raw):
                        bkg['n_coeffs'] = len(raw)
            if b and len(b) > 1 and isinstance(b[1], dict):
                fw = b[1].get('background PWDR')
                if fw and fw[0]:
                    bkg['fixed_pwdr'] = str(fw[0])
                    fm = _num(fw[1]) if len(fw) > 1 else None
                    if fm is not None:
                        bkg['fixed_mult'] = fm
                for k_src, k_dst in (('nDebye', 'n_debye'), ('nPeaks', 'n_peaks')):
                    v = b[1].get(k_src)
                    if v:
                        bkg[k_dst] = int(v)
        except Exception:
            pass
        rec = {'name': str(h.name), 'instrument': inst, 'background': bkg}
        try:
            res = dict(h.residuals or {})
            for k in ('wR', 'R', 'Rb', 'wRmin'):
                v = _num(res.get(k))
                if v is not None:
                    rec[k] = v
        except Exception:
            pass
        hists.append(rec)
    return hists


def phase_table(gpx):
    out = []
    for ph in gpx.phases():
        gen = ph.data.get('General') or {}
        sg = gen.get('SGData') or {}
        cell = gen.get('Cell') or []
        raw = cell[1:8] if len(cell) >= 8 else []
        vals = [v for v in (_num(v) for v in raw) if v is not None]
        rec = {
            'name': str(ph.name),
            'space_group': str(sg.get('SpGrp') or '').strip(),
            'cell_keys': CELL_KEYS[:len(vals)],
            'cell': vals,
            'atoms': atom_table(ph),
        }
        if cell:
            rec['cell_vary'] = bool(cell[0])
        out.append(rec)
    return out


def review_table(gpx, opts=None):
    """Compact reasonableness review of what GSAS-II produced.

    USER RULE (2026-09-18): the AI must sanity-check the GSAS-II output before
    anything is reported - "no particularly deranged U values". This is returned
    with every xrd_gsas_ops call so the check is part of the payload instead of
    something that has to be remembered. It only REPORTS; nothing is blocked.
    """
    opts = opts or {}
    uiso_hi = float(opts.get('uiso_flag_above', 0.2))
    flags, phases = [], []
    n_iso, n_aniso = 0, 0
    for ph in gpx.phases():
        rows = []
        for at in atom_table(ph):
            lbl = str(at.get('label'))
            where = '%s/%s' % (ph.name, lbl)
            row = {'label': lbl, 'element': at.get('element'), 'occ': at.get('occ'),
                   'adp': at.get('adp')}
            if at.get('adp') == 'A':
                n_aniso += 1
                # An anisotropic atom carries six Uij and NO Uiso at all, so the
                # Uiso checks below cannot see it. Report the mode and check the
                # DIAGONAL (U11/U22/U33) here - a negative beta is just as
                # unphysical as a negative Uiso, and used to pass this review
                # silently (user 2026-09-22).
                uij = at.get('uij')
                if uij:
                    row['uij'] = uij
                flags.append({'severity': 'info', 'what': 'anisotropic ADP (Uij)',
                              'where': where,
                              'detail': 'this atom refines six betas; the Uiso column is '
                                        'NOT what is being refined - switch with '
                                        'set_adp_mode if that was not intended'})
                for k, lvl in enumerate(('U11', 'U22', 'U33')):
                    if uij and uij[k] is not None and uij[k] < 0:
                        flags.append({'severity': 'fail',
                                      'what': 'negative %s (anisotropic ADP)' % lvl,
                                      'where': where, 'value': uij[k]})
            else:
                n_iso += 1
            u = at.get('uiso')
            if u is not None:
                row['Uiso'] = u
                if u < 0:
                    flags.append({'severity': 'fail', 'what': 'negative Uiso',
                                  'where': where, 'value': u})
                elif u > uiso_hi:
                    flags.append({'severity': 'warn',
                                  'what': 'Uiso above %.2f' % uiso_hi,
                                  'where': where, 'value': u})
                elif u == 0:
                    flags.append({'severity': 'warn',
                                  'what': 'Uiso exactly 0 (collapsed or fixed)',
                                  'where': where, 'value': u})
            o = at.get('occ')
            if o is not None and not (0.0 < o <= 1.001):
                flags.append({'severity': 'fail', 'what': 'occupancy outside (0,1]',
                              'where': where, 'value': o})
            rows.append(row)
        phases.append({'name': str(ph.name), 'atoms': rows})
    hists = []
    for h in gpx.histograms():
        wR = _num(h.get_wR())
        rec = {'name': str(h.name)}
        if wR is not None:
            rec['wR'] = wR
            if wR >= 50.0:
                flags.append({'severity': 'fail', 'what': 'wR >= 50% (divergent)',
                              'where': str(h.name), 'value': wR})
        hists.append(rec)
    verdict = 'fail' if any(f['severity'] == 'fail' for f in flags) else (
        'warn' if any(f['severity'] == 'warn' for f in flags) else 'pass')
    return {
        'verdict': verdict,
        'flags': flags,
        'adp': {'isotropic': n_iso, 'anisotropic': n_aniso,
                'note': ('GSAS-II decides WHICH thermal parameter is refined from the '
                         'per-atom ADP mode marker, not from the refine flags. '
                         'isotropic atoms are checked on Uiso; anisotropic atoms are '
                         'checked on the U11/U22/U33 diagonal instead, because their '
                         'Uiso column is not what is being refined.')},
        'phases': phases,
        'histograms': hists,
        'note': ('AI review of the GSAS-II output: check Uiso / occupancy sanity '
                 'before reporting. A negative Uiso, a negative U11/U22/U33 on an '
                 'anisotropic atom, or an out-of-range occupancy means this must NOT '
                 'be reported as a finished structure.'),
    }


# ---------------------------------------------------------------------------
# action executors
# ---------------------------------------------------------------------------
def act_set_atoms(gpx, a, rec):
    touched = 0
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        cx, ct, cia = ptr[0], ptr[1], ptr[3]
        for at in (ph.data.get('Atoms') or []):
            lab = str(at[ct - 1])
            if a.get('all'):
                at[ct + 1] = str(a['all'])
                touched += 1
            per = (a.get('atoms') or {}).get(lab)
            if per is not None:
                at[ct + 1] = str(per)
                touched += 1
            u = (a.get('uiso') or {}).get(lab)
            if u is not None:
                at[cia + 1] = float(u)
                touched += 1
            o = (a.get('occ') or {}).get(lab)
            if o is not None:
                at[cx + 3] = float(o)
                touched += 1
            p = (a.get('pos') or {}).get(lab)
            if p:
                for i, v in enumerate(p[:3]):
                    if v is not None:
                        at[cx + i] = float(v)
                        touched += 1
    rec['detail'] = '%d atom field(s) updated' % touched
    return True


def _atom_selector(a):
    """Normalise the `atoms` selector of open_uiso / set_adp_mode.

    Returns None for "every atom" or a list of labels. The hole this closes
    (D15, found by auditing a real SampleA run): the action docstring documented
    `"atoms":["all"]`, but the check was `labels != 'all' and lab not in labels`,
    so the LIST ['all'] matched no atom and set_adp_mode died with
    "matched no atom (atoms=['all'])" - the exact string that run hit. Accepted
    "all atoms" spellings: omitted / null / '' / 'all' / ['all'] / [] (any case).
    """
    raw = a.get('atoms')
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return None if (not s or s.lower() == 'all') else [s]
    if isinstance(raw, (list, tuple, set)):
        labs = [str(x).strip() for x in raw if str(x).strip()]
        if not labs or any(x.lower() == 'all' for x in labs):
            return None
        return labs
    return [str(raw)]


def act_open_uiso(gpx, a, rec):
    n = 0
    labels = _atom_selector(a)
    seen = []
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        ct = ptr[1]
        for at in (ph.data.get('Atoms') or []):
            seen.append(str(at[ct - 1]))
            if labels is not None and str(at[ct - 1]) not in labels:
                continue
            if 'U' not in str(at[ct + 1]):
                at[ct + 1] = str(at[ct + 1]) + 'U'
                n += 1
    rec['detail'] = 'Uiso flag opened on %d atom(s)' % n
    if labels is not None:
        missing = [x for x in labels if x not in seen]
        rec['atoms_matched'] = [x for x in labels if x in seen]
        if missing:
            # A typo'd label used to be silent: 0 atoms touched and nothing said so.
            rec['detail'] += '; WARNING no such atom: %s (available: %s)' % (
                ','.join(missing), ','.join(seen) if seen else 'none')
    return True


def act_set_adp_mode(gpx, a, rec):
    """Switch atoms between isotropic (Uiso) and anisotropic (Uij) ADP mode.

    USER REQUEST (2026-09-22): "gsas-II 引擎没有暴露各向同/异性的接口". Correct -
    nothing in this tool used to touch the ADP MODE MARKER `atom[cia]` ('I' or 'A'),
    and GSAS-II decides WHICH thermal parameter to refine from that marker, not
    from the refine flags. So with the marker left on 'A' the refinement drives six
    betas even when the flags say 'XU', while the Uiso column stays at its initial
    value; `uiso_clamp`, `fix_uiso`, `export_cif(uiso=physical)` and the Uiso review
    were all reading a quantity that was not being refined (measured on a real
    project: six betas refined, Uiso column stale, negative betas invisible).

    isotropic  -> marker 'I'; the six Uij are ZEROED so a stale negative beta cannot
                  be picked up again if the marker ever goes back to 'A'.
    anisotropic-> marker 'A' (the six Uij slots are left as they are - write them
                  with set_atoms/uiso if a starting value is needed).
    `open_uiso` (default true) also puts the 'U' refine flag on, because a mode
    switch that refines nothing is its own trap; pass open_uiso=false to leave the
    flags untouched.
    """
    mode = str(a.get('mode') or '').lower()
    if mode not in ('isotropic', 'anisotropic'):
        raise RuntimeError('set_adp_mode needs mode="isotropic"|"anisotropic"')
    labels = _atom_selector(a)
    open_u = a.get('open_uiso')
    if open_u is None:
        open_u = True
    rows, n_opened = [], 0
    seen = []
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        ct, cia = ptr[1], ptr[3]
        for at in (ph.data.get('Atoms') or []):
            lab = str(at[ct - 1])
            seen.append(lab)
            if labels is not None and lab not in labels:
                continue
            old = str(at[cia])
            if mode == 'isotropic':
                # A -> I: at[cia+1] currently holds U11, NOT a Uiso, so it cannot be
                # inherited as-is. Use the mean of the diagonal as the starting Uiso
                # (an explicit uiso:{label:value} wins), then zero the other five
                # slots so no stale beta can come back if the marker returns to 'A'.
                given = (a.get('uiso') or {}).get(lab)
                diag = [_num(at[cia + k]) for k in (1, 2, 3)]
                diag = [v for v in diag if v is not None]
                if given is not None:
                    u0 = float(given)
                elif diag:
                    u0 = sum(diag) / float(len(diag))
                else:
                    u0 = _num(at[cia + 1])
                at[cia] = 'I'
                if u0 is not None:
                    at[cia + 1] = float(u0)
                for k in range(2, 7):
                    at[cia + k] = 0.0
                if open_u and 'U' not in str(at[ct + 1]):
                    at[ct + 1] = str(at[ct + 1]) + 'U'
                    n_opened += 1
                rows.append({'label': lab, 'phase': str(ph.name), 'from': old,
                             'to': str(at[cia]),
                             'uiso_from_diagonal_mean': _num(u0)})
                continue
            else:
                at[cia] = 'A'
                if open_u and 'U' not in str(at[ct + 1]):
                    at[ct + 1] = str(at[ct + 1]) + 'U'
                    n_opened += 1
            rows.append({'label': lab, 'phase': str(ph.name), 'from': old,
                         'to': str(at[cia])})
    if not rows:
        raise RuntimeError('set_adp_mode matched no atom (atoms=%r; available: %s)'
                           % (a.get('atoms'), ','.join(seen) if seen else 'none'))
    rec['adp_mode'] = {'mode': mode, 'atoms': rows, 'u_flag_opened': n_opened}
    rec['detail'] = ('ADP mode -> %s on %d atom(s)%s'
                     % (mode, len(rows),
                        '; %d U flag(s) opened' % n_opened if n_opened else ''))
    return True


def act_set_hist(gpx, a, rec):
    section = a.get('section')
    if not section:
        raise RuntimeError('set_hist needs "section"')
    value = a.get('value')
    step = {'set': {section: value}}
    if a.get('phases'):
        step['phases'] = a['phases']
    gpx.do_refinements([step])
    wR = _num(gpx.histograms()[0].get_wR())
    rec['detail'] = ('set %s = %s | the wR reported here is the result of the ONE '
                     'refinement step this set_* action runs to install the setting - '
                     'NOT comparable with a previously reported wR'
                     % (section, json.dumps(value)))
    if wR is not None:
        rec['wR'] = wR
        rec['wR_kind'] = 'post-set single-step refit (NOT comparable with a previously reported wR)'
    return True


def act_set_cell(gpx, a, rec):
    if a.get('params'):
        done = []
        want_vary = False
        for ph in gpx.phases():
            cell = ph.data['General']['Cell']
            for k, spec in (a['params'] or {}).items():
                if k not in CELL_KEYS or not isinstance(spec, dict):
                    continue
                i = CELL_KEYS.index(k) + 1
                if spec.get('value') is not None:
                    cell[i] = float(spec['value'])
                    done.append('%s.%s=%s' % (ph.name, k, spec['value']))
                if k == 'volume':
                    continue
                if spec.get('vary') is not None:
                    want_vary = want_vary or bool(spec['vary'])
                    done.append('%s.%s vary=%s' % (ph.name, k, spec['vary']))
        # GSAS-II exposes a single phase-wide Cell refine flag (no per-parameter
        # flag through the scripting API) - say so when per-parameter vary was asked.
        if want_vary:
            gpx.do_refinements([{'set': {'Cell': True}, 'skip': True}])
            rec['detail'] = ('cell params: %s | NOTE: GSAS-II has ONE phase-wide Cell '
                             'flag, so all cell parameters refine together'
                             % ', '.join(done))
        else:
            rec['detail'] = 'cell params: ' + ', '.join(done)
        return True
    step = {'set': {'Cell': True}}
    if a.get('phases') and a['phases'] != ['all']:
        step['phases'] = a['phases']
    gpx.do_refinements([step])
    wR = _num(gpx.histograms()[0].get_wR())
    rec['detail'] = ('Cell refinement flag set | the wR reported here is the result of the '
                     'ONE refinement step this set_* action runs - NOT comparable with a '
                     'previously reported wR')
    if wR is not None:
        rec['wR'] = wR
        rec['wR_kind'] = 'post-set single-step refit (NOT comparable with a previously reported wR)'
    return True


def act_scale(gpx, a, rec):
    phs = gpx.phases()
    want = a.get('phase')
    if want in (None, 'all'):
        targets = phs
    elif want == '2nd':
        targets = phs[1:]
    else:
        targets = [p for p in phs if p.name == want]
    n = 0
    for ph in targets:
        for hn, hp in (ph.data.get('Histograms') or {}).items():
            if a.get('value') is not None:
                hp['Scale'][0] = float(a['value'])
                n += 1
            if a.get('vary') is not None:
                hp['Scale'][1] = bool(a['vary'])
                n += 1
    rec['detail'] = '%d scale field(s) set on %d phase(s)' % (n, len(targets))
    return True


def act_equiv_uiso(gpx, a, rec):
    """Group atoms so they share ONE refined Uiso (GSAS add_EquivConstr).

    Variable naming: '0::AUiso:<index of the atom row in the phase Atoms table>'.
    """
    groups = a.get('groups') or {}
    if not groups:
        raise RuntimeError('equiv_uiso needs "groups"')
    done = []
    # PHASE RESOLUTION (audit 2026-09-21): every group is resolved to ONE phase
    # instead of being applied to whichever phase happens to share the labels. Each
    # group is decided on its own (a multi-phase project can legitimately tie Uiso in
    # different phases with different label sets), and the choice is reported.
    atom_idx = {}
    try:
        gpx.index_ids()
    except Exception as e:
        rec.setdefault('warnings', []).append(
            'equiv_uiso could not index the project ids (%s) - GSAS-II would reject '
            'every variable name' % e)
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        ct = ptr[1]
        atom_idx[ph.name] = (ph, str(ct - 1),
                             {str(at[ct - 1]): i for i, at in enumerate(ph.data.get('Atoms') or [])})
    resolutions = {}
    for gname, labels in groups.items():
        labels = [str(l) for l in (labels or [])]
        hits = []
        for pname, (ph, _c, idx) in atom_idx.items():
            if sum(1 for l in labels if l in idx) >= 2:
                hits.append(pname)
        if not hits:
            rec.setdefault('warnings', []).append(
                'equiv_uiso %s: group %s resolved in NO phase - skipped' % (gname, labels))
            resolutions[gname] = {'status': 'not-found', 'group': labels}
            continue
        if len(hits) > 1:
            rec.setdefault('warnings', []).append(
                'equiv_uiso %s: labels exist in %d phases (%s) - applied to the FIRST '
                '(%s); give unique labels or split the action per phase if that is wrong'
                % (gname, len(hits), ', '.join(hits), hits[0]))
        resolutions[gname] = {
            'status': 'applied-to-first-match' if len(hits) == 1 else 'ambiguous',
            'phase': hits[0], 'n_phases_matching': len(hits), 'group': labels}
        ph, _c, idx = atom_idx[hits[0]]
        pidx, _all_names = _phase_index_of(ph, gpx)
        if pidx is None:
            rec.setdefault('warnings', []).append(
                'equiv_uiso %s: could not determine the project index of phase %s - the '
                'group would point at the wrong phase, so it was NOT applied'
                % (gname, ph.name))
            resolutions[gname]['status'] = 'phase-index-unresolved'
            continue
        resolutions[gname]['phase_index'] = pidx
        varlist = []
        for lab in labels:
            if lab in idx:
                varlist.append('%d::AUiso:%d' % (pidx, idx[lab]))
            else:
                rec.setdefault('warnings', []).append(
                    'equiv_uiso: atom %s not found in phase %s' % (lab, ph.name))
        if len(varlist) < 2:
            continue
        try:
            gpx.add_EquivConstr(tuple(varlist), reloadIdx=False, override=True)
            _stored = set(_constraint_var_phases(gpx))
            if pidx not in _stored:
                rec.setdefault('warnings', []).append(
                    'equiv_uiso %s: stored variables do not reference phase %d (%s); '
                    'phases present: %s - verify before trusting this fit.'
                    % (gname, pidx, ph.name, sorted(_stored) or 'none'))
            done.append('%s@%s(%d atoms)' % (gname, ph.name, len(varlist)))
        except Exception as e:
            rec.setdefault('warnings', []).append(
                'equiv_uiso %s failed: %s' % (gname, e))
    rec['phase_resolution'] = resolutions
    rec['detail'] = 'equivalent-Uiso groups: ' + (', '.join(done) if done else 'none')
    return True


def act_uiso_clamp(gpx, a, rec):
    """Refine, clamp every Uiso below `min` up to `min` and drop its U flag;
    repeat until a round finds nothing new (recipe of
    PB/XRD/mono2phase/gsas_uclamp.py)."""
    umin = float(a.get('min', 0.01))
    rounds = int(a.get('max_rounds', 8))
    clear_flag = a.get('clear_flag', True)
    history = []
    for rnd in range(rounds):
        try:
            gpx.do_refinements([{}])
        except Exception as e:
            rec.setdefault('warnings', []).append('clamp refine failed: %s' % e)
            break
        wR = _num(gpx.histograms()[0].get_wR())
        clamped = []
        for ph in gpx.phases():
            ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
            ct, cia = ptr[1], ptr[3]
            for at in (ph.data.get('Atoms') or []):
                try:
                    u = float(at[cia + 1])
                except Exception:
                    continue
                if u < umin:
                    at[cia + 1] = umin
                    if clear_flag and 'U' in str(at[ct + 1]):
                        at[ct + 1] = str(at[ct + 1]).replace('U', '')
                    clamped.append({'atom': str(at[ct - 1]), 'was': _num(u)})
        entry = {'round': rnd}
        if wR is not None:
            entry['wR'] = wR
        entry['clamped'] = clamped
        history.append(entry)
        log('clamp round %d: wR=%s clamped=%d' % (rnd, wR, len(clamped)))
        if not clamped:
            break
    rec['detail'] = 'Uiso clamp loop: %d round(s), %d atom(s) clamped' % (
        len(history), sum(len(h['clamped']) for h in history))
    rec['rounds'] = history
    return True


def act_fix_uiso(gpx, a, rec):
    want = a.get('atoms') or {}
    n = 0
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        ct, cia = ptr[1], ptr[3]
        for at in (ph.data.get('Atoms') or []):
            lab = str(at[ct - 1])
            if lab in want:
                at[cia + 1] = float(want[lab])
                at[ct + 1] = str(at[ct + 1]).replace('U', '')
                n += 1
    rec['detail'] = '%d atom Uiso fixed and removed from refinement' % n
    return True


def act_clear_constraints(gpx, a, rec):
    cons = (gpx.data.get('Constraints') or {}).get('data') or {}
    before = {k: len(cons.get(k) or []) for k in ('Hist', 'HAP', 'Phase')}
    for k in ('Hist', 'HAP', 'Phase'):
        cons[k] = []
    rec['detail'] = 'constraints cleared: %s' % json.dumps(before)
    return True


def act_split_site(gpx, a, rec):
    """Duplicate a site into n sub-sites along one axis (K split-site recipe).
    New rows start with refinement flags cleared; the original site is retired
    (zero occupancy) so the split is the only modelled species."""
    site = a.get('site')
    n = int(a.get('n', 2))
    frac = float(a.get('frac', 0.5))
    axis = str(a.get('axis', 'x')).lower()
    if axis not in ('x', 'y', 'z'):
        axis = 'x'
    ai = {'x': 0, 'y': 1, 'z': 2}[axis]
    created = []
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        cx, ct = ptr[0], ptr[1]
        atoms = ph.data.get('Atoms') or []
        src = None
        for at in atoms:
            if str(at[ct - 1]) == site:
                src = at
                break
        if src is None:
            rec.setdefault('warnings', []).append('split_site: %s not found' % site)
            continue
        base = list(src)
        total_occ = float(src[cx + 3])
        # UNIQUE sub-site labels (audit 2026-09-21). The old naming was a fixed
        # `%s_%d`, so running split_site twice on the same site produced a SECOND set
        # of rows with the SAME labels — duplicate site IDs in one phase, which makes
        # later constraint lookups ambiguous (and GSAS-II unhappy). Names are now
        # chosen against the labels already present, and any bump is reported.
        existing = {str(at[ct - 1]) for at in atoms}
        names = []
        for k in range(n):
            cand = '%s_%d' % (site, k + 1)
            bump = 1
            while cand in existing or cand in names:
                bump += 1
                cand = '%s_%d_%d' % (site, k + 1, bump)
            names.append(cand)
        if any(nm != '%s_%d' % (site, k + 1) for k, nm in enumerate(names)):
            rec.setdefault('notes', []).append(
                'split_site: sub-site names were adjusted to stay unique (%s) because '
                'the default names already existed - the site may have been split '
                'before' % ', '.join(names))
        # UNIQUE internal atom IDs (audit 2026-09-21, second pass). GSAS-II stores a
        # random atom id as the LAST element of each atom record ("this code assumes
        # that the atom random Id (ranId) is the last element each atom record",
        # GSASIIobj.py) and repairs duplicates by drawing a new number — i.e. a copied
        # row silently shares the source's identity until GSAS fixes it. Fresh ids are
        # assigned here so the split is well-formed the moment it is written.
        used_ids = set()
        for at in atoms:
            if at and isinstance(at[-1], int):
                used_ids.add(at[-1])
        new_ids = _fresh_atom_ids(used_ids, len(names))
        for k, nm in enumerate(names):
            row = list(base)
            row[ct - 1] = nm
            if row and isinstance(row[-1], int):
                row[-1] = new_ids[k]
            row[cx + ai] = float(base[cx + ai]) + (k - (n - 1) / 2.0) * frac
            row[cx + 3] = total_occ / float(n)
            row[ct + 1] = 'X'
            atoms.append(row)
            created.append(nm)
        src[cx + 3] = 0.0
        src[ct + 1] = 'X'
    rec['detail'] = ('split %s into %d sub-site(s) along %s: %s'
                     % (site, n, axis, ', '.join(created) if created else 'none')
                     + ('; fresh atom ids assigned so no record shares the source identity'
                        if created else ''))
    return True


def act_refine(gpx, a, rec):
    raw = a.get('raw')
    steps = raw if isinstance(raw, list) and raw else [{}]
    # P1 (0.8.5): a multi-step recipe is handed to GSAS-II ONE STEP AT A TIME, with a
    # guard checkpoint between steps. The old single `do_refinements(steps)` call had
    # no safe point inside it, so a stop request during a long recipe could only be
    # answered by killing the tree. Same steps, same order — only the granularity of
    # the stop check changes.
    g = active_guard()
    for _si, _st in enumerate(steps, 1):
        if g is not None:
            g.checkpoint('refine step %d/%d' % (_si, len(steps)))
        gpx.do_refinements([_st])
    wR = _num(gpx.histograms()[0].get_wR())
    rec['detail'] = 'do_refinements with %d step(s)' % len(steps)
    if wR is not None:
        rec['wR'] = wR
    return True


def act_replace_phase(gpx, a, rec):
    """Replace the project's phase(s) with a clean CIF while keeping the
    histograms, background, instrument parameters and size/strain settings
    (recipe: PB/XRD/mono2phase/replace_phase_clean_engine.py)."""
    cif = a.get('cif')
    if not (cif and os.path.exists(cif)):
        raise RuntimeError('replace_phase needs an existing "cif"')
    old = [p.name for p in gpx.phases()]
    hist = gpx.histograms()
    for nm in old:
        try:
            (gpx.data.get('Phases') or {}).pop(nm, None)
        except Exception:
            pass
        for lst in gpx.names:
            if lst and lst[0] == 'Phases':
                while nm in lst:
                    lst.remove(nm)
    ph = gpx.add_phase(cif, fmthint='CIF', histograms=hist)
    if a.get('keep_name') and old:
        try:
            ph.name = old[0]
        except Exception:
            pass
    for h in gpx.histograms():
        for p in gpx.phases():
            try:
                if h.name not in (p.data.get('Histograms') or {}):
                    gpx.link_histogram_phase(h.name, p)
            except Exception:
                pass
    ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
    if a.get('all_flags'):
        for at in (ph.data.get('Atoms') or []):
            at[ptr[1] + 1] = str(a['all_flags'])
    if a.get('uiso') is not None:
        for at in (ph.data.get('Atoms') or []):
            at[ptr[3] + 1] = float(a['uiso'])
    rec['detail'] = 'replaced [%s] with %s' % (', '.join(old), getattr(ph, 'name', cif))
    return True


def act_add_phase(gpx, a, rec):
    """Add another phase (second phase / impurity) and link it to every
    histogram (recipe: build_electrode_gpx_engine / gsas_twophase_engine)."""
    cif = a.get('cif')
    if not (cif and os.path.exists(cif)):
        raise RuntimeError('add_phase needs an existing "cif"')
    hist = gpx.histograms()
    ph = gpx.add_phase(cif, fmthint='CIF', histograms=hist)
    for h in gpx.histograms():
        for p in gpx.phases():
            try:
                if h.name not in (p.data.get('Histograms') or {}):
                    gpx.link_histogram_phase(h.name, p)
            except Exception:
                pass
    if a.get('scale') is not None:
        for hn, hp in (ph.data.get('Histograms') or {}).items():
            hp['Scale'][0] = float(a['scale'])
            hp['Scale'][1] = bool(a.get('vary_scale', False))
    rec['detail'] = 'added phase %s (histograms=%d, scale=%s)' % (
        getattr(ph, 'name', cif), len(hist), a.get('scale'))
    return True


def act_new_from(gpx, a, rec, save_path):
    """Derive a NEW project from the current one: save a copy, then optionally
    inject a different data file as the (only) histogram while keeping the
    phases - 'run the new sample against settings that already converged'
    (recipe: build_electrode_from_gpx_engine.py).

    Returns (project, save_path) because the working object changes.
    """
    from GSASII import GSASIIscriptable as G2sc
    target = a.get('save_as') or save_path
    gpx.save(target)
    gpx2 = G2sc.G2Project(gpxfile=target)
    data = a.get('data')
    df = data if isinstance(data, str) else (data or {}).get('file')
    if df and os.path.exists(df):
        # 0.8.8: NO default hint. GSAS-II's import_generic skips every reader whose
        # formatName lacks the hint, so 'GSAS powder' silently restricted imports to
        # GSAS-format files (see gsas_engine.py for the measured control).
        fmthint = (data.get('fmthint') if isinstance(data, dict) else None) or None
        for h in list(gpx2.histograms()):
            nm = h.name
            try:
                gpx2.data.pop(nm, None)
            except Exception:
                pass
            for lst in gpx2.names:
                if lst and lst[0] == nm:
                    lst[:] = []
        hist_objs = gpx2.add_powder_histogram(df, a.get('instprm') or '', fmthint=fmthint)
        if not isinstance(hist_objs, list):
            hist_objs = [hist_objs]
        for h in gpx2.histograms():
            for p in gpx2.phases():
                try:
                    if h.name not in (p.data.get('Histograms') or {}):
                        gpx2.link_histogram_phase(h.name, p)
                except Exception:
                    pass
        rec['detail'] = 'derived %s with new data %s' % (target, os.path.basename(df))
    else:
        rec['detail'] = 'derived project copy %s' % target
    return gpx2, target


def act_set_site_composition(gpx, a, rec):
    """Multi-element site occupancy. GSAS stores ONE element per atom row, so a
    mixed site becomes extra rows at the same coordinates (this is how
    Mn/Ni/Mo-on-one-site or Fe/Mn disorder gets modelled)."""
    spec = a.get('sites') or {}
    if not spec:
        raise RuntimeError('set_site_composition needs "sites": {label: {el: occ}}')
    added = 0
    for ph in gpx.phases():
        ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
        cx, ct = ptr[0], ptr[1]
        atoms = ph.data.get('Atoms') or []
        for label, comp in spec.items():
            src = None
            for at in atoms:
                if str(at[ct - 1]) == str(label):
                    src = at
                    break
            if src is None:
                rec.setdefault('warnings', []).append('site %s not found' % label)
                continue
            items = sorted(comp.items(), key=lambda kv: -float(kv[1]))
            base = list(src)
            src[ct] = str(items[0][0])
            src[cx + 3] = float(items[0][1])
            # GSAS-II keeps a random atom id (ranId) as the LAST element of every atom
            # record and only repairs duplicates at INDEX time ("has repeated ranId.
            # Fixing.", GSASIIobj.py). Copying the row therefore hands each new row the
            # SOURCE's identity — and constraints are stored by ranId, so duplicates
            # make them ambiguous until GSAS happens to fix the table. `split_site`
            # already draws fresh ids this way; this action was missing it
            # (audit 2026-09-21 third pass: the minimal reproduction produced two rows
            # with the same id).
            used_ids = set()
            for at in atoms:
                if at and isinstance(at[-1], int):
                    used_ids.add(at[-1])
            new_ids = _fresh_atom_ids(used_ids, max(0, len(items) - 1))
            for k, (el, occ) in enumerate(items[1:]):
                row = list(base)
                row[ct - 1] = '%s_%s' % (label, el)
                row[ct] = str(el)
                row[cx + 3] = float(occ)
                row[ct + 1] = str(a.get('new_flags') or 'X')
                if row and isinstance(row[-1], int):
                    row[-1] = new_ids[k]
                atoms.append(row)
                added += 1
    rec['detail'] = 'mixed site(s) written: %d extra row(s) for %d site(s)' % (
        added, len(spec))
    return True


def _isolate_flags(gpx):
    """Temporarily clear every refinement flag EXCEPT the background.

    do_refinements() refines whatever the project has open, so a background scan
    used to move the cell / atoms / profile as well — observed on a real tool
    call: the cell drifted 2.8876 -> 2.7491 during an auto_background scan and
    the final wR (16.01%) no longer matched the best trial (7.98%). A background
    scan is only meaningful when the trials differ solely in the Chebyshev order.
    """
    saved = []
    for ph in gpx.phases():
        gen = ph.data.get('General') or {}
        cell = gen.get('Cell')
        if cell:
            saved.append((cell, 0, cell[0]))
            cell[0] = False
        ptr = gen.get('AtomPtrs') or [3, 1, 7, 9]
        for at in (ph.data.get('Atoms') or []):
            saved.append((at, ptr[1] + 1, at[ptr[1] + 1]))
            at[ptr[1] + 1] = ''
        for _hn, hp in (ph.data.get('Histograms') or {}).items():
            for sec, idx in (('Size', 2), ('Mustrain', 2), ('HStrain', 1),
                             ('Extinction', 1), ('Pref.Ori.', 2)):
                try:
                    v = hp.get(sec)
                    if v is None or not hasattr(v, '__len__') or len(v) <= idx:
                        continue
                    old = v[idx]
                    saved.append((v, idx, old))
                    v[idx] = [False] * len(old) if isinstance(old, (list, tuple)) else False
                except Exception:
                    pass
    for h in gpx.histograms():
        inst = (h.data.get('Instrument Parameters') or [{}])[0]
        for _k, v in list(inst.items()):
            if isinstance(v, list) and len(v) >= 3 and isinstance(v[2], bool):
                saved.append((v, 2, v[2]))
                v[2] = False
    return saved


def _restore_flags(saved):
    for obj, idx, val in reversed(saved):
        try:
            obj[idx] = val
        except Exception:
            pass


def _read_bg_curve(path):
    """Read a background curve from a manual-engine components.json ({"x","bg"}),
    a plain {"x","y"} JSON, or a two-column text file."""
    txt = open(path, encoding='utf-8', errors='replace').read()
    if path.lower().endswith('.json') or txt.lstrip()[:1] in ('{', '['):
        d = json.loads(txt)
        x = d.get('x') or d.get('X') or d.get('two_theta')
        y = d.get('bg') or d.get('y') or d.get('Y') or d.get('background')
        if x is None or y is None:
            raise RuntimeError('%s carries no background curve (need "x" plus "bg"/"y")'
                               % os.path.basename(path))
        return [float(v) for v in x], [float(v) for v in y]
    xs, ys = [], []
    for line in txt.splitlines():
        s = line.strip()
        if not s or s[0] in '#!;':
            continue
        p = s.replace(',', ' ').split()
        if len(p) < 2:
            continue
        try:
            xs.append(float(p[0])); ys.append(float(p[1]))
        except ValueError:
            continue
    if len(xs) < 2:
        raise RuntimeError('could not read a background curve from %s' % path)
    return xs, ys


def act_import_background(gpx, a, rec):
    """Import a background curve fitted by the BOUNDED manual engine into GSAS-II.

    Mechanism (verified against the bundled GSAS-II 5.8.1 source): the native
    'lin interpolate' background type takes its parameters as background VALUES
    on an equally spaced grid --
        GSASIIpwd.getBackground:  bakPos = np.linspace(xmin, xmax, nBak)
        GSASIIpwd.SetBackgroundParms:  bakType,bakFlag = Background[0][:2];
                                       backVals = Background[0][3:]
        GSASIIpwdGUI OnBakTerms:  len(Background[0]) == Background[0][2] + 3
    so writing the manual curve's values at n equally spaced 2theta points makes
    that curve GSAS-II's background, with no re-fitting.

    User rule (2026-09-18): fit the background in the manual engine first
    (order 6 by default, never above 20, step 2, stop at the R plateau), then
    import the winning curve here instead of letting GSAS-II reinvent it.
    """
    src = a.get('source') or a.get('components')
    if not src:
        raise RuntimeError('import_background needs "source": a manual-engine '
                           'components.json, a {"x","y"} JSON, or a two-column text file')
    if not os.path.isfile(src):
        raise RuntimeError('import_background source not found: %s' % src)
    n = int(a.get('n_points') or 20)
    if n < 3:
        raise RuntimeError('import_background needs n_points >= 3 (GSAS lin-interpolate)')
    hists = gpx.histograms()
    if not hists:
        raise RuntimeError('project has no histograms')
    want = a.get('hist')
    hist = None
    for h in hists:
        if want and str(h.name) == str(want):
            hist = h
            break
    hist = hist or hists[0]

    x_src, y_src = _read_bg_curve(src)
    pairs = sorted(zip(x_src, y_src))       # monotonic for interpolation
    xs = [float(p[0]) for p in pairs]
    ys = [float(p[1]) for p in pairs]

    x = hist.getdata('X')
    xmin, xmax = float(x[0]), float(x[-1])
    grid = [xmin + (xmax - xmin) * i / (n - 1.0) for i in range(n)]
    vals, j = [], 0
    for gx in grid:
        while j < len(xs) - 2 and xs[j + 1] < gx:
            j += 1
        x0 = xs[j]
        x1 = xs[min(j + 1, len(xs) - 1)]
        y0 = ys[j]
        y1 = ys[min(j + 1, len(ys) - 1)]
        vals.append(y0 if x1 == x0 else y0 + (y1 - y0) * (gx - x0) / (x1 - x0))

    bkg = hist.data.get('Background')
    if not bkg or not isinstance(bkg, list):
        raise RuntimeError('histogram %s has no Background section' % hist.name)
    if len(bkg) == 1:      # fix up old backgrounds, as SetBackgroundParms does
        bkg.append({'nDebye': 0, 'debyeTerms': [], 'nPeaks': 0,
                    'peaksList': [], 'background PWDR': ['', 1.0, False]})
    vary = bool(a.get('vary', False))
    bkg[0] = ['lin interpolate', vary, n] + vals
    rec['detail'] = ('imported background from %s as lin-interpolate on %s: '
                     '%d points over %.3f-%.3f deg (refine=%s)'
                     % (os.path.basename(src), hist.name, n, grid[0], grid[-1], vary))
    rec['background_points'] = n
    rec['background_range'] = [round(grid[0], 4), round(grid[-1], 4)]
    rec['hist'] = str(hist.name)
    wR = _num(hist.get_wR())
    if wR is not None:
        rec['wR_before'] = wR
    return True


def act_auto_background(gpx, a, rec):
    """Scan the Chebyshev background order and KEEP the best one.

    Layer-oxide/PB experience: the number of background coefficients is decisive
    and was always found by hand - powder 3 -> 10 lowered Rwp sharply, electrode
    10 -> 20 gave 11.24 -> 10.23.

    `isolate` (default true) clears every OTHER refinement flag for the duration
    of the scan so the trials differ only in the Chebyshev order; the flags are
    restored afterwards and one final refinement re-converges the project under
    the caller's own settings, giving `wR_final` (comparable with the caller's
    previous runs) next to `best_wR` (the pure-background comparison).
    """
    # USER RULE (2026-09-18, RESTATED): GSAS-II's AUTO background defaults to a
    # 6th-order polynomial too. The order iteration is NOT a default step - it is
    # an escalation that is only used when every other parameter has been
    # optimised and the fit still misses the target, and it must be asked for
    # (coeffs=[...] or iterate=true).
    explicit = a.get('coeffs')
    iterate = a.get('iterate') is True or bool(explicit)
    max_order = int(a.get('max_order') or 20)
    step = int(a.get('step') or 2)
    stop_tol = float(a.get('stop_tol', 0.02))     # wR percentage points
    patience = int(a.get('patience', 1))
    if explicit:
        coeffs = [int(n) for n in explicit]        # an explicit list always wins
    elif iterate:
        start = int(a.get('start_order') or 6)
        coeffs = list(range(start, max_order + 1, step))
    else:
        coeffs = [int(a.get('start_order') or 6)]  # default: a single 6th-order fit
    coeffs = sorted({n for n in coeffs if 1 <= n <= max_order})
    extra = a.get('with_steps') or []
    if not isinstance(extra, list):
        raise RuntimeError('auto_background "with_steps" must be a list of GSAS steps')
    isolate = a.get('isolate', True) is not False
    saved = _isolate_flags(gpx) if isolate else []
    scan = []
    best = None
    prev_wR = None
    flat = 0
    stopped_at = None
    try:
        for n in coeffs:
            entry = {'n_back': n}
            # P1 (0.8.5): a safe point between background-order trials, so a stop
            # request does not have to wait for the whole (potentially long) scan.
            _g = active_guard()
            if _g is not None:
                _g.checkpoint('auto_background n=%d' % n)
            try:
                steps = [{'set': {'Background': {'no. coeffs': n, 'refine': True}}}] + list(extra)
                gpx.do_refinements(steps)
                wR = _num(gpx.histograms()[0].get_wR())
            except Exception as e:
                entry['error'] = str(e)
                scan.append(entry)
                continue
            if wR is not None:
                entry['wR'] = wR
                if prev_wR is not None:
                    entry['gain'] = round(prev_wR - wR, 4)
            scan.append(entry)
            log('auto_background n=%d wR=%s' % (n, wR))
            if wR is not None and (best is None or wR < best[0]):
                best = (wR, n)
            if wR is not None and prev_wR is not None:
                flat = flat + 1 if (prev_wR - wR) < stop_tol else 0
                if flat >= patience:
                    stopped_at = n
                    log('auto_background STOP at n=%d: wR plateaued '
                        '(gain<%.3f for %d step(s))' % (n, stop_tol, flat))
                    break
            if wR is not None:
                prev_wR = wR
    finally:
        _restore_flags(saved)
    restored, final_wR = None, None
    if best and a.get('keep_best', True) is not False:
        try:
            gpx.do_refinements([{'set': {'Background': {'no. coeffs': best[1], 'refine': True}}}])
            restored = best[1]
            final_wR = _num(gpx.histograms()[0].get_wR())
        except Exception as e:
            rec.setdefault('warnings', []).append(
                'could not restore best background (n=%d): %s' % (best[1], e))
    rec['scan'] = scan
    rec['scanned'] = [e['n_back'] for e in scan]
    rec['isolated'] = bool(isolate)
    rec['stop_tol'] = stop_tol
    rec['max_order'] = max_order
    if stopped_at is not None:
        rec['stopped_at'] = stopped_at
        rec['stop_reason'] = ('wR plateaued: gain < %.3f%% for %d consecutive step(s)'
                              % (stop_tol, patience))
    if best:
        rec['best_n_back'] = best[1]
        rec['best_wR'] = best[0]
        if final_wR is not None:
            rec['wR_final'] = final_wR
        if restored is not None:
            rec['restored_n_back'] = restored
        rec['detail'] = ('scanned %d background order(s)%s; best n=%d (background-only '
                         'wR=%.3f%%)%s' % (
                             len(scan), ' [isolated]' if isolate else '', best[1], best[0],
                             '' if final_wR is None else
                             '; re-converged under the caller settings (wR=%.3f%%)' % final_wR))
    else:
        rec['detail'] = 'background scan produced no usable wR'
    return True


def act_save(gpx, a, rec, out_path):
    target = a.get('path') or out_path
    gpx.save(target)
    rec['detail'] = 'saved %s' % target
    rec['saved'] = target
    return True


def main():
    if len(sys.argv) < 2:
        raise SystemExit('plan JSON required')
    planf = sys.argv[1]
    outres = None
    for i, a in enumerate(sys.argv[2:]):
        if a == '--out':
            outres = sys.argv[3 + i]
    plan = json.load(open(planf, encoding='utf-8'))
    guard = start_guard(plan, backend='gsas_ops', out_path=outres)   # P1 (0.8.4)
    outres = outres or os.path.splitext(planf)[0] + '_result.json'
    out_dir = plan.get('out_dir') or os.path.dirname(os.path.abspath(outres))
    os.makedirs(out_dir, exist_ok=True)
    gsas_root = plan.get('gsas_root') or os.environ.get('DSH_GSAS_ROOT') or ''
    if not gsas_root:
        raise RuntimeError(
            'GSAS-II root not configured: pass "gsas_root" in the plan, set DSH_GSAS_ROOT, '
            'or add "gsas_root" to xrd.local.json')
    sys.path.insert(0, gsas_root)

    warnings = []
    actions_log = []
    save_path = plan.get('save_as') or os.path.join(out_dir, 'gsas_ops_out.gpx')
    result = {'ok': False}
    try:
        from GSASII import GSASIIscriptable as G2sc

        gpx_in = plan.get('gpx')
        if plan.get('new_gpx'):
            if os.path.exists(save_path):
                os.remove(save_path)
            gpx = G2sc.G2Project(newgpx=save_path)
            data = plan.get('data') or {}
            if not (data.get('file') and os.path.exists(data['file'])):
                raise RuntimeError('new_gpx needs data.file')
            hist_objs = gpx.add_powder_histogram(
                data['file'], plan.get('instprm') or '',
                fmthint=data.get('fmthint') or None)   # 0.8.8: no default hint (see gsas_engine.py)
            if not isinstance(hist_objs, list):
                hist_objs = [hist_objs]
            for key in ('cif', 'cif2'):
                p = plan.get(key)
                if p and os.path.exists(p):
                    gpx.add_phase(p, fmthint='CIF', histograms=hist_objs)
            for h in gpx.histograms():
                for ph in gpx.phases():
                    try:
                        if h.name not in (ph.data['Histograms'] or {}):
                            gpx.link_histogram_phase(h.name, ph)
                    except Exception as e:
                        warnings.append('link skipped %s<->%s: %s' % (h.name, ph.name, e))
        else:
            if not (gpx_in and os.path.isfile(gpx_in)):
                raise RuntimeError('gpx not found: %s (or pass new_gpx=true + data/cif)' % gpx_in)
            if os.path.getsize(gpx_in) == 0:
                raise RuntimeError('gpx is a 0-byte failed-run placeholder: %s' % gpx_in)
            gpx = G2sc.G2Project(gpxfile=gpx_in)
            if os.path.abspath(save_path) == os.path.abspath(gpx_in):
                save_path = os.path.join(out_dir, 'gsas_ops_continued.gpx')
            gpx.filename = save_path

        if not gpx.phases():
            raise RuntimeError('project has no phases (empty/placeholder .gpx)')
        if not gpx.histograms():
            raise RuntimeError('project has no histograms')
        # element-table findings surface as warnings: they explain a later bare
        # KeyError (the "'Fen_'" crash: a CIF whose type symbol is not an element)
        warnings.extend(ensure_element_tables(gpx))
        # the type-substitution check needs the CIF the phase came from (main
        # phase first, then an optional second phase)
        _cifs = [c for c in (plan.get('cif'), plan.get('cif2')) if c]
        for _i, _ph in enumerate(gpx.phases()):
            _cif = _cifs[_i] if _i < len(_cifs) else (_cifs[-1] if _cifs else None)
            warnings.extend(check_atom_types_vs_cif(_ph, _cif))

        try:
            gpx.data['Controls']['data']['max cyc'] = int(plan.get('max_cycles') or 15)
        except Exception:
            pass

        snapshot_before = {'histograms': hist_table(gpx), 'phases': phase_table(gpx)}

        # USER RULE (2026-09-18, hard-coded): the background is the LAST parameter
        # family to be fitted - it is only optimised after everything else has been
        # tried. A background action followed by any other parameter-changing
        # action invalidates the scan (and lets the background absorb structural
        # misfit), so that ordering is refused outright.
        acts = plan.get('actions') or []
        _tail_ok = ('save', 'snapshot')

        def _following(i):
            return [str(acts[j].get('type') or '?') for j in range(i + 1, len(acts))
                    if str(acts[j].get('type') or '') not in _tail_ok]

        for _i, _x in enumerate(acts):
            _t = str(_x.get('type') or '')
            if _t == 'auto_background':
                after = _following(_i)
                if after:
                    raise RuntimeError(
                        'background must be fitted LAST: auto_background has to come '
                        'after every other parameter action, but these follow it: %s. '
                        'Move it to the end (only save/snapshot may come after it).'
                        % ', '.join(after))
            elif _t == 'import_background' and _x.get('vary') is True:
                after = _following(_i)
                if after:
                    raise RuntimeError(
                        'import_background was called with vary=true, so it FITS the '
                        'background and must be the last parameter action, but these '
                        'follow it: %s. Import it with vary=false to keep the background '
                        'fixed and refine other parameters afterwards.'
                        % ', '.join(after))

        for a in acts:
            atype = str(a.get('type') or '')
            # 0.8.4: a stop request lands BETWEEN actions — one action is the unit
            # of work here, and finishing none of it is better than being killed
            # inside one (which can leave a half-written project)
            guard.checkpoint('action %s' % atype)
            rec = {'action': atype}
            try:
                if atype == 'snapshot':
                    rec['detail'] = 'parameter snapshot taken'
                elif atype == 'set_atoms':
                    act_set_atoms(gpx, a, rec)
                elif atype == 'open_uiso':
                    act_open_uiso(gpx, a, rec)
                elif atype == 'set_adp_mode':
                    act_set_adp_mode(gpx, a, rec)
                elif atype == 'set_hist':
                    act_set_hist(gpx, a, rec)
                elif atype == 'import_background':
                    act_import_background(gpx, a, rec)
                elif atype == 'auto_background':
                    act_auto_background(gpx, a, rec)
                elif atype == 'set_cell':
                    act_set_cell(gpx, a, rec)
                elif atype == 'scale':
                    act_scale(gpx, a, rec)
                elif atype == 'equiv_uiso':
                    act_equiv_uiso(gpx, a, rec)
                elif atype == 'equiv_afrac':
                    act_equiv_afrac(gpx, a, rec)
                elif atype == 'conserve_afrac':
                    act_conserve_afrac(gpx, a, rec)
                elif atype == 'check_conservation':
                    act_check_conservation(gpx, a, rec)
                elif atype == 'uiso_clamp':
                    act_uiso_clamp(gpx, a, rec)
                elif atype == 'fix_uiso':
                    act_fix_uiso(gpx, a, rec)
                elif atype == 'clear_constraints':
                    act_clear_constraints(gpx, a, rec)
                elif atype == 'split_site':
                    act_split_site(gpx, a, rec)
                elif atype == 'refine':
                    act_refine(gpx, a, rec)
                elif atype == 'replace_phase':
                    act_replace_phase(gpx, a, rec)
                elif atype == 'add_phase':
                    act_add_phase(gpx, a, rec)
                elif atype == 'new_from':
                    gpx, save_path = act_new_from(gpx, a, rec, save_path)
                elif atype == 'set_site_composition':
                    act_set_site_composition(gpx, a, rec)
                elif atype == 'save':
                    act_save(gpx, a, rec, save_path)
                else:
                    rec['detail'] = 'unknown action ignored'
                    warnings.append('unknown action type: %s' % atype)
            except Exception as e:
                rec['error'] = str(e)
                warnings.append('action %s failed: %s' % (atype, e))
                log(traceback.format_exc()[-1200:])
            for w in (rec.pop('warnings', None) or []):
                warnings.append(w)
            actions_log.append(rec)

        save_failed = False
        try:
            gpx.save(save_path)
        except Exception as e:
            save_failed = True
            warnings.append('final save failed: %s' % e)

        after = {'histograms': hist_table(gpx), 'phases': phase_table(gpx)}

        wR = None
        try:
            wR = _num(gpx.histograms()[0].get_wR())
        except Exception:
            pass
        converged = True
        if wR is not None and wR >= 99.0:
            converged = False
            warnings.append('wR = %.2f%% after the actions - the refinement looks '
                            'diverged; do not treat this project as converged' % wR)

        # Delivery status (audit 2026-09-21): a failed action, or a failed final
        # save, must NOT be reported as ok:true. The artifacts are still written and
        # the diagnostics are still returned (the user rule "return a result even on
        # error"), but `ok` answers "did everything I asked for actually happen",
        # and `partial` marks the in-between case explicitly so a caller cannot
        # mistake it for success.
        failed_actions = [r for r in actions_log if r.get('error')]
        result_ok = (not failed_actions) and (not save_failed)
        result = {
            'ok': result_ok,
            'partial': bool(failed_actions or save_failed) and bool(actions_log),
            'converged': converged,
            'gpx': save_path,
            'histograms': after['histograms'],
            'phases': after['phases'],
            'actions': actions_log,
            'warning': warnings,
            'summary': 'xrd_gsas_ops: %d action(s), %d failed; save %s; wR=%s; %d phase(s)' % (
                len(actions_log), len(failed_actions),
                'FAILED' if save_failed else 'ok',
                ('%.2f%%' % wR) if wR is not None else 'n/a',
                len(after['phases'])),
        }
        if failed_actions:
            result['summary'] += ' — FAILED: ' + ', '.join(
                str(r.get('type', '?')) for r in failed_actions)
        if plan.get('snapshot_before'):
            result['before'] = snapshot_before
        # reasonableness review of the output (user rule): U values / occupancies
        result['review'] = review_table(gpx, plan.get('review') or {})
        log('OPS_OK', save_path)
    except Exception as e:
        tb = traceback.format_exc()
        log('OPS_FAILED:', e)
        log(tb[-2500:])
        result = {'ok': False, 'error': str(e), 'log_tail': tb[-2000:]}
        if warnings:
            result['warning'] = warnings

    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out run must
    # never leave a result file the caller can mistake for this run's own.
    write_result(outres, result, plan)


if __name__ == '__main__':
    run_guarded_main(main)
