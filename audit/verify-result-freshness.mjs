// verify-result-freshness.mjs — P0 (0.8.3): only a result THIS run wrote may be
// reported as this run's result.
//
// WHY (codex audit of 0.8.1, 2026-09-27): every tool read its engine result from
// a fixed name inside the artifact dir (gsas_result.json, data_io_result.json,
// …). A run that timed out or crashed before writing therefore re-reported the
// PREVIOUS run's file as its own — measured on the manual engine, where a stub
// that wrote nothing returned the earlier run's rwp = 5.0 with ok = true. Only
// the manual engine was guarded; seven other call sites were not.
//
// Two gates now apply everywhere (lib/xrd-tools.js: readFreshResult):
//   * the result's mtime must ADVANCE past a snapshot taken before the child
//     started (a file from an earlier run can never pass);
//   * when the engine echoed a `run_id` (it arrives in the plan the engine
//     already reads), the id must match this run's.
// The engine side (engine/result_io.py) writes atomically (tmp + rename), so a
// killed run cannot leave a half-written file either.
//
// Evidence grade: (b) isolated control — a real engine child process, real
// files, and a negative control that reproduces the old bug on the same fixture.
//
// Usage: node verify-result-freshness.mjs [--root <plugin dir>]
import { readFileSync, writeFileSync, mkdirSync, existsSync, rmSync, copyFileSync, utimesSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { python, pluginRoot } from './_lib.mjs'

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
try {
  python()
} catch (e) {
  console.error(`python not found: ${e?.message ?? e} (set DSH_XRD_PYTHON or install Python 3)`)
  process.exit(2)
}

let pass = 0
const fails = []
const skipped = []
const ok = (cond, label) => { if (cond) { pass++; console.log(`  PASS  ${label}`) } else { fails.push(label); console.log(`  FAIL  ${label}`) } }
const skip = (label) => { skipped.push(label); console.log(`  SKIP  ${label}`) }

// Dot-prefixed ON PURPOSE: this module ships inside the package (audit/), and the
// builder excludes dot-entries from that copy — scratch belongs in a disposable place.
const WORK = join(HERE, '.result-freshness-work')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })

const dataFile = join(WORK, 'two_col.dat')
writeFileSync(dataFile, Array.from({ length: 80 }, (_, i) => `${10 + i * 0.25} ${500 + i * 3}`).join('\n'))

const mod = await import(pathToFileURL(join(ROOT, 'lib', 'xrd-tools.js')).href)

console.log('== 1. readFreshResult: every refusal branch ==')
const { readFreshResult, resultMtime, planRunId, newRunId } = mod
ok(typeof readFreshResult === 'function', 'readFreshResult is exported')
ok(typeof resultMtime === 'function' && typeof planRunId === 'function' && typeof newRunId === 'function',
  'the helpers are exported (resultMtime / planRunId / newRunId)')

const unitDir = join(WORK, 'unit')
mkdirSync(unitDir, { recursive: true })

let r = readFreshResult(join(unitDir, 'absent.json'), { preMtime: -1, runId: 'x' })
ok(r.out === null && /no result file/.test(r.reason || ''), 'a missing file is refused: "no result file"')

const good = join(unitDir, 'good.json')
writeFileSync(good, JSON.stringify({ ok: true, run_id: 'RID-1', payload: 42 }))
r = readFreshResult(good, { preMtime: -1, runId: 'RID-1' })
ok(r.out && r.out.payload === 42, 'a matching run_id is accepted')
ok(r.out && !('run_id' in r.out), 'run_id is stripped from the payload (no output-schema change)')

r = readFreshResult(good, { preMtime: -1, runId: 'RID-2' })
ok(r.out === null && /another run/.test(r.reason || ''), 'a different run_id is refused: "belongs to another run"')

writeFileSync(good, '{ this is not json')
r = readFreshResult(good, { preMtime: -1, runId: null })
ok(r.out === null && /unreadable|half-written/.test(r.reason || ''), 'an unparsable file is refused')

