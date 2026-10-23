#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Process guard for the dsh-xrd engines: make OUR engine run observable,
stoppable, and impossible to leave behind as an orphan.

WHY (codex audit of 0.8.1, 2026-09-27; verified against the source tree):

  * Node killed the engine with a bare `child.kill()` (TerminateProcess on the
    direct child only). On Windows that leaves every grandchild alive, and if
    Node itself dies nobody runs `taskkill` at all. The fp2k job object covered
    only fp2k.
  * Nothing distinguished "still computing" from "stuck": the ONLY long-run
    evidence on this machine is that fp2k writes its outputs 2-3 times per run
    (measured 2026-09-28: a healthy reference run finishes in 0.25 s; two real
    samples then span at 97% CPU for 600 s AFTER their first output changed), so
    a watchdog that judges liveness on other programs' output files cannot tell
    those apart.

What this module provides — one place, standard library only:

  1. `KillOnCloseJob` — our own process is assigned to a job object with
     JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Windows assigns descendants to the
     same job, so when this process dies BY ANY MEANS (normal exit, exception,
     TerminateProcess by a dying parent) the OS closes the handle and takes the
     whole subtree with it. This is the only mechanism that survives the death
     of the launcher; `taskkill` needs a live killer.
  2. `ParentWatchdog` — polls the parent process and exits cleanly if it has
     gone (POSIX: reparenting to init / liveness probe; Windows:
     OpenProcess + GetExitCodeProcess). Belt to the job object's braces, and
     the only protection on POSIX without prctl.
  3. `Ticker` — writes `.xrd-progress.json` into the run directory every
     `tick_s`. The JS layer reads it, so "the process is alive and progressing"
     becomes an OBSERVABLE fact for OUR engines (manual LM iterations, GSAS
     refinement) instead of a guess. fp2k deliberately does NOT use this: its
     liveness is judged from its own output files, which is what the driver's
     stall rule does.
  4. `stop_flag` — a `<run>/.xrd-stop` file the JS layer writes to ask for a
     GRACEFUL stop: engines call `guard.checkpoint()` at safe points and raise
     `EngineStopped`, which lets them finish writing a structured result
     instead of being terminated mid-write.

