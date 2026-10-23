// verify-manual-timeout.mjs — D18① regression: the manual engine must FAIL
// STRUCTURED (artifacts named) when its internal budget expires, instead of
// letting the host's tool timeout produce a bare `tool call timed out`.
//
// WHY A REAL PROCESS: the claim under test is about process lifetime and the
// kill path, so the test spawns a REAL python that prints a heartbeat and then
// sleeps forever. The internal budget is lowered through the documented
// DSH_XRD_MANUAL_BUDGET_MS override (2.5 s) — the shipped default pair
// (620 s internal / 660 s tool) is asserted statically from the source, because
// waiting 620 s in a test is not a test anyone would run.
//
// Evidence grade: (b) isolated control — real spawn, real kill, stub approval.
//
// Usage: node verify-manual-timeout.mjs [--root <plugin dir>]
import { readFileSync, writeFileSync, mkdirSync, existsSync, rmSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
// Python comes from the SHARED resolver in audit/_lib.mjs (env → conventional install
// locations → PATH). It must never be a literal path: this file ships INSIDE the package
// (audit/), so a hardcoded user-profile path both leaks the maintainer's machine and
// makes the test fail on every other machine (codex review 2026-09-27).
import { python, pluginRoot } from './_lib.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const argRoot = (() => {
  const i = process.argv.indexOf('--root')
  return i >= 0 && process.argv[i + 1] ? resolve(process.argv[i + 1]) : null
})()
// pluginRoot() is package-aware: inside a release tree it returns the package root, so a
// package-internal `verify-all` finds the plugin without --root. The old hardcoded
// `../../dsh-xrd` fallback only ever worked in the maintainer's dev layout — measured
// 2026-10-02, running the package's own audit died here with "no plugin tree at
// …\dist\dsh-xrd" and exited 2 (which verify-all then mislabelled MISSING).
const ROOT = argRoot || pluginRoot()
if (!existsSync(join(ROOT, 'lib', 'xrd-tools.js'))) {
  console.error(`no plugin tree at ${ROOT}`)
  process.exit(2)
}
let PY
try {
  PY = python()
} catch (e) {
  console.error(`python not found: ${e?.message ?? e} (set DSH_XRD_PYTHON or install Python 3)`)
  process.exit(2)
}

let pass = 0
const fails = []
const ok = (cond, label) => { if (cond) { pass++; console.log(`  PASS  ${label}`) } else { fails.push(label); console.log(`  FAIL  ${label}`) } }

// Dot-prefixed ON PURPOSE: this module ships inside the package (audit/), and the
// builder excludes dot-entries from that copy. A `_`-prefixed name shipped instead, and
// its plan_calibrate.json carried the maintainer path — which the leak audit missed at
// first because JSON escapes the backslashes (both gaps are closed now, but the scratch
// still belongs in a disposable place, next to the audit module's own `.work/`).
const WORK = join(HERE, '.manual-timeout-work')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })

// A two-column pattern just good enough for buildPlan's existence check.
const dataFile = join(WORK, 'dummy.dat')
writeFileSync(dataFile, Array.from({ length: 60 }, (_, i) => `${10 + i * 0.5} ${1000 + i}`).join('\n'))

// Hang engine: accepts the engine CLI, prints a heartbeat, then never returns.
const hangEngine = join(WORK, 'hang_engine.py')
writeFileSync(hangEngine, [
  'import sys, time',
  'print("hang engine: started", flush=True)',
  'time.sleep(3600)',
].join('\n'))

// Fast stub engine: writes the minimal result the driver reads, so the SUCCESS
// path is exercised through the same budget machinery (positive control).
const fastEngine = join(WORK, 'fast_engine.py')
writeFileSync(fastEngine, [
  'import json, sys',
  'out = None',
  'args = sys.argv[1:]',
  'for i, a in enumerate(args):',
  '    if a == "--out":',
  '        out = args[i + 1]',
  'json.dump({"metrics": {"Rwp": 5.0, "Rexp": 6.0, "chi2": 1.44},',
  '           "components": "comp.json", "fit_status": {"success": True, "nfev": 12}},',
  '          open(out, "w", encoding="utf-8"))',
  'print("fast engine: wrote result", flush=True)',
].join('\n'))

