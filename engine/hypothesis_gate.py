#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hypothesis-driven escalation gate for the manual XRD engine.

WHY (this guards a specific, measured behaviour, not a bookkeeping nicety):
the manual engine accepts any `vary` list and SILENTLY IGNORES names it does
not know, so a misspelled parameter quietly refines one parameter fewer; and
"after three flat rounds you should move to GSAS-II" used to be prose in the
skill file - no code stopped anything. The result is a run that can keep
tweaking one parameter to look busy while never entering GSAS-II, which is the
engine that actually delivers the result.

WHAT THIS MODULE DOES
  * `evaluate(attempts, request)` decides, from the rounds already recorded
    plus THIS request, whether another manual-engine run may start. It is a
    pure function: no IO, no engine call, no pcr access.
  * `record(...)` appends the finished round to the mechanical ledger
    `review_mechanical_<sample>.json` - the same file lib/review.js owns.

THE DECISION (decisions.md D-20261106-01)
  Direction matters more than counting. A legitimate NEW hypothesis always
  allows another manual round, even after three flat rounds; what is refused is
  continuing WITHOUT one, because "propose nothing and tweak" is exactly the
  pattern the old wording produced.

  Refusals, checked in this order:
    empty_hypothesis     hypothesis missing or blank.
    repeated_hypothesis  id, or normalised fingerprint, matches one of the last
                         REPEAT_WINDOW (3) recorded attempts.
    identical_change     change_digest equals the previous round's digest: the
                         same stage+vary is being re-run as if it were new.
    escalate_to_gsas     ESCALATE_STREAK (3) consecutive rounds ended with
                         improved=false AND the hypothesis is not new against
                         ANY earlier attempt. A duplicate older than the 3-round
                         window is still a duplicate; the remedy is GSAS-II.

FINGERPRINT
  strip -> NFKC -> lowercase -> collapse whitespace -> drop Unicode punctuation
  (category P*) -> sha256 of the UTF-8 bytes -> first 16 hex characters.
  An identical `hypothesis_id` means the same hypothesis whatever the wording.
  No stemming, no similarity scoring: a machine predicate has to be the
  deterministic floor of the argument, reproducible and assertable.

LEDGER COMPATIBILITY (shared with lib/review.js)
  schema `dsh-xrd/review-mechanical@1`; `JSON.stringify(state, null, 1)` layout;
  atomic tmp+rename; and a CORRUPT state file is reported, never silently
  rewritten (that accident already happened once - 0.8.0 design note D7).
  `manual_attempts` is appended, every other key is preserved verbatim, and the
  GSAS counters are never touched. A freshly created file carries only
  `_schema`, `sample` and `manual_attempts`; review.js merges it over its own
  empty state on load.

EXIT CODES (CLI)
  0  allowed / command succeeded
  3  denied - gate closed, the JSON verdict is still printed
  1  IO or parse failure      2  usage error

All console output is ASCII (non-ASCII paths are escaped). Files are written
UTF-8 so the JSON byte layout stays compatible with review.js.

Usage:
    python hypothesis_gate.py evaluate --dir . --sample Y2O3 \
        --request '{"hypothesis": "scale 1.02 removes the 33 deg shoulder"}'
    python hypothesis_gate.py record --dir . --sample Y2O3 --request @req.json \
        --rwp-before 21.8 --rwp-after 20.9 --improved true
    python hypothesis_gate.py show --dir . --sample Y2O3
    python hypothesis_gate.py --selftest
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_DENY = 3

SCHEMA = 'dsh-xrd/review-mechanical@1'
MECH_FILE = 'review_mechanical.json'
ATTEMPTS_KEY = 'manual_attempts'
REPEAT_WINDOW = 3
ESCALATE_STREAK = 3
STAGES = ('calibrate', 'structure', 'extras', 'twophase')

GSAS_HINT = ('use xrd_refine_gsas (data+cif): GSAS-II is the delivery engine, '
             'the manual engine only prepares it')

