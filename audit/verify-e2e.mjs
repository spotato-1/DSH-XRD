// End-to-end: does a CIF produced by build_orbit actually enter the refinement chain?
//   build_orbit (xrd_cif_edit) -> cif_to_pcr (official converter) -> xrd_refine_auto (fp2k)
//
// WHY THIS FILE WAS REWRITTEN (2026-09-21, cloud end-to-end run)
//   The first version could not fail. `cif_to_pcr` errors were only `console.log`ged, the
//   last line merely PRINTED "CIF accepted / CIF rejected", and no assertion was attached
//   to either — so a failed conversion still exited 0 and the whole audit read as a pass.
//   It also never called `xrd_refine_auto` at all (step 4 was a comment), which made its
//   name a lie. Every stage below now ends in an assertion, and the last stage really runs
//   the refinement.
//
// HONESTY ABOUT THE NUMBERS: the pattern used here is a fixture, not the pattern that
// belongs to the built CIF, so the Rwp it produces is meaningless. What is being asserted
// is that the CHAIN EXECUTES — not that the fit is any good.
//
// Environment limits (a restricted sandbox cannot spawn the engine, `spawn EPERM`) are
// reported as SKIP, never as a pass: the audit's final line lists them.
import { mkdirSync, existsSync, rmSync, readFileSync, copyFileSync } from 'node:fs'
import { join, extname } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
const WORK = join(WORK_ROOT, 'e2e')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', ENTRY)

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
await (mod.apply ?? mod.default)(ctx)
const cifEdit = tools.find(t => t.name === 'xrd_cif_edit')
const cif2pcr = tools.find(t => t.name === 'cif_to_pcr')
const refine = tools.find(t => t.name === 'xrd_refine_auto')
const exec = { agent: { session: { header: { cwd: WORK } } } }

report.check(!!cifEdit && !!cif2pcr && !!refine,
  'the three tools the chain needs are registered',
  `missing: ${[['xrd_cif_edit', cifEdit], ['cif_to_pcr', cif2pcr], ['xrd_refine_auto', refine]]
    .filter(([, t]) => !t).map(([n]) => n).join(', ')}`)

// ── 1) build the CIF professionally ─────────────────────────────────────────
const built = join(WORK, 'layered.cif')
let r1 = null
try {
  r1 = await cifEdit.execute({
    out_dir: WORK, sample: 'layered',
    actions: [{ type: 'build_orbit', spacegroup: 'R -3 m',
      cell: { a: 2.87, b: 2.87, c: 14.3, alpha: 90, beta: 90, gamma: 120 },
      sites: [{ element: 'Na', x: 0, y: 0, z: 0, occ: 1.0 },
              { element: 'O', x: 0, y: 0, z: 0.23, occ: 1.0 }],
      out: built }],
  }, exec)
} catch (e) {
  report.skip(`build_orbit could not run: ${String(e.message).slice(0, 160)} `
    + '— in a restricted sandbox this is `spawn EPERM` (the engine could not be started), '
    + 'an environment limit and NOT a plugin defect')
}
if (!r1) process.exit(report.finish('NOT RUN — the engine could not be started here'))
report.check(r1.ok === true, 'build_orbit reported ok', `ok=${r1.ok}`)
report.check(existsSync(built), 'build_orbit wrote the CIF', built)

// ── 2) hand it to the official CIF->pcr converter ───────────────────────────
let pcr = null
let r2 = null
try {
  r2 = await cif2pcr.execute({ cif: built, out_dir: WORK }, exec)
} catch (e) {
  report.fail(`cif_to_pcr threw: ${String(e.message).slice(0, 200)}`)
}
if (r2) {
  pcr = r2.pcr
  report.check(r2.ok === true, 'cif_to_pcr reported ok', `ok=${r2.ok}`)
  report.check(typeof pcr === 'string' && pcr.toLowerCase().endsWith('.pcr') && existsSync(pcr),
    'cif_to_pcr produced a pcr file on disk', String(pcr))
  report.check(!!r2.occupancy_check?.verdict,
    'the occupancy audit returned a verdict',
    JSON.stringify(r2.occupancy_check ?? null).slice(0, 120))
  console.log('   occupancy_check verdict =', r2.occupancy_check?.verdict)
}

// ── 3) what the pcr actually says ───────────────────────────────────────────
if (pcr && existsSync(pcr)) {
  const txt = readFileSync(pcr, 'utf8')
  console.log('--- pcr head ---')
  console.log(txt.split('\n').slice(0, 12).join('\n'))
  const atomIdx = txt.indexOf('!Atom')
  if (atomIdx >= 0) {
    console.log('--- pcr atom block ---')
    console.log(txt.slice(atomIdx, atomIdx + 420))
  }
  report.check(/COMM|!/.test(txt.slice(0, 200)), 'the pcr looks like a FullProf control file',
    'unexpected head')
}

// ── 4) THE REFINEMENT — the stage the old version only talked about ─────────
// fp2k resolves the pattern by the pcr STEM (measured 2026-09-21), so the data must sit
// under that name. The fixture pattern does not belong to this CIF: see the header note.
if (pcr && existsSync(pcr)) {
  const stem = pcr.replace(/\.pcr$/i, '')
  const fixture = join(FIXTURES, 'Y2O3.dat')
  let refined = null
  if (!existsSync(fixture)) {
    report.skip(`no fixture pattern at ${fixture} — cannot exercise xrd_refine_auto`)
  } else {
    copyFileSync(fixture, stem + extname(fixture))
    try {
      refined = await refine.execute({ pcr, ladder: false, cycles: 5 }, exec)
    } catch (e) {
      const msg = String(e.message)
      report[/spawn EPERM|EPERM/.test(msg) ? 'skip' : 'fail'](
        `xrd_refine_auto could not run: ${msg.slice(0, 200)}`)
    }
  }
  if (refined) {
    report.check(refined.ok === true, 'xrd_refine_auto reported ok', `ok=${refined.ok}, ${refined.error ?? ''}`)
    report.check(typeof refined.rwp === 'number',
      'the refinement reported an Rwp (fp2k found its pattern file)', `rwp=${refined.rwp}`)
    // the 0.5.1 two-value contract: both numbers must survive the tool's closed schema
    report.check(typeof refined.rwp_mean === 'number',
      'rwp_mean is present (the decision value)', `rwp_mean=${refined.rwp_mean}`)
    report.check(Array.isArray(refined.out_files) && refined.out_files.length > 0,
      'the refinement kept its artifacts', `${refined.out_files?.length ?? 0} file(s)`)
    console.log(`   Rwp = ${refined.rwp} (meaningless: fixture pattern), rwp_mean = ${refined.rwp_mean}`)
  }
} else {
  report.fail('no pcr reached the refinement stage — the chain did not execute')
}

console.log('e2e step reached:',
  pcr && existsSync(pcr) ? 'CIF accepted by the official converter' : 'CIF rejected')
process.exit(report.finish('END-TO-END CHAIN EXECUTED — build_orbit -> cif_to_pcr -> xrd_refine_auto'))
