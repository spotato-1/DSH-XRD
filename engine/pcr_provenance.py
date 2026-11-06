#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provenance sidecar for FullProf .pcr control files.

WHY (this is a guard against a specific failure, not bookkeeping): the plugin
hands a .pcr to fp2k, and a .pcr is a plain text file that anybody - including
an AI agent improvising - can produce by hand. A hand-assembled .pcr looks
exactly like a generated one and can even refine to a plausible R value while
its structure block is invented. So every pcr that the generator actually
produced gets a sidecar next to it recording WHAT produced it, FROM WHICH CIF,
WITH WHICH OPTIONS, and the sha256 of the bytes as written. Verification is then
a hash comparison against that record, not a guess from the file's contents.

SIDECAR: same directory, name = <pcr file name> + ".provenance.json"
(e.g. Y2O3.pcr -> Y2O3.pcr.provenance.json), UTF-8, indent=2, sort_keys=True.

RECORD (version 1):
  {
    "version": 1,
    "kind": "generated" | "exe_fallback" | "user_provided",
    "generator": {"name": str, "version": str, "sha256": str},
    "source_cif": {"path": str, "sha256": str} | null,
    "options": {...},
    "pcr": {"path": str, "sha256": str, "size": int, "mtime": float},
    "created_at": "<ISO8601 UTC>",
    "descendants": [{"label": str, "sha256": str,
                     "parent_sha256": str, "at": "<ISO8601 UTC>"}]
  }

`pcr.path` is stored as the pcr file NAME, not an absolute path: a sidecar that
travels with its pcr (copied to another machine, or moved) must still verify.
`pcr.size` / `pcr.mtime` are recorded for TRIAGE ONLY and never decide anything
- only sha256 does. When a tool writes the pcr back through itself (a refinement
run rewrites the same file), that is a legitimate new set of bytes, so the
caller `append`s a descendant entry instead of overwriting the head.

SUBCOMMANDS
  write        --pcr P --kind K [--generator-name N] [--generator-version V]
               [--generator-sha256 H] [--cif C] [--options-json J] [--force]
                 Create the sidecar. Refuses (exit 1) when a sidecar already
                 exists unless --force, so a second, different producer cannot
                 silently take over the lineage of a file.
  append       --pcr P --label L [--force]
                 Record the CURRENT bytes of the pcr as a derived file: its
                 sha256 is stored, parent_sha256 is the chain head (the
                 pcr.sha256 written at `write` time). Existing entries are kept.
  verify       --pcr P [--json]
                 Exit 0 = verified, 1 = not verified, 2 = usage error.
  user-provide --pcr P --note T
                 Mark the pcr as kind="user_provided" (the user supplied this
                 pcr themselves and nothing generated it here).
  --selftest   Run the end-to-end check in a temporary directory.

VERIFY RULES (all five are covered by --selftest)
  1. no sidecar                              -> ok=false, reason="no provenance"
  2. hash(current pcr) == pcr.sha256         -> ok=true,  position="head"
  3. hash == some descendant.sha256          -> ok=true,  position="descendant:<label>"
     and that descendant's parent_sha256 must sit on the chain (the head or an
     EARLIER descendant's sha256); if it does not, verification fails with
     "chain broken at descendant:<label>"
  4. neither matches                         -> ok=false, reason="pcr bytes do not match any recorded hash"
  5. sidecar JSON unparseable                -> ok=false, and the reason carries
     the ORIGINAL exception text (never swallowed)

Writes are atomic (temp file in the same directory + os.replace) so a killed run
leaves either the previous sidecar or the complete new one. All console output is
ASCII; paths may be Unicode.

Usage:
    python pcr_provenance.py write --pcr Y2O3.pcr --kind generated --cif Y2O3.cif
    python pcr_provenance.py append --pcr Y2O3.pcr --label fp2k_run1
    python pcr_provenance.py verify --pcr Y2O3.pcr --json
    python pcr_provenance.py user-provide --pcr hand.pcr --note "from the beamline"
    python pcr_provenance.py --selftest
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SIDECAR_SUFFIX = '.provenance.json'
KINDS = ('generated', 'exe_fallback', 'user_provided')
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# --- small helpers ----------------------------------------------------------
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def ascii_safe(text):
    """Console/JSON-safe copy of a path or message.

    A CIF or a sample name may well carry non-ASCII characters, and the console
    (and any consumer reading our output) must not choke on them - but the
    information must survive, so non-ASCII is escaped rather than dropped.
    """
    return str(text).encode('ascii', 'backslashreplace').decode('ascii')