REASON_TEXT = {
    'empty_hypothesis': (
        'hypothesis is blank: say what this run is meant to test or change',
        'one sentence that a flat Rwp would prove wrong, e.g. "Zn occupancy '
        '0.90 -> 0.85 removes the 34.2 deg shoulder"'),
    'repeated_hypothesis': (
        'this hypothesis already ran within the last %d rounds (same id or same '
        'normalised text); re-stating it to add a round is not progress' % REPEAT_WINDOW,
        'state a DIFFERENT thing this run would prove, with a different vary set - '
        'or go to GSAS'),
    'identical_change': (
        'identical change_digest: the same stage and vary set as the previous round, '
        'so nothing new is being varied',
        'change the varied parameters (or the stage) to match a genuinely new '
        'hypothesis, or go to GSAS'),
    'escalate_to_gsas': (
        '%d consecutive rounds without improvement, and this hypothesis is not new '
        'against earlier attempts' % ESCALATE_STREAK,
        GSAS_HINT),
}


class GateError(Exception):
    """An IO or state problem that must stop the caller (never swallowed)."""


# --- small helpers -----------------------------------------------------------
def ascii_safe(text):
    """Escape anything non-ASCII so console output survives a cp936 console."""
    return str(text).encode('ascii', 'backslashreplace').decode('ascii')


