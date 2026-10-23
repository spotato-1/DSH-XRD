#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Result-file I/O shared by every dsh-xrd engine: atomic write + run identity.

WHY (codex audit of 0.8.1, 2026-09-27; re-verified against the source tree):
seven engine entry points wrote their result with a plain `open(path, 'w')`,
and the JS layer read that fixed-name file back with no way to tell THIS run's
result from one an earlier run had left behind. A timed-out or crashed run
could therefore report the previous run's numbers as its own — the same bug
class that had already been fixed for the manual engine by a pre-run mtime
snapshot, but nowhere else.

Two remedies, both here so every engine behaves identically:

  * `write_result()` dumps to `<path>.tmp-<pid>` and `os.replace()`s it onto
    the final name. `os.replace` is atomic on POSIX and Windows (same volume),
    so a reader never observes a half-written file and a killed run leaves
    either the previous file or the complete new one — never a torn one.
  * `run_id` — the caller (JS) puts a fresh id into the plan; the engine echoes
    it into the result. The reader accepts a result only when that id matches
    the one it issued. This is a stronger gate than mtime alone (mtime can
    advance for a file a dying process wrote part of) and it costs nothing:
    the id travels inside the plan JSON the engine already reads.

No tool output schema changes: the JS reader consumes `run_id` and deletes it
before the payload is returned to the model.
"""

import json
import os
import sys

RUN_ID_KEY = 'run_id'


def run_id_of(plan):
    """The run id this invocation was launched with (None for older callers)."""
    if not isinstance(plan, dict):
        return None
    rid = plan.get(RUN_ID_KEY)
    return rid if isinstance(rid, str) and rid else None


def _finite(value, path=()):
    """Replace non-finite floats with None, counting them (0.8.7).

    NaN and Infinity are NOT valid JSON: `json.dump` writes them as bare `NaN`/`Infinity`
    tokens, and the JS side reads the result with `JSON.parse`, which REJECTS the whole
    file — so one diverged fit parameter could make an entire run's result unreadable
    (the fourth review's finding). `allow_nan=True` was the default here, which is exactly
    how that happened. Non-finite numbers become `null` (the tool layer already drops
    absent/null fields from its payloads) and a line naming them goes to stderr, which the
    guard captures into the run's log tail.
    """
    if isinstance(value, float) and (value != value or value in (float('inf'), float('-inf'))):
        return None, ['/'.join(str(p) for p in path)]
    if isinstance(value, dict):
        out, bad = {}, []
        for k, v in value.items():
            nv, nb = _finite(v, path + (k,))
            out[k] = nv
            bad += nb
        return out, bad
    if isinstance(value, (list, tuple)):
        out, bad = [], []
        for i, v in enumerate(value):
            nv, nb = _finite(v, path + (i,))
            out.append(nv)
            bad += nb
        return (out if isinstance(value, list) else tuple(out)), bad
    return value, []


def write_result(path, payload, plan=None, indent=1, ensure_ascii=False,
                 allow_nan=False):
    """Atomically write an engine result, stamping this run's id when known.

    `allow_nan` defaults to **False** (0.8.7): a payload that still holds a non-finite
    value is sanitised to `null` so the file is strict JSON and stays readable. Pass
    `allow_nan=True` only for a deliberately non-strict dump.

    Returns the dict that was written (payload + `run_id`), so a caller that
    wants to print or post-process the stamped payload can use it.
    """
    data = dict(payload) if isinstance(payload, dict) else {'result': payload}
    rid = run_id_of(plan)
    if rid:
        data[RUN_ID_KEY] = rid
    if not allow_nan:
        data, bad = _finite(data)
        if bad:
            try:
                sys.stderr.write('[result_io] %d non-finite value(s) written as null: %s\n'
                                 % (len(bad), ', '.join(bad[:10])))
            except Exception:
                pass
    tmp = '%s.tmp-%d' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=ensure_ascii, indent=indent,
                      default=float, allow_nan=allow_nan)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        # never leave the scratch file behind for the next run to trip over
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return data
