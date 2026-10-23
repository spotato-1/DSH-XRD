#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Orphan protection for the fp2k child processes spawned by fp2k_driver.

WHY (measured incident, 2026-09-25): two `fp2k.exe` processes were left running
after their python driver was killed mid-ladder (the tool call was interrupted).
They had no parent any more and kept spinning at ~100% of one CPU core for
**10.3 h** and **8.8 h**; their outputs were zero-progress (`<stem>.out` 2 B,
`<stem>.sum` 2 B, `<stem>.prf` 0 B). On this 8-core machine that pinned 2 cores
and starved the desktop renderer until the GUI sidebar stopped responding.

Root cause: `subprocess.run(..., timeout=...)` only kills the child while the
DRIVER is alive to execute the timeout branch. Windows does not kill
grandchildren when a parent dies, and the driver held no job object, so an
interrupted call (stop button, cancelled turn, engine recycled by the host,
reboot of the driver) left fp2k running forever. Nothing tracked or swept it.

This module provides the two remedies chosen by the user (2026-09-25, "A+B"):

  * `run_guarded()`  -- spawn fp2k inside a Windows Job Object configured with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The job handle is held by this process
    and closed by the OS when this process dies, so **the child dies with the
    driver no matter how the driver dies** (normal exit, exception, or
    TerminateProcess). On POSIX the child gets its own session/process group
    and the whole group is killed on timeout.
  * `sweep_orphans()` -- find `fp2k` processes whose parent is gone and
    terminate them. This heals anything an earlier build already leaked (it is
    deliberately restricted to parentless processes so a live driver's child is
    never touched).