def sidecar_path(pcr_path):
    """<dir>/<pcr name>.provenance.json - never replaces the pcr's own suffix."""
    p = Path(pcr_path)
    return p.with_name(p.name + SIDECAR_SUFFIX)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    """sha256 of a file's exact bytes (streamed: pcr files are small, but a data
    file passed as `source` may not be)."""
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def utc_now():
    """ISO8601 UTC with a trailing Z, second resolution."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def atomic_write_json(path, data):
    """Write JSON atomically: temp file in the SAME directory, then os.replace.

    Same directory matters: os.replace is only atomic within one volume, and a
    temp file in %TEMP% could land on another one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('%s.tmp-%d' % (path.name, os.getpid()))
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def load_sidecar(pcr_path):
    """(record or None, error text or None).

    Returns (None, None) when there is no sidecar at all; (None, message) when
    the sidecar exists but cannot be read/parsed. The parse error is passed
    through verbatim - rule 5 exists so a corrupt sidecar is diagnosed, not
    hidden behind a generic "invalid".
    """
    sc = sidecar_path(pcr_path)
    if not sc.exists():
        return None, None
    try:
        with open(sc, 'r', encoding='utf-8') as fh:
            raw = fh.read()
        data = json.loads(raw)
    except Exception as e:                                   # noqa: BLE001 - reported verbatim
        return None, 'sidecar unreadable: %s: %s' % (type(e).__name__, e)
    if not isinstance(data, dict):
        return None, 'sidecar is not a JSON object (got %s)' % type(data).__name__
    return data, None


def pcr_fingerprint(pcr_path):
    """{path(name), sha256, size, mtime} of the pcr's current bytes."""
    p = Path(pcr_path)
    st = p.stat()
    return {
        'path': p.name,
        'sha256': file_sha256(p),
        'size': int(st.st_size),
        'mtime': float(st.st_mtime),
    }


# --- chain logic ------------------------------------------------------------
def _descendants(record):
    d = record.get('descendants')
    return d if isinstance(d, list) else []


def locate_hash(record, current_sha):
    """(position, reason) for a current hash against a sidecar record.

    position is "head", "descendant:<label>" or None. The descendant case also
    requires the link to be sound: parent_sha256 must be the head hash or the
    hash of an EARLIER descendant, otherwise a descendant entry could be
    attached to a chain it never belonged to.
    """
    head = (record.get('pcr') or {}).get('sha256')
    if head and current_sha == head:
        return 'head', None
    head_sha = head
    seen = set([head_sha]) if head_sha else set()
    for idx, d in enumerate(_descendants(record)):
        if not isinstance(d, dict):
            continue
        sha = d.get('sha256')
        label = ascii_safe(d.get('label') or ('#%d' % idx))
        if sha == current_sha:
            parent = d.get('parent_sha256')
            if parent and parent in seen:
                return 'descendant:%s' % label, None
            return None, ('chain broken at descendant:%s (parent_sha256 %s is not on the '
                          'chain; head is %s)' % (label, str(parent)[:16], str(head_sha)[:16]))
        # only a descendant whose own link is sound extends the chain
        if sha and (d.get('parent_sha256') in seen):
            seen.add(sha)
    return None, 'pcr bytes do not match any recorded hash'


def verify(pcr_path):
    """Full verification dict. Never raises for the five documented outcomes."""
    p = Path(pcr_path)
    result = {
        'ok': False,
        'pcr': str(p),
        'sidecar': str(sidecar_path(p)),
        'position': None,
        'reason': None,
    }
    if not p.is_file():
        result['reason'] = 'pcr not found'
        return result

    record, err = load_sidecar(p)
    if record is None and err is None:                        # rule 1
        result['reason'] = 'no provenance'
        return result
    if err:                                                   # rule 5
        result['reason'] = err
        return result

    current = pcr_fingerprint(p)
    result['sha256'] = current['sha256']
    result['kind'] = record.get('kind')
    result['generator'] = record.get('generator')
    result['version'] = record.get('version')

    position, reason = locate_hash(record, current['sha256'])
    if position:                                              # rules 2 and 3
        result['ok'] = True
        result['position'] = position
        rec = (record.get('pcr') or {})
        # triage only: the recorded size/mtime never decide anything, they are
        # echoed so a stale copy of the pcr (right bytes, different file) is
        # visible next to the hash
        result['size_match'] = (rec.get('size') == current['size'])
        result['recorded_mtime'] = rec.get('mtime')
        return result

    result['reason'] = reason                                 # rules 3(broken) and 4
    known = []
    head = (record.get('pcr') or {}).get('sha256')
    if head:
        known.append({'role': 'head', 'sha256': head})
    for d in _descendants(record):
        if isinstance(d, dict) and d.get('sha256'):
            known.append({'role': 'descendant:%s' % (d.get('label') or ''),
                          'sha256': d.get('sha256')})
    result['recorded_hashes'] = known
    return result