Everything degrades honestly: when a mechanism is unavailable the guard records
that fact (`guard`, `self_assigned`, `job`) in the tick stream and in the engine
result, rather than pretending to be protected.
"""

import json
import os
import sys
import threading
import time

IS_WINDOWS = os.name == 'nt'

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

TICK_NAME = '.xrd-progress.json'
STOP_NAME = '.xrd-stop'


def tick_name(run_id):
    """Run-scoped tick file name (0.8.6).

    Two runs can share an artifact directory, and with one fixed name they overwrote
    each other's heartbeat — a healthy run then looked stalled. The legacy fixed name is
    still written alongside, so a reader that only knows that one keeps working.
    """
    return ('.xrd-progress-%s.json' % run_id) if run_id else TICK_NAME


def stop_name(run_id):
    """Run-scoped stop flag name (0.8.6); the legacy name is still honoured."""
    return ('.xrd-stop-%s' % run_id) if run_id else STOP_NAME


# The guard of the engine currently running in this interpreter, so the shared
# stop handler can find the result path without every engine plumbing it through.
_ACTIVE = None


class EngineStopped(BaseException):
    """Raised at a checkpoint when the caller asked for a graceful stop.

    **Why BaseException and not Exception (0.8.5):** a stop is CONTROL FLOW, not an
    error the engine should handle. Every engine runs its work inside `try/except
    Exception` blocks — a per-action loop in `data_io`/`cif_edit`/`phase_screen`, a
    per-run block in `fp2k_driver`, a per-step loop in the GSAS engines — so an
    EngineStopped derived from Exception was CAUGHT by those handlers and merely
    recorded as "this action/run failed", after which the engine carried on and
    finished normally. Measured: with a stop flag in place the fp2k driver exited 0
    with an ordinary error result instead of stopping. Deriving from BaseException
    (like KeyboardInterrupt/SystemExit) means no `except Exception` can swallow it,
    while `finally:` cleanup still runs and `run_guarded_main`'s explicit
    `except EngineStopped` still turns it into the structured "stopped" result.
    """


def log(*a):
    """Guard diagnostics go to stderr: stdout carries the engine payload."""
    try:
        sys.stderr.write('[guard] ' + ' '.join(str(x) for x in a) + '\n')
        sys.stderr.flush()
    except Exception:
        pass


if IS_WINDOWS:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [('ReadOperationCount', ctypes.c_ulonglong),
                    ('WriteOperationCount', ctypes.c_ulonglong),
                    ('OtherOperationCount', ctypes.c_ulonglong),
                    ('ReadTransferCount', ctypes.c_ulonglong),
                    ('WriteTransferCount', ctypes.c_ulonglong),
                    ('OtherTransferCount', ctypes.c_ulonglong)]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ('IoInfo', _IO_COUNTERS),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _create_kill_on_close_job():
    if not IS_WINDOWS:
        return None
    handle = _k32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation,
                                        ctypes.byref(info), ctypes.sizeof(info)):
        _k32.CloseHandle(handle)
        return None
    return handle


def _parent_alive(ppid):
    """Is process `ppid` still running? (Windows: exit code; POSIX: kill 0.)"""
    if ppid is None or ppid <= 0:
        return True
    if IS_WINDOWS:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(ppid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True                     # cannot tell ⇒ do not kill ourselves
            return code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(int(ppid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                             # exists, owned by someone else


class Guard(object):
    """Per-run guard: job object, parent watchdog, ticker, stop flag."""

    def __init__(self, out_dir=None, run_id=None, backend='engine', tick_s=5.0,
                 stopped_exit_code=97, progress_paths=(), out_path=None, plan=None):
        self.out_dir = out_dir
        self.run_id = run_id
        self.backend = backend
        self.tick_s = float(tick_s)
        self.stopped_exit_code = stopped_exit_code
        self.progress_paths = [p for p in (progress_paths or []) if p]
        self.out_path = out_path          # where a graceful stop writes its result
        self.plan = plan if isinstance(plan, dict) else {}
        self.seq = 0
        self.phase = 'start'
        self.detail = {}
        self._stop = threading.Event()
        self._parent_gone = threading.Event()
        self._threads = []
        self.self_assigned = None      # None = not attempted, True/False = result
        self.job = None
        self.guard_kind = 'none'
        self.ppid = os.getppid() if hasattr(os, 'getppid') else None
        self._prog_snap = None         # last (mtime, size) of the watched artifacts

    def _progress_changed(self):
        """True when one of the watched artifacts changed since the last tick.

        THIS is what makes a heartbeat mean something: for a black box (or for a
        library call that can stall inside its own C loop) the mere fact that the
        wrapper is alive says nothing. Watching the artifact the tool itself
        writes is the closest thing to real progress we can observe.
        """
        if not self.progress_paths:
            return False
        snap = []
        for p in self.progress_paths:
            try:
                st = os.stat(p)
                snap.append((round(st.st_mtime, 3), st.st_size))
            except OSError:
                snap.append(None)
        snap = tuple(snap)
        changed = self._prog_snap is not None and snap != self._prog_snap
        self._prog_snap = snap
        return changed

    # ---- lifecycle -----------------------------------------------------
    def start(self):
        if IS_WINDOWS:
            job = _create_kill_on_close_job()
            if job:
                # our own process joins the job: every descendant we spawn
                # inherits membership (Windows auto-assigns children of a job
                # member), so the subtree dies with us however we die
                ok = bool(_k32.AssignProcessToJobObject(job, _k32.GetCurrentProcess()))
                self.job = job
                self.self_assigned = ok
                self.guard_kind = 'job-object' if ok else 'job-object(unassigned)'
                log('self-assign job=%s assigned=%s' % ('on', ok))
            else:
                self.self_assigned = False
                log('job object unavailable')
        else:
            try:
                os.setsid()                        # own session / process group
                self.guard_kind = 'process-group'
                self.self_assigned = True
            except Exception as e:
                log('setsid failed: %s' % e)
                self.self_assigned = False
        self._spawn(self._watch_parent, 'parent-watchdog')
        if self.out_dir:
            self._spawn(self._watch_stop, 'stop-watch')
            self._spawn(self._tick_loop, 'ticker')
        # publish ONE tick immediately: the caller learns `guard` / `self_assigned`
        # (i.e. whether the job-object protection really took) before any progress
        # tick exists — 0.8.4, after the review showed this never reached the caller
        self.tick(force=True)
        return self

    def _spawn(self, fn, name):
        t = threading.Thread(target=fn, name=name)
        t.daemon = True
        t.start()
        self._threads.append(t)

    def close(self, reason='done'):
        self.phase = reason
        try:
            self.tick(force=True)
        except Exception:
            pass
        if self.job and IS_WINDOWS:
            try:
                _k32.CloseHandle(self.job)         # kills any surviving descendant
            except Exception:
                pass
            self.job = None

    # ---- observation ---------------------------------------------------
    def tick(self, phase=None, force=False, progress=None, **detail):
        """Record progress. Cheap: call it from hot loops, it rate-limits itself.

        `progress=True` asserts "the thing being computed really moved" (the
        manual engine does this from its residual loop). `progress=None` derives
        it from `progress_paths`; a plain heartbeat therefore never claims
        progress it cannot see.
        """
        if phase:
            self.phase = phase
        if detail:
            self.detail.update(detail)
        now = time.time()
        # REAL PROGRESS BYPASSES THE RATE LIMIT (0.8.7). A plain heartbeat tick sets
        # `_last_tick`, so the manual engine's residual-loop call (`progress=True`) that
        # follows within `tick_s` used to be DROPPED — the one tick that proves the fit is
        # advancing was swallowed by the one that proves nothing (the fourth review measured
        # "plain tick, then real progress -> still progress:false"). That can make a healthy
        # fit look stalled once the JS side stops seeing progress.
        if progress is True:
            force = True
        if not force and (now - getattr(self, '_last_tick', 0.0)) < self.tick_s:
            return
        self._last_tick = now
        self.seq += 1
        moved = self._progress_changed() if progress is None else bool(progress)
        self._write_tick(now, moved)

    def _write_tick(self, now, progress=False):
        if not self.out_dir:
            return
        payload = {'run_id': self.run_id, 'backend': self.backend, 'pid': os.getpid(),
                   'seq': self.seq, 't': round(now, 3), 'phase': self.phase,
                   'guard': self.guard_kind, 'self_assigned': self.self_assigned,
                   'progress': bool(progress),
                   'progress_paths': len(self.progress_paths),
                   'parent_alive': not self._parent_gone.is_set(),
                   'stop_requested': self._stop.is_set(),
                   'detail': dict(self.detail)}
        # run-scoped name first, legacy fixed name alongside (0.8.6) — see tick_name()
        for name in {tick_name(self.run_id), TICK_NAME}:
            path = os.path.join(self.out_dir, name)
            tmp = '%s.tmp-%d' % (path, os.getpid())
            try:
                with open(tmp, 'w', encoding='utf-8') as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception as e:
                log('tick write failed (%s): %s' % (name, e))

    # ---- control -------------------------------------------------------
    def stop_requested(self):
        if self._stop.is_set():
            return True
        if self._stop_is_mine():
            self._stop.set()
        return self._stop.is_set()

    def checkpoint(self, phase=None, **detail):
        """Call at a safe point: raises EngineStopped on a graceful-stop request.

        0.8.9: the tick is written UNCONDITIONALLY (`force=True`). A checkpoint is a step
        BOUNDARY, not a heartbeat, and it is the only record of WHICH step a run is in —
        rate-limiting it away hid exactly that. Measured 2026-09-28: a two-step GSAS
        refinement finishing in ~5 s left only the initial `phase:"start"` tick on disk
        (tick_s=5.0), so the per-step checkpoints the 0.8.5 restructure introduced were
        invisible to both the caller and the audit. The number of checkpoints is bounded
        by the number of refinement steps, so forcing every one of them is cheap.
        """
        self.tick(phase, force=True, **detail)
        if self._stop.is_set() or self._stop_is_mine():
            self._stop.set()
            raise EngineStopped('stop requested by the caller')

    def stop_flag_path(self):
        """The run-scoped flag (0.8.6); `legacy_stop_flag_path()` is still honoured."""
        return os.path.join(self.out_dir, stop_name(self.run_id)) if self.out_dir else None

    def legacy_stop_flag_path(self):
        return os.path.join(self.out_dir, STOP_NAME) if self.out_dir else None

    def _stop_is_mine(self):
        """True when a stop flag asks THIS run to stop.

        Two names are consulted (0.8.6): the run-scoped one, then the legacy fixed
        `.xrd-stop`. The caller writes `{run_id, at}` into both, so a flag naming a
        DIFFERENT run is ignored (0.8.5: the flag used to be honoured on mere existence,
        so one run's cancel stopped another), while a hand-written flag with no id at
        all still counts.
        """
        for path in (self.stop_flag_path(), self.legacy_stop_flag_path()):
            if not path:
                continue
            try:
                if not os.path.exists(path):
                    continue
                with open(path, encoding='utf-8') as fh:
                    data = json.load(fh)
                rid = data.get('run_id') if isinstance(data, dict) else None
            except FileNotFoundError:
                continue
            except Exception:
                return True                  # unreadable ⇒ treat as a stop request
            if not rid or not self.run_id or rid == self.run_id:
                return True
        return False

    def _watch_stop(self):
        while not self._stop.is_set():
            try:
                if self._stop_is_mine():
                    self._stop.set()
                    log('stop flag seen: %s (run_id match)' % self.stop_flag_path())
                    return
            except Exception:
                pass
            time.sleep(1.0)

    def _watch_parent(self):
        while not self._parent_gone.is_set():
            if not _parent_alive(self.ppid):
                self._parent_gone.set()
                log('parent %s is gone — exiting to avoid becoming an orphan' % self.ppid)
                try:
                    self._write_tick(time.time())
                except Exception:
                    pass
                # closing the job handle (at interpreter exit) takes the subtree;
                # exiting now is what makes that happen promptly
                os._exit(self.stopped_exit_code)
            time.sleep(2.0)

    def _tick_loop(self):
        while True:
            time.sleep(self.tick_s)
            if self._stop.is_set() and self.phase == 'stopped':
                return
            try:
                self.tick()
            except Exception:
                pass


def start_guard(plan, out_dir=None, backend='engine', tick_s=5.0, progress_paths=(),
                out_path=None):
    """Start a guard for one engine run. Never raises: a guard is best effort.

    `out_path` is the engine's result file: a graceful stop writes a structured
    "stopped" result there instead of leaving the caller with nothing.
    """
    global _ACTIVE
    if not isinstance(plan, dict):
        plan = {}
    out_dir = out_dir or plan.get('out_dir')
    try:
        run_id = plan.get('run_id') if isinstance(plan.get('run_id'), str) else None
        g = Guard(out_dir=out_dir, run_id=run_id, backend=backend, tick_s=tick_s,
                  progress_paths=progress_paths, out_path=out_path, plan=plan).start()
    except Exception as e:                                          # pragma: no cover
        log('guard start failed (%s) — continuing unguarded' % e)
        g = Guard(out_dir=out_dir, backend=backend, out_path=out_path, plan=plan)
    _ACTIVE = g
    return g


def active_guard():
    """The guard of the engine running in this interpreter (None when unguarded)."""
    return _ACTIVE


def write_stop_result(out_path, plan, reason='stopped on request'):
    """Leave a machine-readable trace where the caller expects a result."""
    from result_io import write_result
    return write_result(out_path, {
        'ok': False, 'stopped': True,
        'error': '%s (graceful stop at a safe point)' % reason,
    }, plan)


def run_guarded_main(main_fn):
    """Run an engine's main() with the graceful-stop contract.

    Engines end with `if __name__ == '__main__': run_guarded_main(main)` so that a
    stop request that lands at a checkpoint turns into a structured "stopped"
    result at the documented path plus a distinct exit code (97) — the caller,
    which asked for the stop, then knows the run ended ON PURPOSE. Without this
    the exception would surface as a traceback and no result file at all.
    """
    try:
        return main_fn()
    except EngineStopped as e:
        log('stopped: %s' % e)
        g = _ACTIVE
        if g is not None and g.out_path:
            try:
                write_stop_result(g.out_path, g.plan, str(e))
            except Exception as werr:                               # pragma: no cover
                log('could not write the stop result: %s' % werr)
        sys.exit(97)