writeFileSync(good, JSON.stringify({ ok: true, run_id: 'RID-1' }))
const now = Date.now()
utimesSync(good, new Date(now - 60_000), new Date(now - 60_000))
r = readFreshResult(good, { preMtime: now, runId: 'RID-1' })
ok(r.out === null && /stale/.test(r.reason || ''), 'mtime that did not advance is refused: "stale"')
r = readFreshResult(good, { preMtime: -1, runId: 'RID-1' })
ok(r.out !== null, 'the same file is accepted when no pre-run snapshot exists (fresh first run)')
ok(newRunId() !== newRunId(), 'newRunId() yields distinct ids')

console.log('== 2. a real engine run: plan carries run_id, result echoes it, tool output does not ==')
let tool = null
const ctx = {
  tools: { register: t => { tool = t }, guard: () => {} },
  get: () => ({ request: async () => ({ granted: true }) }),   // stub approval
}
mod.registerXrdDataIo(ctx)
if (!tool) { console.error('registerXrdDataIo did not register a tool'); process.exit(2) }
ok(tool.name === 'xrd_data_io', 'registered xrd_data_io')

const runDir = join(WORK, 'run1')
mkdirSync(runDir, { recursive: true })
const exec = { agent: { session: { header: { cwd: WORK } } }, callId: 'test', signal: undefined }
let res = null
let threw = null
try {
  res = await tool.execute({ out_dir: runDir, actions: [{ type: 'read', file: dataFile }] }, exec)
} catch (e) { threw = e }
if (threw) console.log(`  [diag] ${String(threw.message).split('\n').slice(0, 3).join(' | ')}`)
// A restricted sandbox denies spawning a child with piped stdio (named pipes).
// That is an ENVIRONMENT limit, not a verdict on the code: report SKIP and keep
// the sections that need no child process. "Ran but was blocked" is never a pass.
const blocked = !!threw && /EPERM|spawn EPERM|pipe/i.test(String(threw?.message ?? ''))
if (blocked) {
  skip('sections 2b/3 need a real engine child process — sandbox denied spawn (EPERM); rerun with one escalation')
} else {
  ok(threw === null, 'the real data_io run returns a value')
  ok(res && res.ok === true, 'ok=true from the real engine')
  ok(res && Array.isArray(res.datasets) && res.datasets.length === 1, 'the engine payload really arrived (1 dataset)')
  ok(res && !('run_id' in res), 'the returned payload carries no run_id (schema untouched)')
}

const planFile = join(runDir, 'data_io_plan.json')
const resultFile = join(runDir, 'data_io_result.json')
ok(existsSync(planFile), 'the plan file was written')
const onDisk = existsSync(resultFile) ? JSON.parse(readFileSync(resultFile, 'utf8')) : null
ok(!!onDisk, 'the engine wrote its result file')
if (onDisk) {
  ok(typeof onDisk.run_id === 'string' && onDisk.run_id.length > 0, 'the on-disk result carries a run_id')
  ok(planRunId(planFile) === onDisk.run_id, 'the plan\'s run_id and the echoed run_id are the same id')
  ok(resultMtime(resultFile) > 0, 'resultMtime() reports the file\'s mtime')
} else {
  skip('the on-disk result assertions (no engine result was produced in this environment)')
}

console.log('== 3. negative control: the same fixture reproduces the OLD bug ==')
const staleDir = join(WORK, 'stale')
mkdirSync(staleDir, { recursive: true })
const staleFile = join(staleDir, 'data_io_result.json')
// The fixture must be a real engine result; when the sandbox blocked the child,
// synthesise the same shape so the control still exercises the gate itself.
if (existsSync(resultFile)) copyFileSync(resultFile, staleFile)
else writeFileSync(staleFile, JSON.stringify({ ok: true, run_id: 'run-from-an-earlier-attempt', datasets: [{ n_points: 80 }] }))
const old = Date.now() - 120_000
utimesSync(staleFile, new Date(old), new Date(old))

// (a) the pre-fix read: parse whatever is at the fixed path, no questions asked
const naive = JSON.parse(readFileSync(staleFile, 'utf8'))
ok(naive.ok === true, 'negative control: the leftover file looks like a SUCCESSFUL result (that is what made the bug dangerous)')
ok(typeof naive.run_id === 'string', 'negative control: the leftover file even carries the older run\'s id, which the old code ignored')