# --- sidecar writers --------------------------------------------------------
def _generator_dict(args):
    return {
        'name': args.generator_name or '',
        'version': args.generator_version or '',
        'sha256': args.generator_sha256 or '',
    }


def _parse_options(raw):
    """--options-json: a JSON object string, OR '@path' naming a JSON file."""
    if raw is None:
        return {}
    text = raw
    if raw.startswith('@'):
        text = Path(raw[1:]).read_text(encoding='utf-8')
    val = json.loads(text)
    if not isinstance(val, dict):
        raise ValueError('--options-json must be a JSON object, got %s' % type(val).__name__)
    return val


def cmd_write(args):
    pcr = Path(args.pcr)
    if not pcr.is_file():
        log('PROVENANCE write FAILED: pcr not found: %s' % ascii_safe(pcr))
        return EXIT_FAIL
    if args.kind not in KINDS:
        log('PROVENANCE write FAILED: --kind must be one of %s' % ', '.join(KINDS))
        return EXIT_USAGE
    sc = sidecar_path(pcr)
    if sc.exists() and not args.force:
        log('PROVENANCE write REFUSED: sidecar already exists (%s); pass --force to '
            'overwrite the lineage' % ascii_safe(sc))
        return EXIT_FAIL
    try:
        options = _parse_options(args.options_json)
    except Exception as e:                                    # noqa: BLE001
        log('PROVENANCE write FAILED: bad --options-json: %s: %s' % (type(e).__name__, e))
        return EXIT_USAGE

    source_cif = None
    if args.cif:
        cif = Path(args.cif)
        if not cif.is_file():
            log('PROVENANCE write FAILED: cif not found: %s' % ascii_safe(cif))
            return EXIT_FAIL
        # DATA field: keep the real filename. `ascii_safe` is a DISPLAY helper
        # (backslash-escapes non-ASCII) and must never reach the sidecar, or a
        # Chinese CIF name is recorded as literal "\u6c27\u5316..." text that no
        # one can resolve back to a file. The sidecar is dumped with
        # ensure_ascii=False, so JSON itself handles the Unicode fine.
        source_cif = {'path': cif.name, 'sha256': file_sha256(cif)}

    record = {
        'version': SCHEMA_VERSION,
        'kind': args.kind,
        'generator': _generator_dict(args),
        'source_cif': source_cif,
        'options': options,
        'pcr': pcr_fingerprint(pcr),
        'created_at': utc_now(),
        'descendants': [],
    }
    if args.note:
        record['note'] = args.note
    try:
        atomic_write_json(sc, record)
    except Exception as e:                                    # noqa: BLE001
        log('PROVENANCE write FAILED: %s: %s' % (type(e).__name__, e))
        return EXIT_FAIL
    print('wrote %s' % ascii_safe(sc))
    print('kind=%s pcr_sha256=%s' % (record['kind'], record['pcr']['sha256']))
    return EXIT_OK


def cmd_append(args):
    pcr = Path(args.pcr)
    if not pcr.is_file():
        log('PROVENANCE append FAILED: pcr not found: %s' % ascii_safe(pcr))
        return EXIT_FAIL
    record, err = load_sidecar(pcr)
    if err:
        log('PROVENANCE append FAILED: %s' % ascii_safe(err))
        return EXIT_FAIL
    if record is None:
        log('PROVENANCE append REFUSED: no provenance sidecar for %s (write one first)'
            % ascii_safe(pcr))
        return EXIT_FAIL
    label = (args.label or '').strip()
    if not label:
        log('PROVENANCE append FAILED: --label must not be empty')
        return EXIT_USAGE

    current = pcr_fingerprint(pcr)
    head = (record.get('pcr') or {}).get('sha256') or ''
    ents = _descendants(record)
    # a repeated drain of an already-recorded state must not append duplicates
    for d in ents:
        if isinstance(d, dict) and d.get('sha256') == current['sha256']:
            print('already recorded as descendant:%s (sha256=%s) - nothing appended'
                  % (ascii_safe(d.get('label')), current['sha256']))
            return EXIT_OK

    entry = {
        'label': label,
        'sha256': current['sha256'],
        'parent_sha256': head,
        'at': utc_now(),
    }
    ents.append(entry)
    record['descendants'] = ents
    record.setdefault('version', SCHEMA_VERSION)
    try:
        atomic_write_json(sidecar_path(pcr), record)
    except Exception as e:                                    # noqa: BLE001
        log('PROVENANCE append FAILED: %s: %s' % (type(e).__name__, e))
        return EXIT_FAIL
    print('appended descendant:%s (sha256=%s, parent=%s)'
          % (ascii_safe(label), current['sha256'], head))
    return EXIT_OK