process.env.DSH_XRD_MANUAL_BUDGET_MS = '2500'
process.env.DSH_XRD_MANUAL_TOOL_TIMEOUT_MS = '60000'
const mod = await import(pathToFileURL(join(ROOT, 'lib', 'xrd-tools.js')).href)

let tool = null
const ctx = {
  tools: { register: t => { tool = t }, guard: () => {} },
  get: () => ({ request: async () => ({ granted: true }) }),   // stub approval
}
mod.registerXrdManual(ctx)
if (!tool) { console.error('registerXrdManual did not register a tool'); process.exit(2) }
ok(tool.name === 'xrd_refine_manual', 'registered xrd_refine_manual')

const exec = { agent: { session: { header: { cwd: WORK } } }, callId: 'test', signal: undefined }

console.log('== 1. shipped defaults (static, source-read) ==')
const src = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
// 2026-09-28 (P2): the pair was RAISED because the manual engine became a
// heartbeat backend — a healthy fit is judged stuck only when its progress ticks
// stop, so these numbers are a ceiling, not an expected duration.
ok(/return Number\.isFinite\(v\) && v > 0 \? v : 1_500_000/.test(src), 'internal budget default = 1500 s')
ok(/return Number\.isFinite\(v\) && v > 0 \? v : 1_800_000/.test(src), 'tool timeout default = 1800 s')
ok(/timeoutMs: MANUAL_TOOL_TIMEOUT_MS/.test(src), 'the tool advertises the shared timeout constant (pair cannot drift)')
ok(/1_500_000/.test(src) && src.indexOf('1_500_000') < src.indexOf('1_800_000'),
  'internal budget is declared before / below the tool timeout')
ok(/MANUAL_RUN_BUDGET_MS/.test(src) && /MANUAL_TOOL_TIMEOUT_MS/.test(src),
  'both names exist (the env overrides keep working through them)')

console.log('== 2. budget expiry ⇒ structured failure (real process, real kill) ==')
const t0 = Date.now()
let res = null
let threw = null
try {
  res = await tool.execute({ data: dataFile, out_dir: WORK, engine: hangEngine }, exec)
} catch (e) { threw = e }
const dt = Date.now() - t0
if (threw) console.log(`  [diag] threw in ${dt} ms: ${String(threw.message).split('\n').slice(0, 3).join(' | ')}`)
ok(threw === null, 'the timeout returns a value instead of throwing')
ok(res && res.ok === false, 'ok=false on the structured failure')
ok(res && res.timed_out === true, 'timed_out=true')
ok(res && res.out_dir === resolve(WORK), `out_dir names the artifact directory (${res && res.out_dir})`)
ok(res && Array.isArray(res.artifacts), 'artifacts is a list')
ok(res && res.artifacts.includes('hang_engine.py'), 'artifact list names files really on disk in out_dir')
ok(res && /620 s|2500|2\.5|budget/i.test(res.error || ''), 'error explains the budget, not just "no result"')
ok(res && /MISSING R VALUE IS NOT A BAD FIT/.test(res.hint || ''), 'hint says a missing R value is not a bad fit')
ok(res && /artifacts preserved|do NOT simply retry/i.test(res.hint || ''), 'hint gives the next step and forbids a blind retry')
ok(res && res.plan_file && existsSync(res.plan_file), 'plan file really exists (kept on disk)')
ok(typeof res?.log_tail === 'string' && res.log_tail.length > 0, 'log tail carries the engine heartbeat')
ok(dt < 25_000, `call returned promptly after the budget (${dt} ms, well below the tool timeout)`)
// 0.8.5 — THE FUSE MUST NOT FIRE FIRST. With this test's 60 s tool timeout the 0.8.4
// fuse expression (`tool timeout - 60 s`) evaluated to 0, so the watchdog killed the
// run on its very first tick (1 s) and the "budget" the message named was a lie. The
// report says which reason actually fired, so the test can check the real one.
ok(res && !/reason hard-fuse/.test(res.error || ''),
  `the fuse did NOT fire instead of the budget (${(res?.error || '').match(/reason [a-z-]+/) || 'no reason'})`)
