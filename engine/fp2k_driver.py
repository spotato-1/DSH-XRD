#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fp2k driver engine - refine a FullProf .pcr DIRECTLY, without AutoFP.

WHY (2026-09-20, user's proposal, verified): "这个CIF转换器是使用官方FULLPROF软件中
的解析器转换的，该PCR文件似乎FP2K可以直接读取。如果这个方案正确是否可以绕过AUTOFP
的GUI界面，直接使用PYTHON写一个启动器调用完整逻辑。" -- Confirmed by measurement:

  * `fp2k.exe <stem>.pcr` runs a .pcr written by the official CIFs_to_PCR.exe in
    **0.3 s**, exit code 0, and prints
    "Conventional Rietveld Rp,Rwp,Re and Chi2: ..." into <stem>.out.
  * AutoFP's own driver (shautofp.py) crashes on that same pcr inside
    diffpy/pyfullprof's reader (pcrfilereader.py:749) and then reports
    "did not report Rwp"; the GUI suite ships no other command-line driver
    (fpsuite contains only CIFs_to_PCR.exe, fp2k.exe, WinPLOTR).
  * Driving fp2k directly and switching refinement codes reproduces the
    reference behaviour step by step (same Y2O3 pattern, autoFP reference 7.04%):
        no codes                       Rwp 81.1
        + zero point                   Rwp 72.7
        + background (6)               Rwp 53.3
        + scale                        Rwp 51.6
        + U V W X Y                    Rwp 21.4
    i.e. every switch really takes effect and Rwp falls monotonically.

So this engine replaces AutoFP as the driver for pcrs that AutoFP cannot read,
while keeping FullProf itself as the refinement engine (no physics is
reimplemented here). It is the counterpart of gsas_engine.py: plan JSON in,
result JSON out, artifacts kept.

REFINE-CODE CONVENTION (FullProf): each parameter block has a value line and a
code line beneath it. A code of 0 fixes the parameter, 1 refines it
independently; the full Cx = sign*(10*p + |a|) form is supported by fp2k but not
needed here. Codes are written into the SHORT panel:
    !  Zero   Code  SyCos  Code  SySin  Code  Lambda  Code MORE
    !  Scale  Shape1 Bov Str1 Str2 Str3  Strain-Model
    !  U V W X Y GauSiz LorSiz Size-Model
    !  a b c alpha beta gamma  #Cell Info
    !  Pref1 Pref2 Asy1 Asy2 Asy3 Asy4  S_L D_L
The long background panel (up to 24 coefficients, 8 alternating lines) is
addressed separately.

LADDER ORDER MATTERS (measured): opening the CELL too early destroyed the fit
(Rwp 21.4 -> 100), so the default ladder goes background/zero -> scale ->
profile -> cell -> atoms, and every step is rolled back if Rwp worsens.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import traceback

from result_io import write_result
from proc_guard import start_guard, run_guarded_main, active_guard

# Orphan protection (2026-09-25). Kept optional on purpose: if the module is
# missing or the platform API is unavailable, the driver must still refine —
# it just falls back to plain subprocess semantics.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from fp2k_guard import run_guarded, sweep_orphans, platform_guard
except Exception:                                          # pragma: no cover
    run_guarded = None
    sweep_orphans = None

    def platform_guard():
        return 'none'


# C (2026-09-25): how long fp2k may produce NO output growth before it is judged
# stalled and killed. Measured hangs show ~100% CPU with <stem>.out stuck at 2 B
# and <stem>.prf at 0 B, so a healthy run (seconds on these data) never comes
# close. Overridable per call through plan['stall_timeout'].
STALL_TIMEOUT_S = 60.0


def log(*a):
    sys.stderr.write(' '.join(str(x) for x in a) + '\n')
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# pcr access helpers
# ---------------------------------------------------------------------------
def _read_lines(pcr):
    with open(pcr, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read().replace('\r\n', '\n').split('\n')


def _write_lines(pcr, lines):
    with open(pcr, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines))


def _num_fields(s):
    """'1.0' -> False, '0.00' -> True (is this token numeric?)."""
    try:
        float(s)
        return True
    except ValueError:
        return False


def find_header(lines, needle, within=0):
    for i, l in enumerate(lines):
        if needle in l and (l.strip().startswith('!') or within == 0):
            return i
    return None


def set_codes(lines, hdr_i, offset, idxs, value='1.0'):
    """Set refine codes at `idxs` of the code line `offset` lines below the header."""
    ci = hdr_i + offset
    if ci >= len(lines):
        return False, 'code line beyond end of file'
    toks = lines[ci].split()
    if not toks:
        return False, 'empty code line'
    changed = []
    for k in idxs:
        if k < len(toks):
            toks[k] = value
            changed.append(k)
    lines[ci] = '  ' + '  '.join(toks)
    return (bool(changed), 'set code fields %s -> %s' % (changed, value) if changed
            else 'no such field index %s (line has %d)' % (idxs, len(toks)))


def set_values(lines, hdr_i, offset, idxs, value):
    ci = hdr_i + offset
    toks = lines[ci].split()
    for k in idxs:
        if k < len(toks):
            toks[k] = value
    lines[ci] = '  ' + '  '.join(toks)
    return True, 'set value fields %s -> %s' % (idxs, value)


def sanitise_phase_name(lines):
    """Fix the phase-name line the converter writes from the CIF FILENAME.

    CIFs_to_PCR.exe puts the input file's name on the phase-name line, e.g.
    `Y2O3.cif`. fp2k reads that line with a numeric field scanner and aborts the
    whole run on the dot:
        Error reading the PCR file:
        FindFMT: invalid character in a numeric field !
        'Y2O3.cif'   ^----    at line: 33
        Program interrupted
    (measured 2026-09-20; the .out stops at 515 bytes and no R-factor line is
    ever produced). The phase name must be a plain token, so the extension and
    any character that is not a letter, digit, underscore or dash is stripped.
    """
    i = find_header(lines, '!Nat Dis Ang')
    if i is None:
        return False, 'no phase block (no "!Nat Dis Ang" line)'
    j = i - 1
    while j >= 0 and (not lines[j].strip() or lines[j].strip().startswith('!')):
        j -= 1
    if j < 0:
        return False, 'could not locate the phase-name line'
    raw = lines[j].strip()
    clean = re.sub(r'\.[A-Za-z0-9]+$', '', raw)          # drop a file extension
    clean = re.sub(r'[^A-Za-z0-9_\-]', '_', clean)[:20]  # keep it a plain token
    if not clean:
        clean = 'phase1'
    if clean == raw:
        return False, 'phase name already clean (%r)' % raw
    lines[j] = clean
    return True, 'phase name %r -> %r (fp2k aborts on a dot in that field)' % (raw, clean)


def set_ncy(lines, n):
    i = find_header(lines, '!NCY')
    if i is None:
        return False, 'no !NCY line'
    toks = lines[i + 1].split()
    if not toks:
        return False, 'empty NCY value line'
    toks[0] = str(int(n))
    lines[i + 1] = ' ' + '  '.join(toks)
    return True, 'NCY=%d' % int(n)


def polynomialise_background(lines, order=6):
    """Rewrite the converter's Chebyshev background panel into a POLYNOMIAL one.

    CIFs_to_PCR.exe writes `(Chebychev polynomials, up to 24 coefficients)` and
    lays the 24 numbers out as EIGHT alternating lines of six. A polynomial panel
    is TWO lines (six values, then six codes). fp2k accepts both, but only the
    polynomial form exposes the coefficients to the refinement codes this engine
    drives -- with the Chebyshev layout every `complex background` level past the
    first reports "no such field index" (measured: levels 4+ on a real pcr).

    Also corrects the Job line's Nba field: the converter states -5 there (which
    in FullProf means "5 user-defined background POINTS", not coefficients), while
    a polynomial background is Nba=0 with the coefficient count implied by the
    panel. Both corrections are background-only; nothing else is touched.
    """
    hdr = next((i for i, l in enumerate(lines) if 'Background coefficients' in l), None)
    if hdr is None:
        return False, 'no background panel'
    if 'Chebychev' not in lines[hdr] and 'Chebyshev' not in lines[hdr]:
        return False, 'background is already polynomial'
    values, codes, k = [], [], hdr + 1
    # Consume the WHOLE panel, whatever its length: stopping as soon as 24 values
    # have been read leaves the remaining coefficient lines in place, and those
    # stray rows push every later line up so fp2k then reads the phase-name line
    # as a numeric field and aborts ("FindFMT: invalid character in a numeric
    # field", measured 2026-09-20).
    while k < len(lines):
        t = lines[k].strip()
        if not t or t.startswith('!') or t.startswith('---'):
            break
        (values if (k - hdr) % 2 == 1 else codes).extend(t.split())
        k += 1
    try:
        coeffs = [float(v) for v in values[:order]]
    except ValueError:
        return False, 'background values are not numeric'
    if len(coeffs) < order:
        coeffs += [0.0] * (order - len(coeffs))
    lines[hdr:k] = [
        '!   Background coefficients/codes  for Pattern#  1  (Polynomial of %dth degree)'
        % (order - 1),
        '     ' + ''.join('%12.3f' % v for v in coeffs),
        '     ' + ''.join('%12s' % '0.00' for _ in coeffs),
    ]
    # Job line. The two pcr generations label this line differently (the
    # converter writes "!Job Npr Nph Nba Nex ..." with a 19-value row, the older
    # FullProf layout writes "!Job Npr Nba Nex ..." with 14), so a column-by-name
    # mapping is WRONG and silently edits the neighbouring field -- measured: a
    # name-based edit moved Nex from 2 to 5, fp2k then found too few excluded
    # regions and stopped right after its banner (no R-factor line at all).
    # The two fields are therefore located by POSITION WITHIN THE NUMERIC RUN:
    # the converter's row starts "0 7 1 -5 2 ..." = Job, Npr, Nph, Nba, Nex, so
    # within a numeric-only row the profile function is element 1 and Nba is
    # element 3. Both are skipped when the row carries text (a phase name).
    jh = next((i for i, l in enumerate(lines[:hdr])
               if l.lstrip().startswith('!Job')), None)
    if jh is not None:
        raw = lines[jh + 1]
        toks = raw.split()
        if toks and all(_num_fields(t) for t in toks):
            if len(toks) > 3:
                if toks[1] == '7':
                    toks[1] = '5'          # T-C-H -> the profile the templates use
                toks[3] = '0'              # Nba: polynomial background
                lines[jh + 1] = ' ' + '   '.join(toks)
            else:
                return False, ('background rewritten, but the !Job row has only %d '
                               'numeric field(s) so Npr/Nba were left as they were'
                               % len(toks))
    return True, ('Chebyshev background rewritten as a polynomial of degree %d '
                  '(Job row: Npr 7->5, Nba -> 0)' % (order - 1))


def set_step_range(lines, step=None):
    """The Thmin/Step/Thmax panel can either follow the NCY value line or sit on
    it (the converter writes both on one line). Handle both."""
    i = find_header(lines, '!NCY')
    if i is None:
        return False, 'no !NCY line'
    on_same = len(lines[i + 1].split()) > 6
    if step is None:
        return True, 'range left as is'
    if on_same:
        toks = lines[i + 1].split()
        toks[7] = str(step)
        lines[i + 1] = ' ' + '  '.join(toks)
        return True, 'step=%s (same-line layout)' % step
    for j in range(i + 1, min(i + 4, len(lines))):
        toks = lines[j].split()
        if len(toks) >= 3 and all(_num_fields(t) for t in toks):
            toks[1] = str(step)
            lines[j] = '   ' + '   '.join(toks)
            return True, 'step=%s' % step
    return False, 'could not locate the Thmin/Step/Thmax panel'


def pcr_refine_fingerprint(lines):
    """Compact fingerprint of every refinement DECISION in the pcr (read-only).

    The audit (2026-09-21) pointed out that `codes_changed` only compared the ATOM
    code block, so a step that legitimately rewrote a control value — scale, zero,
    cell, background, profile — reported `codes_changed: false`, which reads like
    "this step did nothing". This fingerprint covers the whole decision surface, so
    "the switch did not reach the file" can be told apart from "it did, and it did not
    help" for EVERY step rather than only for the atomic ones.
    """
    picks = []
    keys = ('Zero    Code', 'Background coefficients', 'Scale', 'GauSiz',
            '#Cell Info', '!Atom', 'Pref', 'Extinc')
    for key in keys:
        for i, l in enumerate(lines):
            if key in l and l.lstrip().startswith('!'):
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ''
                nxt2 = lines[i + 2].strip() if i + 2 < len(lines) else ''
                picks.append('%s|%s|%s' % (key, nxt[:60], nxt2[:60]))
                break
    return ' ;; '.join(picks)


def atom_code_fingerprint(lines):
    """The refinement codes actually present in the atom block, as a compact string.

    Logged before/after every ladder attempt so a "did not improve Rwp" entry can be
    told apart from "the switch never reached the pcr" — the two look identical in the
    Rwp column, and telling them apart is what an audit needs. Read-only.
    """
    i = find_header(lines, '!Atom')
    if i is None:
        return None
    out = []
    j = i + 1
    n = 0
    while j < len(lines):
        f = lines[j].split()
        if len(f) >= 11 and _num_fields(f[2]) and _num_fields(f[6]):
            n += 1
            k = j + 1
            while k < len(lines) and lines[k].strip() and len(lines[k].split()) < 11:
                cf = lines[k].split()
                if all(_num_fields(t) for t in cf):
                    out.append(','.join(cf))
                    break
                k += 1
            j = k
            continue
        if not lines[j].strip() or lines[j].lstrip().startswith('!'):
            j += 1
            continue
        break
    return 'atom%d codes[%s]' % (n, ' | '.join(out)) if out else 'atom%d codes[none]' % n


def set_atom_code(lines, mode):
    """Toggle ONE atomic refinement code, in the column that actually carries it.

    The atom block is not a single line. Authoritative layout (read off AutoFP's own
    pcr reader, `diffpy/pyfullprof/pcrfilereader.py` lines 1524-1548 — the
    implementation that works):

        !Atom   Typ  X Y Z Biso Occ In Fin N_t Spc /Codes   <- 11 values, Spc LAST
        !     beta11 ... beta23 /Codes
        <atom> <typ> x y z biso occ in fin n_t spc          <- 11 fields (main line)
                     <X> <Y> <Z> <Biso> <Occ>               <- the five REFINEMENT CODES
            <b11> <b22> <b33> <b12> <b13> <b23>             <- anisotropic values
                <codes for the six beta values>

    The old implementation wrote the LAST field of the atom main line — that field is
    `Spc`, the space-group code, so no atomic parameter was ever released: the trial
    pcr carried identical refinement codes, Rwp could not move, the step was rolled
    back and the ladder reported an innocent-looking "did not improve" entry. Measured
    2026-09-21 on the refined pcr: every atom code still 0, i.e. the atom / Biso /
    coordinate steps had never run.

    `mode` picks which code(s) are set: 'xyz' sets the first three, 'biso' / 'occ' one
    each, 'off' clears them.
    """
    i = find_header(lines, '!Atom')
    if i is None:
        return False, 'no !Atom header'

    idxs = {'xyz': (0, 1, 2), 'biso': (3,), 'occ': (4,)}.get(mode, (3,))

    def _is_main_line(l):
        f = l.split()
        return len(f) >= 11 and _num_fields(f[2]) and _num_fields(f[6])

    def _is_code_line(l):
        f = l.split()
        return 5 <= len(f) <= 7 and all(_num_fields(t) for t in f)

    j = i + 1
    n_atoms = 0
    while j < len(lines):
        if not _is_main_line(lines[j]):
            # the beta header line ("!  beta11 ... /Codes") sits between the !Atom
            # header and the first atom — skipping '!' lines keeps it out of the way,
            # while other text still ends the block
            if not lines[j].strip() or lines[j].lstrip().startswith('!'):
                j += 1
                continue
            break
        n_t = None
        try:
            n_t = int(lines[j].split()[9])
        except (IndexError, ValueError):
            pass
        # N_t: 0 isotropic, 2 anisotropic (extra beta values + beta codes), 4 formfactor,
        # negative values a different block layout again. The five-code line below is
        # identical for N_t 0 and 2 (reader: the biso_code is taken from the same
        # `words[3]`), so 0 and 2 are both fine; 4/negative are refused rather than
        # writing a code into a layout we have not verified.
        if n_t is not None and (n_t == 4 or n_t < 0):
            return False, ('atom block uses the N_t=%s layout, which this switch has '
                           'not been verified against - refusing rather than writing a '
                           'code into the wrong column' % n_t)
        j += 1
        # 25-2: the five codes, in the order X Y Z Biso Occ
        ci = None
        for k in range(j, min(j + 3, len(lines))):
            if _is_code_line(lines[k]):
                ci = k
                break
        if ci is None:
            return False, 'no atom refinement-code line after atom %d' % (n_atoms + 1)
        f = lines[ci].split()
        if len(f) < 5:
            return False, ('atom code line carries %d field(s); the five codes '
                           'X Y Z Biso Occ were expected' % len(f))
        for t in idxs:
            f[t] = '1' if mode != 'off' else '0.00'
        lines[ci] = '   ' + '   '.join(f)
        n_atoms += 1

        # beta (anisotropic) codes live on the SECOND six-value line after 25-2:
        # the first is the beta values, the second their codes.
        if mode == 'beta':
            bk = ci + 1
            seen = []
            while bk < len(lines) and not _is_main_line(lines[bk]):
                if lines[bk].strip():
                    seen.append(bk)
                bk += 1
            if len(seen) < 2:
                return False, ('atom %d declares no anisotropic beta codes '
                               '(N_t=%s)' % (n_atoms, n_t))
            bf = lines[seen[1]].split()
            for t in range(min(6, len(bf))):
                bf[t] = '1'
            lines[seen[1]] = '   ' + '   '.join(bf)

        k = ci + 1                          # skip beta values + beta codes (+ extra line)
        while k < len(lines):
            l = lines[k]
            if _is_main_line(l) or not l.strip():
                break
            k += 1
        j = k

    return (n_atoms > 0), 'atom %s code set on %d atom(s)' % (mode, n_atoms)


# ---------------------------------------------------------------------------
# fp2k runner + Rwp extraction
# ---------------------------------------------------------------------------
def run_fp2k(fp2k, stem, cwd, timeout=300):
    out_file = os.path.join(cwd, stem + '.out')
    try:
        if os.path.exists(out_file):
            os.remove(out_file)
    except OSError:
        pass
    try:
        _g = active_guard()
        if _g is not None:
            # 0.8.4: one fp2k run is the unit of work here; a stop request is
            # honoured between runs instead of only by killing the tree
            _g.checkpoint('fp2k run')
        if run_guarded is not None:
            # Spawn inside a kill-on-close job object so fp2k cannot outlive this
            # driver (2026-09-25: two orphans spun for 10.3 h / 8.8 h and starved
            # the desktop GUI). `watch` adds the C-fix: judged stalled — and killed
            # early — when the output files stop growing.
            g = run_guarded([fp2k, stem + '.pcr'], cwd=cwd, timeout=timeout,
                            watch=[out_file, os.path.join(cwd, stem + '.prf')],
                            stall_s=STALL_TIMEOUT_S,
                            nan_probe=out_file)
            if g.get('stalled'):
                return {'ok': False, 'stalled': True,
                        'stall_kind': g.get('stall_kind'),
                        'stall_after_s': g.get('stall_after_s'),
                        'error': 'fp2k stalled (%s): no output growth for %ss'
                                 % (g.get('stall_kind'), g.get('stall_after_s')),
                        'guard': g.get('guard'), 'pid': g.get('pid'),
                        'job_assigned': g.get('assigned'),
                        'alive_after_kill': g.get('alive_after_kill')}
            if g.get('timed_out'):
                return {'ok': False, 'error': 'fp2k timed out after %ss' % timeout,
                        'guard': g.get('guard'), 'pid': g.get('pid'),
                        'job_assigned': g.get('assigned'),
                        'alive_after_kill': g.get('alive_after_kill')}
            code = g.get('returncode')
            tail = (g.get('output') or '')[-1500:]
            guard_info = {'guard': g.get('guard'), 'pid': g.get('pid'),
                          'assigned': g.get('assigned'),
                          'alive_after_kill': g.get('alive_after_kill')}
        else:
            p = subprocess.run([fp2k, stem + '.pcr'], cwd=cwd, capture_output=True,
                               text=True, timeout=timeout)
            code = p.returncode
            tail = (p.stdout or '')[-1500:]
            guard_info = {'guard': 'none'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'fp2k timed out after %ss' % timeout}
    except Exception as e:
        return {'ok': False, 'error': 'fp2k failed to start: %s' % e}
    if not os.path.exists(out_file):
        return {'ok': False, 'error': 'fp2k produced no .out (exit %s)' % code,
                'log_tail': tail}
    text = open(out_file, encoding='utf-8', errors='replace').read()
    m = re.findall(r'Conventional Rietveld Rp,Rwp,Re and Chi2:\s+'
                   r'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)', text)
    if not m:
        return {'ok': False, 'error': 'fp2k .out has no R-factor line '
                                      '(the refinement did not run to completion)',
                'log_tail': text[-800:]}
    rp, rwp, rexp, chi2 = m[-1]
    bragg = re.findall(r'Bragg R-factor:\s+([\d.]+)', text)
    return {'ok': True, 'exit': code, 'Rp': float(rp), 'Rwp': float(rwp),
            'Rexp': float(rexp), 'Chi2': float(chi2),
            'Bragg_R': float(bragg[-1]) if bragg else None,
            'n_rfactor_lines': len(m), 'out': out_file,
            # the PROTECTION state travels with a SUCCESSFUL run too (0.8.7): the
            # fourth review found only the failure paths carried it, so a failed
            # job-object assignment stayed invisible on the normal path.
            'guard': guard_info.get('guard'), 'pid': guard_info.get('pid'),
            'assigned': guard_info.get('assigned'),
            'alive_after_kill': guard_info.get('alive_after_kill')}


def run_fp2k_mean(fp2k, stem, cwd, timeout, repeats, restore_lines=None, pcr_path=None,
                  cache_prefix=None):
    """Run fp2k `repeats` times FROM THE SAME STARTING pcr and average Rwp.

    WHY (user decision 2026-09-21): a single fp2k run is not a reproducible measure —
    re-running the same pcr produced 16 R-factor lines oscillating between 17.5 and
    21.6. The ladder therefore compares the MEAN of `repeats` runs, and reports every
    individual value plus the spread, so two candidates whose means are close can both
    be kept in consideration instead of one being discarded on single-run noise.

    WHY THE RESTORE (audit, same day, second pass): fp2k WRITES the refined values
    back into the pcr, so three runs in a row were three SUCCESSIVE ITERATIONS, not
    three independent repetitions of the same trial. `restore_lines` is written back
    before every run so each repetition starts from an identical state.

    The packet reports the run CLOSEST TO THE MEAN as its representative, so Rp/Chi2/
    Bragg belong to the same state as the averaged Rwp (previously the packet carried
    the best-Rwp run's Rp next to the three-run mean Rwp — two different states in one
    record, which is what made the restored-best comparison unreadable).
    """
    runs = []
    # In-memory pcr state AFTER each run: fp2k writes the refined values back into the
    # pcr, so the file on disk when the loop ends belongs to the LAST run — which is not
    # necessarily the run whose Rwp the packet reports. Without this the caller received
    # the mean of three runs next to a pcr from a different one of them (audit
    # 2026-09-21 third pass: three runs 11/14/17 reported 14 and delivered 17's state).
    states = []
    # PER-STEP CACHE ON DISK: the START state, which is the file that actually CARRIES
    # the reported Rwp, plus the write-back as a labelled backup (user decision
    # 2026-09-21: "交付起点状态;写回状态作为备份保留,但是不进流程").
    #
    # WHY NOT THE WRITE-BACK -- measured, not reasoned. fp2k reports the Rwp of the
    # parameter set it EVALUATED, but what it leaves in the pcr is the NEXT update, which
    # it has not evaluated yet. Refining the written-back file therefore does not return
    # the number the run reported. Real data, same fixture, one fresh fp2k run each:
    #   from the run's own start state     -> trajectory 29.6 ... 16.0   (LAST 16.0)
    #   from the post-run written-back file -> trajectory 22.3 ... 17.5  (LAST 17.5)
    # while the driver had recorded 16.0 for that run. Pairing the write-back with 16.0
    # would give a file and a number that agree on paper -- identical bytes, even a
    # matching hash -- and disagree in the one place that matters. So the deliverable is
    # the start state and the reported Rwp is that run's own: one measurement, nothing
    # left to reproduce. The write-back is still kept because it holds the refined
    # parameter values, but it is a backup and does not enter the flow.
    start_cache = None
    if cache_prefix and restore_lines is not None:
        start_cache = '%s_start.pcr' % cache_prefix
        _write_lines(start_cache, restore_lines)
    for k in range(max(1, repeats)):
        if restore_lines is not None and pcr_path:
            _write_lines(pcr_path, restore_lines)
        r = run_fp2k(fp2k, stem, cwd, timeout)
        runs.append(r)
        states.append(_read_lines(pcr_path)
                      if pcr_path and os.path.exists(pcr_path) else None)
        if r.get('stalled'):
            # C-fix: a stalled run is a property of the TRIAL, not of one repetition —
            # repeating it would only pay the stall window `repeats` times.
            break
    ok_pairs = [(i, r) for i, r in enumerate(runs)
                if r.get('ok') and isinstance(r.get('Rwp'), (int, float))]
    if not ok_pairs:
        return {'ok': False, 'error': runs[-1].get('error') if runs else 'no runs',
                'Rwp': None, 'n_runs': len(runs), 'rwp_runs': [],
                'log_tail': (runs[-1].get('log_tail') if runs else '')}
    vals = [float(r['Rwp']) for _, r in ok_pairs]
    mean = sum(vals) / len(vals)
    rep_i, rep = min(ok_pairs, key=lambda p: abs(float(p[1]['Rwp']) - mean))
    out = dict(rep)                      # representative = the run nearest the mean
    out['Rwp_each'] = [round(v, 4) for v in vals]
    out['Rwp'] = round(mean, 4)          # MEAN is the comparison quantity
    out['Rwp_spread'] = round(max(vals) - min(vals), 4)
    out['n_runs'] = len(runs)
    out['n_ok_runs'] = len(ok_pairs)
    out['representative_run_rwp'] = round(float(rep['Rwp']), 4)
    out['representative_run_index'] = rep_i
    # The (file, R) PAIR that travels together: the state the representative run was
    # LAUNCHED from, and that same run's own Rwp. Never the mean -- the mean belongs to
    # the set of runs, not to any file -- and never the write-back (see above).
    out['representative_run_pcr'] = start_cache
    out['representative_run_end_pcr'] = None
    if cache_prefix and states[rep_i] is not None:
        out['representative_run_end_pcr'] = '%s_end.pcr' % cache_prefix
        _write_lines(out['representative_run_end_pcr'], states[rep_i])
    out['runs'] = [{'index': i,
                    'Rwp': (round(float(r['Rwp']), 4)
                            if r.get('ok') and isinstance(r.get('Rwp'), (int, float))
                            else None),
                    'ok': bool(r.get('ok'))}
                   for i, r in enumerate(runs)]
    # Hand back the state the REPORTED numbers belong to, so "delivered pcr" and
    # "reported Rwp" are one model, not two.
    if pcr_path and states[rep_i] is not None:
        _write_lines(pcr_path, states[rep_i])
        out['delivered_state'] = 'representative_run#%d' % rep_i
    if len(vals) > 1 and (max(vals) - min(vals)) > 0.5:
        out['unstable'] = True
    return out


# ---------------------------------------------------------------------------
# ladder
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# AUTOFP'S REFINEMENT STRATEGY, PORTED (2026-09-20)
# ---------------------------------------------------------------------------
# The missing piece was never an algorithm we had to invent: AutoFP's auto-refine
# logic is pure strategy, sitting in its source and independent of the pcr
# parser that keeps breaking. Ported from
#   autofp/strategy/strategy_xray.py   -> 'param_order' + 'target'
#   autofp/paramgroup_xray.py          -> Param_Order_Group (the same order)
#   autofp/auto.py  autorun()          -> the stepwise loop
# and it reads:
#   target = MIN=R_Factor["Rwp"]        (autofp/setting_default.txt: eps 0.1, NCY 10)
#   order  = scale, zero, simple background, cell a,b,c, W, complex background,
#            V,U, Asym, Y,X, Atom x y z, Pref,Bov, Biso-Atom, GausS,1G, Occ-Atom,
#            anisotropic thermal factors, S_L,D_L, manual background
# The algorithm (auto.py:124-244) enables ONE parameter (group) at a time, runs
# fp2k, compares Rwp against the best so far, and ROLLS BACK when it got worse
# (r.back()), keeping rwp.txt / rwplist.txt / rwp_all.txt traces.
#
# WHY THIS MATTERS FOR US: my own ad-hoc ladder opened the cell far too early and
# opened the whole background at once, which is exactly what the measured failure
# showed (cell early -> Rwp 21.4 -> 100). AutoFP's order exists to avoid that:
# simple background first, then cell, then W, then the REST of the background,
# then V/U. So the ladder below is not my invention - it is AutoFP's, expressed
# as pcr edits.
#
# WHAT CANNOT BE PORTED: r.params.paramlist / r.setParam(...) / r.back() come from
# diffpy/pyfullprof's parse of the pcr, which is the component that fails on
# CIFs_to_PCR output. Everything else - the order, the group members, the target,
# the accept/rollback rule, the traces - is portable, and is what this engine
# implements. Note fp2k itself exposes NO library interface (fpsuite ships only
# fp2k.exe + Intel runtimes), so the pcr file is unavoidable: it is the only
# channel into FullProf. What gets replaced is the PARSER, not the pcr.
#
# A group may need SEVERAL levels (AutoFP's 'simple background' is BACK[0],BACK[1]
# and 'complex background' is the whole BACK list; 'V,U' is two steps). A ladder
# entry is therefore a list of levels, applied cumulatively, each level tried
# separately so a bad level can be rolled back on its own.
LADDER_SPEC = [
    ('scale', 'scale', [('scale', 0)]),
    ('zero', 'zero point', [('zero', 0)]),
    ('simple background', 'background terms 1-2', [('bg', 0), ('bg', 1)]),
    ('cell a,b,c', 'cell parameters', [('cell', 0), ('cell', 1), ('cell', 2)]),
    ('W', 'profile W', [('uvw_named', 2)]),
    # AutoFP's 'complex background' is the WHOLE BACK list (up to 24 coefficients
    # in the panel CIFs_to_PCR writes). It is released AFTER W and cell, and
    # term by term so a bad term is rolled back on its own.
    ('complex background', 'background terms 3-24',
     [('bg', n) for n in range(2, 24)]),
    ('V,U', 'profile V then U', [('uvw_named', 1), ('uvw_named', 0)]),
    ('Y,X', 'profile Y then X', [('uvw_named', 4), ('uvw_named', 3)]),
    ('Atom x y z', 'atom coordinates', [('atom_coord', 0)]),
    ('Pref,Bov', 'preferred orientation / Bov', [('bov', 0)]),
    ('Biso-Atom', 'atom Biso', [('atom_biso', 0)]),
    ('GausS,1G', 'Gaussian size / 1G', [('uvw_named', 5)]),
    ('Occ-Atom', 'site occupancy', [('atom_occ', 0)]),
    ('Anisotropic Thermal factors', 'anisotropic temperature factors',
     [('atom_beta', 0)]),
]
# 'Asym' (PA) and 'S_L,D_L' from AutoFP's order are deliberately NOT ported: their
# fields are not refinable codes in the pcr panel this engine writes, so enabling
# them would be guesswork. They are reported as skipped instead.


def apply_switch(lines, sw):
    kind = sw[0]
    level = sw[1] if len(sw) > 1 else 0

    def panel(needle, bang=False):
        return find_header(lines, needle) if not bang else \
            next((i for i, l in enumerate(lines)
                  if needle in l and l.strip().startswith('!')), None)

    if kind == 'zero':
        i = panel('Zero    Code')
        return set_codes(lines, i, 1, [1, 3, 5]) if i is not None else (False, 'no Zero panel')
    if kind == 'bg':
        i = panel('Background coefficients')
        if i is None:
            return False, 'no background panel'
        # background coefficient n sits at field (n, n+6) on the value/code pair
        return set_codes(lines, i, 2, [level])
    if kind == 'scale':
        i = panel('Scale', bang=True)
        return set_codes(lines, i, 2, [0]) if i is not None else (False, 'no Scale panel')
    if kind == 'uvw_named':
        i = panel('GauSiz', bang=True)
        return set_codes(lines, i, 2, [level]) if i is not None else (False, 'no U V W panel')
    if kind == 'cell':
        i = panel('#Cell Info')
        return set_codes(lines, i, 2, [level]) if i is not None else (False, 'no cell panel')
    if kind == 'bov':
        i = panel('Scale', bang=True)
        return set_codes(lines, i, 2, [2]) if i is not None else (False, 'no Bov field')
    if kind in ('atom_coord', 'atom_biso', 'atom_occ', 'atom_beta'):
        which = {'atom_coord': 'xyz', 'atom_biso': 'biso',
                 'atom_occ': 'occ', 'atom_beta': 'beta'}[kind]
        return set_atom_code(lines, which)
    return False, 'unknown switch %r' % (kind,)


def main():
    if len(sys.argv) < 2:
        raise SystemExit('plan JSON required')
    plan_path = sys.argv[1]
    out_path = None
    for i, a in enumerate(sys.argv[2:]):
        if a == '--out':
            out_path = sys.argv[3 + i]
    plan = json.load(open(plan_path, encoding='utf-8'))
    out_path = out_path or os.path.splitext(plan_path)[0] + '_result.json'
    # P1 (0.8.3): the driver itself joins a kill-on-close job and watches its
    # parent, so a dead Node can no longer leave this process (and the fp2k it
    # holds in run_guarded's job) running forever. NOTE: fp2k's own liveness is
    # NOT judged from these ticks — fp2k is a black box that touches its outputs
    # only a couple of times per run (measured 2026-09-28), so the stall rule in
    # fp2k_guard stays the authority for that backend.
    # 0.8.5: the guard must be told WHERE to look for the stop request. It used to
    # fall back to plan["out_dir"] alone, and the fp2k plan did not carry one — so the
    # driver's guard never watched `.xrd-stop` and never wrote `.xrd-progress.json`,
    # which made the graceful-stop path dead for this backend. The result file's own
    # directory is where the JS layer writes the stop file.
    guard = start_guard(plan, backend='fp2k', out_path=out_path,
                        out_dir=plan.get('out_dir') or os.path.dirname(
                            os.path.abspath(out_path or plan_path)))
    global STALL_TIMEOUT_S
    STALL_TIMEOUT_S = float(plan.get('stall_timeout') or 60)
    out_path = out_path or os.path.splitext(plan_path)[0] + '_result.json'
    result = {'ok': False, 'steps': [], 'warning': []}
    # Self-heal: terminate fp2k processes whose driver died (2026-09-25 incident).
    # It runs before any refinement, is limited to parentless processes, never raises,
    # and (0.8.5) only kills processes whose IMAGE PATH is the fp2k binary THIS run is
    # about to use — so a name match can no longer hit another project's, or the user's
    # own hand-started, fp2k. It therefore sits right after `fp2k` is resolved.
    try:
        pcr = plan['pcr']
        if not os.path.isfile(pcr):
            raise RuntimeError('pcr not found: %s' % pcr)
        cwd = os.path.dirname(os.path.abspath(pcr))
        stem = os.path.splitext(os.path.basename(pcr))[0]
        # 0.9.0 wavelength (EXPLICIT-ONLY): when the plan carries one, the pcr's
        # `! Lambda1 Lambda2 Ratio` VALUE line is rewritten BEFORE anything runs -
        # this driver otherwise never touches that line. Omitted => the pcr keeps
        # whatever the converter wrote (default-off, byte-identical behaviour).
        wl_set = None
        _wl = plan.get('wavelength')
        if _wl:
            try:
                from cif2pcr_helper import set_wavelength
                wl_set = set_wavelength(pcr, _wl.get('kalpha1'),
                                        _wl.get('kalpha2'), _wl.get('ratio'))
                if wl_set.get('changed'):
                    log('wavelength: pcr Lambda1 line set to [%s] (was [%s])'
                        % (wl_set.get('after'), wl_set.get('before')))
                else:
                    log('wavelength: %s' % (wl_set.get('note')
                                            or wl_set.get('warnings')))
                result['wavelength'] = wl_set
            except Exception as e:
                wl_set = {'ok': False, 'warnings': ['%s: %s' % (type(e).__name__, e)]}
                result['wavelength'] = wl_set
                result.setdefault('warning', []).append(
                    'wavelength was requested but could not be applied: %s' % e)
        fp2k = plan.get('fp2k') or os.environ.get('DSH_FP2K') or \
            os.path.join(os.environ.get('DSH_AUTOFP_ROOT', ''), 'fp2k.exe')
        if not fp2k or not os.path.isfile(fp2k):
            raise RuntimeError('fp2k.exe not found (set plan.fp2k or $DSH_FP2K)')
        if sweep_orphans is not None:
            try:
                sweep = sweep_orphans(min_age_s=int(plan.get('orphan_min_age_s') or 300),
                                      allow_paths=[fp2k])
                result['orphan_sweep'] = sweep
                if sweep.get('killed'):
                    log('[guard] swept %d orphan fp2k: %s'
                        % (len(sweep['killed']), [k['pid'] for k in sweep['killed']]))
                elif sweep.get('candidates'):
                    log('[guard] %d orphan fp2k candidate(s) found (dry run)'
                        % len(sweep['candidates']))
                if sweep.get('skipped_unverified'):
                    log('[guard] %d fp2k-named process(es) left alone (image path is not ours)'
                        % len(sweep['skipped_unverified']))
            except Exception as e:
                log('[guard] sweep failed: %s' % e)
        working = plan.get('working_copy', True)
        if working:
            work_pcr = os.path.join(cwd, stem + '_fp2kdrive.pcr')
            shutil.copyfile(pcr, work_pcr)
            for ext in ('.dat',):
                src = os.path.join(cwd, stem + ext)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(
                        cwd, os.path.splitext(os.path.basename(work_pcr))[0] + ext))
            stem = os.path.splitext(os.path.basename(work_pcr))[0]
            pcr_work = work_pcr
        else:
            pcr_work = pcr

        ncy = int(plan.get('cycles') or 30)
        # NOTE: `or 1` must NOT be used here. The caller passes outer_cycles=0 to mean
        # "score the structure once, run no ladder at all", and 0 is falsy — the old
        # expression silently turned that into 1, so xrd_refine_auto with ladder:false
        # ran the whole parameter ladder anyway (measured 2026-09-21: three fp2k runs,
        # and the candidate ordering was no longer a baseline ordering).
        _outer_raw = plan.get('outer_cycles')
        outer_cycles = 1 if _outer_raw is None else max(0, int(_outer_raw))
        # trials are judged on the MEAN of N runs (user decision 2026-09-21) because a
        # single fp2k run is not reproducible; 1 restores the old single-run behaviour.
        # SCORE-ONLY MEANS ONE RUN (0.8.7): the documented contract for `ladder:false` is
        # "score the structure once, run no ladder at all", and running the baseline three
        # times contradicted it (the fourth review measured 3 runs where 1 was promised).
        score_only = (outer_cycles == 0)
        trial_repeats = 1 if score_only else max(1, int(plan.get('trial_repeats') or 3))
        if score_only:
            result['score_only'] = True
            result['repeats'] = 1

        # Expand the requested groups (AutoFP's order by default) into a flat,
        # cumulative level list: within a group each level STACKS on the previous
        # one, and a level that worsens Rwp is rolled back on its own.
        wanted = plan.get('groups')
        steps = []
        for name, desc, levels in LADDER_SPEC:
            if wanted and name not in wanted:
                continue
            for lv, sw in enumerate(levels):
                steps.append({'group': name, 'desc': desc, 'level': lv, 'switch': sw})

        lines = _read_lines(pcr_work)
        ok, msg = set_ncy(lines, ncy)
        if not ok:
            result['warning'].append(msg)
        # Background normalisation BEFORE the ladder: the converter's Chebyshev
        # panel hides its coefficients from the refinement codes.
        bg_rewritten = False
        if plan.get('polynomial_background', True):
            did, bmsg = polynomialise_background(lines, int(plan.get('background_order') or 6))
            result['background'] = bmsg
            bg_rewritten = bool(did)
            if did:
                result['warning'].append(bmsg)
        # BACKGROUND STEPS ARE CAPPED BY THE FIELD COUNT THE PANEL ACTUALLY EXPOSES (0.8.7).
        # The rewrite writes exactly `background_order` coefficient fields (6 by default),
        # while the ladder's 'complex background' group asks for terms 3..24: every level
        # past the real count reports "no such field index" and changes NOTHING. The 0.8.6
        # guard only fired when the rewrite had FAILED, so on a successful rewrite the run
        # still issued ~18 dead steps and reported background_levels_skipped = 0 — the
        # fourth review's finding. The cap is now computed from the field count itself.
        bg_terms = int(plan.get('background_order') or 6) if bg_rewritten else 2
        _n_before = len(steps)
        steps = [s for s in steps
                 if not (isinstance(s.get('switch'), tuple) and s['switch'][0] == 'bg'
                         and int(s['switch'][1]) >= bg_terms)]
        if len(steps) != _n_before:
            result['background_levels_skipped'] = _n_before - len(steps)
            result['background_terms'] = bg_terms
            result['warning'].append(
                'background: the panel exposes %d coefficient field(s)%s, so %d ladder level(s) '
                'asking for higher terms were SKIPPED — they would report "no such field index" '
                'and change nothing'
                % (bg_terms,
                   '' if bg_rewritten else ' (the Chebyshev panel could NOT be rewritten: %s)'
                   % result.get('background'),
                   _n_before - len(steps)))
        # Phase name: the converter copies the CIF FILENAME in, and fp2k aborts on
        # the dot when it scans that field.
        did, pmsg = sanitise_phase_name(lines)
        result['phase_name'] = pmsg
        if did:
            result['warning'].append(pmsg)
        _write_lines(pcr_work, lines)

        accepted = []
        best = None
        # per-TRIAL counter: names the per-run pcr cache (<stem>_stepNN_runKK.pcr) so the
        # file a delivery falls back to is identifiable in the artifact directory
        trial_no = 0
        # snapshot file for the best accepted state (see the snapshot write below)
        best_pcr = os.path.join(cwd, stem + '_fp2kbest.pcr')
        # The baseline is LAUNCHED from the prepared working file, so cache that state:
        # it is the one the baseline's Rwp belongs to (see the note in run_fp2k_mean on
        # why the write-back cannot stand in for it). The write-back is kept as backup.
        baseline_start = os.path.join(cwd, '%s_baseline_start.pcr' % stem)
        _write_lines(baseline_start, _read_lines(pcr_work))
        # The baseline uses the SAME statistics as every candidate (0.8.6): fp2k is not
        # reproducible run-to-run (measured 21.6 vs 23.3 on one pcr), which is exactly
        # why the ladder compares MEANS of `trial_repeats` runs. Running the baseline
        # ONCE against candidate means made the comparison unfair in one direction (a
        # lucky baseline, or an unlucky candidate, decides the whole ladder) and let a
        # candidate be accepted on a mean where only one of three runs had succeeded.
        baseline = run_fp2k_mean(fp2k, stem, cwd, int(plan.get('timeout') or 300),
                                 trial_repeats, restore_lines=list(_read_lines(pcr_work)),
                                 pcr_path=pcr_work, cache_prefix=os.path.join(cwd, stem + '_baseline'))
        if not baseline.get('ok'):
            raise RuntimeError('baseline fp2k run failed: %s' % baseline.get('error'))
        # BOTH SIDES NOW DEMAND THE FULL REPEAT COUNT (0.8.7). The candidates already
        # required 3/3; letting the baseline continue on 1/3 compared a mean of one run
        # against a mean of three — the fourth review's finding. A baseline that cannot
        # measure itself the same way is not a baseline, so the run fails loudly.
        if baseline.get('n_ok_runs') is not None and baseline['n_ok_runs'] < trial_repeats:
            raise RuntimeError(
                'baseline: only %d of %d repeat run(s) succeeded, so the baseline and the '
                'candidates would be measured on different footings (each candidate requires '
                'every repeat to succeed). Fix the pcr/data (a missing or unreadable intensity '
                'file is the usual cause) or pass trial_repeats=1 to ask for single runs '
                'everywhere.' % (baseline['n_ok_runs'], trial_repeats))
        result['baseline'] = {'Rwp': baseline['Rwp'], 'Rwp_each': baseline.get('Rwp_each'),
                              'n_runs': baseline.get('n_runs'), 'n_ok_runs': baseline.get('n_ok_runs'),
                              'spread': baseline.get('Rwp_spread'), 'repeats': trial_repeats}
        first = {'Rwp': baseline['Rwp'], 'Rp': baseline.get('Rp'), 'Rexp': baseline.get('Rexp'),
                 'Chi2': baseline.get('Chi2'), 'Bragg_R': baseline.get('Bragg_R'),
                 'representative_run_rwp': baseline.get('representative_run_rwp')}
        baseline_end = os.path.join(cwd, '%s_baseline_end.pcr' % stem)
        _write_lines(baseline_end, _read_lines(pcr_work))
        best = {'Rwp': first['Rwp'], 'Rp': first['Rp'], 'Rexp': first['Rexp'],
                'Chi2': first['Chi2'], 'Bragg_R': first.get('Bragg_R'),
                'after': '(baseline)', 'outer': 0,
                'run_rwp': first.get('representative_run_rwp', first['Rwp']), 'cached_pcr': baseline_start,
                'end_pcr': baseline_end}
        result['steps'].append({'outer': 0, 'group': '(baseline)', 'step': '(baseline)',
                                'Rwp': first['Rwp'], 'Rp': first['Rp'],
                                'Rexp': first['Rexp'], 'Chi2': first['Chi2'],
                                'accepted': True})

        for outer in range(outer_cycles):
            improved_any = False
            for st in steps:
                before = _read_lines(pcr_work)
                trial = list(before)
                ok, msg = apply_switch(trial, st['switch'])
                if not ok:
                    result['warning'].append('%s level %d: %s'
                                             % (st['group'], st['level'], msg))
                    continue
                # per-step traceability (read-only, no interface change): did this
                # attempt actually change the pcr's refinement codes? "did not improve"
                # and "never reached the file" are otherwise indistinguishable.
                fp_before = atom_code_fingerprint(before)
                fp_after = atom_code_fingerprint(trial)
                # two DIFFERENT questions, both answered per step:
                #   atom_codes_changed — did the atomic refinement codes move? (only
                #     meaningful for atom steps; false for scale/zero/cell by design)
                #   pcr_changed        — did this switch change the pcr AT ALL? A step
                #     with pcr_changed:false genuinely did nothing and must not be read
                #     as "the model did not improve".
                codes_changed = (fp_before != fp_after)
                pcr_before = pcr_refine_fingerprint(before)
                pcr_after = pcr_refine_fingerprint(trial)
                pcr_changed = (pcr_before != pcr_after)
                _write_lines(pcr_work, trial)
                run = run_fp2k_mean(fp2k, stem, cwd, int(plan.get('timeout') or 300),
                                    trial_repeats,
                                    restore_lines=list(trial), pcr_path=pcr_work,
                                    cache_prefix=os.path.join(
                                        cwd, '%s_step%02d' % (stem, trial_no)))
                trial_no += 1
                # A candidate may only be ACCEPTED when every repeat succeeded (0.8.6):
                # the mean already ignores failed runs, so with 1 of 3 surviving the
                # comparison used a mean of one — the third review's finding. Partial
                # success is still reported (n_ok_runs) but cannot decide the ladder.
                repeats_ok = run.get('n_ok_runs', run.get('n_runs', trial_repeats)) >= trial_repeats
                if run.get('assigned') is False:
                    result['warning'].append(
                        'the fp2k child could NOT be put into the kill-on-close job object, so only '
                        'the direct kill protects it (a grandchild could survive)')
                if not repeats_ok:
                    result['warning'].append(
                        '%s level %d: only %s of %d repeat run(s) succeeded — the step is NOT '
                        'accepted on a partial mean (Rwp %s)'
                        % (st['group'], st['level'], run.get('n_ok_runs'), trial_repeats,
                           run.get('Rwp')))
                better = bool(run.get('ok')) and repeats_ok and run['Rwp'] < best['Rwp'] - 1e-6
                if run.get('unstable'):
                    result['warning'].append(
                        '%s level %d: repeat runs spread %s percentage points (fp2k is not '
                        'reproducible) — treat this step\'s Rwp as noisy'
                        % (st['group'], st['level'], run.get('Rwp_spread')))
                result['steps'].append({
                    'outer': outer + 1, 'group': st['group'], 'step': st['group'],
                    'level': st['level'], 'switch': msg,
                    'Rwp': run.get('Rwp'), 'Rp': run.get('Rp'),
                    'Rexp': run.get('Rexp'), 'Chi2': run.get('Chi2'),
                    'Bragg_R': run.get('Bragg_R'), 'accepted': bool(better),
                    'codes_changed': codes_changed,
                    'pcr_changed': pcr_changed,
                    'atom_codes': fp_after,
                    'Rwp_each': run.get('Rwp_each'),
                    'Rwp_spread': run.get('Rwp_spread'),
                    'n_runs': run.get('n_runs'),
                    'run_rwp': run.get('representative_run_rwp'),
                    'run_pcr': run.get('representative_run_pcr'),
                    'runs': run.get('runs'),
                    'unstable': run.get('unstable'),
                    'error': run.get('error')})
                if better:
                    accepted.append(st['switch'])
                    # TWO R VALUES, TWO ROLES (user decision 2026-09-21):
                    #   'Rwp'    = the MEAN of the repeats -> the DECISION value. It is
                    #              what picks between candidate CIFs and what the gate
                    #              "good enough to go straight into GSAS-II" is judged on.
                    #   'run_rwp'= the representative run's OWN Rwp, and 'cached_pcr' is
                    #              that same run's file -> the FLOW value. It travels with
                    #              the pcr that is handed to the next stage, because up to
                    #              GSAS-II this workflow is R-driven (weak on other
                    #              physical constraints), so the number that enters the
                    #              next step must belong to the file that enters it.
                    best = {'Rwp': run['Rwp'], 'Rp': run['Rp'], 'Rexp': run['Rexp'],
                            'Chi2': run['Chi2'], 'Bragg_R': run.get('Bragg_R'),
                            'after': '%s L%d' % (st['group'], st['level']),
                            'outer': outer + 1,
                            'run_rwp': run.get('representative_run_rwp'),
                            'cached_pcr': run.get('representative_run_pcr'),
                            'end_pcr': run.get('representative_run_end_pcr')}
                    improved_any = True
                    # Snapshot the state the repeats were LAUNCHED FROM (`trial`), NOT the
                    # state fp2k left behind. `run['Rwp']` is the mean over runs that all
                    # started from `trial`; pcr_work at this point is the endpoint of the
                    # LAST repeat, which fp2k has already refined further and which is a
                    # different state entirely. Taking the endpoint is what made the
                    # best value look "not reproducible" on real data: recorded best 16.0,
                    # and re-measuring the endpoint-shaped snapshot gave 17.5. Measured
                    # 2026-09-21 by changing only this line: re-measured 16.0, tolerance
                    # met. (The snapshot is kept for audit/replay; DELIVERY no longer
                    # depends on re-measuring anything -- see the cached-run pcr below.)
                    _write_lines(best_pcr, list(trial))
                    # then re-assert the accepted switches on the working copy, so the
                    # next trial stacks on them
                    cur = _read_lines(pcr_work)
                    set_ncy(cur, ncy)
                    for sw in accepted:
                        apply_switch(cur, sw)
                    _write_lines(pcr_work, cur)
                else:
                    _write_lines(pcr_work, before)
                    if run.get('ok'):
                        result['warning'].append(
                            '%s L%d did not improve Rwp (%.3f vs best %.3f) - rolled back'
                            % (st['group'], st['level'], run['Rwp'], best['Rwp']))
                    else:
                        result['warning'].append(
                            '%s L%d: %s' % (st['group'], st['level'], run.get('error')))
            if not improved_any:
                break
        # Skipped groups are reported ONLY when a ladder actually ran. In score-only
        # mode (outer_cycles=0) the caller asked for a baseline number, so claiming
        # "Asym was not ported" there would be noise about steps never attempted.
        if outer_cycles > 0:
            for name in ('Asym', 'S_L,D_L'):
                result['warning'].append(
                    'AutoFP group %r was NOT ported: its fields (PA / S_L / D_L) are '
                    'not refinable codes in the pcr panel this engine writes, so '
                    'enabling them would be guesswork' % name)

        # The baseline run IS the final result in score-only mode: re-running an
        # unmodified pcr would add a second identical fp2k call for nothing.
        # final measurement uses the SAME repeats as the trials, so "the final state"
        # and "the tested states" are measured on one footing (a single run against a
        # three-run mean compared two different quantities).
        _fin_lines = _read_lines(pcr_work)

        def measure_final():
            return run_fp2k_mean(fp2k, stem, cwd, int(plan.get('timeout') or 300),
                                 trial_repeats if outer_cycles > 0 else 1,
                                 restore_lines=_fin_lines, pcr_path=pcr_work,
                                 cache_prefix=os.path.join(cwd, stem + '_final'))

        if outer_cycles > 0:
            final = measure_final()
        else:
            # SCORE-ONLY USES THE BASELINE OBJECT ITSELF (0.8.7). `first` is a hand-built
            # dict for the ladder's rollback bookkeeping and carries no `ok`, no
            # representative-run index and no cached start state, so using it as the final
            # measurement made a SUCCESSFUL score-only run report ok:false and delivered_pcr
            # null — the fourth review's finding.
            final = dict(baseline)
            if result['steps']:
                result['steps'][0]['step'] = '(baseline, score only)'
                result['steps'][0]['Rwp'] = baseline.get('Rwp')

        # DELIVERY GUARANTEE, BY CACHED FILE -- NOT BY RE-MEASUREMENT
        # (user decision 2026-09-21).
        #
        # The guarantee is unchanged: never hand back a state worse than the best the
        # ladder actually reached. What changed is HOW. The old contract restored the best
        # snapshot and RE-MEASURED it, which required fp2k to converge back to the same
        # solution from a state it had already refined once. It does not do that: on real
        # data the recorded best was 16.0 and the restored snapshot re-measured 17.5 with
        # within_tolerance false -- measured AFTER the cache-refresh fix, so the
        # re-measurement really did see the snapshot. FullProf simply has too many
        # parameters for that reproduction to be relied on (user: "既然fullprof参数过多
        # 难以稳定复现").
        #
        # So we stop asking it to. run_fp2k_mean caches the pcr every run produced, and
        # when the ladder ends worse than its best step the DELIVERED file is the cached
        # pcr of the run that measured that best -- copied, never re-run. The delivered
        # Rwp is that same run's OWN value, so the reported number and the delivered file
        # are one measurement BY CONSTRUCTION rather than by hope.
        #
        # TWO NUMBERS, TWO ROLES (user decision 2026-09-21):
        #   best['Rwp']   = mean of the repeats -> the DECISION value: it is what picks
        #                   between candidate CIFs and what the "good enough to go
        #                   straight into GSAS-II" gate is judged on.
        #   result['Rwp'] = the delivered run's own R -> the FLOW value: it belongs to the
        #                   pcr that goes into the next stage. Up to GSAS-II this workflow
        #                   is R-driven, so the number entering the next step must belong
        #                   to the file entering it.
        result['restored_best'] = False
        result['restore_verified'] = None
        result['delivered_from'] = None
        if outer_cycles > 0 and best:
            fr = final.get('Rwp')
            if fr is None or (best.get('Rwp') is not None and fr > best['Rwp'] + 1e-6):
                src = best.get('cached_pcr')
                if src and os.path.exists(src):
                    shutil.copyfile(src, pcr_work)
                    final = {'ok': True, 'Rwp': best.get('run_rwp'),
                             'Rp': best.get('Rp'), 'Rexp': best.get('Rexp'),
                             'Chi2': best.get('Chi2'), 'Bragg_R': best.get('Bragg_R'),
                             'out': final.get('out')}
                    result['restored_best'] = True
                    result['Rwp_mean'] = best.get('Rwp')   # the DECISION value
                    result['delivered_from'] = {
                        'source': 'cached_start_state',
                        'delivered_pcr': src,
                        'delivered_run_rwp': best.get('run_rwp'),
                        'decision_mean_rwp': best.get('Rwp'),
                        'after': best.get('after', '-'),
                        'ladder_end_rwp': fr,
                        'backup_writeback_pcr': best.get('end_pcr'),
                        'remeasured': False,
                        'note': ("the ladder ended worse than its best step, so the START "
                                 "state of the run that measured the best is delivered and "
                                 "the reported Rwp is THAT run's own value. The state fp2k "
                                 "wrote back is kept as a labelled backup but does not "
                                 "enter the flow: it holds the refined parameters, and its "
                                 "own refinement returns a DIFFERENT Rwp (measured), so it "
                                 "cannot carry this number.")}
                    result['restore_verified'] = dict(result['delivered_from'])
                    result['warning'].append(
                        'the ladder ended in a worse state than its own best step '
                        '(%.3f vs best mean %.3f after %s); the cached START state of the '
                        'run that measured %.3f was delivered instead - copied, not '
                        're-measured'
                        % (fr if fr is not None else -1, best['Rwp'],
                           best.get('after', '-'),
                           best.get('run_rwp') if best.get('run_rwp') is not None else -1))
                else:
                    result['Rwp_mean'] = fr
                    result['warning'].append(
                        'the ladder ended worse than its best step (%.3f vs %.3f) but no '
                        'cached per-run pcr was available to deliver; the ladder-end state '
                        'stands and the reported Rwp belongs to it'
                        % (fr if fr is not None else -1, best['Rwp']))
        result['ok'] = bool(final.get('ok'))
        result['Rwp'] = final.get('Rwp')
        result['Rp'] = final.get('Rp')
        result['Rexp'] = final.get('Rexp')
        result['Chi2'] = final.get('Chi2')
        result['Bragg_R'] = final.get('Bragg_R')
        result['best'] = best
        result['refined_pcr'] = pcr_work
        result['out_files'] = [f for f in (final.get('out'),) if f]
        # BOTH NUMBERS, LABELLED BY ROLE, ON EVERY PATH -- not only when the cached best
        # had to be substituted. The headline `Rwp` is the value that BELONGS TO THE
        # DELIVERED pcr: the Rwp the representative run measured from the state we hand
        # over. `Rwp_mean` is the DECISION quantity. Reporting the mean as the headline
        # was the older contract, and it put a number next to a file that had never
        # measured it.
        if result['delivered_from'] is None:
            mean_now = final.get('Rwp')
            run_now = final.get('representative_run_rwp')
            result['Rwp_mean'] = mean_now
            if run_now is not None:
                result['Rwp'] = run_now
                # SAME RULE ON THE NORMAL PATH: deliver the START state whose refinement
                # produced this number, not the write-back. `run_fp2k_mean` left pcr_work
                # holding the representative run's write-back, which is NOT the state the
                # number belongs to (measured: refining it returns 17.5 where the run
                # reported 16.0). Swap in the cached start state.
                start_now = final.get('representative_run_pcr')
                if start_now and os.path.exists(start_now):
                    shutil.copyfile(start_now, pcr_work)
                result['delivered_from'] = {
                    'source': 'cached_start_state',
                    'delivered_pcr': start_now,
                    'delivered_run_rwp': run_now,
                    'decision_mean_rwp': mean_now,
                    'delivered_run_index': final.get('representative_run_index'),
                    'backup_writeback_pcr': final.get('representative_run_end_pcr'),
                    'remeasured': False,
                    'note': ("the ladder ended no worse than its best step, so that step's "
                             "start state is delivered; the reported Rwp is the value that "
                             "run measured from it. The write-back is kept as a labelled "
                             "backup and does not enter the flow.")}
                result['restore_verified'] = dict(result['delivered_from'])
        result['Rwp_delivered'] = result['Rwp']
        result['summary'] = ('fp2k driver%s: %d step(s), delivered Rwp %.3f '
                             '(decision mean %.3f, best step %s)'
                             % (' [score-only]' if outer_cycles == 0 else '',
                                len(result['steps']), result['Rwp'] or -1,
                                result['Rwp_mean'] if result['Rwp_mean'] is not None else -1,
                                (best or {}).get('after', '-')))
    except Exception as e:
        result['ok'] = False
        result['error'] = str(e)
        result['log_tail'] = traceback.format_exc()[-1500:]
    # P0 (0.8.3): atomic write + run identity — a crashed or timed-out ladder must
    # never leave a result file the caller can mistake for this run's own.
    payload = json.dumps(write_result(out_path, result, plan),
                         ensure_ascii=False, indent=1)
    print(payload)


if __name__ == '__main__':
    run_guarded_main(main)