def cmd_verify(args):
    result = verify(args.pcr)
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print('provenance: %s' % ('OK' if result['ok'] else 'FAIL'))
        print('  pcr      : %s' % ascii_safe(result['pcr']))
        print('  sidecar  : %s' % ascii_safe(result['sidecar']))
        if result.get('kind'):
            print('  kind     : %s' % result['kind'])
        if result.get('position'):
            print('  position : %s' % result['position'])
        if result.get('reason'):
            print('  reason   : %s' % ascii_safe(result['reason']))
        if result.get('sha256'):
            print('  sha256   : %s' % result['sha256'])
    return EXIT_OK if result['ok'] else EXIT_FAIL


def cmd_user_provide(args):
    pcr = Path(args.pcr)
    if not pcr.is_file():
        log('PROVENANCE user-provide FAILED: pcr not found: %s' % ascii_safe(pcr))
        return EXIT_FAIL
    record, err = load_sidecar(pcr)
    if err:
        log('PROVENANCE user-provide FAILED: %s' % ascii_safe(err))
        return EXIT_FAIL
    if record is None:
        record = {
            'version': SCHEMA_VERSION,
            'generator': {'name': '', 'version': '', 'sha256': ''},
            'source_cif': None,
            'options': {},
            'created_at': utc_now(),
            'descendants': [],
        }
    # the current bytes are what the user is handing over: they become the head,
    # and any previously recorded derived state is kept as descendants untouched
    record['version'] = SCHEMA_VERSION
    record['kind'] = 'user_provided'
    record['note'] = args.note or ''
    record['pcr'] = pcr_fingerprint(pcr)
    record.setdefault('descendants', [])
    try:
        atomic_write_json(sidecar_path(pcr), record)
    except Exception as e:                                    # noqa: BLE001
        log('PROVENANCE user-provide FAILED: %s: %s' % (type(e).__name__, e))
        return EXIT_FAIL
    print('marked user_provided: %s (sha256=%s)' % (ascii_safe(sidecar_path(pcr)),
                                                    record['pcr']['sha256']))
    return EXIT_OK


