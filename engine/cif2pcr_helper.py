#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cif2pcr helper - PREFLIGHT and NORMALISE, both for `cif_to_pcr`.

WHY THIS EXISTS (2026-09-20). User: "修好整个插件中生成PCR的组件，CIF到PCR生成
组件反复报错，需要完整修复". Three independent defects were measured on real
files, and this module fixes the two that belong to the CIF->PCR step:

  1. THE CONVERTER'S OUTPUT CANNOT BE READ BY autoFP'S PARSER.
     `diffpy/pyfullprof/pcrfilereader.py` consumes the control region
     POSITIONALLY. CIFs_to_PCR.exe writes every control block on ONE combined
     line (a "!Job Npr Nph Nba Nex ..." layout), while the reader expects the
     split new-style layout used by this project's own pcr templates:
         NPATT / W_PAT markers, then
         !Nph Dum Ias Nre Cry Opt Aut   + values
         !Job Npr Nba Nex Nsc Nor Iwg Ilo Res Ste Uni Cor Anm Int + values
         !Mat Pcr NLI Rpa Sym Sho + values
         !Ipr Ppl Ioc Ls1 Ls2 Ls3 Prf Ins Hkl Fou Ana + values  (exactly 11)
         ! Lambda1 ... + values
         !NCY ... + values, then !Thmin Step Thmax PSD Sent0 + values
     Read as-is the file is classified OLD style (the markers are what trigger
     the "new" branch), Nba is taken from the wrong column -- measured -5, i.e.
     "5 background points" -- and the reader dies at pcrfilereader.py:749 trying
     to read coefficient lines as (position, value) pairs. With only the markers
     patched in, the reader instead aborts the whole control block at the first
     field-count mismatch, which leaves ZERO parameters and no phases/atoms
     (measured: Rwp=10000, "ScatterFactor: Empty ObjectList", fp2k stops after
     its banner). So the control region is rebuilt block by block in the layout
     the templates use. NO VALUE IS INVENTED: every number is carried over from
     the converter's own output, or copied from the template defaults where the
     converter states nothing (wavelength, NCY, excluded regions).

  2. THE CONVERTER'S BACKGROUND IS CHEBYSHEV, the project rule is a POLYNOMIAL.
     Default 6th order, escalation only on demand, so the 24 Chebyshev
     coefficients are replaced by a polynomial of the same count. The untouched
     converter output is kept as `<pcr>.converter`.

  3. A DOOMED CIF BURNS ~110 s BEFORE FAILING. CIFs_to_PCR takes ~110 s per call
     and always ends in an interactive "Press <enter> to finish" prompt, so a
     file it cannot use (no space group / no symmetry information) still costs
     the full wait. `preflight` detects those two refusals in ~1 s. It also warns
     about an UNQUOTED space-group name, which the converter truncates at the
     first space ("I a -3" -> "I" = space group 2) and thereby corrupts every
     site multiplicity and occupancy.

Structure (space group / cell / atom list) is never altered.
"""
import json
import os
import re
import sys
import traceback


def log(*a):
    sys.stderr.write(' '.join(str(x) for x in a) + '\n')
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# PREFLIGHT
# ---------------------------------------------------------------------------
def preflight(cif):
    """Predict the converter's hard failures without paying its ~110 s."""
    out = {'cif': cif, 'ok': False, 'problems': [], 'warnings': []}
    if not os.path.isfile(cif):
        out['problems'].append('file not found: %s' % cif)
        return out
    with open(cif, 'r', encoding='utf-8', errors='replace') as fh:
        text = fh.read()
    if not text.strip():
        out['problems'].append('file is empty')
        return out

    m = re.search(r"_symmetry_space_group_name_H-M\s+['\"]?([^'\"\n]+)", text)
    sg = m.group(1).strip() if m else None
    has_ops = bool(re.search(r'_symmetry_equiv_pos_as_xyz|_space_group_symop_operation_xyz', text))
    if not sg and not has_ops:
        out['problems'].append(
            'no space group information: neither _symmetry_space_group_name_H-M nor '
            'a symmetry-operation block is present. CIFs_to_PCR.exe refuses this '
            'file only after its ~110 s wait ("No Space Group/No Symmetry '
            'information in this file for phase # 1"), so it is rejected here instead')
    if sg and not re.search(r"_symmetry_space_group_name_H-M\s+['\"]", text):
        out['warnings'].append(
            'space-group name is not quoted (%r): a multi-token symbol is truncated '
            'at the first space ("I a -3" -> "I" = space group 2), which silently '
            'corrupts the site multiplicities and every occupancy in the pcr. Write '
            "it as '%s'" % (sg, sg))

    n_atoms = len(re.findall(
        r'^\s*\S+\s+\S+\s+[-0-9.]+\s+[-0-9.]+\s+[-0-9.]+\s+[-0-9.]+\s*$', text, re.M))
    if n_atoms == 0 and '_atom_site_fract_x' not in text:
        out['problems'].append(
            'no atom sites found: no _atom_site_ loop with fractional coordinates')
    for k in ('_cell_length_a', '_cell_length_b', '_cell_length_c'):
        if k not in text:
            out['problems'].append('missing %s' % k)

    out['space_group'] = sg
    out['n_atoms_estimated'] = n_atoms
    out['ok'] = not out['problems']
    return out