Only the standard library is used (ctypes / os / time): the shipped package must
not gain a new dependency for this. Both functions degrade gracefully — if the
platform API is unavailable, `run_guarded` falls back to plain subprocess
semantics and `sweep_orphans` reports what it could not do instead of raising.
"""

import os
import re
import subprocess
import sys
import time

IS_WINDOWS = os.name == 'nt'

# --- Windows API surface (ctypes, stdlib only) -------------------------------
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = -1

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

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD),
                    ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]

    class _FILETIME(ctypes.Structure):
        _fields_ = [('dwLowDateTime', wintypes.DWORD),
                    ('dwHighDateTime', wintypes.DWORD)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    # 0.8.5: read a process's image path so the orphan sweep can require a path match
    # instead of trusting the executable NAME.
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                                     ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]


def log(*a):
    """Write a guard line to STDERR.

    stderr, not stdout: the driver prints its result JSON payload on stdout, so
    anything written there could be mistaken for payload by a caller that reads
    the stream. The driver's own log() also uses stderr.
    """
    try:
        sys.stderr.write('[guard] ' + ' '.join(str(x) for x in a) + '\n')
        sys.stderr.flush()
    except Exception:
        pass


_NAN_R_LINE = re.compile(r'(Conventional Rietveld|Rwp).*nan', re.IGNORECASE)


def r_line_is_nan(out_path):
    """True when the R-factor line fp2k already wrote is NaN.

    Measured on two real samples (2026-09-28): fp2k writes
    `=> Conventional Rietveld Rp,Rwp,Re and Chi2:  NaN  NaN  25.5  NaN`
    within ~1 s and then spins at ~97% of one core indefinitely. The refinement
    is already over and meaningless, so this is worth detecting directly rather
    than waiting for the stall window to expire.
    """
    try:
        with open(out_path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if _NAN_R_LINE.search(line):
                    return True
    except OSError:
        return False
    return False


def platform_guard():
    """Return which mechanism protects fp2k children on this platform."""
    if IS_WINDOWS:
        return 'job-object'
    if hasattr(os, 'killpg'):
        return 'process-group'
    return 'none'


# --- A: kill-on-parent-death spawn ------------------------------------------
def _new_kill_on_close_job():
    """Create a job object that kills its members when the last handle closes."""
    handle = _k32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _k32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation,
                                      ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        _k32.CloseHandle(handle)
        return None
    return handle


def child_cpu_seconds(p):
    """CPU seconds consumed by a Popen child so far (None when unavailable)."""
    try:
        if IS_WINDOWS:
            creation, exit_t, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            if not _k32.GetProcessTimes(int(p._handle), ctypes.byref(creation),
                                        ctypes.byref(exit_t), ctypes.byref(kernel),
                                        ctypes.byref(user)):
                return None
            to_s = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
            return to_s(kernel) + to_s(user)
        with open('/proc/%d/stat' % p.pid, 'rb') as fh:
            fields = fh.read().decode('utf-8', 'replace')
        close = fields.rfind(')')
        parts = fields[close + 2:].split()
        hertz = os.sysconf('SC_CLK_TCK')
        return (int(parts[11]) + int(parts[12])) / hertz
    except Exception:
        return None


def _monitor(p, timeout, watch, stall_s, poll_s, kill, nan_probe=None):
    """Wait for `p` while judging progress on `watch`. Returns (timed_out, stalled, kind, secs).

    C (user decision 2026-09-25): fp2k can HANG — measured twice on real data as
    ~100% of one core with `<stem>.out` stuck at 2 B and `<stem>.prf` at 0 B, and
    the same signature cost 10.3 h / 8.8 h in the orphan incident. Waiting for the
    full per-run timeout on every attempt wastes the whole window, so progress is
    judged directly: if the watched output files stop growing for `stall_s`, the
    run is killed early. Two stall kinds are distinguished:
      * 'busy'    -- CPU keeps climbing while output does not: the classic
                     diverging/gone-nowhere fp2k run (what we measured);
      * 'waiting' -- no CPU and no output: fp2k is sitting on a prompt/EOF.

    D (2026-09-28): a THIRD signature was measured on real samples — fp2k writes
    an R-factor line whose values are NaN and then keeps spinning at ~97% of one
    core for as long as it is allowed to. That is already a finished, meaningless
    refinement, so it is detected from the file itself and killed at once instead
    of waiting out the stall window ('nan_result').
    """
    t0 = time.time()

    def snap():
        rows = []
        for w in watch:
            try:
                st = os.stat(w)
                rows.append((round(st.st_mtime, 3), st.st_size))
            except OSError:
                rows.append(None)
        return tuple(rows)

    last = snap()
    last_change = t0
    cpu0 = child_cpu_seconds(p)
    while True:
        if p.poll() is not None:
            return False, False, None, time.time() - t0
        try:
            p.wait(timeout=poll_s)
            return False, False, None, time.time() - t0
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        cur = snap()
        if cur != last:
            last = cur
            last_change = now
        if nan_probe and r_line_is_nan(nan_probe):
            kill()
            return False, True, 'nan_result', now - t0
        if now - t0 >= timeout:
            kill()
            return True, False, None, now - t0
        if stall_s and (now - last_change) >= stall_s:
            cpu1 = child_cpu_seconds(p)
            elapsed = max(now - t0, 1e-6)
            busy = (cpu0 is not None and cpu1 is not None
                    and (cpu1 - cpu0) > 0.4 * elapsed)
            kill()
            return False, True, ('busy' if busy else 'waiting'), now - last_change


def run_guarded(argv, cwd, timeout, capture_limit=4000, watch=(), stall_s=None,
                poll_s=2.0, nan_probe=None):
    """Run `argv` so that it cannot outlive this process.

    Returns a dict: {returncode, timed_out, stalled, stall_kind, stall_after_s,
    output, pid, guard, assigned}.
    `output` is the child's merged stdout+stderr, truncated to the tail (empty
    when progress monitoring is on, because then stdio is /dev/null and the
    driver reads the .out file itself).
    """
    watch = [w for w in watch if w]
    monitor = bool(watch) and bool(stall_s)

    if IS_WINDOWS:
        job = _new_kill_on_close_job()
        assigned = None

        def kill():
            # Terminate the whole job when we own one AND the child is really in it. An
            # EMPTY job object accepts TerminateJobObject and returns TRUE, so keying the
            # decision on that return value made a failed assignment look like a
            # successful whole-tree kill and the direct p.kill() never ran — the third
            # review's finding (the child then survived until the 10 s wait expired).
            done = False
            if job and assigned:
                try:
                    done = bool(_k32.TerminateJobObject(job, 1))
                except Exception:
                    done = False
            if not done:
                try:
                    p.kill()
                except Exception:
                    pass

        if monitor:
            p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            assigned = bool(job) and bool(_k32.AssignProcessToJobObject(job, int(p._handle)))
            log('fp2k pid=%s job=%s assigned=%s watch=%d stall=%ss'
                % (p.pid, 'on' if job else 'off', assigned, len(watch), stall_s))
            try:
                timed_out, stalled, kind, secs = _monitor(p, timeout, watch, stall_s, poll_s,
                                                          kill, nan_probe)
            finally:
                if p.poll() is None:
                    try:
                        p.wait(timeout=10)
                    except Exception as e:
                        log('child %s did not exit within 10 s of the kill: %s' % (p.pid, e))
                        try:
                            p.kill()
                        except Exception:
                            pass
                if job:
                    _k32.CloseHandle(job)
            return {'returncode': p.returncode, 'timed_out': timed_out,
                    'stalled': stalled, 'stall_kind': kind, 'stall_after_s': secs,
                    'output': '', 'pid': p.pid, 'guard': 'job-object',
                    'assigned': assigned, 'alive_after_kill': p.poll() is None}

        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        assigned = bool(job) and bool(_k32.AssignProcessToJobObject(job, int(p._handle)))
        log('fp2k pid=%s job=%s assigned=%s' % (p.pid, 'on' if job else 'off', assigned))
        timed_out = False
        out = ''
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill()
            try:
                p.kill()
            except Exception:
                pass
            try:
                out, _ = p.communicate(timeout=10)
            except Exception:
                out = ''
        finally:
            if job:
                _k32.CloseHandle(job)
        return {'returncode': p.returncode, 'timed_out': timed_out,
                'stalled': False, 'stall_kind': None, 'stall_after_s': None,
                'output': (out or '')[-capture_limit:], 'pid': p.pid,
                'guard': 'job-object', 'assigned': assigned}

    # POSIX: own session => own process group => killpg reaches every descendant.
    def kill():
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    if monitor:
        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        log('fp2k pid=%s guard=process-group watch=%d stall=%ss' % (p.pid, len(watch), stall_s))
        timed_out, stalled, kind, secs = _monitor(p, timeout, watch, stall_s, poll_s, kill,
                                                  nan_probe)
        if p.poll() is None:
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        return {'returncode': p.returncode, 'timed_out': timed_out, 'stalled': stalled,
                'stall_kind': kind, 'stall_after_s': secs, 'output': '', 'pid': p.pid,
                'guard': 'process-group', 'assigned': True}

    p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         start_new_session=True)
    log('fp2k pid=%s guard=process-group' % p.pid)
    timed_out = False
    out = ''
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill()
        try:
            out, _ = p.communicate(timeout=10)
        except Exception:
            out = ''
    return {'returncode': p.returncode, 'timed_out': timed_out, 'stalled': False,
            'stall_kind': None, 'stall_after_s': None,
            'output': (out or '')[-capture_limit:], 'pid': p.pid,
            'guard': 'process-group', 'assigned': True}


# --- B: sweep parentless leftovers (self-heal) ------------------------------
def _list_processes_windows():
    """Return [{pid, ppid, name, created}] from a Toolhelp snapshot."""
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE or not snap:
        raise OSError('CreateToolhelp32Snapshot failed')
    rows = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append({'pid': int(entry.th32ProcessID),
                         'ppid': int(entry.th32ParentProcessID),
                         'name': entry.szExeFile})
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    for r in rows:
        r['created'] = _process_creation_time(r['pid'])
    return rows


def _process_creation_time(pid):
    """Windows FILETIME -> epoch seconds, or None when unavailable."""
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_t, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not _k32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 1e7 - 11644473600.0  # 1601-01-01 -> 1970-01-01
    finally:
        _k32.CloseHandle(handle)


def _list_processes_posix():
    rows = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % name, 'rb') as fh:
                stat = fh.read().decode('utf-8', 'replace')
            close = stat.rfind(')')
            fields = stat[close + 2:].split()
            ppid = int(fields[1])
            start_ticks = int(fields[19])
            with open('/proc/uptime') as fh:
                uptime = float(fh.read().split()[0])
            hertz = os.sysconf('SC_CLK_TCK')
            created = time.time() - (uptime - start_ticks / hertz)
            rows.append({'pid': int(name), 'ppid': ppid,
                         'name': stat[stat.find('(') + 1:close], 'created': created})
        except Exception:
            continue
    return rows


def _terminate_windows(pid):
    handle = _k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_k32.TerminateProcess(handle, 1))
    finally:
        _k32.CloseHandle(handle)


def _process_image_path(pid):
    """Best-effort absolute path of a process's executable, or None (0.8.5).

    Needed to make the orphan sweep CONSERVATIVE: matching the bare name
    "fp2k.exe" also matches an fp2k the user started by hand in another project, and
    killing that is worse than leaving an orphan behind. When the path cannot be read
    the process is left alone.
    """
    if IS_WINDOWS:
        try:
            h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return None
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return buf.value or None
                return None
            finally:
                _k32.CloseHandle(h)
        except Exception:
            return None
    try:
        return os.readlink('/proc/%d/exe' % int(pid))
    except Exception:
        return None


def sweep_orphans(min_age_s=300, dry_run=False, exe_names=('fp2k.exe', 'fp2k'),
                  allow_paths=()):
    """Terminate fp2k processes whose parent no longer exists.

    A live driver always has a live parent, so its child is never a candidate.
    `min_age_s` is a second, independent safety margin (a process younger than
    this is left alone even if it looks parentless).

    NAME IS NOT ENOUGH (0.8.5): the review pointed out that killing on name + parent
    + age alone can hit another project's — or the user's own hand-started — fp2k.
    A candidate is therefore only killed when its IMAGE PATH matches one of
    `allow_paths` (the binaries this plugin itself runs). When the path cannot be
    read, or `allow_paths` is empty, the process is reported under
    `skipped_unverified` and left running.
    """
    wanted = {n.lower() for n in exe_names}
    allow = {os.path.normcase(os.path.abspath(p)) for p in allow_paths if p}
    result = {'guard': platform_guard(), 'scanned': 0, 'candidates': [],
              'killed': [], 'kept': [], 'skipped_unverified': [],
              'path_checked': bool(allow), 'errors': [], 'dry_run': bool(dry_run)}
    try:
        rows = _list_processes_windows() if IS_WINDOWS else _list_processes_posix()
    except Exception as e:
        result['errors'].append('enumerate failed: %s' % e)
        return result
    alive = {r['pid'] for r in rows}
    now = time.time()
    for r in rows:
        if r['name'].lower() not in wanted:
            continue
        result['scanned'] += 1
        ppid = r['ppid']
        parentless = ppid == 0 or ppid not in alive
        age = None if r.get('created') is None else now - r['created']
        info = {'pid': r['pid'], 'ppid': ppid, 'name': r['name'],
                'age_s': None if age is None else round(age, 1)}
        if not parentless:
            result['kept'].append(dict(info, why='parent alive'))
            continue
        if age is not None and age < min_age_s:
            result['kept'].append(dict(info, why='younger than %ss' % min_age_s))
            continue
        image = _process_image_path(r['pid'])
        info['image'] = image
        if not allow:
            result['skipped_unverified'].append(
                dict(info, why='no allow_paths given — a name match alone is not evidence'))
            continue
        if not image or os.path.normcase(os.path.abspath(image)) not in allow:
            result['skipped_unverified'].append(
                dict(info, why=('image path unreadable' if not image
                                else 'image path is not one of ours: %s' % image)))
            continue
        result['candidates'].append(info)
        if dry_run:
            continue
        try:
            ok = _terminate_windows(r['pid']) if IS_WINDOWS else _kill_posix(r['pid'])
            if ok:
                result['killed'].append(info)
                log('swept orphan fp2k pid=%s age=%ss image=%s' % (r['pid'], info['age_s'], image))
            else:
                result['errors'].append('terminate failed for pid %s' % r['pid'])
        except Exception as e:
            result['errors'].append('terminate raised for pid %s: %s' % (r['pid'], e))
    return result


def _kill_posix(pid):
    try:
        os.kill(pid, 9)
        return True
    except Exception:
        return False


if __name__ == '__main__':  # manual probe: python fp2k_guard.py [--dry-run]
    dry = '--dry-run' in sys.argv
    print('platform guard:', platform_guard())
    print(sweep_orphans(dry_run=dry))