# --- selftest ---------------------------------------------------------------
def _selftest():
    """End-to-end check of the whole contract, in a throwaway directory.

    The scratch directory is created with os.mkdir + a uuid suffix, NOT with
    tempfile.mkdtemp: measured under this environment's file sandbox, a directory
    made by mkdtemp is itself not writable (any file created inside it raises
    PermissionError errno 13), while an os.mkdir directory under the same
    %TEMP% base is fully writable. The selftest must exercise the module, not
    the sandbox, so it creates its directory the way that works.
    """
    import uuid
    checks = []

    def check(name, cond, detail=''):
        checks.append((name, bool(cond), detail))
        print('%s %s%s' % ('PASS' if cond else 'FAIL', name,
                           ('  [' + detail + ']') if detail else ''))

    base = Path(tempfile.gettempdir())
    tmpdir = base / ('pcr_prov_selftest_' + uuid.uuid4().hex[:10])
    tmpdir.mkdir()
    try:
        root = Path(tmpdir)
        pcr = root / 'FAKE_test.pcr'
        cif = root / 'FAKE_test.cif'
        pcr.write_text('! FullProf pcr (selftest)\nFAKE_test 1 1.0 1.0 1.0\n', encoding='ascii')
        cif.write_text('data_FAKE\n_cell_length_a 5.0\n', encoding='ascii')

        ns = argparse.Namespace
        # 1. write
        rc = cmd_write(ns(pcr=str(pcr), kind='generated', generator_name='cif_to_pcr',
                          generator_version='0.9.0', generator_sha256='deadbeef',
                          cif=str(cif), options_json='{"wavelength":1.54051}',
                          force=False, note=None))
        sc = sidecar_path(pcr)
        check('write creates sidecar', rc == 0 and sc.is_file(), 'rc=%d' % rc)
        rec = json.loads(sc.read_text(encoding='utf-8'))
        check('sidecar is valid JSON version=1', rec.get('version') == 1)
        check('sidecar records the cif', (rec.get('source_cif') or {}).get('sha256') == file_sha256(cif))
        check('sidecar keys sorted', sc.read_text(encoding='utf-8').find('"created_at"')
              < sc.read_text(encoding='utf-8').find('"descendants"'))

        # 2. verify -> head
        rc = cmd_verify(ns(pcr=str(pcr), json=True))
        res = verify(pcr)
        check('verify head ok', rc == 0 and res['ok'] and res['position'] == 'head',
              'position=%s' % res['position'])

        # 3. rewrite the file to simulate a derived pcr, THEN record it.
        #    Order matters and it is the real workflow's order: a refinement run
        #    rewrites the pcr first, and only then does the caller append the new
        #    bytes. Appending BEFORE the rewrite would record the head's own hash
        #    as a descendant (a self-link), which proves nothing.
        pcr.write_text('! FullProf pcr (selftest, refined)\nFAKE_test 1 1.0002 1.0 1.0\n',
                       encoding='ascii')
        derived_sha = file_sha256(pcr)
        rc = cmd_verify(ns(pcr=str(pcr), json=True))
        res = verify(pcr)
        check('unrecorded rewrite fails (rule 4)', rc == 1 and not res['ok']
              and res['reason'] == 'pcr bytes do not match any recorded hash',
              'reason=%s' % ascii_safe(res['reason']))
        rc = cmd_append(ns(pcr=str(pcr), label='fp2k_run1'))
        check('append returns 0', rc == 0, 'rc=%d' % rc)
        rc = cmd_verify(ns(pcr=str(pcr), json=True))
        res = verify(pcr)
        check('verify descendant ok', rc == 0 and res['ok']
              and res['position'] == 'descendant:fp2k_run1',
              'position=%s' % res['position'])
        rec = json.loads(sc.read_text(encoding='utf-8'))
        ent = rec['descendants'][0]
        check('descendant sha is the derived bytes', ent['sha256'] == derived_sha)
        check('descendant parent is the head',
              ent['parent_sha256'] == rec['pcr']['sha256'])
        check('append kept the head entry', rec['pcr']['sha256'] != derived_sha)

        # a second derived state becomes another link, whose parent is still the head
        pcr.write_text('! FullProf pcr (selftest, refined twice)\nFAKE_test 1 1.0004 1.0 1.0\n',
                       encoding='ascii')
        rc = cmd_append(ns(pcr=str(pcr), label='gsas_run2'))
        res = verify(pcr)
        check('second descendant verifies', rc == 0 and res['ok']
              and res['position'] == 'descendant:gsas_run2', 'position=%s' % res['position'])
        # appending the same bytes twice must not duplicate the entry
        n_before = len(json.loads(sc.read_text(encoding='utf-8'))['descendants'])
        rc = cmd_append(ns(pcr=str(pcr), label='gsas_run2_repeat'))
        n_after = len(json.loads(sc.read_text(encoding='utf-8'))['descendants'])
        check('duplicate append is a no-op', rc == 0 and n_after == n_before,
              'entries %d -> %d' % (n_before, n_after))

        # an unrelated file, hash of nothing recorded -> rule 4
        pcr.write_text('! tampered\n', encoding='ascii')
        rc = cmd_verify(ns(pcr=str(pcr), json=True))
        res = verify(pcr)
        check('tampered pcr fails', rc == 1 and not res['ok']
              and res['reason'] == 'pcr bytes do not match any recorded hash',
              'reason=%s' % ascii_safe(res['reason']))

        # rule 3 (second half): a descendant whose parent is not on the chain must
        # NOT verify. The pcr holds the fp2k_run1 bytes, so only that entry can
        # match - re-point its parent at a stranger hash and it must be rejected.
        rec = json.loads(sc.read_text(encoding='utf-8'))
        pcr.write_text('! FullProf pcr (selftest, refined)\nFAKE_test 1 1.0002 1.0 1.0\n',
                       encoding='ascii')
        bad = json.loads(json.dumps(rec))
        bad['descendants'][0]['parent_sha256'] = 'f' * 64
        sc.write_text(json.dumps(bad, indent=2, sort_keys=True), encoding='utf-8')
        res = verify(pcr)
        check('broken chain is rejected', not res['ok']
              and str(res['reason']).startswith('chain broken at descendant:'),
              'reason=%s' % ascii_safe(str(res['reason'])[:48]))
        sc.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding='utf-8')

        # rule 5: corrupt sidecar -> original exception text is reported
        sc.write_text('{ this is not json', encoding='utf-8')
        rc = cmd_verify(ns(pcr=str(pcr), json=True))
        res = verify(pcr)
        check('corrupt sidecar reports the parser error', rc == 1 and not res['ok']
              and res['reason'].startswith('sidecar unreadable:')
              and 'JSONDecodeError' in res['reason'],
              'reason=%s' % ascii_safe(res['reason'])[:60])

        # rule 1: no sidecar
        sc.unlink()
        rc = cmd_verify(ns(pcr=str(pcr), json=True))
        res = verify(pcr)
        check('missing sidecar -> no provenance', rc == 1 and not res['ok']
              and res['reason'] == 'no provenance', 'reason=%s' % res['reason'])

        # user_provide path
        rc = cmd_user_provide(ns(pcr=str(pcr), note='hand supplied'))
        res = verify(pcr)
        check('user-provide marks kind and verifies head', rc == 0 and res['ok']
              and res['kind'] == 'user_provided' and res['position'] == 'head')

        # write refuses to overwrite without --force
        rc = cmd_write(ns(pcr=str(pcr), kind='generated', generator_name='x',
                          generator_version='1', generator_sha256='', cif=None,
                          options_json=None, force=False, note=None))
        check('write without --force is refused', rc == 1, 'rc=%d' % rc)

        # non-ASCII path survives
        uni = root / ('Y2O3_\u6837\u54c1.pcr')
        uni.write_text('! ascii body\n', encoding='ascii')
        rc = cmd_write(ns(pcr=str(uni), kind='exe_fallback', generator_name='CIFs_to_PCR',
                          generator_version='', generator_sha256='', cif=None,
                          options_json=None, force=False, note=None))
        res = verify(uni)
        check('unicode path works', rc == 0 and res['ok'] and res['position'] == 'head')
        check('no temp files left behind',
              not any(p.name.startswith('.') or '.tmp-' in p.name for p in root.iterdir()))
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    n_fail = sum(1 for _n, ok, _d in checks if not ok)
    print('SELFTEST %d/%d passed' % (len(checks) - n_fail, len(checks)))
    return EXIT_OK if n_fail == 0 else EXIT_FAIL


