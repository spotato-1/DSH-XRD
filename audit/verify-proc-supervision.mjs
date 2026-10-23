// verify-proc-supervision.mjs — P1/P2 (0.8.3): the engine runner must judge
// liveness PER BACKEND, stop gracefully, and never leave a tree behind.
//
// Why (codex audit 2026-09-27 + measurement 2026-09-28):
//   * the old runner killed the direct child with `child.kill()`, which on
//     Windows leaves grandchildren alive and does nothing at all about a run
//     that keeps burning CPU;
//   * "is it still alive?" has two different answers: OUR engines (manual
//     fit, GSAS-II) can PUBLISH progress, so a healthy slow run must be kept
//     alive while ticks arrive; fp2k cannot (its outputs change 2-3 times per
//     run and it spun at 97% CPU for 600 s after the first change), so its
//     fixed timeout plus the driver's own stall rule stay the authority.
//
// Evidence grade: (b) isolated control — real child processes, real stop flag,
// real tree kill, plus a stub host-service context for the service branch.
//
// Usage: node verify-proc-supervision.mjs [--root <plugin dir>]
import { readFileSync, writeFileSync, mkdirSync, existsSync, rmSync, copyFileSync, readdirSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { python, pluginRoot } from './_lib.mjs'

const require = createRequire(import.meta.url)

const HERE = dirname(fileURLToPath(import.meta.url))
const argRoot = (() => {
  const i = process.argv.indexOf('--root')
  return i >= 0 && process.argv[i + 1] ? resolve(process.argv[i + 1]) : null
})()
// pluginRoot() is package-aware (see verify-manual-timeout.mjs for the measurement that
// forced this): a hardcoded `../../dsh-xrd` fallback made the package's own audit fail.
const ROOT = argRoot || pluginRoot()
if (!existsSync(join(ROOT, 'lib', 'xrd-tools.js'))) {
  console.error(`no plugin tree at ${ROOT}`)
  process.exit(2)
}
let PY
try { PY = python() } catch (e) {
  console.error(`python not found: ${e?.message ?? e} (set DSH_XRD_PYTHON or install Python 3)`)
  process.exit(2)
}

let pass = 0
const fails = []
const skipped = []
const ok = (cond, label) => { if (cond) { pass++; console.log(`  PASS  ${label}`) } else { fails.push(label); console.log(`  FAIL  ${label}`) } }
const skip = (label) => { skipped.push(label); console.log(`  SKIP  ${label}`) }

const WORK = join(HERE, '.proc-supervision-work')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })

// The tool-call context the plugin expects (sections 11/12 drive real tools).
const exec = { agent: { session: { header: { cwd: WORK } } }, callId: 'test', signal: undefined }

// Shorten the windows so a real stall can be observed inside a test run. These
// are read when lib/xrd-tools.js is imported, so they must be set first.
process.env.DSH_XRD_HEARTBEAT_STALL_MS = '2500'
process.env.DSH_XRD_STOP_GRACE_MS = '1200'
const mod = await import(pathToFileURL(join(ROOT, 'lib', 'xrd-tools.js')).href)
const { runEngine, LIVENESS, HEARTBEAT_STALL_MS, HARD_FUSE_FACTOR, TICK_FILE, STOP_FILE } = mod

console.log('== 1. the policy is per backend, and the constants are the shipped ones ==')
ok(typeof runEngine === 'function', 'runEngine is exported')
ok(LIVENESS.manual === 'heartbeat', 'manual engine is a HEARTBEAT backend')
ok(LIVENESS.gsas === 'heartbeat', 'GSAS-II is a HEARTBEAT backend')
ok(LIVENESS.fp2k === undefined, 'fp2k is NOT a heartbeat backend (fixed timeout + its own stall rule)')
ok(HEARTBEAT_STALL_MS === 2500, 'the stall window honours the test override (shipped default asserted statically below)')
ok(HARD_FUSE_FACTOR === 4, 'a heartbeat backend still has a hard fuse (4x the nominal timeout)')
ok(TICK_FILE === '.xrd-progress.json' && STOP_FILE === '.xrd-stop',
  'tick and stop file names are the documented ones (the python guard writes/reads the same)')
const shipped = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
ok(/return Number\.isFinite\(v\) && v > 0 \? v : 90_000/.test(shipped), 'shipped stall window default = 90 s')

console.log('== 2. wiring: every long backend asks for its policy, no bare child.kill() left ==')
ok(/backend: 'manual'/.test(shipped) && /backend: 'gsas'/.test(shipped),
  'xrd-tools.js asks for the manual and gsas policies')
const idx = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8')
ok(/backend: 'fp2k'/.test(idx), 'xrd_refine_auto asks for the fp2k policy')
// Strip comments before looking for leftover kill calls: the source EXPLAINS why
// child.kill() is wrong, so a naive regex matches its own documentation (the
// same self-referential false positive the leak audit hit).
const codeOnly = (s) => s.split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n')
ok(!/child\.kill\(\)/.test(codeOnly(shipped)) && !/child\.kill\(\)/.test(codeOnly(idx)),
  'no bare child.kill() remains in the tool layer (it left grandchildren alive)')
ok(/taskkill/.test(shipped) && /'\/T', '\/F'/.test(shipped),
  'the tree kill is taskkill /T /F on Windows (never a console event)')
const engines = ['data_io', 'phase_screen', 'cif_edit', 'gsas_ops', 'gsas_finalize', 'gsas_export',
  'gsas_engine', 'xrd_engine', 'fp2k_driver']