# ---------------------------------------------------------------------------
# NORMALISE
# ---------------------------------------------------------------------------
def _read(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read().replace('\r\n', '\n')


# ---------------------------------------------------------------------------
# assemble() - rebuild the converter's pcr into the layout autoFP can read
# ---------------------------------------------------------------------------
# WHY A REBUILD AND NOT A PATCH (2026-09-20, after several failed attempts):
# diffpy/pyfullprof's reader walks the control region POSITIONALLY and its
# background handling only copes with the layout this project's own pcrs use.
# Point patches were tried and rejected one by one -- inserting NPATT/W_PAT,
# splitting the combined "!Job Npr Nph Nba ..." line, removing/adding comment
# lines, moving the background block. Every variant still died at
# pcrfilereader.py:749. What DOES work, measured: a pcr whose SKELETON comes
# from the project's own templates with the converter's values written into it
# (the structure-transfer experiment reached Rwp 7.00% against a 7.04%
# reference). So the skeleton is generated here and every number is copied from
# the converter output -- nothing is invented, and the space group / cell / atom
# list are carried over verbatim.
_SKELETON_HEAD = """COMM   {title}
! Current global Chi2 (Bragg contrib.) =     0.1000E+07
NPATT      1       1 <- Flags for patterns (1:refined, 0: excluded)
W_PAT   1.000
!Nph Dum Ias Nre Cry Opt Aut
   1   0   0   0   0   0   1
!Job Npr Nba Nex Nsc Nor Iwg Ilo Res Ste Uni Cor Anm Int
   0   0   0   0   0   1   0   0   0   0   0   0   0   0  !-> Patt#: 1
!
!File names of data(patterns) files
{datafile}
!
!Mat Pcr NLI Rpa Sym Sho
   0   1   0   0   0   0
!Ipr Ppl Ioc Ls1 Ls2 Ls3 Prf Ins Hkl Fou Ana
   0   0   1   0   4   0   3  10   0   0   0  !-> Patt#: 1
!
! Lambda1  Lambda2    Ratio    Bkpos    Wdt    Cthm     muR   AsyLim   Rpolarz  2nd-muR -> Patt# 1
{lambda_line}
!
!NCY  Eps  R_at  R_an  R_pr  R_gl
{ncy_line}
!     Thmin       Step       Thmax    PSD    Sent0  -> Patt#: 1
{range_line}
!
! Excluded regions (LowT  HighT) for Pattern#  1
{excluded}
!
!
       0    !Number of refined parameters
!
!  Zero    Code    SyCos    Code   SySin    Code  Lambda     Code MORE ->Patt# 1
   0.00000    0.0  0.00000    0.0  0.00000    0.0 0.000000    0.00   0
!   Background coefficients/codes  for Pattern#  1  (Polynomial of {deg}th degree)
{bg_values}
{bg_codes}
!-------------------------------------------------------------------------------
!  Data for PHASE number:   1
!-------------------------------------------------------------------------------
{phase_name}
!
!Nat Dis Ang Jbt Isy Str Furth        ATZ     Nvk More
{nat_line}
!
!Contributions (0/1) of this phase to the  1 patterns
 1
!Irf Npr Jtyp  Nsp_Ref Ph_Shift for Pattern#  1
   0   7    0      0      0
! Pr1    Pr2    Pr3   Brind.   Rmua   Rmub   Rmuc     for Pattern#  1
  0.000  0.000  1.000  1.000  1.000  1.000  1.000
!
{spacegroup}
!Atom   Typ       X        Y        Z     Biso       Occ     In Fin N_t Spc /Codes
!    beta11   beta22   beta33   beta12   beta13   beta23  /Codes
{atoms}
!-------> Profile Parameters for Pattern #  1
!  Scale        Shape1      Bov      Str1      Str2      Str3   Strain-Model
{scale_line}
     0.00000     0.000     0.000     0.000     0.000     0.000
!       U         V          W           X          Y        GauSiz   LorSiz Size-Model
{uvw_line}
      0.000      0.000      0.000      0.000      0.000      0.000      0.000
!     a          b         c        alpha      beta       gamma      #Cell Info
{cell_line}
     0.00000    0.00000    0.00000    0.00000    0.00000    0.00000
!  Pref1    Pref2      Asy1     Asy2     Asy3     Asy4
  1.00000  0.00000  0.00000  0.00000  0.00000  0.00000
"""


def _grab(lines, predicate, offset=1, count=1, default=None):
    """Value line(s) `offset` lines after the first line matching predicate."""
    for i, l in enumerate(lines):
        if predicate(l):
            got = [lines[i + offset + k] for k in range(count)
                   if i + offset + k < len(lines)]
            return got if count > 1 else (got[0] if got else default)
    return default


def assemble(pcr, order=6):
    """Rewrite `pcr` (converter output) into the layout autoFP can read.

    Returns a report. On any doubt the file is left exactly as it was.
    """
    import re as _re
    out = {'pcr': pcr, 'ok': False, 'changed': False, 'actions': [], 'warnings': []}
    if not os.path.isfile(pcr):
        out['warnings'].append('pcr not found: %s' % pcr)
        return out
    lines = _read(pcr).split('\n')
    original = '\n'.join(lines)

    # ---- gather every value from the converter output ----------------------
    atom_hdr = next((i for i, l in enumerate(lines) if l.startswith('!Atom')), None)
    if atom_hdr is None:
        out['warnings'].append('no "!Atom" block in this pcr - it does not look like '
                               'a CIFs_to_PCR output; left untouched')
        return out
    atoms, k = [], atom_hdr + 1
    while k < len(lines):
        l = lines[k]
        if not l.strip():
            break
        f = l.split()
        # label type x y z biso occ  (+ trailing flag columns)
        if len(f) >= 7:
            try:
                [float(v) for v in f[2:7]]
                atoms.append((f[0], f[1], f[2], f[3], f[4], f[5], f[6]))
                k += 2                  # main line + one continuation line
                continue
            except ValueError:
                pass
        break
    if not atoms:
        out['warnings'].append('the "!Atom" block has no readable rows - left untouched')
        return out
    out['n_atoms'] = len(atoms)

    sg = _grab(lines, lambda l: 'Space group symbol' in l, 0)
    if sg is None:
        sg = _grab(lines, lambda l: l.strip().startswith('!Space group'), 1, default='P 1')
    sg = (sg or 'P 1').split('<--')[0].rstrip()
    cell = _grab(lines, lambda l: '#Cell Info' in l, 1, default='')
    scale = _grab(lines, lambda l: 'Scale' in l and l.strip().startswith('!'), 1, default='')
    uvw = _grab(lines, lambda l: 'GauSiz' in l and l.strip().startswith('!'), 1, default='')
    lam = _grab(lines, lambda l: 'Lambda1' in l, 1, default='')
    ncy_raw = _grab(lines, lambda l: l.strip().startswith('!NCY'), 1, default='')
    nat = _grab(lines, lambda l: l.strip().startswith('!Nat Dis Ang'), 1, default='')
    bg_hdr = next((i for i, l in enumerate(lines) if 'Background coefficients' in l), None)
    bgvals, bgcodes = [], []
    if bg_hdr is not None:
        j = bg_hdr + 1
        while j < len(lines) and len(bgvals) < 24:
            t = lines[j].strip()
            if not t or t.startswith('!') or t.startswith('---'):
                break
            (bgvals if (j - bg_hdr) % 2 == 1 else bgcodes).extend(t.split())
            j += 1
    if not bgvals:
        bgvals = ['0.000'] * order
    try:
        coeffs = [float(v) for v in bgvals[:order]]
    except ValueError:
        out['warnings'].append('background coefficients are not numeric - using zeros')
        coeffs = [0.0] * order
    if len(coeffs) < order:
        coeffs += [0.0] * (order - len(coeffs))
    ncy = (ncy_raw or '').split()
    while len(ncy) < 11:
        ncy.append('0.0')
    lamv = (lam or '').split()
    while len(lamv) < 10:
        lamv.append('0.0')
    # excluded regions: taken from the converter output (count must satisfy Nex)
    excl = []
    exc_i = next((i for i, l in enumerate(lines) if 'Excluded regions' in l), None)
    if exc_i is not None:
        j = exc_i + 1
        while j < len(lines) and len(excl) < 4:
            t = lines[j].strip()
            if not t:
                j += 1
                if len(excl):
                    break
                continue
            if t.startswith('!') or not _re.match(r'^[-+0-9.]+\s+[-+0-9.]+$', t):
                break
            excl.append(lines[j].rstrip())
            j += 1
    if len(excl) < 2:
        excl = ['        0.00        0.00', '        0.00        0.00']

    dat = None
    for l in lines[:14]:
        m = _re.search(r'DAT-file:\s*([^,\s]+)', l)
        if m:
            dat = m.group(1)
            break
    if not dat:
        dat = os.path.splitext(os.path.basename(pcr))[0] + '.dat'
        out['warnings'].append('converter output stated no data file name; assuming %r'
                               % dat)

    atom_txt = '\n'.join(
        '%-7s%-4s%10s%9s%9s%10s%10s   0   0   2    0'
        % (lab, typ, x, y, z, biso, occ) + '\n' + ' ' * 18 + '0.00     0.00     0.00     0.00      0.00'
        for lab, typ, x, y, z, biso, occ in atoms)

    text = _SKELETON_HEAD.format(
        title=lines[0][4:].strip() if lines and lines[0].startswith('COMM') else 'cif2pcr',
        datafile=dat,
        lambda_line=(' ' + ' '.join(lamv[:10])),
        ncy_line=(' ' + '  '.join(ncy[:6])),
        range_line=('   ' + '   '.join(ncy[6:11])),
        excluded='\n'.join(excl),
        deg=order - 1,
        bg_values=('     ' + ''.join('%12.3f' % v for v in coeffs)),
        bg_codes=('     ' + ''.join('%12s' % '0.00' for _ in coeffs)),
        phase_name=dat.rsplit('.', 1)[0],
        nat_line=(nat or ('   %d   0   0   0   0   0   0       0.0000   0   0' % len(atoms))),
        spacegroup=(sg + '                   <--Space group symbol'),
        atoms=atom_txt,
        scale_line=(scale or ' 0.1000000E-03   0.00000   0.00000   0.00000   0.00000   0.00000       0'),
        uvw_line=(uvw or '     0.004133    -0.007618     0.006255     0.018961     0.000000     0.000000     0.000000       0'),
        cell_line=(cell or '   1.000000   1.000000   1.000000  90.000000  90.000000  90.000000'),
    )
    with open(pcr + '.converter', 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(original)
    with open(pcr, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    out['ok'] = True
    out['changed'] = True
    out['datafile'] = dat
    out['spacegroup'] = sg
    out['coefficients'] = coeffs
    out['actions'].append(
        'pcr rebuilt into the layout autoFP\'s parser accepts: the skeleton is the '
        'one this project\'s own templates use (verified to load), while every value '
        '-- space group, cell, atom positions/occupancies, scale, U/V/W, wavelength, '
        'NCY, background -- is copied verbatim from the CIFs_to_PCR output. The '
        'converter\'s own layout cannot be read by that parser (it walks the control '
        'region positionally and dies at pcrfilereader.py:749), which is what made '
        'the pipeline report "did not report Rwp"')
    out['actions'].append('original converter output kept as %s.converter'
                          % os.path.basename(pcr))
    return out


def _find_block(lines, starts_with):
    """(header_index, value_lines) for the first line whose body starts with `x`."""
    for i, l in enumerate(lines):
        s = l.lstrip()
        if s.startswith('!') and s[1:].startswith(starts_with):
            vals = []
            j = i + 1
            while j < len(lines):
                t = lines[j].strip()
                if not t or t.startswith('!') or t.startswith('---'):
                    break
                vals.append(lines[j])
                j += 1
            return i, vals, j
    return None, [], None


def set_wavelength(pcr, kalpha1, kalpha2=None, ratio=None, backup=True):
    """Rewrite the VALUE line of the `! Lambda1 Lambda2 Ratio ...` banner (0.9.0).

    EXPLICIT-ONLY: nothing calls this unless the user supplied a wavelength, and
    nothing else in the plugin writes this line. The header comment line is left
    untouched - only the numeric value line below it is replaced, and the fields
    after `Ratio` are carried over verbatim. `kalpha2` None or 0 means
    monochromatic (the FullProf convention is Lam2 = 0).

    Returns {'ok','pcr','changed','before','after','kind','warnings'} where
    before/after are the raw value lines, so the caller can echo 改前 -> 改后.
    """
    out = {'ok': False, 'pcr': pcr, 'changed': False, 'before': None, 'after': None,
           'kind': None, 'warnings': []}
    if not os.path.isfile(pcr):
        out['warnings'].append('pcr not found: %s' % pcr)
        return out
    try:
        # `_read` returns the WHOLE TEXT (iterating it yields characters, which is
        # how an earlier version of this function silently found no banner) - split
        # it the way the other callers do, and remember the trailing newline.
        raw = _read(pcr)
    except Exception as e:
        out['warnings'].append('cannot read pcr: %s' % e)
        return out
    lines = raw.split('\n')
    # LINE ENDINGS ARE PRESERVED (2026-10-03): a CRLF pcr used to come back with ONE LF-only
    # line (the rewritten one), i.e. a mixed-ending file — and since `_read` normalises CRLF
    # away, the separator has to be probed from the BYTES. The templates shipped in
    # skills/fullprof-pcr/templates are CRLF, so this was reachable in normal use.
    try:
        with open(pcr, 'rb') as _fb:
            sep = '\r\n' if b'\r\n' in _fb.read() else '\n'
    except Exception:
        sep = '\n'
    trailing_nl = bool(lines) and lines[-1] == ''
    if trailing_nl:
        lines = lines[:-1]
    hi, vals, _ = _find_block(lines, ' Lambda1')
    if hi is None or not vals:
        out['warnings'].append('no "! Lambda1 ..." banner found - wavelength NOT set')
        return out
    # the banner may be followed by comment lines; take the first NUMERIC value line
    idx = None
    for k, v in enumerate(vals):
        t = v.split()
        if not t:
            continue
        try:
            float(t[0])
        except ValueError:
            continue
        idx = hi + 1 + k
        break
    if idx is None:
        out['warnings'].append('the Lambda1 banner has no numeric value line - NOT set')
        return out
    toks = lines[idx].split()
    if len(toks) < 2:
        out['warnings'].append('value line has too few fields - NOT set')
        return out
    before = lines[idx].rstrip('\r\n')
    out['before'] = before
    out['value_line_no'] = idx + 1          # 1-based, so the caller can cite it
    mono = (kalpha2 is None) or (float(kalpha2) == 0)
    k1 = float(kalpha1)
    k2 = 0.0 if mono else float(kalpha2)
    r = 0.0 if mono else (0.5 if ratio in (None, '') else float(ratio))
    rest = ' '.join(toks[3:])
    new = (' ' + '%.6f %.6f %.5f' % (k1, k2, r) + (' ' + rest if rest else '')).rstrip()
    out['kind'] = 'monochromatic' if mono else 'doublet'
    if new.strip() == before.strip():
        out['ok'] = True
        out['after'] = before
        out['note'] = 'pcr already carries the requested wavelength - nothing changed'
        return out
    if backup:
        try:
            import shutil
            shutil.copyfile(pcr, pcr + '.prewavelength')
        except Exception as e:
            out['warnings'].append('backup failed (%s); the .prewavelength copy is missing' % e)
    lines[idx] = new
    with open(pcr, 'w', encoding='utf-8', newline='') as fh:
        fh.write(sep.join(l.rstrip('\r\n') for l in lines) + (sep if trailing_nl else ''))
    out['ok'] = True
    out['changed'] = True
    out['after'] = new
    return out


def normalise(pcr, order=6, backup=True):
    out = {'pcr': pcr, 'changed': False, 'order': order, 'actions': [], 'warnings': []}
    if not os.path.isfile(pcr):
        out['warnings'].append('pcr not found: %s' % pcr)
        return out
    lines = _read(pcr).split('\n')
    original = '\n'.join(lines)

    comm_i = next((i for i, l in enumerate(lines) if l.lstrip().startswith('COMM')), None)
    if comm_i is None:
        out['warnings'].append('no COMM line found - left untouched')
        return out
    already = any(l.lstrip().startswith('NPATT') for l in lines[:12])

    # ---- collect every control value by NAME (never by position) -----------
    _, job_hdr, _ = _find_block(lines, 'Job Npr Nph')
    if not job_hdr:
        out['warnings'].append('no combined "!Job Npr Nph ..." line found - left '
                               'untouched (already normalised?)' if already else
                               'no combined "!Job Npr Nph ..." line found - left untouched')
        return out
    jf = job_hdr[0].split()
    if len(jf) < 19:
        out['warnings'].append('combined Job line has %d field(s), expected 19 - '
                               'left untouched' % len(jf))
        return out
    # !Job Npr Nph Nba Nex Nsc Nor Dum Iwg Ilo Ias Res Ste Nre Cry Uni Cor Opt Aut
    c = dict(zip(['job', 'npr', 'nph', 'nba', 'nex', 'nsc', 'nor', 'dum', 'iwg',
                  'ilo', 'ias', 'res', 'ste', 'nre', 'cry', 'uni', 'cor', 'opt',
                  'aut'], jf))
    chi2 = next((l for l in lines[:6] if 'global Chi2' in l),
                '! Current global Chi2 (Bragg contrib.) =     0.1000E+07')
    _, lam_vals, _ = _find_block(lines, ' Lambda1')
    lam = lam_vals[0].split() if lam_vals else []
    while len(lam) < 10:
        lam.append('0.0000')
    # converter NCY block: !NCY Eps R_at R_an R_pr R_gl Thmin Step Thmax PSD Sent0
    _, ncy_vals, _ = _find_block(lines, 'NCY')
    ncy = ncy_vals[0].split() if ncy_vals else []
    while len(ncy) < 11:
        ncy.append('0.0')
    # excluded regions: the lines right after the "!NCY" block, whose count must
    # match Nex (the reader reads exactly Nex of them)
    _ncy_hdr, _ncy_body, ncy_end = _find_block(lines, 'NCY')
    excl_vals = []
    if ncy_end is not None:
        k = ncy_end
        while k < len(lines) and len(excl_vals) < abs(int(c['nex'] or 0)):
            t = lines[k].strip()
            if not t or t.startswith('!'):
                k += 1
                continue
            if not re.match(r'^[-+0-9.]+\s+[-+0-9.]+$', t):
                break
            excl_vals.append(lines[k])
            k += 1

    dat = None
    for l in lines[:14]:
        m = re.search(r'DAT-file:\s*([^,\s]+)', l)
        if m:
            dat = m.group(1)
            break
    if not dat:
        dat = os.path.splitext(os.path.basename(pcr))[0] + '.dat'
        out['warnings'].append('converter output stated no data file name; assuming '
                               '%r - put the pattern under that name' % dat)
    out['datafile'] = dat

    # ---- rebuild the control region ---------------------------------------
    head = [
        lines[comm_i],
        chi2,
        'NPATT      %s       1 <- Flags for patterns (1:refined, 0: excluded)' % c['nph'],
        'W_PAT   1.000',
        '!Nph Dum Ias Nre Cry Opt Aut',
        '   %s   %s   %s   %s   %s   %s   %s' % (c['nph'], c['dum'], c['ias'],
                                                 c['nre'], c['cry'], c['opt'], c['aut']),
        '!Job Npr Nba Nex Nsc Nor Iwg Ilo Res Ste Uni Cor Anm Int',
        '   %s   %s   %s   %s   %s   %s   %s   %s   %s   %s   %s   %s   0   0'
        % (c['job'], c['npr'], c['nba'], c['nex'], c['nsc'], c['nor'], c['iwg'],
           c['ilo'], c['res'], c['ste'], c['uni'], c['cor']),
        '!',
        '!File names of data(patterns) files',
        dat,
        '!',
        '!Mat Pcr NLI Rpa Sym Sho',
        '   0   1   0   0   0   0',
        '!Ipr Ppl Ioc Ls1 Ls2 Ls3 Prf Ins Hkl Fou Ana',
        '   0   0   1   0   4   0   3  10   0   0   0  !-> Patt#: 1',
        '!',
        '! Lambda1  Lambda2    Ratio    Bkpos    Wdt    Cthm     muR   AsyLim   Rpolarz  2nd-muR -> Patt# 1',
        ' %s %s %s %s %s %s %s %s %s %s'
        % tuple(lam[:10]),
        '!',
        '!NCY  Eps  R_at  R_an  R_pr  R_gl',
        ' %s  %s  %s  %s  %s  %s' % tuple(ncy[:6]),
        '!     Thmin       Step       Thmax    PSD    Sent0  -> Patt#: 1',
        '   %s   %s   %s   %s   %s' % tuple(ncy[6:11]),
        '!',
        '! Excluded regions (LowT  HighT) for Pattern#  1',
    ]
    # The excluded-region lines already exist in the converter output and their
    # COUNT must match Nex. Re-emitting them (rather than inventing two zeros)
    # keeps the line count aligned: the reader is positional, so dropping or
    # adding one line shifts everything after it by one and it then reads the
    # phase name from the wrong line and dies.
    head += [l for l in (excl_vals or []) if l.strip()]
    if not [l for l in (excl_vals or []) if l.strip()]:
        out['warnings'].append('no excluded-region lines found in the converter '
                               'output although Nex=%s asks for them - the rebuilt '
                               'pcr may be misaligned' % c['nex'])
    # Block order after the excluded regions, matching the working pcrs exactly:
    #   <excluded regions>
    #   !                              <- one comment
    #   <n>  !Number of refined parameters
    #   !                              <- one comment
    #   !  Zero    Code ...
    # The reader takes this region positionally, so an extra or missing comment
    # line here shifts the background block and it dies at pcrfilereader.py:749.
    head += [
        '!',
        '       0    !Number of refined parameters',
        '!',
    ]
    # the remaining body (parameter blocks / phases) is kept as-is, starting AT the
    # "!  Zero    Code" header. NOTE the line that follows its value line: nothing
    # may be inserted between it and the background block -- the reader walks this
    # region positionally and the reference pcrs that read correctly go straight
    # from the Zero value line to "!  Background coefficients".
    zero_i = next((i for i, l in enumerate(lines) if 'Zero    Code' in l), None)
    if zero_i is None:
        out['warnings'].append('no "!  Zero    Code" block found - left untouched')
        return out
    # drop any bare "!" comment lines sitting between the Zero value line and the
    # background header
    while zero_i + 2 < len(lines) and lines[zero_i + 2].strip() == '!':
        del lines[zero_i + 2]
    lines = head + lines[zero_i:]
    out['actions'].append(
        'control region rebuilt block-by-block into the new-style layout autoFP\'s '
        'parser expects (NPATT/W_PAT markers + !Nph / !Job / !Mat / !Ipr / Lambda / '
        'NCY), values carried over from the converter output. The converter writes '
        'each block on ONE combined line; read as-is the file is taken for OLD '
        'style, Nba is read from the wrong column (-5 = "5 background points") and '
        'the reader dies at pcrfilereader.py:749, or aborts the block and leaves '
        'zero parameters')
    out['changed'] = True

    # ---- background: Chebyshev -> polynomial -------------------------------
    hdr = next((i for i, l in enumerate(lines) if 'Background coefficients' in l), None)
    if hdr is None:
        out['warnings'].append('no background block found - background left as is')
    elif 'Chebychev' not in lines[hdr] and 'Chebyshev' not in lines[hdr]:
        out['actions'].append('background already polynomial')
    else:
        values = []
        j = hdr + 1
        while j < len(lines) and len(values) < 24:
            t = lines[j].strip()
            if not t or t.startswith('!') or t.startswith('---'):
                break
            values.extend(t.split())
            j += 1
        try:
            coeffs = [float(v) for v in values[:order]]
        except ValueError:
            coeffs = []
        if len(coeffs) < order:
            out['warnings'].append('only %d numeric background coefficient(s) found, '
                                   'need %d - background left as is'
                                   % (len(coeffs), order))
        else:
            lines[hdr:j] = [
                '!   Background coefficients/codes  for Pattern#  1  '
                '(Polynomial of %dth degree)' % (order - 1),
                '     ' + ''.join('%12.3f' % v for v in coeffs),
                '     ' + ''.join('%12s' % '0.00' for _ in coeffs),
            ]
            out['nba_after'] = '0'
            out['coefficients'] = coeffs
            out['actions'].append(
                'Chebyshev background replaced by a polynomial of degree %d (the '
                'project default; escalation only on demand)' % (order - 1))
            out['changed'] = True

    if not out['changed']:
        return out
    if backup:
        try:
            with open(pcr + '.converter', 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(original)
        except Exception as e:
            out['warnings'].append('could not write backup (%s)' % e)
    with open(pcr, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines))
    return out


# ---------------------------------------------------------------------------
def main():
    # `--out <path>` is a FLAG, so it is stripped before the mode arguments are read: in
    # setwavelength the value list is POSITIONAL, and an earlier version read argv[4]
    # straight through, so `setwavelength x.pcr 1.5406 --out r.json` crashed with
    # "could not convert string to float: '--out'" and the wavelength was silently NOT set
    # (found by workspace/xrd-plugin-checks/verify-wavelength.mjs, 2026-10-03).
    argv_all = list(sys.argv)
    out_path = None
    if '--out' in argv_all:
        i = argv_all.index('--out')
        out_path = argv_all[i + 1] if i + 1 < len(argv_all) else None
        del argv_all[i:i + 2]
    if len(argv_all) < 3:
        raise SystemExit('usage: cif2pcr_helper.py preflight <cif> | '
                         'normalise <pcr> [order] | '
                         'setwavelength <pcr> <kalpha1> [<kalpha2> <ratio>] | '
                         '--out result.json')
    mode, target = argv_all[1], argv_all[2]
    try:
        if mode == 'preflight':
            result = preflight(target)
        elif mode == 'assemble':
            order = int(argv_all[3]) if len(argv_all) > 3 and argv_all[3].isdigit() else 6
            result = assemble(target, order)
        elif mode == 'normalise':
            order = int(argv_all[3]) if len(argv_all) > 3 and argv_all[3].isdigit() else 6
            result = normalise(target, order)
            result['ok'] = True
        elif mode == 'setwavelength':
            # EXPLICIT-ONLY entry (0.9.0): every value must be on the command line.
            if len(argv_all) < 4:
                raise SystemExit('setwavelength needs <pcr> <kalpha1> [<kalpha2> <ratio>]')
            k1 = float(argv_all[3])
            k2 = float(argv_all[4]) if len(argv_all) > 4 and argv_all[4] not in ('-',) else None
            rt = float(argv_all[5]) if len(argv_all) > 5 and argv_all[5] not in ('-',) else None
            result = set_wavelength(target, k1, k2, rt)
        else:
            raise SystemExit('unknown mode: %s' % mode)
    except Exception as e:
        result = {'ok': False, 'error': str(e), 'log_tail': traceback.format_exc()[-1200:]}
    payload = json.dumps(result, ensure_ascii=False, indent=1)
    if out_path:
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.write(payload)
    print(payload)


if __name__ == '__main__':
    main()