# --- CLI --------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog='pcr_provenance.py',
        description='Provenance sidecar for FullProf .pcr files (write/append/verify).')
    ap.add_argument('--selftest', action='store_true',
                    help='run the end-to-end self test in a temporary directory')
    sub = ap.add_subparsers(dest='cmd')

    w = sub.add_parser('write', help='create the sidecar for a pcr')
    w.add_argument('--pcr', required=True)
    w.add_argument('--kind', required=True, choices=KINDS)
    w.add_argument('--generator-name', default='')
    w.add_argument('--generator-version', default='')
    w.add_argument('--generator-sha256', default='')
    w.add_argument('--cif', default=None)
    w.add_argument('--options-json', default=None,
                   help='JSON object string, or @path to a JSON file')
    w.add_argument('--force', action='store_true')
    w.add_argument('--note', default=None)

    a = sub.add_parser('append', help='record the current pcr bytes as a descendant')
    a.add_argument('--pcr', required=True)
    a.add_argument('--label', required=True)
    a.add_argument('--force', action='store_true',
                   help='accepted for symmetry; duplicates are always refused')

    v = sub.add_parser('verify', help='verify the pcr against its sidecar')
    v.add_argument('--pcr', required=True)
    v.add_argument('--json', action='store_true')

    u = sub.add_parser('user-provide', help='mark the pcr as user supplied')
    u.add_argument('--pcr', required=True)
    u.add_argument('--note', required=True)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.cmd:
        ap.print_help()
        return EXIT_USAGE
    handler = {
        'write': cmd_write,
        'append': cmd_append,
        'verify': cmd_verify,
        'user-provide': cmd_user_provide,
    }[args.cmd]
    return handler(args)


if __name__ == '__main__':
    sys.exit(main())