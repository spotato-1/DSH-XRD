// ctrl-plateau-basis.mjs — control for the `r_plateau.basis` fix (bug 2).
//
// THE DEFECT: `basis` was a fixed string — "fit converged, no parameter on a bound, and
// nvar/npts below 0.2" — emitted unconditionally. When the fit had hit max_nfev or left a
// parameter on a bound, the sentence contradicted `is_plateau` and `fit_status`, and the
// skills tell the reader to trust `basis`. The VERDICT was always computed correctly; only
// the prose was wrong.
//
// THE CONTROL: drive the manual engine twice through the real tool layer, with max_nfev=1
// (must hit the ceiling, so a plateau must NOT be claimed) and with a generous ceiling
// (may converge). The two runs must not produce the same `basis` sentence.
import { existsSync, mkdirSync, rmSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
const WORK = join(WORK_ROOT, 'plateau')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', ENTRY)

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
await (mod.apply ?? mod.default)(ctx)
const manual = tools.find(t => t.name === 'xrd_refine_manual')
report.check(!!manual, 'xrd_refine_manual is registered', 'tool not found')

const data = join(FIXTURES, 'Y2O3.dat')
const cif = join(FIXTURES, 'Y2O3.cif')
report.check(existsSync(data) && existsSync(cif),
  'the Y2O3 fixtures are available for the engine', `${data}`)

async function run(label, nfev) {
  const out = join(WORK, label)
  mkdirSync(out, { recursive: true })
  let res = null
  try {
    res = await manual.execute({
      data, cif, out_dir: out, stage_hint: 'calibrate', max_nfev: nfev,
    }, { agent: { session: { header: { cwd: out } } } })
  } catch (e) {
    report.skip(`${label}: engine could not run — ${String(e.message).slice(0, 140)}`)
    return null
  }
  const fs = res?.fit_status ?? {}
  const rp = res?.r_plateau ?? {}
  console.log(`\n--- ${label} (max_nfev=${nfev}) ---`)
  console.log(`  fit_status.success   = ${fs.success}`)
  console.log(`  hit_max_nfev         = ${fs.hit_max_nfev}  (nfev=${fs.nfev})`)
  console.log(`  n_at_bound           = ${fs.n_at_bound}`)
  console.log(`  r_plateau.is_plateau = ${rp.is_plateau}`)
  console.log(`  r_plateau.basis      = ${JSON.stringify(rp.basis)}`)
  console.log(`  r_plateau.blockers   = ${JSON.stringify(rp.blockers)} (not in the packet, by design)`)
  return { fs, rp, raw: res }
}

const tight = await run('tight', 1)
if (!tight) process.exit(report.finish('NOT RUN — the engine could not be started here'))

report.check(tight.fs.hit_max_nfev === true || tight.fs.success !== true,
  'the max_nfev=1 run really did stop at the ceiling',
  `success=${tight.fs.success} hit_max_nfev=${tight.fs.hit_max_nfev}`)
report.check(tight.rp.is_plateau === false,
  'no plateau is claimed for a run that hit the ceiling', `is_plateau=${tight.rp.is_plateau}`)
report.check(typeof tight.rp.basis === 'string' && /NOT a plateau/.test(tight.rp.basis),
  'basis SAYS SO instead of claiming convergence', JSON.stringify(tight.rp.basis))
report.check(!/fit converged/.test(String(tight.rp.basis)),
  'the old fixed sentence is gone', JSON.stringify(tight.rp.basis))
report.check(/max_nfev/.test(tight.rp.basis),
  'the sentence names the actual cause (max_nfev)', JSON.stringify(tight.rp.basis))
// `blockers` is deliberately NOT in the packet: the packet's schema is closed and the
// house rule is to express things in logs/derived data rather than grow the interface.
// `basis` already names every blocker in prose, so the structured array stays in the
// engine's result.json — which is what this asserts.
const rf = tight.raw?.result_file
if (rf && existsSync(rf)) {
  const j = JSON.parse(readFileSync(rf, 'utf8'))
  const bl = j?.r_plateau?.blockers
  console.log(`  result.json r_plateau.blockers = ${JSON.stringify(bl)}`)
  report.check(Array.isArray(bl) && bl.length > 0,
    'result.json carries the structured blockers array', JSON.stringify(bl))
} else {
  report.skip(`no result_file to inspect (got ${rf})`)
}

const loose = await run('loose', 8000)
if (loose) {
  report.check(loose.rp.basis !== tight.rp.basis,
    'a converged run and a ceiling-limited run do not share one sentence',
    `tight=${JSON.stringify(tight.rp.basis)} loose=${JSON.stringify(loose.rp.basis)}`)
}

process.exit(report.finish('PLATEAU BASIS IS DERIVED FROM THE FLAGS'))
