#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Discriminative control for the fp2k orphan guard (2026-09-25).

Reproduces the real incident at the smallest possible scale. On 2026-09-25 two
`fp2k.exe` processes outlived the python driver that had spawned them and spun
at ~100% of a core for 10.3 h / 8.8 h, starving the desktop GUI. Mechanism to
be proven here: "kill the driver, does the grandchild survive?".

Cases (each must behave as stated, otherwise the guard is not proven):
  A) WITHOUT guard -- plain subprocess: parent killed => child SURVIVES (the bug)
  B) WITH guard    -- fp2k_guard.run_guarded: parent killed => child DIES
  C) sweep_orphans -- a parentless process named fp2k.exe IS terminated when its image
                      path is one the caller allows, while both a same-named process
                      with a live parent and a parentless one at a DIFFERENT path are
                      left alone (0.8.5: a name match alone is no longer enough)

Exit code 0 only when every assertion holds. Each case prints PASS/FAIL so a
partial result is visible instead of a single bottom line.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve the tree from HERE, never from a literal path: this file ships inside the
# package (audit/py/) and the release leak audit flags a hardcoded maintainer path.
#   1. DSH_XRD_SRC          — explicit plugin dir
#   2. DSH_XRD_ROOT         — what verify-all.mjs exports for its --root argument, so the
#                             bundled gate works in a package too (0.8.8: it used to fail
#                             here and the transplanted gate reported 1 of 13 FAILED for
#                             a control that simply could not find the tree)
#   3. package layout       — <pkg>/audit/py/  -> 2 up = <pkg>, when it looks like a plugin
#   4. dev layout           — <repo>/workspace/xrd-plugin-checks/py/ -> 3 up = <repo>,
#                             then <repo>/dsh-xrd
def _resolve_plugin():
    explicit = os.environ.get('DSH_XRD_SRC')
    if explicit and os.path.isdir(explicit):
        return explicit
    candidates = []
    root = os.environ.get('DSH_XRD_ROOT')
    if root:
        candidates.append(root)
    pkg = os.path.abspath(os.path.join(HERE, '..', '..'))
    candidates.append(pkg)
    repo = os.environ.get('DSH_XRD_REPO') or os.path.abspath(os.path.join(HERE, '..', '..', '..'))
    candidates.append(os.path.join(repo, 'dsh-xrd'))
    for c in candidates:
        if os.path.isfile(os.path.join(c, 'lib', 'index.js')) and os.path.isfile(os.path.join(c, 'package.json')):
            return c
    # nothing looked like a plugin: report the candidates instead of a single guess
    return None


PLUGIN = _resolve_plugin()
ENGINE = os.path.join(PLUGIN, 'engine') if PLUGIN else None
WORK = os.environ.get('DSH_XRD_CTRL_WORK') or os.path.join(tempfile.gettempdir(), 'ctrl-orphan')
PY = sys.executable
IS_WINDOWS = os.name == 'nt'

if not PLUGIN:
    sys.exit('no dsh-xrd tree found (looked at DSH_XRD_SRC, DSH_XRD_ROOT, the package '
             'layout above this file, and the dev layout). Pass DSH_XRD_SRC=<plugin dir>.')

sys.path.insert(0, ENGINE)
import fp2k_guard  # noqa: E402  (import after sys.path setup on purpose)

DRIVER_SRC = '''
import os, subprocess, sys, time
mode, pidfile = sys.argv[1], sys.argv[2]
sys.path.insert(0, r"%s")
# The GRANDCHILD writes its own pid: run_guarded() is synchronous (it waits for
# the child), so the driver can never report that pid itself.
sleeper = [sys.executable, "-c",
           "import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(900)",
           pidfile]
if mode == "guard":
    from fp2k_guard import run_guarded
    run_guarded(sleeper, cwd=os.getcwd(), timeout=900)
else:
    subprocess.Popen(sleeper, cwd=os.getcwd()).wait()
''' % ENGINE


def pid_alive(pid):
    """Alive check that is safe on Windows (os.kill(pid, 0) TERMINATES there)."""
    if IS_WINDOWS:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def hard_kill(pid):
    if IS_WINDOWS:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    os.kill(pid, 9)
    return True