ok(dt >= 2_000, `it ran until the nominal budget, not until a zero fuse (${dt} ms ≥ 2000 ms)`)

console.log('== 3. render() leads with the verdict (what the model actually sees) ==')
const render = tool.output?.render
ok(typeof render === 'function', 'the tool exposes output.render')
const text = typeof render === 'function' ? render({}, res).map(b => b.text).join('\n') : ''
ok(/RESULT: failed \(timeout, artifacts preserved\)/.test(text), 'render says RESULT: failed (timeout, artifacts preserved)')
ok(/artifacts kept in:/.test(text), 'render prints the artifact directory')
ok(/HINT:/.test(text), 'render prints the hint')
ok(/MISSING R VALUE IS NOT A BAD FIT/.test(text), 'render carries the "not a bad fit" clarification')

console.log('== 4. negative control: a fast engine still succeeds (budget not over-eager) ==')
res = null
threw = null
try {
  res = await tool.execute({ data: dataFile, out_dir: WORK, engine: fastEngine }, exec)
} catch (e) { threw = e }
ok(threw === null && res && res.ok === true, 'fast engine ⇒ ok=true (no spurious timeout)')
ok(res && res.rwp === 5.0, `rwp read from the engine result (${res && res.rwp})`)
ok(!(res && res.timed_out), 'no timed_out flag on the success path')

console.log('== 5. no-result (non-timeout) path still names the scene ==')
const muteEngine = join(WORK, 'mute_engine.py')
writeFileSync(muteEngine, 'import sys\nprint("mute engine: nothing written", flush=True)\n')
threw = null
try { await tool.execute({ data: dataFile, out_dir: WORK, engine: muteEngine }, exec) } catch (e) { threw = e }
ok(threw !== null, 'a genuine engine fault still throws (not silently structured)')
ok(threw && /artifacts kept in/.test(threw.message), 'the throw names the artifact directory')
ok(threw && /R 值缺失 ≠ 拟合差/.test(threw.message), 'the throw warns that a missing R value is not a bad fit')
// 2026-09-27：这句话原本只有中文，而同义的英文只出现在超时路径的 hint 里 ⇒ 两条路径口径不一致。
// 现在中文原句保留、英文追加，并各带语言标记（让主 agent 看出是同一条警示的两种语言）。
ok(threw && /language-zh-/.test(threw.message) && /language-en-/.test(threw.message),
  'the caution carries both language markers')
ok(threw && /A missing R value is not a bad fit/i.test(threw.message),
  'the caution is also in English (same warning on both paths)')
ok(threw && threw.message.indexOf('language-zh-') < threw.message.indexOf('language-en-'),
  'Chinese block comes first, English second')

console.log('== 6. a stale result.json is never reported as this run\'s result ==')
// Found by section 5 of this very test: the fast stub's result.json was still in
// out_dir, so a stub that wrote NOTHING returned the previous run's rwp = 5.0 and
// ok = true. A crashed run must not inherit an older number.
const staleDir = join(WORK, 'stale')
mkdirSync(staleDir, { recursive: true })
const staleFile = join(staleDir, 'result.json')
writeFileSync(staleFile, JSON.stringify({ metrics: { Rwp: 1.23, Rexp: 2.0, chi2: 1.0 } }))
threw = null
let staleRes = null
try { staleRes = await tool.execute({ data: dataFile, out_dir: staleDir, engine: muteEngine }, exec) } catch (e) { threw = e }
ok(threw !== null, 'a run that writes nothing still throws even with an old result.json present')
ok(!staleRes || staleRes.rwp !== 1.23, `the stale Rwp 1.23 is NOT re-reported (got ${staleRes ? staleRes.rwp : 'a throw'})`)
ok(existsSync(staleFile), 'the freshness gate does not delete the older file (it stays on disk, just unused)')

console.log(`\n  ${fails.length ? 'FAILURES' : 'ALL PASS'} — pass=${pass} fail=${fails.length}`)
if (fails.length) { for (const f of fails) console.log(`   - ${f}`); process.exit(1) }
