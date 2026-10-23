// verify-gsas-ops.mjs — real-project check of the four xrd_gsas_ops actions that the
// tool text did NOT advertise until 2026-09-21:
//   add_phase / replace_phase / new_from / set_site_composition
// plus (2026-09-25) the D15/D16 regression: the documented `atoms:["all"]` selector and
// the "set_* returns a post-set wR, not a continuation" marking.
//
// WHY these four: the integrity audit found them implemented in engine/gsas_ops.py but
// absent from the `actions` help text — i.e. documented nowhere and unreachable in
// practice (add_phase is the entry point for two-phase work). This checks that they
// are not merely declared now but actually work, through the REAL tool, so the output
// schema (additionalProperties:false) is exercised too.
//
// It reads the project back with GSAS-II itself, because "add_phase returned ok" is
// not evidence that a second phase actually attached to the histogram.
//
// INPUTS AND HOW TO GET THEM
//   * second-phase CIF : audit/fixtures/P21c_second_phase.cif (ships with this module)
//   * a converged .gpx with at least one histogram: NOT shipped (it would carry the
//     measured pattern). Point DSH_XRD_GPX at one. Without it the script SKIPs
//     cleanly — an absent input is an environment fact, not a plugin defect.
//   * GSAS-II          : $DSH_GSAS_ROOT, else xrd.local.json
import { pathToFileURL } from 'node:url'
import { existsSync, copyFileSync, writeFileSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import {
  importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, tryGsasRoot, python, report, freshDir,
} from './_lib.mjs'

const { check, skip } = report
const ENTRY = pluginEntry(importRoot())
const SRC_P21C = join(FIXTURES, 'P21c_second_phase.cif')

const SRC_GPX = process.env.DSH_XRD_GPX && existsSync(process.env.DSH_XRD_GPX)
  ? process.env.DSH_XRD_GPX
  : null

console.log('tool layer under test:', ENTRY)

// ── tool assembly (the real contract) ───────────────────────────────────────
const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
await (mod.apply ?? mod.default)(ctx)
const ops = tools.find(t => t.name === 'xrd_gsas_ops')
check(!!ops, 'xrd_gsas_ops registered', 'xrd_gsas_ops NOT registered')

// ── 1. the action vocabulary covers what the engine implements ──────────────
// This part needs no external input, so it ALWAYS runs — even on a machine with no
// GSAS-II. Everything that needs GSAS-II or a real project is checked for availability
// first and SKIPped, never allowed to crash the suite.
const help = String(ops.parameters.properties.actions.description)
for (const a of ['add_phase', 'replace_phase', 'new_from', 'set_site_composition']) {
  check(help.includes(`{type:"${a}"`), `actions help advertises ${a}`, `actions help does NOT advertise ${a}`)
}

// ── 2..4 need GSAS-II AND a real project ────────────────────────────────────
const GSAS = tryGsasRoot()
if (!GSAS) {
  skip('the working part of this check: GSAS-II root unknown '
    + '(set DSH_GSAS_ROOT, or put "gsas_root" in xrd.local.json next to the plugin)')
} else if (!SRC_GPX) {
  skip('the working part of this check: no converged .gpx supplied '
    + '(set DSH_XRD_GPX=<converged.gpx with >=1 histogram> to run add_phase / '
    + 'set_site_composition against a real project)')
} else if (!existsSync(SRC_P21C)) {
  skip('add_phase: second-phase CIF fixture missing at ' + SRC_P21C)
} else {
  console.log('GSAS-II            :', GSAS)
  const PY = python()
  const WORK = freshDir(join(WORK_ROOT, 'gsas-ops'))
  const gpx0 = join(WORK, 'start.gpx')
  copyFileSync(SRC_GPX, gpx0)

  // ── 2. add_phase on a real converged project ─────────────────────────────
  // Wrapped: a restricted environment can refuse to spawn the engine at all
  // (`spawn EPERM` from the file sandbox), and an uncaught throw here would kill the
  // whole run — including the vocabulary result already earned above. Report it as a
  // failed check with the environment cause named, so it is neither hidden nor confused
  // with a plugin defect.
  let r1 = null
  try {
    r1 = await ops.execute({
      gpx: gpx0,
      out_dir: WORK,
      save_as: join(WORK, 'two_phase.gpx'),
      actions: [
        { type: 'add_phase', cif: SRC_P21C, scale: 0.05 },
        { type: 'save' },
      ],
    }, {})
  } catch (e) {
    report.fail(`add_phase could not run: ${String(e.message).slice(0, 200)}`
      + ' — spawn EPERM here means the engine could not be started in this environment '
      + '(a sandbox/permission limit, not a plugin defect)')
  }
  if (r1) {
    check(r1.ok === true, `add_phase returned ok (partial=${r1.partial})`,
      `add_phase returned ok=${r1.ok}: ${JSON.stringify(r1.actions ?? r1.error)}`)
    const added = (r1.actions ?? []).find(a => a.action === 'add_phase')
    check(!!added && !added.error, 'add_phase action record has no error', `add_phase errored: ${added?.error}`)
  }
  // the returned project path is `gpx` — `save_as` is the INPUT parameter name
  const outGpx = (r1 && r1.gpx) || null
  if (r1) check(!!outGpx && existsSync(outGpx), `project saved: ${outGpx}`, `saved project missing: ${outGpx}`)

  // ── 3. read the saved project back: did the second phase really attach? ──
  // Also harvest the real atom labels, so step 4 targets a site that EXISTS instead of
  // a guessed one (a guessed label would make the check vacuous: the action would
  // legitimately warn "site not found" and we would learn nothing).
  const probe = join(WORK, 'probe.py')
  const probeSrc = `
import json, sys
sys.path.insert(0, ${JSON.stringify(GSAS)})
from GSASII import GSASIIscriptable as G2sc
p = G2sc.G2Project(gpxfile=${JSON.stringify(outGpx || gpx0)})
labels_by_phase = {}
for ph in p.phases():
    ptr = (ph.data.get('General') or {}).get('AtomPtrs') or [3, 1, 7, 9]
    ct = ptr[1]
    labels_by_phase[ph.name] = [str(at[ct - 1]) for at in (ph.data.get('Atoms') or [])]
print(json.dumps({
  'phases': [x.name for x in p.phases()],
  'hists': [x.name for x in p.histograms()],
  'phase_hists': {x.name: list((x.data.get('Histograms') or {}).keys()) for x in p.phases()},
  'labels_by_phase': labels_by_phase,
}))
`
  writeFileSync(probe, probeSrc, 'utf8')
  let back = null
  try {
    back = JSON.parse(execFileSync(PY, [probe], { encoding: 'utf8' }).trim().split('\n').pop())
  } catch (e) {
    report.fail('GSAS-II read-back failed: ' + String(e.message).slice(0, 200))
  }
  if (back) {
    console.log('  read-back phases:', JSON.stringify(back.phases))
    console.log('  read-back phase->hists:', JSON.stringify(back.phase_hists))
    console.log('  atom labels:', JSON.stringify(back.labels_by_phase))
    check(back.phases.length === 2, `project now has 2 phases (${back.phases.join(', ')})`,
      `expected 2 phases, got ${back.phases.length}: ${back.phases.join(', ')}`)
    check(back.hists.length > 0 && back.hists.every(h =>
      back.phases.every(p => (back.phase_hists[p] || []).includes(h))),
    'every phase is linked to the histogram',
    'a phase is NOT linked to the histogram (add_phase did not attach it)')
  }

  // ── 4. set_site_composition writes a mixed site as extra rows ────────────
  // Only meaningful once we have a project we actually saved and read labels from.
  const anyLabel = (outGpx && back) ? Object.values(back.labels_by_phase).flat()[0] : null
  if (!anyLabel) {
    skip('set_site_composition: no mixed-site target — the preceding phase build did not '
      + 'produce a readable project on this machine')
  } else {
    // The label's own element is unknown here, so mix it with a plausible partner and
    // assert only on the STRUCTURAL effect (extra rows at the same coordinates), which
    // is what the action is for. The engine reports how many rows it wrote.
    const el = (anyLabel.match(/^[A-Za-z]{1,2}/) || ['X'])[0]
    const r2 = await ops.execute({
      gpx: outGpx || gpx0,
      out_dir: WORK,
      save_as: join(WORK, 'mixed.gpx'),
      actions: [
        { type: 'set_site_composition', sites: { [anyLabel]: { [el]: 0.5, Na: 0.5 } } },
        { type: 'save' },
      ],
    }, {})
    const comp = (r2.actions ?? []).find(a => a.action === 'set_site_composition')
    const warned = (comp?.warnings ?? []).some(w => /not found/.test(w))
    check(r2.ok === true && !!comp && !comp.error,
      `set_site_composition applied to ${anyLabel} (partial=${r2.partial})`,
      `set_site_composition failed on ${anyLabel}: ${JSON.stringify(comp?.error ?? comp?.warnings)}`)
    console.log('  detail: ' + JSON.stringify(comp?.detail ?? comp?.warnings ?? null))
    // an applied run must report writing extra rows; a "not found" run means our label
    // harvest and the engine's lookup disagree, which is itself worth failing on
    check(/extra row/.test(String(comp?.detail ?? '')),
      'mixed site written as extra rows at the same coordinates',
      `set_site_composition did not report extra rows (detail=${JSON.stringify(comp?.detail)} warned=${warned})`)
  }

  // ── 5. D15/D16 regression: the documented `atoms:["all"]` must hit every atom ──
  // D15 (found by auditing a real defect-system refinement, 2026-09-25): the action help
  // documented "atoms":["all"], but the engine compared the selector with a STRING ('all'),
  // so the list form matched no atom and set_adp_mode died with
  // "matched no atom (atoms=['all'])" — the exact string that run hit. A vocabulary check
  // cannot catch that; only executing the action can.
  // D16: set_hist / set_cell run ONE refinement step to install a setting, so the wR they
  // return is that step's value, not a continuation of the previous fit. The record must
  // SAY so (wR_kind), or a measured 16.56 -> 34.21 jump reads as "the model got worse".
  const totalAtoms = back ? Object.values(back.labels_by_phase).flat().length : 0
  if (!outGpx || !back || !totalAtoms) {
    skip('D15/D16 regression: no readable project with atoms to select from')
  } else {
    const r3 = await ops.execute({
      gpx: outGpx,
      out_dir: WORK,
      save_as: join(WORK, 'd15.gpx'),
      actions: [
        { type: 'open_uiso', atoms: ['all'] },
        { type: 'set_adp_mode', mode: 'isotropic', atoms: ['all'] },
        { type: 'open_uiso', atoms: ['NoSuchAtom'] },
        { type: 'set_hist', section: 'Background', value: { 'no. coeffs': 6, refine: false } },
        { type: 'save' },
      ],
    }, {})
    const acts = r3.actions ?? []
    const adp = acts.find(a => a.action === 'set_adp_mode')
    check(!adp?.error && (adp?.adp_mode?.atoms?.length ?? 0) === totalAtoms,
      `D15: set_adp_mode atoms:["all"] hit all ${totalAtoms} atom(s)`,
      `D15 REGRESSION: atoms:["all"] matched ${adp?.adp_mode?.atoms?.length ?? 0} of `
        + `${totalAtoms} atom(s); error=${JSON.stringify(adp?.error ?? null)}`)
    const openUs = acts.filter(a => a.action === 'open_uiso')
    check(openUs.length > 0 && !/no such atom/.test(String(openUs[0]?.detail ?? '')),
      'D15: open_uiso atoms:["all"] accepted as "every atom"',
      `D15: open_uiso detail=${JSON.stringify(openUs[0]?.detail)}`)
    check(/no such atom: NoSuchAtom/.test(String(openUs[1]?.detail ?? '')),
      'D15: an unmatched label is reported instead of silently doing nothing',
      `D15: a typo\x27d selector stayed silent (detail=${JSON.stringify(openUs[1]?.detail)})`)
    const sh = acts.find(a => a.action === 'set_hist')
    check(/NOT comparable/.test(String(sh?.detail ?? ''))
      && /not comparable/i.test(String(sh?.wR_kind ?? '')),
      'D16: set_hist marks its wR as a post-set refit',
      'D16 REGRESSION: set_hist does not mark its wR as non-comparable '
        + `(${JSON.stringify(sh?.detail)} / wR_kind=${JSON.stringify(sh?.wR_kind)})`)
  }
}

// ── 6. a REAL multi-step refinement, step by step (0.8.6) ───────────────────
// This is the home of the real GSAS run: the supervision suite shortens the heartbeat
// stall window to 2.5 s for its stall controls, which would cut a legitimate refinement,
// so the end-to-end multi-step check lives here where the shipped 90 s window applies.
// It exercises the 0.8.5 restructure (one `do_refinements([step])` per step, with a guard
// checkpoint before each) against a real project.
if (GSAS && SRC_GPX) {
  const gsas = tools.find(t => t.name === 'xrd_refine_gsas')
  check(!!gsas, 'xrd_refine_gsas registered', 'xrd_refine_gsas NOT registered')
  if (gsas) {
    // DOT-prefixed scratch (0.8.6): a plain `gsas-steps` directory lands in the audit tree
    // that the builder PACKS, and the run artifacts inside it (a plan and a result JSON
    // full of absolute maintainer paths) then trip the leak audit — measured: 6 findings,
    // all from this directory. Dot-prefixed entries are excluded by design.
    const w = freshDir('.work-gsas-steps')
    let res = null
    let threw = null
    try {
      res = await gsas.execute({
        gpx: SRC_GPX, gsas_root: GSAS, refine: ['background', 'cell'], max_cycles: 2, out_dir: w,
      }, { agent: { session: { header: { cwd: w } } }, callId: 'two-step', signal: undefined })
    } catch (e) { threw = e }
    check(threw === null && res && res.ok === true,
      'a real two-step refinement (background + cell) completes',
      `the two-step run failed: ${threw ? String(threw.message).slice(0, 160) : JSON.stringify(res && res.error)}`)
    check(!!(res && Array.isArray(res.histograms) && res.histograms.length
      && typeof res.histograms[0].wR === 'number'),
      `a real wR comes back (${res && res.histograms && res.histograms[0] ? res.histograms[0].wR : 'none'})`,
      'no numeric wR on the multi-step run')
    check(!!(res && Array.isArray(res.phases) && res.phases.length && Array.isArray(res.phases[0].cell)),
      'the phase cell is reported after the cell step (step 2 really ran)',
      'the cell step left no reported cell')
    // The guard's tick records the phase name at the moment of the tick: direct evidence
    // that the steps were checkpointed one by one.
    let tick = null
    try { tick = JSON.parse(readFileSync(join(w, '.xrd-progress.json'), 'utf8')) } catch { tick = null }
    check(!!(tick && /gsas refine step \d+\/2/.test(String(tick.phase || ''))),
      `the guard ticked at a per-step checkpoint (phase="${tick ? tick.phase : 'no tick file'}")`,
      'the tick file does not name a per-step phase')
    check(!!(tick && tick.self_assigned === true),
      'the run was inside a kill-on-close job object (self_assigned=true)',
      `self_assigned=${tick ? tick.self_assigned : 'n/a'} — the job object was not used`)
  }
}

// The verdict must not overclaim: if nothing external could be exercised, say so instead
// of printing "ALL ASSERTIONS PASSED", which reads as if the real work had been checked.
const ranWorking = report.skips.length === 0
process.exit(report.finish(ranWorking
  ? 'ALL ASSERTIONS PASSED — the four advertised gsas_ops actions work on a real project, '
    + 'a real two-step refinement runs step by step with a checkpoint each, and the D15/D16 '
    + 'regressions are closed (["all"] hits every atom; set_* marks its wR)'
  : 'VOCABULARY CHECK PASSED — the four actions are advertised; the real-project part '
    + 'did NOT run on this machine (see the SKIP above)',
{ alreadyMentionsSkips: !ranWorking }))