def run_case(mode, wait_s=6.0):
    """Start a driver, read the grandchild pid, hard-kill the driver, report."""
    os.makedirs(WORK, exist_ok=True)
    pidfile = os.path.join(WORK, 'child_%s.txt' % mode)
    if os.path.exists(pidfile):
        os.remove(pidfile)
    drv = os.path.join(WORK, 'driver_%s.py' % mode)
    with open(drv, 'w', encoding='utf-8') as fh:
        fh.write(DRIVER_SRC)
    p = subprocess.Popen([PY, drv, mode, pidfile], cwd=WORK)
    deadline = time.time() + 20
    child = None
    while time.time() < deadline:
        if os.path.exists(pidfile):
            txt = open(pidfile).read().strip()
            if txt.isdigit():
                child = int(txt)
                break
        if p.poll() is not None:
            break
        time.sleep(0.2)
    if child is None:
        hard_kill(p.pid)
        return {'mode': mode, 'ok': False, 'why': 'grandchild pid never appeared'}
    time.sleep(1.5)                       # let the child settle
    alive_before = pid_alive(child)
    hard_kill(p.pid)                      # <-- this is the incident: driver dies
    time.sleep(wait_s)
    alive_after = pid_alive(child)
    if alive_after:                       # cleanup so the test leaves nothing behind
        hard_kill(child)
        time.sleep(1.0)
    return {'mode': mode, 'driver_pid': p.pid, 'child_pid': child,
            'alive_before': alive_before, 'alive_after': alive_after,
            'ok': True, 'child_alive_after_driver_death': alive_after}


def run_sweep_case():
    """sweep_orphans must kill OUR parentless fp2k and spare everything else.

    Updated for 0.8.5: the sweep now requires the candidate's IMAGE PATH to be one the
    caller allows, because a name match alone cannot tell our fp2k from another
    project's (or the user's own hand-started) one. So this control passes
    `allow_paths=[fake]` — and adds case C2, a same-named process whose image is NOT in
    the allow list, which must be left alone.
    """
    os.makedirs(WORK, exist_ok=True)
    fake = os.path.join(WORK, 'fp2k.exe')            # name matters, not the binary
    if not os.path.exists(fake):
        shutil.copyfile(PY, fake)
    other = os.path.join(WORK, 'fp2k_other', 'fp2k.exe')   # same NAME, other path
    os.makedirs(os.path.dirname(other), exist_ok=True)
    if not os.path.exists(other):
        shutil.copyfile(PY, other)
    # (1) orphan fixture: parent exits immediately -> process becomes parentless
    launcher = os.path.join(WORK, 'launch_orphan.py')
    with open(launcher, 'w', encoding='utf-8') as fh:
        fh.write('import subprocess, sys\n'
                 'p = subprocess.Popen([r"%s", "-c", "import time; time.sleep(900)"])\n'
                 'open(r"%s", "w").write(str(p.pid))\n' % (fake, os.path.join(WORK, 'orphan.txt')))
    if os.path.exists(os.path.join(WORK, 'orphan.txt')):
        os.remove(os.path.join(WORK, 'orphan.txt'))
    subprocess.run([PY, launcher], cwd=WORK, timeout=60)
    time.sleep(1.5)
    orphan_pid = int(open(os.path.join(WORK, 'orphan.txt')).read().strip())
    # (1b) the same trick with a binary at a DIFFERENT path
    launcher2 = os.path.join(WORK, 'launch_orphan_other.py')
    with open(launcher2, 'w', encoding='utf-8') as fh:
        fh.write('import subprocess, sys\n'
                 'p = subprocess.Popen([r"%s", "-c", "import time; time.sleep(900)"])\n'
                 'open(r"%s", "w").write(str(p.pid))\n' % (other, os.path.join(WORK, 'orphan2.txt')))
    if os.path.exists(os.path.join(WORK, 'orphan2.txt')):
        os.remove(os.path.join(WORK, 'orphan2.txt'))
    subprocess.run([PY, launcher2], cwd=WORK, timeout=60)
    time.sleep(1.5)
    other_pid = int(open(os.path.join(WORK, 'orphan2.txt')).read().strip())
    # (2) parented fixture: parent stays alive
    parent_pid = None
    keep = subprocess.Popen([fake, '-c', 'import time; time.sleep(900)'], cwd=WORK)
    parent_pid = keep.pid
    time.sleep(1.5)
    res = fp2k_guard.sweep_orphans(min_age_s=0, allow_paths=[fake])
    killed = [k['pid'] for k in res.get('killed', [])]
    kept = [k['pid'] for k in res.get('kept', [])]
    skipped = [k['pid'] for k in res.get('skipped_unverified', [])]
    orphan_dead = not pid_alive(orphan_pid)
    parent_alive = pid_alive(parent_pid)
    other_alive = pid_alive(other_pid)          # must STAY alive: not our image path
    for pid in (parent_pid, other_pid):
        if pid_alive(pid):
            hard_kill(pid)
    return {'orphan_pid': orphan_pid, 'parent_pid': parent_pid, 'other_pid': other_pid,
            'killed': killed, 'kept': kept, 'skipped': skipped,
            'orphan_dead': orphan_dead, 'parent_alive': parent_alive,
            'other_alive': other_alive,
            'ok': orphan_dead and parent_alive and other_alive and orphan_pid in killed
                  and parent_pid not in killed and other_pid not in killed
                  and other_pid in skipped,
            'sweep': res}