def _sha16(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _now_iso():
    return (datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            .replace('+00:00', 'Z'))


def normalize_hypothesis(text):
    """The comparison form of a hypothesis sentence (see FINGERPRINT above)."""
    s = unicodedata.normalize('NFKC', str(text if text is not None else ''))
    s = s.strip().lower()
    s = re.sub(r'\s+', ' ', s)
    return ''.join(ch for ch in s if not unicodedata.category(ch).startswith('P'))


def fingerprint(text):
    """Stable 16-hex identity of a hypothesis sentence."""
    return _sha16(normalize_hypothesis(text))


def change_digest(stage=None, vary=None, detail=None):
    """Default digest of WHAT the run changes, used when the caller gives none.

    Derived from (stage, sorted vary, optional detail) so that an omitted digest
    cannot silently disable the identical_change check.
    """
    payload = [str(stage or ''), sorted(str(v) for v in (vary or []))]
    if detail:
        payload.append(str(detail))
    return _sha16(json.dumps(payload, ensure_ascii=False))


def safe_key(sample):
    """Mirror of review.js `safeKey`, so both halves name one file.

    0.10.0: pure-ASCII names keep the historical fold EXACTLY (no rename for existing users);
    a name containing any non-ASCII character appends sha1(original)[:12], because the fold
    alone mapped every pure-Chinese sample name to '_' and two samples then shared one ledger
    (measured 2026-11-06: a sample that had never failed was denied with "3 consecutive
    rounds without improvement" that belonged to ANOTHER sample).
    """
    s = str(sample or '')
    fold = re.sub(r'[^A-Za-z0-9_.-]+', '_', s)[:60]
    if not s:
        return fold
    # A fold carrying no alphanumeric cannot identify anything -- the pure-Chinese case, but
    # also a name that really IS '_', '   ' or '///'. Those get a hash too, so two different
    # samples can never share a ledger through the fold. This clause MUST mirror review.js
    # exactly: a JS/PY disagreement is worse than the collision it fixes (each side would then
    # write its own ledger). Verified pairwise on 'Y2O3' / 氧化钇 / 氧化镧 / '_' / '   '.
    meaningful = re.search(r'[A-Za-z0-9]', fold) is not None
    if meaningful and all(ord(ch) < 128 for ch in s):
        return fold
    return '%s_%s' % (fold, hashlib.sha1(s.encode('utf-8')).hexdigest()[:12])


def mech_path(dirpath, sample=None):
    key = safe_key(sample)
    name = MECH_FILE.replace('.json', '_%s.json' % key) if key else MECH_FILE
    return os.path.join(str(dirpath), name)


def resolve_path(path=None, sample=None):
    """Accept either a directory or a .json ledger path."""
    if not path:
        raise GateError('no ledger location: pass --dir or --state')
    p = str(path)
    if p.lower().endswith('.json'):
        return p
    return mech_path(p, sample)


# --- ledger IO ---------------------------------------------------------------
def load_state(path):
    """Read the mechanical ledger. Missing -> None; damaged -> GateError."""
    p = str(path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as fh:
            raw = fh.read()
    except OSError as exc:
        raise GateError('cannot read %s (%s)' % (os.path.basename(p), exc))
    if not raw.strip():
        raise GateError('%s is empty; fix or delete it, this module will not '
                        'overwrite a damaged state file' % os.path.basename(p))
    try:
        state = json.loads(raw)
    except ValueError as exc:
        raise GateError('%s is not valid JSON (%s). Fix or delete it; the gate '
                        'will not silently overwrite a damaged state file.'
                        % (os.path.basename(p), exc))
    if not isinstance(state, dict):
        raise GateError('%s is not a JSON object; the ledger must be one'
                        % os.path.basename(p))
    return state


def load_attempts(path, sample=None):
    """The recorded manual attempts, oldest first (empty when absent)."""
    state = load_state(path)
    if state is None:
        return []
    got = state.get(ATTEMPTS_KEY)
    if got is None:
        return []
    if not isinstance(got, list):
        raise GateError('%s has a %s that is not a list'
                        % (os.path.basename(str(path)), ATTEMPTS_KEY))
    return [a for a in got if isinstance(a, dict)]


def atomic_write(path, state):
    """tmp + os.replace in the same directory: a killed run leaves one whole file."""
    p = os.path.abspath(str(path))
    folder = os.path.dirname(p) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.hyp_gate_', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
            fh.write('\n')
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return p


# --- the request -------------------------------------------------------------
def _prepare(request):
    """Normalise one request into the fields both functions act on."""
    req = request if isinstance(request, dict) else {}
    text = req.get('hypothesis')
    text = '' if text is None else str(text)
    hid = req.get('hypothesis_id')
    hid = str(hid).strip() if hid not in (None, '') else None
    cd = req.get('change_digest')
    cd = str(cd).strip() if cd not in (None, '') else None
    stage = req.get('stage')
    stage = str(stage).strip() if stage not in (None, '') else None
    vary = req.get('vary') or []
    if isinstance(vary, str):
        vary = [vary]
    vary = [str(v) for v in vary]
    if not cd:
        cd = change_digest(stage, vary, req.get('detail'))
    return {'text': text, 'id': hid, 'digest': cd, 'stage': stage,
            'vary': vary, 'fingerprint': fingerprint(text)}


def _same_hypothesis(attempt, fp, hid):
    """Two ways to be the same hypothesis: same id, or same normalised text."""
    a_id = attempt.get('hypothesis_id')
    if hid and a_id not in (None, '') and str(a_id) == hid:
        return True
    a_fp = attempt.get('hypothesis_fingerprint')
    if not a_fp:
        a_fp = fingerprint(attempt.get('hypothesis'))
    return a_fp == fp


def no_improve_streak(attempts):
    """How many rounds at the END of the ledger ended without improvement."""
    n = 0
    for attempt in reversed(attempts):
        if attempt.get('improved') is False:
            n += 1
        else:
            break
    return n


def _result(allow, reason, message, next_step, request, extra=None):
    out = {
        'decision': 'allow' if allow else 'deny',
        'allow': bool(allow),
        'reason': reason,
        'message': message,
        'next_step': next_step,
        'hypothesis_fingerprint': request['fingerprint'],
        'change_digest': request['digest'],
        'warnings': [],
    }
    if extra:
        out.update(extra)
    return out


def evaluate(attempts, request):
    """Decide whether this manual-engine run may start.

    Pure: the same (attempts, request) always yields the same verdict.

    @param attempts: recorded entries, oldest first (dicts, see record()).
    @param request: this call's fields - hypothesis (required), optionally
                    hypothesis_id, change_digest, stage, vary, detail.
    @returns dict: decision/allow, reason, message, next_step,
                   hypothesis_fingerprint, change_digest, counters, warnings.
    """
    attempts = [a for a in (attempts or []) if isinstance(a, dict)]
    req = _prepare(request)
    streak = no_improve_streak(attempts)
    counters = {'attempts': len(attempts), 'no_improve_streak': streak,
                'escalate_at_streak': ESCALATE_STREAK,
                'repeat_window': REPEAT_WINDOW}

    # 1. nothing to test -> nothing to run
    if not req['text'].strip():
        msg, nxt = REASON_TEXT['empty_hypothesis']
        return _result(False, 'empty_hypothesis', msg, nxt, req, {'counters': counters})

    # 2. the same hypothesis inside the recent window
    for attempt in attempts[-REPEAT_WINDOW:]:
        if _same_hypothesis(attempt, req['fingerprint'], req['id']):
            msg, nxt = REASON_TEXT['repeated_hypothesis']
            return _result(False, 'repeated_hypothesis', msg, nxt, req,
                           {'counters': counters,
                            'matched_round': attempt.get('n')})

    # 3. the same change as the round immediately before
    if attempts:
        last_digest = attempts[-1].get('change_digest')
        if req['digest'] and last_digest and req['digest'] == last_digest:
            msg, nxt = REASON_TEXT['identical_change']
            return _result(False, 'identical_change', msg, nxt, req,
                           {'counters': counters,
                            'matched_round': attempts[-1].get('n')})

    # 4. a failing streak with no new hypothesis to justify it
    if streak >= ESCALATE_STREAK:
        for attempt in attempts:
            if _same_hypothesis(attempt, req['fingerprint'], req['id']):
                msg, nxt = REASON_TEXT['escalate_to_gsas']
                return _result(False, 'escalate_to_gsas', msg, nxt, req,
                               {'counters': counters,
                                'matched_round': attempt.get('n')})

    out = _result(True, 'ok', 'accepted: this hypothesis is new relative to the '
                   'recorded rounds', '', req, {'counters': counters})

    # Non-blocking hints: the caller should see the debt before it is due.
    if streak >= ESCALATE_STREAK:
        out['warnings'].append(
            'warn: %d flat rounds already - a new hypothesis keeps this line open, '
            'but the next flat round is the last one' % streak)
    elif streak == ESCALATE_STREAK - 1:
        out['warnings'].append(
            'warn: %d consecutive flat rounds; the next flat run must bring a new '
            'hypothesis or move to GSAS' % streak)
    for attempt in attempts:
        if attempt.get('change_digest') and attempt['change_digest'] == req['digest']:
            out['warnings'].append(
                'warn: this stage+vary set was already run in round %s'
                % attempt.get('n'))
            break
    if req['stage'] and req['stage'] not in STAGES:
        out['warnings'].append(
            'warn: stage "%s" is not one of %s' % (req['stage'], '/'.join(STAGES)))
    return out


def record(path, request, rwp_before=None, rwp_after=None, improved=None,
           at=None, sample=None):
    """Append this round to the mechanical ledger (append-only, atomic).

    Existing keys - including the GSAS counters review.js writes - are kept
    verbatim; only `manual_attempts` grows.

    @param path: ledger directory or .json path.
    @param request: the same request dict evaluate() saw.
    @param rwp_before/rwp_after: the Rwp bracket of this run, or None.
    @param improved: True/False/None - False is what feeds the escalation streak.
    @param at: ISO8601 timestamp; defaults to now (UTC, Z).
    @param sample: ledger key; required when `path` is a directory.
    @returns dict: ok, path, n, entry.
    """
    p = resolve_path(path, sample)
    req = _prepare(request)
    if not req['text'].strip():
        raise GateError('refusing to record an empty hypothesis')
    state = load_state(p)
    if state is None:
        state = {'_schema': SCHEMA, 'sample': sample, ATTEMPTS_KEY: []}
    if not isinstance(state.get(ATTEMPTS_KEY), list):
        state[ATTEMPTS_KEY] = []
    if sample and not state.get('sample'):
        state['sample'] = sample
    entry = {
        'n': len(state[ATTEMPTS_KEY]) + 1,
        'at': at or _now_iso(),
        'hypothesis': req['text'],
        'hypothesis_id': req['id'],
        'hypothesis_fingerprint': req['fingerprint'],
        'change_digest': req['digest'],
        'stage': req['stage'],
        'vary': req['vary'],
        'rwp_before': rwp_before,
        'rwp_after': rwp_after,
        'improved': improved,
    }
    state[ATTEMPTS_KEY].append(entry)
    atomic_write(p, state)
    return {'ok': True, 'path': p, 'n': entry['n'], 'entry': entry}


# --- self test ---------------------------------------------------------------
def _attempt(n, hyp, improved, digest, stage='calibrate', vary=('scale',), hid=None):
    return {'n': n, 'at': '2026-11-06T00:00:0%dZ' % (n % 10), 'hypothesis': hyp,
            'hypothesis_id': hid, 'hypothesis_fingerprint': fingerprint(hyp),
            'change_digest': digest, 'stage': stage, 'vary': list(vary),
            'rwp_before': 21.8, 'rwp_after': 21.4, 'improved': improved}


def _scratch_dir():
    """A writable scratch directory under %TEMP%.

    NOT tempfile.mkdtemp(): in DSH's sandbox (2026-11-06) writes into a mkdtemp
    directory were refused with PermissionError on the first file, while a
    directory made with os.makedirs in the same %TEMP% was writable - measured
    there, not here (this machine writes mkdtemp dirs fine). The selftest has to
    run where the caller runs, so use the form that works in both.
    """
    path = os.path.join(tempfile.gettempdir(), 'hyp_gate_' + uuid.uuid4().hex[:12])
    os.makedirs(path, exist_ok=False)
    return path


def _selftest():
    checks = []

    def check(idx, name, cond, detail=''):
        checks.append((idx, bool(cond), name, detail))
        print('[%d/6] %s %s%s' % (idx, 'PASS' if cond else 'FAIL', name,
                                  '' if cond or not detail else '  (%s)' % detail))

    # 1 - a blank hypothesis can never pass, however it is spelled.
    blanks = [None, '', '   ', '\t\n', '   \u00a0  ']
    got = [evaluate([], {'hypothesis': v}) for v in blanks]
    check(1, 'blank hypothesis is refused as empty_hypothesis',
          all((not r['allow']) and r['reason'] == 'empty_hypothesis' for r in got),
          'verdicts=%s' % [r['reason'] for r in got])

    base = [_attempt(1, 'scale 1.02 removes the 33 deg shoulder', True, 'd1'),
            _attempt(2, 'Zn occupancy 0.90 to 0.85', False, 'd2'),
            _attempt(3, 'background order 8', False, 'd3', hid='bg-8')]

    # 2 - same hypothesis, twice: rewritten text and reused id both count.
    r_text = evaluate(base, {'hypothesis': '  Background   ORDER 8!! ',
                             'change_digest': 'dz'})
    r_id = evaluate(base, {'hypothesis': 'an entirely different sentence',
                           'hypothesis_id': 'bg-8', 'change_digest': 'dz'})
    check(2, 'a hypothesis already used in the last 3 rounds is refused',
          (not r_text['allow']) and r_text['reason'] == 'repeated_hypothesis'
          and (not r_id['allow']) and r_id['reason'] == 'repeated_hypothesis',
          'text=%s id=%s' % (r_text['reason'], r_id['reason']))

    # 3 - the same stage+vary as the previous round, under a new sentence.
    r_same = evaluate(base, {'hypothesis': 'try the 41 deg peak again',
                             'change_digest': 'd3'})
    r_new = evaluate(base, {'hypothesis': 'profile w governs the 41 deg peak',
                            'stage': 'extras', 'vary': ['profile_w']})
    check(3, 'an unchanged change_digest is refused as identical_change',
          (not r_same['allow']) and r_same['reason'] == 'identical_change'
          and r_new['allow'],
          'same=%s new_digest=%s' % (r_same['reason'], r_new['change_digest']))

    flat = [_attempt(1, 'old guess: scale 1.02', False, 'e1'),
            _attempt(2, 'try occupancy on Zn', False, 'e2'),
            _attempt(3, 'try background order 10', False, 'e3'),
            _attempt(4, 'try profile w wider', False, 'e4'),
            _attempt(5, 'try zero shift', False, 'e5')]

    # 4 - five flat rounds, and the request repeats a hypothesis from round 1,
    #     which sits OUTSIDE the 3-round window: escalate, do not count again.
    r_old = evaluate(flat, {'hypothesis': 'OLD guess:   scale 1.02', 'change_digest': 'e6'})
    check(4, 'a flat streak plus an old duplicate is refused as escalate_to_gsas',
          (not r_old['allow']) and r_old['reason'] == 'escalate_to_gsas'
          and 'xrd_refine_gsas' in (r_old['message'] + r_old['next_step']),
          'reason=%s matched=%s' % (r_old['reason'], r_old.get('matched_round')))

    # 5 - the point of the whole design. The assertion NAME states it, because
    #     this is the rule that keeps the gate from becoming "three strikes out".
    r_fresh = evaluate(flat, {'hypothesis': 'zero shift 0.012 deg explains the '
                                           '41 deg residual asymmetry',
                              'change_digest': 'e7', 'stage': 'calibrate',
                              'vary': ['zero_shift']})
    check(5, 'directionality: a legitimate new hypothesis is ALLOWED even after '
             '3 rounds without improvement',
          r_fresh['allow'] and r_fresh['reason'] == 'ok'
          and r_fresh['counters']['no_improve_streak'] == 5,
          'allow=%s reason=%s' % (r_fresh['allow'], r_fresh['reason']))

    # 6 - record(): append-only, foreign keys intact, atomic, no temp litter,
    #     and a damaged ledger is reported rather than overwritten.
    tmpdir = _scratch_dir()
    try:
        ledger = mech_path(tmpdir, 'Y2O3')
        with open(ledger, 'w', encoding='utf-8') as fh:
            json.dump({'gsas_round_count': 7, 'rounds': [{'round': 7}],
                       'frozen': {'active': False}}, fh, indent=1)
        req = {'hypothesis': 'scale 1.02 removes the 33 deg shoulder',
               'stage': 'calibrate', 'vary': ['scale']}
        rec1 = record(tmpdir, req, rwp_before=21.8, rwp_after=20.9,
                      improved=True, sample='Y2O3', at='2026-11-06T00:00:01Z')
        rec2 = record(tmpdir, dict(req, hypothesis='Zn occupancy 0.90 to 0.85',
                                   vary=['occ_Zn']),
                      rwp_before=20.9, rwp_after=21.0, improved=False,
                      sample='Y2O3', at='2026-11-06T00:00:02Z')
        state = load_state(ledger)
        attempts = state[ATTEMPTS_KEY]
        litter = [f for f in os.listdir(tmpdir) if f.startswith('.hyp_gate_')]
        stable = fingerprint('A  B!') == fingerprint('a b') == fingerprint(' a   b ')
        with open(ledger, 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        damaged = None
        try:
            load_state(ledger)
        except GateError as exc:
            damaged = str(exc)
        check(6, 'record() appends without clobbering the shared ledger',
              os.path.basename(ledger) == 'review_mechanical_Y2O3.json'
              and rec1['n'] == 1 and rec2['n'] == 2 and len(attempts) == 2
              and attempts[1]['improved'] is False
              and state['gsas_round_count'] == 7 and state['rounds'] == [{'round': 7}]
              and state['frozen'] == {'active': False} and not litter and stable
              and damaged is not None
              and 'not valid JSON' in damaged,
              'n=%s,%s entries=%d gsas=%s litter=%s damaged=%s'
              % (rec1['n'], rec2['n'], len(attempts),
                 state.get('gsas_round_count'), litter, bool(damaged)))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    failed = [c for c in checks if not c[1]]
    print('SELFTEST %d/%d passed' % (len(checks) - len(failed), len(checks)))
    return EXIT_OK if not failed else EXIT_FAIL


# --- CLI ---------------------------------------------------------------------
def _read_request(value):
    if value is None:
        raise GateError('no --request given')
    text = str(value)
    if text.startswith('@'):
        with open(text[1:], 'r', encoding='utf-8') as fh:
            text = fh.read()
    try:
        req = json.loads(text)
    except ValueError as exc:
        raise GateError('--request is not valid JSON (%s)' % exc)
    if not isinstance(req, dict):
        raise GateError('--request must be a JSON object')
    return req


def cmd_evaluate(args):
    p = resolve_path(args.state or args.dir, args.sample)
    verdict = evaluate(load_attempts(p, args.sample), _read_request(args.request))
    print(json.dumps(verdict, ensure_ascii=True, indent=1))
    return EXIT_OK if verdict['allow'] else EXIT_DENY


def cmd_record(args):
    improved = {'true': True, 'false': False, 'unknown': None}[args.improved]
    res = record(args.state or args.dir, _read_request(args.request),
                 rwp_before=args.rwp_before, rwp_after=args.rwp_after,
                 improved=improved, at=args.at, sample=args.sample)
    print(json.dumps(res, ensure_ascii=True, indent=1))
    return EXIT_OK


def cmd_show(args):
    p = resolve_path(args.state or args.dir, args.sample)
    attempts = load_attempts(p, args.sample)
    # 0.10.0: say WHICH sample this ledger belongs to, in the ORIGINAL spelling. A non-ASCII
    # sample's file name carries a hash, so without this line a reader cannot tell which
    # sample review_mechanical_<hash>.json describes (the T-20261106-05 review found the name
    # was in the JSON but never printed).
    # Deliberately NOT ascii_safe: show is read by a HUMAN, and the "ASCII only" rule governs
    # engine output, not hiding a sample's own name from its owner.
    try:
        with open(p, encoding='utf-8') as fh:
            state = json.load(fh)
        name = state.get('sample')
    except Exception:                                        # noqa: BLE001 - show must not die
        name = None
    print('SAMPLE %s  LEDGER %s' % (name if name else '(unset)', os.path.basename(p)))
    for attempt in attempts:
        print('n=%-3s improved=%-5s stage=%-9s digest=%s  %s'
              % (attempt.get('n'), attempt.get('improved'),
                 attempt.get('stage'), attempt.get('change_digest'),
                 ascii_safe(str(attempt.get('hypothesis'))[:70])))
    print('ATTEMPTS %d  NO_IMPROVE_STREAK %d' % (len(attempts),
                                                 no_improve_streak(attempts)))
    return EXIT_OK


def build_parser():
    ap = argparse.ArgumentParser(
        prog='hypothesis_gate.py',
        description='Hypothesis-driven gate for the manual XRD engine '
                    '(decide before the run, record after it).')
    ap.add_argument('--selftest', action='store_true',
                    help='run the six assertions in a temporary directory')
    sub = ap.add_subparsers(dest='cmd')

    def common(sp):
        sp.add_argument('--dir', default=None,
                        help='directory holding review_mechanical_<sample>.json')
        sp.add_argument('--state', default=None,
                        help='explicit path to the ledger .json (overrides --dir)')
        sp.add_argument('--sample', default=None,
                        help='ledger key, same value review.js uses')

    ev = sub.add_parser('evaluate', help='decide whether a manual run may start')
    common(ev)
    ev.add_argument('--request', required=True,
                    help='JSON object, or @path to a JSON file')

    rc = sub.add_parser('record', help='append the finished round to the ledger')
    common(rc)
    rc.add_argument('--request', required=True)
    rc.add_argument('--rwp-before', type=float, default=None, dest='rwp_before')
    rc.add_argument('--rwp-after', type=float, default=None, dest='rwp_after')
    rc.add_argument('--improved', default='unknown',
                    choices=['true', 'false', 'unknown'])
    rc.add_argument('--at', default=None, help='ISO8601 timestamp (default: now)')

    sh = sub.add_parser('show', help='list the recorded attempts')
    common(sh)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.cmd:
        ap.print_help()
        return EXIT_USAGE
    handler = {'evaluate': cmd_evaluate, 'record': cmd_record,
               'show': cmd_show}[args.cmd]
    try:
        return handler(args)
    except GateError as exc:
        print('ERROR %s' % ascii_safe(exc))
        return EXIT_FAIL


if __name__ == '__main__':
    sys.exit(main())