// (b) the gated read must refuse exactly that file
r = readFreshResult(staleFile, { preMtime: Date.now(), runId: newRunId() })
ok(r.out === null, 'the gate refuses the leftover file')
ok(/stale/.test(r.reason || ''), `and says why (${r.reason})`)
ok(existsSync(staleFile), 'the gate does not delete the older file — it stays on disk, just unused')

console.log('== 4. engine side: atomic write + run identity are in place everywhere ==')
const engines = ['data_io', 'phase_screen', 'cif_edit', 'gsas_ops', 'gsas_finalize', 'gsas_export',
  'gsas_engine', 'xrd_engine', 'fp2k_driver']
ok(existsSync(join(ROOT, 'engine', 'result_io.py')), 'engine/result_io.py exists')
for (const e of engines) {
  const src = readFileSync(join(ROOT, 'engine', `${e}.py`), 'utf8')
  ok(/from result_io import write_result/.test(src), `${e}.py imports the shared writer`)
  ok(/write_result\(/.test(src), `${e}.py calls write_result(...)`)
  ok(!/json\.dump\(result, f, ensure_ascii=False, indent=1, default=float\)/.test(src),
    `${e}.py no longer dumps its result with a bare open()/json.dump()`)
}
const rio = readFileSync(join(ROOT, 'engine', 'result_io.py'), 'utf8')
ok(/os\.replace\(tmp, path\)/.test(rio), 'result_io writes to a temp name and renames it (atomic)')
ok(/fsync/.test(rio), 'result_io fsyncs before the rename')

console.log('== 5. every tool call site goes through the shared gate ==')
const toolsSrc = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
const idxSrc = readFileSync(join(ROOT, 'lib', 'index.js'), 'utf8')
const gateCount = (toolsSrc.match(/readFreshResult\(/g) || []).length - 1 // minus the definition line is not counted (export function)
const planIds = (toolsSrc.match(/run_id: newRunId\(\)/g) || []).length
// 0.8.10: NINE plan builders now — the multiphase entry added one. The second assertion is
// tied to planIds on purpose: a new tool that builds a plan WITHOUT a run_id still fails
// here, while a tool that adds a plan+gate pair passes without editing this file twice.
ok(planIds === 9, `all 9 plans carry a run_id (found ${planIds})`)
ok(gateCount >= planIds, `at least as many readFreshResult() call sites as plan builders (plans ${planIds}, gates ${gateCount})`)
ok(/readFreshResult\(/.test(idxSrc) && /run_id: runId/.test(idxSrc),
  'xrd_refine_auto (index.js) also carries a run_id and gates its result read')
ok(!/driver = JSON\.parse\(readFileSync\(resultPath/.test(idxSrc),
  'xrd_refine_auto no longer parses its result file unguarded')
// The gate's OWN body contains exactly that parse — exclude the helper before
// asking whether any OTHER site still parses a result unguarded (a detector that
// matches itself is the same false-positive class the leak audit hit before).
const helperStart = toolsSrc.indexOf('export function newRunId')
const helperEnd = toolsSrc.indexOf('// A5: runCli supports an AbortSignal')
ok(helperStart > 0 && helperEnd > helperStart, 'the gate helper block was located in the source')
const outsideHelper = helperStart > 0 && helperEnd > helperStart
  ? toolsSrc.slice(0, helperStart) + toolsSrc.slice(helperEnd)
  : toolsSrc
ok(!/JSON\.parse\(readFileSync\(resultFile/.test(outsideHelper),
  'no unguarded JSON.parse(readFileSync(resultFile…)) call site remains outside the gate itself')

console.log(`\n  ${fails.length ? 'FAILURES' : 'ALL PASS'} — pass=${pass} fail=${fails.length} skip=${skipped.length}`)
if (skipped.length) for (const s of skipped) console.log(`   ~ ${s}`)
if (fails.length) { for (const f of fails) console.log(`   - ${f}`); process.exit(1) }