def run_stall_case():
    """C-fix: a child that produces no output growth must be judged stalled early.

    Two kinds are distinguished: 'busy' (CPU climbing, output frozen — the
    signature measured on real fp2k hangs) and 'waiting' (no CPU, no output).
    Both are killed and reported well before the full per-run timeout.
    """
    os.makedirs(WORK, exist_ok=True)
    watch = os.path.join(WORK, 'watch_never_written.txt')
    if os.path.exists(watch):
        os.remove(watch)
    results = {}
    for kind, code in (('busy', 'x = 0\nwhile True: x += 1'),
                       ('waiting', 'import time\ntime.sleep(600)')):
        t0 = time.time()
        r = fp2k_guard.run_guarded(
            [PY, '-c', code], cwd=WORK, timeout=300,
            watch=[watch], stall_s=5, poll_s=1)
        elapsed = time.time() - t0
        child_dead = not pid_alive(r['pid'])
        results[kind] = {'stalled': r.get('stalled'), 'stall_kind': r.get('stall_kind'),
                         'stall_after_s': r.get('stall_after_s'),
                         'elapsed_s': round(elapsed, 1), 'child_dead': child_dead,
                         'pid': r.get('pid')}
        if not child_dead:
            hard_kill(r['pid'])
    ok = all(v['stalled'] and v['stall_kind'] == k and v['child_dead']
             and v['elapsed_s'] < 25 for k, v in results.items())
    return {'ok': ok, 'cases': results}


def main():
    print('platform guard :', fp2k_guard.platform_guard())
    print('python         :', PY)
    print('fixture dir    :', WORK)
    fails = []

    a = run_case('plain')
    ok_a = a.get('ok') and a.get('child_alive_after_driver_death') is True
    print('\nA) NO guard  : child alive after driver death = %s  -> %s'
          % (a.get('child_alive_after_driver_death'), 'PASS' if ok_a else 'FAIL'))
    print('   detail:', a)
    if not ok_a:
        fails.append('A')

    b = run_case('guard')
    ok_b = b.get('ok') and b.get('child_alive_after_driver_death') is False
    print('\nB) WITH guard: child alive after driver death = %s  -> %s'
          % (b.get('child_alive_after_driver_death'), 'PASS' if ok_b else 'FAIL'))
    print('   detail:', b)
    if not ok_b:
        fails.append('B')

    c = run_sweep_case()
    print('\nC) sweep     : our orphan killed = %s, parented spared = %s, '
          'same-named foreign orphan spared = %s  -> %s'
          % (c['orphan_dead'], c['parent_alive'], c['other_alive'],
             'PASS' if c['ok'] else 'FAIL'))
    print('   detail: orphan_pid=%s parent_pid=%s other_pid=%s killed=%s '
          'kept_parented=%s skipped_unverified=%s'
          % (c['orphan_pid'], c['parent_pid'], c['other_pid'], c['killed'],
             c['parent_pid'] in c['kept'], c['skipped']))
    if not c['ok']:
        fails.append('C')

    d = run_stall_case()
    print('\nD) stall     : %s  -> %s'
          % ({k: (v['stall_kind'], v['elapsed_s'], 'dead' if v['child_dead'] else 'ALIVE')
              for k, v in d['cases'].items()}, 'PASS' if d['ok'] else 'FAIL'))
    if not d['ok']:
        fails.append('D')

    print('\n=== VERDICT: %s ===' % ('ALL PASS' if not fails else 'FAIL ' + ','.join(fails)))
    return 0 if not fails else 1


if __name__ == '__main__':
    raise SystemExit(main())