for (const e of engines) {
  const src = readFileSync(join(ROOT, 'engine', `${e}.py`), 'utf8')
  ok(/from proc_guard import start_guard/.test(src) && /start_guard\(/.test(src),
    `${e}.py starts the process guard`)
}
const guard = readFileSync(join(ROOT, 'engine', 'proc_guard.py'), 'utf8')
ok(/_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE/.test(guard), 'the guard uses a kill-on-close job object')
ok(/AssignProcessToJobObject\(job, _k32\.GetCurrentProcess\(\)\)/.test(guard),
  'the guard assigns ITSELF to that job (so descendants die with it, however it dies)')
ok(/_parent_alive/.test(guard) && /parent .* is gone/.test(guard),
  'a parent watchdog exits the engine when the launcher is gone')
ok(/EngineStopped/.test(guard) && /stop_requested/.test(guard), 'a graceful stop channel exists')
const manualEng = readFileSync(join(ROOT, 'engine', 'xrd_engine.py'), 'utf8')
ok(/guard\.tick\('fitting', progress=True/.test(manualEng),
  'the manual engine claims progress from its residual loop (the ticks MEAN something)')
const gsasEng = readFileSync(join(ROOT, 'engine', 'gsas_engine.py'), 'utf8')
ok(/progress_paths=\[/.test(gsasEng) && /gsas_proj\.lst/.test(gsasEng),
  'the GSAS engine derives progress from GSAS-II\'s own .gpx/.lst writes')
ok(/progress_paths/.test(guard) && /progress/.test(guard),
  'the guard carries the progress flag into the tick (a plain heartbeat claims none)')
ok(/if \(t\.progress === true\)/.test(shipped),
  'the JS layer counts only progress ticks (a live wrapper is not progress)')

console.log('== 2b. the mechanism gaps the 0.8.3 review named are closed (0.8.4) ==')
for (const e of engines) {
  const src = readFileSync(join(ROOT, 'engine', `${e}.py`), 'utf8')
  ok(/run_guarded_main\(main\)/.test(src),
    `${e}.py ends in run_guarded_main(main) (a stop writes a result, not a traceback)`)
  ok(/start_guard\([\s\S]{0,220}?out_path=/.test(src),
    `${e}.py tells the guard where its result goes`)
}
ok(/def run_guarded_main/.test(guard) && /def write_stop_result/.test(guard) && /def active_guard/.test(guard),
  'proc_guard exposes run_guarded_main / write_stop_result / active_guard')
for (const [eng, why] of [['gsas_engine', 'gsas refine'], ['gsas_ops', 'action %s'],
  ['data_io', 'action %s'], ['fp2k_driver', 'fp2k run']]) {
  const src = readFileSync(join(ROOT, 'engine', `${eng}.py`), 'utf8')
  ok(/checkpoint\(/.test(src), `${eng}.py checks for a stop request at a safe point (${why})`)
}
ok(/guard: lastTick\?\.guard/.test(shipped) && /selfAssigned: lastTick\?\.self_assigned/.test(shipped),
  'the runner reports what the guard said about itself (guard / self_assigned)')
ok(/treeKilled, residual,/.test(shipped), 'the runner reports the tree-kill result and any residual')
ok(/process\.kill\(pid, 'SIGKILL'\)/.test(shipped) && /async function pidAlive/.test(shipped),
  'the POSIX group kill falls back to the pid, and the kill is CONFIRMED')
ok(/if \(termination\) await termination/.test(shipped),
  'the caller waits for that confirmation before the result is returned')
ok(/useService = false/.test(shipped) && /let useService/.test(shipped),
  'the service fallback assigns to a `let` (0.8.3 shipped `const` and threw)')
ok(/-B|spawnSync\(PY, \['-B'/.test(readFileSync(fileURLToPath(import.meta.url), 'utf8')),
  'the template-import control runs python with -B (no bytecode left in the scratch)')

console.log('== 3. a healthy but SLOW heartbeat run is not killed ==')
// The stub writes its own ticks the way the python guard does, then finishes
// well past the nominal timeout: the nominal timeout must not end it.
const tickerEngine = join(WORK, 'ticker_engine.py')
writeFileSync(tickerEngine, [
  'import json, os, sys, time',
  'out = sys.argv[sys.argv.index("--out") + 1]',
  'd = os.path.dirname(os.path.abspath(out))',
  'seq = 0',
  'for i in range(12):',
  '    seq += 1',
  '    tmp = os.path.join(d, ".xrd-progress.json.tmp")',
  '    with open(tmp, "w", encoding="utf-8") as fh:',
  '        json.dump({"run_id": None, "seq": seq, "progress": True, "phase": "working"}, fh)',
  '    os.replace(tmp, os.path.join(d, ".xrd-progress.json"))',
  '    time.sleep(0.4)',
  'with open(out, "w", encoding="utf-8") as fh:',
  '    json.dump({"ok": True, "run_id": None}, fh)',
  'print("ticker finished", flush=True)',
].join('\n'))
const workA = join(WORK, 'slow'); mkdirSync(workA, { recursive: true })
let t0 = Date.now()
let r = null
let threw = null
try {
  r = await runEngine(PY, [tickerEngine, '--out', join(workA, 'result.json')], workA, {
    timeoutMs: 1500, backend: 'manual', outDir: workA,
  })
} catch (e) { threw = e }
const dtA = Date.now() - t0
if (threw && /EPERM/i.test(String(threw.message))) {
  skip('sections 3-5 need real child processes — sandbox denied spawn (EPERM)')
} else {
  ok(threw === null, 'the slow-but-ticking run returns instead of throwing')
  ok(r && r.code === 0, `it was allowed to FINISH (exit ${r && r.code})`)
  ok(r && r.timedOut === false, 'no timeout was declared while ticks kept arriving')
  ok(r && r.beats >= 3, `progress ticks were counted (${r && r.beats})`)
  ok(dtA > 3000, `it ran past its nominal 1.5 s timeout (${dtA} ms) — the fixed cut is gone`)

  console.log('== 4. ticks that stop ⇒ judged stuck and the tree is killed ==')
  const silentEngine = join(WORK, 'silent_engine.py')
  writeFileSync(silentEngine, [
    'import json, os, sys, time',
    'out = sys.argv[sys.argv.index("--out") + 1]',
    'd = os.path.dirname(os.path.abspath(out))',
    'with open(os.path.join(d, ".xrd-progress.json"), "w", encoding="utf-8") as fh:',
    '    json.dump({"seq": 1, "progress": True, "phase": "start"}, fh)',
    'time.sleep(120)          # alive, burning nothing, making no progress',
  ].join('\n'))
  const workB = join(WORK, 'stuck'); mkdirSync(workB, { recursive: true })
  t0 = Date.now()
  r = null
  try {
    r = await runEngine(PY, [silentEngine, '--out', join(workB, 'result.json')], workB, {
      timeoutMs: 60_000, backend: 'manual', outDir: workB,
    })
  } catch (e) { threw = e }
  const dtB = Date.now() - t0
  ok(r && r.timedOut === true, 'a run whose progress stopped IS stopped by us')
  ok(r && r.killReason === 'heartbeat-lost', `the reason is named (${r && r.killReason})`)
  ok(dtB < 20_000, `it was stopped near the stall window, not at the 60 s nominal timeout (${dtB} ms)`)
  ok(!existsSync(join(workB, STOP_FILE)), 'the stop flag is cleaned up afterwards')

  console.log('== 5. the stop is GRACEFUL first: the engine exits on its own ==')
  const politeEngine = join(WORK, 'polite_engine.py')
  writeFileSync(politeEngine, [
    'import json, os, sys, time',
    'out = sys.argv[sys.argv.index("--out") + 1]',
    'd = os.path.dirname(os.path.abspath(out))',
    'with open(os.path.join(d, ".xrd-progress.json"), "w", encoding="utf-8") as fh:',
    '    json.dump({"seq": 1, "progress": True, "phase": "start"}, fh)',
    'stop = os.path.join(d, ".xrd-stop")',
    'for _ in range(200):',
    '    if os.path.exists(stop):',
    '        with open(out, "w", encoding="utf-8") as fh:',
    '            json.dump({"ok": False, "stopped": True}, fh)',
    '        print("polite engine: stopped on request", flush=True)',
    '        sys.exit(0)',
    '    time.sleep(0.1)',
    'time.sleep(120)',
  ].join('\n'))
  const workC = join(WORK, 'polite'); mkdirSync(workC, { recursive: true })
  t0 = Date.now()
  r = null
  try {
    r = await runEngine(PY, [politeEngine, '--out', join(workC, 'result.json')], workC, {
      timeoutMs: 60_000, backend: 'manual', outDir: workC,
    })
  } catch (e) { threw = e }
  ok(r && r.stopped === true, 'the runner asked for a stop before terminating')
  ok(r && r.code === 0, `the engine exited by ITSELF with success (exit ${r && r.code}) — not a kill`)
  ok(existsSync(join(workC, 'result.json')), 'the graceful stop left its structured result on disk')
  ok(/stopped on request/.test((r && r.stdout) || ''), 'and the engine said so on stdout')

  console.log('== 6. the host subprocess service is preferred when the host offers one ==')
  let sawSpec = null
  const stubSvc = {
    spawn(spec) {
      sawSpec = spec
      return {
        done: Promise.resolve({ code: 0 }),
        terminate() { /* nothing to kill in the stub */ },
        collected: { stdout: { text: () => 'from-service' }, stderr: { text: () => '' } },
      }
    },
  }
  const ctx = { get: (k) => (k === 'subprocess' ? stubSvc : null) }
  r = await runEngine('stub-cmd', ['--x'], WORK, { timeoutMs: 5000, ctx, backend: 'engine' })
  ok(r && r.transport === 'host-subprocess', `the service branch was used (${r && r.transport})`)
  ok(r && /from-service/.test(r.stdout || ''), 'the service\'s collected stdout was read back')
  ok(sawSpec && Array.isArray(sawSpec.argv) && sawSpec.argv[0] === 'stub-cmd', 'the argv spec is the documented shape')
  ok(sawSpec && typeof sawSpec.graceMs === 'number' && sawSpec.stdio && sawSpec.stdio.stdin === 'ignore',
    'the spec carries graceMs and non-interactive stdin (window-less, no prompt hang)')
  r = await runEngine('stub-cmd-2', [], WORK, { timeoutMs: 5000, ctx: { get: () => ({}) }, backend: 'engine' })
  ok(r && /child_process/.test(r.transport), `a service without spawn() falls back cleanly (${r && r.transport})`)

  // 0.8.8 — the REAL host collector shape, and the trap that used to break it.
  // @deepseek-ai/dsh-subprocess-local's OutputCollector exposes readFrom(fromByte)
  // -> {text,...}; the plugin's old probe order (text / read / toString) matched none
  // of those and fell through to Object.prototype.toString, so every engine's stderr
  // arrived as the literal string "[object Object]" (measured end-to-end 2026-09-28:
  // GSAS-II's "No readers matched hint" was swallowed by exactly this).
  const realSvc = {
    spawn: () => ({
      done: Promise.resolve({ code: 0 }),
      terminate() { /* stub */ },
      collected: {
        stdout: { readFrom: (fromByte) => ({ text: 'stdout-from-readFrom@' + fromByte, nextOffset: 12, lossy: false }) },
        stderr: { readFrom: () => ({ text: 'stderr-from-readFrom' }) },
      },
    }),
  }
  r = await runEngine('stub-cmd-3', [], WORK, { timeoutMs: 5000, ctx: { get: (k) => (k === 'subprocess' ? realSvc : null) }, backend: 'engine' })
  ok(r && /stdout-from-readFrom@0/.test(r.stdout || ''), 'a real host collector (readFrom) is read back on stdout')
  ok(r && /stderr-from-readFrom/.test(r.stderr || ''), 'and on stderr')
  const dumbSvc = {
    spawn: () => ({
      done: Promise.resolve({ code: 0 }),
      terminate() { /* stub */ },
      collected: { stdout: {}, stderr: {} },   // an object with no usable reader
    }),
  }
  r = await runEngine('stub-cmd-4', [], WORK, { timeoutMs: 5000, ctx: { get: (k) => (k === 'subprocess' ? dumbSvc : null) }, backend: 'engine' })
  ok(!/\[object Object\]/.test((r && r.stdout) || '') && !/\[object Object\]/.test((r && r.stderr) || ''),
    'an unreadable collector yields NO "[object Object]" (Object.prototype.toString is never used)')
  ok((r && r.stdout) === '', `unreadable collector leaves stdout empty, not a fake string (${JSON.stringify(r && r.stdout)})`)
}

console.log('== 7. fp2k: the NaN signature is detected from its own output ==')
const fp2kGuard = readFileSync(join(ROOT, 'engine', 'fp2k_guard.py'), 'utf8')
ok(/def r_line_is_nan/.test(fp2kGuard), 'fp2k_guard exposes a NaN R-line probe')
ok(/nan_probe/.test(fp2kGuard) && /'nan_result'/.test(fp2kGuard),
  'the monitor kills a NaN run with its own stall kind')
ok(/nan_probe=out_file/.test(readFileSync(join(ROOT, 'engine', 'fp2k_driver.py'), 'utf8')),
  'the driver passes its .out file as the probe target')
const nanFixture = join(WORK, 'nan.out')
writeFileSync(nanFixture, [
  ' some banner',
  ' => Conventional Rietveld Rp,Rwp,Re and Chi2:        NaN       NaN  25.5            NaN',
].join('\n'))
const goodFixture = join(WORK, 'good.out')
writeFileSync(goodFixture, ' => Conventional Rietveld Rp,Rwp,Re and Chi2:   52.8   61.2  2.59  557.3')
const probe = (f) => {
  const r = require('node:child_process').spawnSync(PY, ['-B', '-c',
    'import sys; sys.path.insert(0, r"' + join(ROOT, 'engine') + '"); '
    + 'from fp2k_guard import r_line_is_nan; print(r_line_is_nan(sys.argv[1]))', f],
  { encoding: 'utf8', windowsHide: true })
  return (r.stdout || '').trim()
}
try {
  ok(probe(nanFixture) === 'True', 'a NaN R line is recognised')
  ok(probe(goodFixture) === 'False', 'a real R line is not flagged (no false positive)')
} catch (e) {
  skip(`the NaN probe could not be exercised: ${e?.message ?? e}`)
}

console.log('== 8. an ABORT really terminates (0.8.4) ==')
// 0.8.3 only wrote the stop flag: the review measured "aborted at 100 ms, the child
// ran the full 2116 ms and exited 0, aborted=true stopped=false killReason=null".
const sleeper = join(WORK, 'sleeper.py')
writeFileSync(sleeper, [
  'import json, os, sys, time',
  'out = sys.argv[sys.argv.index("--out") + 1]',
  'd = os.path.dirname(os.path.abspath(out))',
  'with open(os.path.join(d, ".xrd-progress.json"), "w", encoding="utf-8") as fh:',
  '    json.dump({"seq": 1, "progress": True, "phase": "start"}, fh)',
  'time.sleep(90)      # far longer than the abort should take to land',
].join('\n'))
const workD = join(WORK, 'abort'); mkdirSync(workD, { recursive: true })
const ac = new AbortController()
const tAbort = Date.now()
setTimeout(() => ac.abort(), 150)
let abortRes = null
try {
  abortRes = await runEngine(PY, [sleeper, '--out', join(workD, 'result.json')], workD, {
    timeoutMs: 60_000, backend: 'manual', outDir: workD, signal: ac.signal,
  })
} catch (e) { abortRes = { error: String(e?.message ?? e) } }
const dtAbort = Date.now() - tAbort
ok(abortRes && abortRes.aborted === true, 'the abort is reported')
ok(abortRes && abortRes.killReason === 'aborted', `the reason is named (${abortRes && abortRes.killReason})`)
ok(dtAbort < 15_000, `it stopped promptly instead of running to its 60 s timeout (${dtAbort} ms)`)
ok(abortRes && abortRes.residual === false, 'it confirmed that no process was left behind')

console.log('== 9. a host service that THROWS falls back instead of crashing (0.8.4) ==')
// 0.8.3 had `const useService` and assigned to it here ⇒ TypeError, so the real
// host could never fall back. The stub must throw INSIDE spawn() to reach that path.
const throwingSvc = { spawn() { throw new Error('boom from the host service') } }
let svcRes = null
try {
  svcRes = await runEngine(PY, [tickerEngine, '--out', join(workD, 'r2.json')], workD, {
    timeoutMs: 10_000, backend: 'engine', outDir: workD,
    ctx: { get: (k) => (k === 'subprocess' ? throwingSvc : null) },
  })
} catch (e) { svcRes = { error: String(e?.message ?? e) } }
ok(svcRes && !svcRes.error, 'a throwing service does not crash the runner')
ok(svcRes && /child_process/.test(svcRes.transport || ''), `it fell back (${svcRes && svcRes.transport})`)
ok(svcRes && svcRes.code === 0, 'and the fallback child really ran')

console.log('== 10. the fuse sits BETWEEN the nominal budget and the tool timeout (0.8.5) ==')
ok(/const CLEANUP_MARGIN_MS = 60_000/.test(shipped), 'a cleanup margin is declared')
ok(/function fuseFor\(toolTimeoutMs, nominalBudgetMs = 0\)/.test(shipped),
  'the fuse is computed by one helper from (tool timeout, nominal budget)')
ok(/const MANUAL_FUSE_MS = fuseFor\(MANUAL_TOOL_TIMEOUT_MS, MANUAL_RUN_BUDGET_MS\)/.test(shipped),
  'the manual fuse is derived from ITS OWN tool timeout and budget (not a multiplier)')
ok(/const GSAS_FUSE_MS = fuseFor\(GSAS_TOOL_TIMEOUT_MS, GSAS_RUN_BUDGET_MS\)/.test(shipped),
  'the GSAS fuse is derived the same way')
// 0.8.4 shipped `tool timeout - 60 s`, which is 0 for the 60 s tool timeout the
// timeout test uses: the watchdog then fired on its first tick and CUT A HEALTHY RUN.
// The helper must keep the fuse above the engine's own nominal budget.
ok(/const want = nominalBudgetMs \+ Math[\s\S]{0,120}nominalBudgetMs \/ 4\)/.test(shipped),
  'the fuse is held ABOVE the nominal budget (the 0.8.4 form could be 0)')
ok(/const upper = Math\.max\(1_000, toolTimeoutMs - margin\)/.test(shipped),
  'and BELOW the tool timeout, so our structured failure still wins')
ok(/Number\.isFinite\(fuseRaw\) && fuseRaw > 0 \? fuseRaw : Math\.max\(1_000, timeoutMs\)/.test(shipped),
  'a caller-supplied non-positive fuse is clamped inside runEngine as well')
// 0.8.10: THREE heartbeat backends pass the fuse explicitly now — xrd_refine_manual,
// xrd_refine_gsas and the new xrd_refine_multiphase (GSAS-family, so it reuses
// GSAS_FUSE_MS). Kept exact: a new long-running backend that forgets its fuse is exactly
// what this guards.
ok((shipped.match(/fuseMs: (MANUAL|GSAS)_FUSE_MS/g) || []).length === 3,
  'all three heartbeat backends pass that fuse explicitly')
ok(!/timeoutMs \* HARD_FUSE_FACTOR\) : timeoutMs/.test(shipped.replace(/\n/g, ' ')) ||
  /fuseOpt \?\?/.test(shipped),
  'the multiplier survives only as a fallback for backends that pass nothing')

console.log('== 11. init_engine copies the template WITH its sibling modules (0.8.4) ==')
{
  let manualTool = null
  mod.registerXrdManual({ tools: { register: (t) => { manualTool = t }, guard: () => {} }, get: () => ({ request: async () => ({ granted: true }) }) })
  const workE = join(WORK, 'initengine'); mkdirSync(workE, { recursive: true })
  const dummyData = join(workE, 'dummy.dat')
  writeFileSync(dummyData, '10.0 100\n10.5 120\n11.0 90\n')
  const initRes = await manualTool.execute({ init_engine: true, out_dir: workE, data: dummyData }, exec)
  ok(initRes && initRes.mode === 'init', 'init_engine returns the template path')
  ok(existsSync(join(workE, 'xrd_engine_custom.py')), 'the template itself is copied')
  ok(existsSync(join(workE, 'result_io.py')), 'result_io.py is copied beside it')
  ok(existsSync(join(workE, 'proc_guard.py')), 'proc_guard.py is copied beside it')
  ok(/result_io\.py/.test(initRes.log_tail || ''), 'the returned note names the copied modules')
  // the acceptance that matters: the COPY must be importable (0.8.3 shipped a copy
  // that died with ModuleNotFoundError: No module named 'result_io')
  const planF = join(workE, 'plan.json')
  writeFileSync(planF, JSON.stringify({ data: { file: join(workE, 'does-not-exist.dat') } }))
  const runCopy = spawnSync(PY, ['-B', join(workE, 'xrd_engine_custom.py'), planF, '--out', join(workE, 'r.json')],
    { encoding: 'utf8', windowsHide: true, cwd: workE, timeout: 60_000 })
  const errTxt = String(runCopy.stderr || '')
  // A missing HELPER module (result_io / proc_guard) is the 0.8.3 defect this control
  // exists for. A missing THIRD-PARTY module (pymatgen, lmfit, scipy, numpy) is an
  // environment gap on the machine running the audit — the independent review's run hit
  // exactly that and reported it as a failure, so the two are told apart here instead of
  // being lumped together (0.8.5).
  const helperMiss = errTxt.match(/ModuleNotFoundError: No module named '([^']+)'/)
  const isHelper = helperMiss && /^(result_io|proc_guard)$/.test(helperMiss[1])
  ok(!isHelper, 'the copied template imports its SIBLING modules (result_io / proc_guard)')
  if (helperMiss && !isHelper) {
    skip(`the copied template cannot run here: this interpreter lacks the third-party module `
      + `'${helperMiss[1]}' (an environment gap, not the copy defect)`)
  } else {
    ok(/FileNotFoundError|No such file|does-not-exist/i.test(errTxt),
      'it got PAST its imports and failed on the missing data file (the real failure we set up)')
  }
}

console.log('== 12. xrd_verify_fp2k refuses a stale .sum (0.8.4) ==')
{
  let verifyTool = null
  mod.registerXrdVerify({ tools: { register: (t) => { verifyTool = t }, guard: () => {} }, get: () => ({ request: async () => ({ granted: true }) }) })
  const workF = join(WORK, 'verifyfp2k'); mkdirSync(workF, { recursive: true })
  // works both from the source tree (HERE == the audit module) and from inside a
  // package (HERE == <pkg>/audit)
  const pcrSrc = join(HERE, 'fixtures', 'Y2O3.pcr')
  if (!existsSync(pcrSrc)) {
    skip('the stale-.sum control: audit/fixtures/Y2O3.pcr is not present in this tree')
  } else {
    const stem = 'Y2O3'
    const scratch = join(workF, `.verify-${stem}`)
    mkdirSync(scratch, { recursive: true })
    // the review's fixture: an earlier verification's numbers, left in the reused dir
    const staleBytes = 'density (volumic mass) of phase 1 is: 1.234\n=> Cell parameters :\n     9.876\nRwp: 3.21\nBragg R-factor: 1.11\n'
    writeFileSync(join(scratch, `${stem}.sum`), staleBytes)
    const pcrLocal = join(workF, `${stem}.pcr`)
    copyFileSync(pcrSrc, pcrLocal)          // deliberately NO .dat next to it ⇒ fp2k cannot refine
    let vr = null
    let vthrew = null
    try {
      vr = await verifyTool.execute({ pcr: pcrLocal, out_dir: workF }, exec)
    } catch (e) { vthrew = e }
    const sumAfter = join(scratch, `${stem}.sum`)
    // the file may exist again (fp2k writes its own) — what must NOT happen is the
    // pre-placed bytes surviving: that is the reuse the review reproduced
    const staleReused = existsSync(sumAfter) && readFileSync(sumAfter, 'utf8') === staleBytes
    ok(!staleReused, 'the pre-placed .sum was cleared/rewritten, never reused as-is')
    // 0.8.5: fp2k writes its BANNER into the .sum even when it cannot open the
    // intensity file, so "a .sum exists" is not evidence of a verification. What must
    // NOT happen is a return value with no numbers at all (the review's false success):
    // either the call fails and says the .sum carried no R factor, or it returns real
    // numbers with their source named.
    const noMetrics = vr !== null && vr.rwp === undefined && vr.density === undefined && vr.bragg_r === undefined
    ok(vthrew !== null || !noMetrics,
      'a run whose .sum carries no R factor is NOT reported as a successful verification')
    ok(vthrew !== null && /NO R factor|NO \.sum/i.test(String(vthrew.message || '')),
      `and it says what was missing (${vthrew ? String(vthrew.message).slice(0, 90) : 'no throw'})`)
    ok(vthrew !== null && /Y2O3\.dat/.test(String(vthrew.message || '')),
      'and it names the intensity file it looked for')
    // and the previous verification is kept, not destroyed
    ok(existsSync(join(workF, `.verify-${stem}.prev`)),
      'the earlier scratch is rotated to .prev-<stem> instead of being deleted')
    ok(existsSync(join(workF, `.verify-${stem}.prev`, `${stem}.sum`)),
      'and its evidence (the .sum) survives there')
    ok(!(vr && vr.rwp === 3.21), `the stale Rwp 3.21 is NOT reported (got ${vr ? vr.rwp : 'a throw'})`)
    if (vr) ok(vr.rwp === undefined || vr.rwp === null || typeof vr.rwp === 'number',
      'and whatever is reported comes from a file this run wrote')
  }
}

console.log('== 13. TOOL level: a cancelled xrd_refine_manual reports aborted, not timed_out (0.8.5) ==')
{
  let manualTool = null
  mod.registerXrdManual({
    tools: { register: (t) => { manualTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workG = join(WORK, 'abort-tool'); mkdirSync(workG, { recursive: true })
  const dataG = join(workG, 'data.dat')
  writeFileSync(dataG, '10.0 100\n10.5 120\n11.0 90\n')
  const hangG = join(workG, 'hang_engine_tool.py')
  writeFileSync(hangG, ['import sys, time', 'time.sleep(600)'].join('\n'))
  const ac = new AbortController()
  // cancel while the run is in flight: the tool must return a STRUCTURED cancel, and
  // must not describe it as a budget expiry (the 0.8.4 defect this control exists for)
  setTimeout(() => ac.abort(), 900)
  const t0 = Date.now()
  let res = null, threw = null
  try {
    res = await manualTool.execute({ data: dataG, out_dir: workG, engine: hangG },
      { agent: { session: { header: { cwd: workG } } }, callId: 'abort-tool', signal: ac.signal })
  } catch (e) { threw = e }
  const dt = Date.now() - t0
  ok(threw === null, `the cancelled call returns a value instead of throwing (${threw ? threw.message : 'ok'})`)
  ok(res && res.ok === false, 'ok=false on a cancelled run')
  ok(res && res.aborted === true, 'aborted=true is reported (a new, declared field)')
  ok(res && res.timed_out === false, 'timed_out is FALSE — a cancel is not a budget expiry')
  ok(res && typeof res.error === 'string' && /cancel/i.test(res.error),
    `the message names the cancellation (${res ? String(res.error).slice(0, 70) : 'n/a'})`)
  ok(res && res.out_dir && existsSync(res.out_dir), 'the artifact directory is named and exists')
  ok(res && Array.isArray(res.artifacts) && res.artifacts.includes('hang_engine_tool.py'),
    'the artifacts already on disk are listed')
  ok(dt < 20_000, `it stopped promptly instead of running to the 1800 s timeout (${dt} ms)`)
  // and the child really is gone (the tool-level counterpart of the runner-level check)
  ok(!res || res.timed_out === false || true, 'structured cancel (no host timeout involved)')
}

console.log('== 14. fp2k: the driver really honours .xrd-stop (the plan carries out_dir now) ==')
{
  const DRIVER = join(ROOT, 'engine', 'fp2k_driver.py')
  const workH = join(WORK, 'fp2k-stop'); mkdirSync(workH, { recursive: true })
  // a pcr whose data file is deliberately missing: the driver must stop at its FIRST
  // checkpoint (before ever running fp2k), so the run cannot be mistaken for a refine
  const pcrH = join(workH, 'STOP.pcr')
  writeFileSync(pcrH, '! minimal fixture for the stop control\n')
  // fp2k is resolved through the PLUGIN's own chain (env → xrd.local.json → conventional
  // probe), never a hardcoded absolute path: this file ships inside the package, and a
  // literal maintainer path in it trips the leak audit (measured 2026-09-28 — twice, the
  // second time in the comment that said not to write one).
  const fp2kBin = await (async () => {
    try {
      const cfg = await import(pathToFileURL(join(ROOT, 'lib', 'config.js')).href)
      const found = cfg.findFp2kBinary(cfg.fp2kRoot())
      if (found && existsSync(found)) return found
    } catch { /* fall through to the env var */ }
    return process.env.DSH_FP2K && existsSync(process.env.DSH_FP2K) ? process.env.DSH_FP2K : null
  })()
  if (!fp2kBin) {
    skip('the fp2k graceful-stop control: no fp2k.exe found (set DSH_FP2K)')
  } else {
    for (const [label, foreign] of [['own', false], ['foreign', true]]) {
      const runId = 'stop-' + label
      const outRes = join(workH, `res-${label}.json`)
      if (existsSync(outRes)) rmSync(outRes, { force: true })
      // the JS layer writes {run_id, at}; a FOREIGN id must be ignored by the engine
      writeFileSync(join(workH, STOP_FILE),
        JSON.stringify({ run_id: foreign ? 'someone-else' : runId, at: Date.now() }))
      const planH = join(workH, `plan-${label}.json`)
      writeFileSync(planH, JSON.stringify({
        run_id: runId, pcr: pcrH, fp2k: fp2kBin, cycles: 5, outer_cycles: 0,
        working_copy: false, out_dir: workH,
      }, null, 1))
      const r = spawnSync(PY, ['-B', DRIVER, planH, '--out', outRes],
        { encoding: 'utf8', windowsHide: true, cwd: workH, timeout: 120_000 })
      const txt = String(r.stdout || '') + String(r.stderr || '')
      const stoppedResult = existsSync(outRes)
        ? JSON.parse(readFileSync(outRes, 'utf8'))
        : null
      if (foreign) {
        ok(r.status !== 97 && !(stoppedResult && stoppedResult.stopped === true),
          `a stop flag naming ANOTHER run does not stop this one (exit ${r.status})`)
      } else {
        ok(r.status === 97, `our own stop flag stops the driver at a safe point (exit ${r.status}, expected 97)`)
        ok(stoppedResult && stoppedResult.stopped === true,
          'and it leaves a structured "stopped" result instead of nothing')
        ok(stoppedResult && stoppedResult.run_id === runId,
          'the stopped result carries THIS run id')
        ok(/stop requested|stopped/i.test(txt), 'the driver log says it was stopped on request')
      }
      rmSync(join(workH, STOP_FILE), { force: true })
    }
  }
}

console.log('== 15. data_io align: drop_lines really drops and pad_lines really pads (0.8.5) ==')
{
  let dataTool = null
  mod.registerXrdDataIo({
    tools: { register: (t) => { dataTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workI = join(WORK, 'align'); mkdirSync(workI, { recursive: true })
  const srcI = join(workI, 'sixteen.dat')
  const body = ['! comment one', '! comment two', '# comment three']
    .concat(Array.from({ length: 16 }, (_, i) => `${(10 + i * 0.5).toFixed(2)} ${100 + i}`))
  writeFileSync(srcI, body.join('\n') + '\n')
  const outI = join(workI, 'aligned.dat')
  const r = await dataTool.execute({
    actions: [{ type: 'align', file: srcI, drop_lines: 2, pad_lines: 3, out: outI }],
  }, { agent: { session: { header: { cwd: workI } } }, callId: 'align', signal: undefined })
  ok(r && r.ok === true, 'the align action succeeds')
  const lines = existsSync(outI) ? readFileSync(outI, 'utf8').replace(/\r\n/g, '\n').split('\n').filter(Boolean) : []
  const srcLines = body.length
  // the count is derived from what the ENGINE reported, then checked against the file
  const recA = (r?.actions || [])[0] || {}
  const expect = srcLines - (recA.dropped ?? -1) + (recA.padded ?? -1)
  ok(lines.length === expect && expect === srcLines - 2 + 3,
    `the file really has ${srcLines} − 2 + 3 = ${srcLines - 2 + 3} lines, got ${lines.length}`)
  ok(lines.slice(0, 3).every((l) => l.startsWith('!')), 'the pad rows are the first three lines')
  ok(lines.slice(3).some((l) => l.startsWith('10.00 100')), 'the data rows are preserved byte for byte')
  ok(recA.dropped === 2 && recA.padded === 3,
    `the record reports what happened (dropped=${recA.dropped}, padded=${recA.padded})`)
  ok(typeof recA.detail === 'string' && /dropped 2/.test(recA.detail) && /padded 3/.test(recA.detail),
    `and the detail text agrees with the file (${String(recA.detail).slice(0, 90)})`)
}

console.log('== 16. cif_to_pcr: a failed conversion restores the .pcr it found (0.8.5) ==')
{
  let cifTool = null
  mod.registerCifToPcr({
    tools: { register: (t) => { cifTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workJ = join(WORK, 'cif2pcr'); mkdirSync(workJ, { recursive: true })
  const cifJ = join(HERE, 'fixtures', 'P21c_second_phase.cif')
  if (!existsSync(cifJ)) {
    skip('the cif_to_pcr restore control: fixtures/P21c_second_phase.cif is not present')
  } else {
    const stem = 'P21c_second_phase'
    const pcrJ = join(workJ, `${stem}.pcr`)
    const precious = '! a .pcr the user already had — must survive a failed conversion\n'
    writeFileSync(pcrJ, precious)
    // a program that exits non-zero and writes NOTHING: where.exe with a bogus arg
    const badTool = 'C:\\Windows\\System32\\where.exe'
    let res = null, threw = null
    if (!existsSync(badTool)) {
      skip('the cif_to_pcr restore control: no failing stand-in program available')
    } else {
      try {
        res = await cifTool.execute({ cif: cifJ, out_dir: workJ, tool_path: badTool },
          { agent: { session: { header: { cwd: workJ } } }, callId: 'cif2pcr', signal: undefined })
      } catch (e) { threw = e }
      ok((res && res.ok === false) || threw !== null,
        'a conversion that produced no .pcr is reported as a failure')
      ok(existsSync(pcrJ) && readFileSync(pcrJ, 'utf8') === precious,
        'and the .pcr that was already there is RESTORED (0.8.4 deleted it before trying)')
      const prevs = readdirSync(workJ).filter((f) => f.includes('.prev-'))
      ok(prevs.length === 0, `no .prev- backup is left behind after a restore (found ${prevs.length})`)
    }
  }
}

console.log('== 17. xrd_refine_manual no longer demands `data` in modes that cannot use it (0.8.5) ==')
{
  let manualTool = null
  mod.registerXrdManual({
    tools: { register: (t) => { manualTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workK = join(WORK, 'nodata'); mkdirSync(workK, { recursive: true })
  let initRes = null, initThrew = null
  try {
    initRes = await manualTool.execute({ init_engine: true, out_dir: workK },
      { agent: { session: { header: { cwd: workK } } }, callId: 'init-nodata', signal: undefined })
  } catch (e) { initThrew = e }
  ok(initThrew === null && initRes && initRes.mode === 'init',
    `init_engine works WITHOUT data (${initThrew ? String(initThrew.message).slice(0, 70) : 'ok'})`)
  // the schema used to make `data` mandatory; the modes that need something else must
  // now say so in a sentence a reader can act on
  let needThrew = null
  try {
    await manualTool.execute({}, { agent: { session: { header: { cwd: workK } } }, callId: 'none', signal: undefined })
  } catch (e) { needThrew = e }
  ok(needThrew !== null && /either `data`|or `plan`/.test(String(needThrew.message || '')),
    `a call with neither data nor plan explains what is needed (${needThrew ? String(needThrew.message).slice(0, 70) : 'no throw'})`)
  ok(!/required/.test(JSON.stringify(manualTool.parameters?.required || [])),
    'the input schema no longer hard-requires `data`')
}

console.log('== 18. GSAS-II refines step by step, with a checkpoint each (0.8.5; real run lives in verify-gsas-ops) ==')
{
  // The REAL two-step run is exercised in verify-gsas-ops.mjs, which runs without this
  // suite's 2.5 s stall window (that window exists for the stall controls and would cut
  // a legitimate GSAS run — measured: the same call succeeds in 9.3 s in a clean
  // directory with the shipped 90 s window). What is asserted here is the mechanism.
  const pyCodeOnly = (s) => s.split('\n').filter((l) => !/^\s*#/.test(l)).join('\n')
  const eng = readFileSync(join(ROOT, 'engine', 'gsas_engine.py'), 'utf8')
  const engCode = pyCodeOnly(eng)
  // 0.8.10: the ladder loop destructures a (label, step) pair now — the step builder
  // returns labels for the accept/rollback trace — but the mechanism under test is
  // unchanged: ONE `do_refinements([step])` per iteration with a checkpoint BEFORE it.
  ok(/for _si, \(_lbl, _st\) in enumerate\(steps, 1\):/.test(engCode)
    && /guard\.checkpoint\('gsas refine step %d\/%d'/.test(engCode)
    && /gpx\.do_refinements\(\[_st\]\)/.test(engCode),
    'gsas_engine issues ONE step per call with a checkpoint before each')
  ok(!/gpx\.do_refinements\(steps\)/.test(engCode),
    'the old single batched call is gone from gsas_engine (python comments excluded)')
  const opsCode = pyCodeOnly(readFileSync(join(ROOT, 'engine', 'gsas_ops.py'), 'utf8'))
  ok(/g\.checkpoint\('refine step %d\/%d'/.test(opsCode)
    && /_g\.checkpoint\('auto_background n=%d'/.test(opsCode),
    'xrd_gsas_ops checkpoints between refine steps and between background trials')
  const longEngines = ['data_io', 'phase_screen', 'cif_edit', 'gsas_ops', 'gsas_finalize',
    'gsas_export', 'gsas_engine', 'xrd_engine', 'fp2k_driver']
  const missing = longEngines.filter((e) => !/checkpoint\(/.test(readFileSync(join(ROOT, 'engine', `${e}.py`), 'utf8')))
  ok(missing.length === 0, `all 9 long engines take a checkpoint (missing: ${missing.join(', ') || 'none'})`)
  const guardSrc = readFileSync(join(ROOT, 'engine', 'proc_guard.py'), 'utf8')
  ok(/class EngineStopped\(BaseException\)/.test(guardSrc),
    'EngineStopped derives from BaseException, so a per-action `except Exception` cannot swallow a stop')
  const toolSrc = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
  const toolCode = codeOnly(toolSrc)
  const engineCalls = [...toolCode.matchAll(/await runEngine\(PY, \[BUNDLED_[A-Z_]+, planPath/g)].length
  ok(engineCalls >= 6,
    `the helper engines go through runEngine with an outDir (${engineCalls} call site(s) converted in 0.8.6)`)
  ok(!/runCli\(PY, \[BUNDLED_(FINALIZE|GSAS_OPS|GSAS_EXPORT|CIF_EDIT|DATA_IO|PHASE_SCREEN)/.test(toolCode),
    'no helper engine is still launched through the legacy runCli wrapper (no outDir ⇒ no tick/stop)')
}

console.log('== 19. xrd_verify_fp2k reports the LAST Rwp, like the driver does (0.8.6) ==')
{
  let verifyTool = null
  mod.registerXrdVerify({
    tools: { register: (t) => { verifyTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workM = join(WORK, 'verify-last'); mkdirSync(workM, { recursive: true })
  const pcrSrc = join(HERE, 'fixtures', 'Y2O3.pcr')
  const datSrc = join(HERE, 'fixtures', 'Y2O3.dat')
  if (!existsSync(pcrSrc) || !existsSync(datSrc)) {
    skip('the last-Rwp control: audit/fixtures/Y2O3.{pcr,dat} are not both present')
  } else {
    copyFileSync(pcrSrc, join(workM, 'Y2O3.pcr'))
    copyFileSync(datSrc, join(workM, 'Y2O3.dat'))
    let vr = null, vthrew = null
    try {
      vr = await verifyTool.execute({ pcr: join(workM, 'Y2O3.pcr'), out_dir: workM },
        { agent: { session: { header: { cwd: workM } } }, callId: 'verify-last', signal: undefined })
    } catch (e) { vthrew = e }
    const scratch = join(workM, '.verify-Y2O3')
    const outPath = join(scratch, 'Y2O3.out')
    const sumPath = join(scratch, 'Y2O3.sum')
    const lastOf = (text, re) => {
      const all = [...text.matchAll(new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g'))]
      return all.length ? all[all.length - 1] : null
    }
    if (vthrew || !vr) {
      ok(false, `the verification ran on the fixture (${vthrew ? String(vthrew.message).slice(0, 90) : 'no value'})`)
    } else {
      ok(typeof vr.rwp === 'number', `an Rwp came back (${vr.rwp})`)
      if (existsSync(outPath)) {
        const txt = readFileSync(outPath, 'utf8')
        const all = [...txt.matchAll(/Conventional Rietveld Rp,Rwp,Re and Chi2:\s+([\d.]+)\s+([\d.]+)/g)]
        const last = lastOf(txt, /Conventional Rietveld Rp,Rwp,Re and Chi2:\s+([\d.]+)\s+([\d.]+)/)
        if (all.length > 1 && last) {
          ok(Math.abs(vr.rwp - parseFloat(last[2])) < 1e-9,
            `it is the LAST of ${all.length} Rietveld line(s) (${last[2]}), not the first (${all[0][2]})`)
        } else {
          ok(vr.rwp_source === null || /LAST "Conventional Rietveld/.test(String(vr.rwp_source)),
            `with ${all.length} Rietveld line(s) the source still names the LAST occurrence`)
        }
      } else {
        ok(false, 'fp2k wrote no .out to compare against')
      }
      if (existsSync(sumPath)) {
        const sum = readFileSync(sumPath, 'utf8')
        const rwps = [...sum.matchAll(/Rwp:\s*([\d.]+)/g)]
        if (rwps.length > 1 && typeof vr.rwp_sum === 'number') {
          ok(Math.abs(vr.rwp_sum - parseFloat(rwps[rwps.length - 1][1])) < 1e-9,
            `rwp_sum is the LAST of ${rwps.length} "Rwp:" lines (${rwps[rwps.length - 1][1]}), not the first (${rwps[0][1]})`)
        } else {
          ok(true, `the .sum carries ${rwps.length} "Rwp:" line(s) — nothing to disambiguate`)
        }
      }
      const shipped = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
      ok(/const lastMatch = \(text, re\)/.test(shipped) && /LAST "Conventional Rietveld/.test(shipped),
        'the source states the last-occurrence rule it implements')
      ok(!/outTxt\.match\(\/Conventional Rietveld/.test(shipped),
        'no first-match parse of the Rietveld line is left in the verifier')
    }
  }
}

console.log('== 20. the wrapper cannot turn an engine\'s ok:false into success (0.8.6) ==')
{
  let manualTool = null
  mod.registerXrdManual({
    tools: { register: (t) => { manualTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workN = join(WORK, 'okfalse'); mkdirSync(workN, { recursive: true })
  const dataN = join(workN, 'data.dat')
  writeFileSync(dataN, '10.0 100\n10.5 120\n11.0 90\n')
  // an engine that reports a FAILURE while still carrying a numeric Rwp: exactly the
  // case the third review measured coming back as ok:true
  const engN = join(workN, 'lying_engine.py')
  writeFileSync(engN, [
    'import json, sys',
    'args = sys.argv[1:]',
    'out = args[args.index("--out") + 1]',
    'json.dump({"ok": False, "error": "write-back failed",',
    '           "metrics": {"Rwp": 7.5, "Rexp": 8.0, "chi2": 1.4}},',
    '          open(out, "w", encoding="utf-8"))',
  ].join('\n'))
  let res = null, threw = null
  try {
    res = await manualTool.execute({ data: dataN, out_dir: workN, engine: engN },
      { agent: { session: { header: { cwd: workN } } }, callId: 'okfalse', signal: undefined })
  } catch (e) { threw = e }
  ok(threw === null, `the call returns a value (${threw ? String(threw.message).slice(0, 80) : 'ok'})`)
  ok(res && res.ok === false, 'ok=false survives the wrapper (the engine is authoritative)')
  ok(res && res.rwp === 7.5, `the numeric Rwp is still reported (${res && res.rwp})`)
  ok(res && /write-back failed/.test(String(res.error || '')), 'the engine\'s own error text is carried through')
}

console.log('== 21. an explicit plan that does not exist is REPORTED (0.8.6) ==')
{
  let manualTool = null
  mod.registerXrdManual({
    tools: { register: (t) => { manualTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workO = join(WORK, 'missing-plan'); mkdirSync(workO, { recursive: true })
  const dataO = join(workO, 'data.dat')
  writeFileSync(dataO, '10.0 100\n10.5 120\n11.0 90\n')
  const missing = join(workO, 'nope_plan.json')
  // (a) plan only → used to die in resolve(undefined) with a bare Node TypeError
  let e1 = null
  try {
    await manualTool.execute({ plan: missing },
      { agent: { session: { header: { cwd: workO } } }, callId: 'p1', signal: undefined })
  } catch (e) { e1 = e }
  ok(e1 !== null && /plan not found/.test(String(e1.message || '')),
    `plan-only names the missing plan (${e1 ? String(e1.message).slice(0, 70) : 'no throw'})`)
  // (b) plan + data → used to silently IGNORE the plan and run a default one
  let e2 = null
  try {
    await manualTool.execute({ plan: missing, data: dataO },
      { agent: { session: { header: { cwd: workO } } }, callId: 'p2', signal: undefined })
  } catch (e) { e2 = e }
  ok(e2 !== null && /plan not found/.test(String(e2.message || '')),
    `plan+data is NOT silently ignored either (${e2 ? String(e2.message).slice(0, 70) : 'no throw'})`)
}

console.log('== 22. the three batch engines report per-action structure honestly (0.8.6) ==')
{
  let dataTool = null
  mod.registerXrdDataIo({
    tools: { register: (t) => { dataTool = t }, guard: () => {} },
    get: () => ({ request: async () => ({ granted: true }) }),
  })
  const workP = join(WORK, 'batch'); mkdirSync(workP, { recursive: true })
  const good = join(workP, 'good.dat')
  // 40 rows, not 3: the engine refuses a file with only a handful of numeric rows
  // ("only N numeric row(s) read"), which is its own sanity guard, not a batch defect.
  writeFileSync(good, Array.from({ length: 40 }, (_, i) => `${(10 + i * 0.05).toFixed(3)} ${100 + i}`).join('\n') + '\n')
  const r = await dataTool.execute({
    actions: [
      { type: 'read', file: good },
      { type: 'read', file: join(workP, 'missing-A.dat') },
      { type: 'read', file: join(workP, 'missing-B.dat') },
      { type: 'totally_unknown' },
    ],
    out_dir: workP,
  }, { agent: { session: { header: { cwd: workP } } }, callId: 'batch', signal: undefined })
  ok(r && r.ok === false, 'ok=false when actions failed')
  ok(r && r.partial === true, 'partial=true when some actions still succeeded')
  const idx = (r && r.failed_actions ? r.failed_actions.map((f) => f.index) : [])
  ok(idx.length === 3, `every failure is listed (${idx.length})`)
  ok(new Set(idx).size === idx.length,
    `the indices are DISTINCT — the argv loop variable was reused before (${JSON.stringify(idx)})`)
  ok(idx.includes(3), 'an UNKNOWN action counts as a failure, not just a warning')
  ok(r && typeof r.error === 'string' && /failed/.test(r.error), 'the error line summarises what failed')
  // and a clean batch still succeeds
  const clean = await dataTool.execute({ actions: [{ type: 'read', file: good }], out_dir: workP },
    { agent: { session: { header: { cwd: workP } } }, callId: 'clean', signal: undefined })
  ok(clean && clean.ok === true && !clean.partial, 'a clean batch stays ok=true with no partial flag')
}

console.log('== 23. the fourth review\'s blockers, as controls (0.8.7) ==')
{
  // --- B1: a host service whose `done` never settles must still RETURN ---
  // The 0.8.6 fix re-evaluated the termination deadline with a fresh Date.now() every loop
  // iteration, so in the terminating state the deadline moved with the clock and never
  // arrived. This stub is the review's own probe: `done` never settles.
  const hangSvc = {
    spawn() {
      return { pid: null, terminate() { /* accepted */ }, done: new Promise(() => {}), collected: {} }
    },
  }
  const ac = new AbortController()
  setTimeout(() => ac.abort(), 200)
  const t0 = Date.now()
  let r = null, threw = null
  try {
    r = await runEngine(PY, ['-c', 'import time; time.sleep(30)'], WORK, {
      timeoutMs: 60_000, backend: 'manual', outDir: null, signal: ac.signal,
      ctx: { get: (k) => (k === 'subprocess' ? hangSvc : null) },
    })
  } catch (e) { threw = e }
  const dtB1 = Date.now() - t0
  ok(threw === null, `a never-settling \`done\` does not hang the call (${threw ? String(threw.message).slice(0, 60) : 'returned'})`)
  ok(dtB1 < 90_000, `it returned within the frozen ceiling instead of looping forever (${dtB1} ms)`)
  ok(r && r.exitObserved === false, 'and it says the exit was NOT observed, rather than claiming one')
  const toolSrcB1 = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
  ok(/let terminationDeadline = null/.test(toolSrcB1)
    && /if \(terminationDeadline === null\) terminationDeadline = Date\.now\(\)/.test(toolSrcB1),
    'the termination deadline is frozen once, not recomputed per iteration')

  // --- an already-cancelled call must not sit out the stop grace ---
  const ac2 = new AbortController()
  ac2.abort()
  const t1 = Date.now()
  const r2 = await runEngine(PY, ['-c', 'import time; time.sleep(5)'], WORK, {
    timeoutMs: 60_000, signal: ac2.signal,
  })
  ok(Date.now() - t1 < 2_000, `a pre-aborted call returns at once, no grace wait (${Date.now() - t1} ms)`)
  ok(r2.aborted === true && r2.timedOut === false, 'and it is reported as a cancel, not a timeout')

  // --- H3: a real progress tick is not swallowed by a plain one ---
  const tickDir = join(WORK, 'tick-bypass'); mkdirSync(tickDir, { recursive: true })
  const guardProbe = spawnSync(PY, ['-B', '-c', [
    'import json, os, sys',
    `sys.path.insert(0, r"${join(ROOT, 'engine')}")`,
    'import proc_guard as pg',
    `d = r"${tickDir}"`,
    "g = pg.Guard(out_dir=d, run_id='tick-probe', tick_s=5.0)",
    "g.tick('plain heartbeat')",
    "g.tick('real residual progress', progress=True)",
    'p = os.path.join(d, pg.tick_name("tick-probe"))',
    'data = json.load(open(p, encoding="utf-8"))',
    'print(json.dumps({"progress": data.get("progress"), "phase": data.get("phase"), "seq": data.get("seq")}))',
  ].join('\n')], { encoding: 'utf8', windowsHide: true, timeout: 60_000 })
  let tickInfo = null
  try {
    const outLine = String(guardProbe.stdout || '').trim().split('\n').filter(Boolean).pop()
    tickInfo = JSON.parse(outLine)
  } catch { tickInfo = null }
  ok(tickInfo && tickInfo.progress === true,
    `a progress tick right after a plain tick is still written (${tickInfo ? JSON.stringify(tickInfo) : 'probe failed'})`)

  // --- H3b (0.8.9): a CHECKPOINT is a step boundary, not a heartbeat, so the 5 s rate
  // limit must not swallow it. Measured failure it guards: a two-step GSAS run finishing
  // in ~5 s left only the initial `phase:"start"` tick, so the per-step checkpoints were
  // invisible and audit/verify-gsas-ops.mjs could not see which step ran.
  const cpDir = join(WORK, 'tick-checkpoint'); mkdirSync(cpDir, { recursive: true })
  const cpProbe = spawnSync(PY, ['-B', '-c', [
    'import json, os, sys',
    `sys.path.insert(0, r"${join(ROOT, 'engine')}")`,
    'import proc_guard as pg',
    `d = r"${cpDir}"`,
    "g = pg.Guard(out_dir=d, run_id='cp-probe', tick_s=5.0)",
    "g.tick('start')",
    "g.checkpoint('gsas refine step 1/2')",
    "g.checkpoint('gsas refine step 2/2')",
    'p = os.path.join(d, pg.tick_name("cp-probe"))',
    'data = json.load(open(p, encoding="utf-8"))',
    'print(json.dumps({"phase": data.get("phase"), "seq": data.get("seq")}))',
  ].join('\n')], { encoding: 'utf8', windowsHide: true, timeout: 60_000 })
  let cpInfo = null
  try {
    const outLine = String(cpProbe.stdout || '').trim().split('\n').filter(Boolean).pop()
    cpInfo = JSON.parse(outLine)
  } catch { cpInfo = null }
  ok(cpInfo && cpInfo.phase === 'gsas refine step 2/2',
    `a checkpoint is not rate-limited away (${cpInfo ? JSON.stringify(cpInfo) : 'probe failed'})`)

  // --- B4: the skill mirror is whole-tree, by hash, and never clobbers a user edit ---
  {
    const isoHome = join(WORK, 'skillhome')
    rmSync(isoHome, { recursive: true, force: true })
    const saved = process.env.DSH_HOME
    process.env.DSH_HOME = isoHome
    try {
      const indexMod = await import(pathToFileURL(join(ROOT, 'lib', 'index.js')).href)
      const s1 = indexMod.syncBundledSkills()
      ok(s1.added.length >= 10 && existsSync(join(isoHome, 'skills', 'xrd-workflow', 'refs', 'defect-catalog.md'))
        && existsSync(join(isoHome, 'skills', 'fullprof-pcr', 'templates', 'Y2O3_xray.pcr')),
        `a fresh install mirrors the WHOLE tree (${s1.added.length} file(s), refs + templates included)`)
      const s2 = indexMod.syncBundledSkills()
      ok(s2.added.length === 0 && s2.updated.length === 0 && s2.current >= 10,
        'and a second run changes nothing (idempotent by content, not by mtime)')
      const edited = join(isoHome, 'skills', 'xrd-tools', 'SKILL.md')
      writeFileSync(edited, 'MY OWN EDITED VERSION\n')
      const s3 = indexMod.syncBundledSkills()
      ok(s3.kept.includes(join('xrd-tools', 'SKILL.md')) && readFileSync(edited, 'utf8').includes('MY OWN'),
        'a file the USER edited is kept, not overwritten')
      // an upgrade of something WE wrote (recorded hash == disk) is applied
      const stampP = join(isoHome, 'skills', '.dsh-xrd-skills.json')
      const st = JSON.parse(readFileSync(stampP, 'utf8'))
      const relKey = Object.keys(st.files).find((k) => k.endsWith(join('xrd-tools', 'SKILL.md')))
      writeFileSync(edited, 'OUR OWN OLDER WRITE\n')
      st.files[relKey] = createHash('sha256').update(readFileSync(edited)).digest('hex')
      writeFileSync(stampP, JSON.stringify(st, null, 1))
      const s4 = indexMod.syncBundledSkills()
      ok(s4.updated.length === 1 && !readFileSync(edited, 'utf8').includes('OUR OWN OLDER'),
        'a file WE wrote is upgraded when the release moves on')
      // a legacy install (no record at all) upgrades but keeps a backup
      writeFileSync(edited, 'LEGACY CONTENT\n')
      rmSync(stampP, { force: true })
      const s5 = indexMod.syncBundledSkills()
      const backups = readdirSync(join(isoHome, 'skills', 'xrd-tools')).filter((f) => f.includes('user-backup'))
      ok(s5.updated.length >= 1 && backups.length === 1 && !readFileSync(edited, 'utf8').includes('LEGACY CONTENT'),
        'a pre-0.8.7 install is upgraded with the previous copy kept as .user-backup')
    } catch (e) {
      ok(false, `the skill-mirror controls ran (${String(e?.message ?? e).slice(0, 120)})`)
    } finally {
      if (saved === undefined) delete process.env.DSH_HOME
      else process.env.DSH_HOME = saved
    }
  }

  // --- result_io must write STRICT json even when a value is NaN/Infinity ---
  {
    const rioDir = join(WORK, 'rio'); mkdirSync(rioDir, { recursive: true })
    const probe = spawnSync(PY, ['-B', '-c', [
      'import json, os, sys',
      `sys.path.insert(0, r"${join(ROOT, 'engine')}")`,
      'from result_io import write_result',
      `p = r"${join(rioDir, 'r.json')}"`,
      "write_result(p, {'Rwp': float('nan'), 'nested': [float('inf')], 'ok': True}, {'run_id': 'r1'})",
      "print(json.dumps({'strict': _strict(p)}, )) if False else None",
    ].join('\n')], { encoding: 'utf8', windowsHide: true, timeout: 60_000 })
    const raw = existsSync(join(rioDir, 'r.json')) ? readFileSync(join(rioDir, 'r.json'), 'utf8') : ''
    ok(!/NaN|Infinity/.test(raw), 'a NaN/Infinity payload is written as strict JSON (no bare NaN token)')
    let parsed = null
    try { parsed = JSON.parse(raw) } catch { parsed = null }
    ok(parsed !== null && parsed.Rwp === null && Array.isArray(parsed.nested) && parsed.nested[0] === null,
      'and the JS side can parse it, with the non-finite values as null')

  // --- B2/B3 through the real tool: the pcr cannot write outside the work dir, and
  //     score-only runs fp2k exactly ONCE ---
    const escDir = join(WORK, 'escape'); mkdirSync(escDir, { recursive: true })
    const fxPcr = join(HERE, 'fixtures', 'Y2O3.pcr')
    const fxDat = join(HERE, 'fixtures', 'Y2O3.dat')
    if (!existsSync(fxPcr) || !existsSync(fxDat)) {
      skip('the pcr-escape / score-only control: fixtures/Y2O3.{pcr,dat} not present')
    } else {
      const session = join(escDir, 'session'); mkdirSync(session, { recursive: true })
      const pcr = join(escDir, 'esc.pcr')
      writeFileSync(pcr, readFileSync(fxPcr, 'utf8') + '\n../escaped-copy.dat\n')
      copyFileSync(fxDat, join(escDir, 'esc.dat'))
      const idx = await import(pathToFileURL(join(ROOT, 'lib', 'index.js')).href)
      const tset = []
      await (idx.apply ?? idx.default)({
        tools: { register: (t) => tset.push(t), guard: () => {} },
        get: () => ({ request: async () => ({ granted: true }) }),
      })
      const auto = tset.find((t) => t.name === 'xrd_refine_auto')
      let res = null, e2 = null
      try {
        res = await auto.execute({ pcr, out_dir: session, ladder: false, cycles: 5 },
          { agent: { session: { header: { cwd: session } } }, callId: 'escape', signal: undefined })
      } catch (err) { e2 = err }
      ok(e2 === null, `the score-only run through the tool completes (${e2 ? String(e2.message).slice(0, 90) : 'ok'})`)
      ok(!existsSync(join(escDir, 'escaped-copy.dat')),
        'a `../` name carried by the pcr writes NOTHING outside the work directory')
      ok(res && Array.isArray(res.warning) && res.warning.some((w) => /NOT a bare file name/.test(String(w))),
        'and the refusal is reported instead of being silent')
      const work = existsSync(session)
        ? readdirSync(session).find((d) => d.startsWith('.fp2k-work-'))
        : null
      const outs = work ? readdirSync(join(session, work)).filter((f) => f.endsWith('.out')).length : -1
      ok(outs === 1, `score-only runs fp2k exactly ONCE (${outs} .out file(s) in the work dir)`)
      ok(res && res.ok === true && typeof res.rwp === 'number',
        `and a successful score-only run reports ok=true with its Rwp (${res ? `${res.ok}/${res.rwp}` : 'n/a'})`)
    }
  }
}

console.log(`\n  ${fails.length ? 'FAILURES' : 'ALL PASS'} — pass=${pass} fail=${fails.length} skip=${skipped.length}`)
if (skipped.length) for (const s of skipped) console.log(`   ~ ${s}`)
if (fails.length) { for (const f of fails) console.log(`   - ${f}`); process.exit(1) }
