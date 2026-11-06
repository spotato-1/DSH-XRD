// Full E2E of the dsh-xrd plugin straight from the SOURCE TREE (no restart needed: this
// process imports lib/index.js itself). Adapts the independent cloud run's harness, and adds
// the two assertions that were missing there because the run stopped before them:
//   * defect A  — the refined pcr must carry lineage (driver registers a descendant) and the
//                 twin gate in xrd_verify_fp2k must therefore ACCEPT it;
//   * defect B  — the driver must actually apply the plan's NCY and see the background panel
//                 on a GENERATOR-produced pcr (the exe-written fixture never showed this).
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const [rootArg, workArg, fixturesArg] = process.argv.slice(2)
if (!rootArg || !workArg) throw new Error('Usage: node e2e-full.mjs <PLUGIN_ROOT> <EMPTY_OUT_DIR> [FIXTURES_DIR]')
const root = resolve(rootArg)
const out = resolve(workArg)
// The SOURCE tree does not carry audit/fixtures (the audit module is a maintainer artefact kept
// in workspace\dsh-xrd\audit); the PACKAGED tree does. Accept an explicit fixtures dir.
const fixtures = resolve(fixturesArg || join(root, 'audit', 'fixtures'))
if (existsSync(out) && readdirSync(out).length) throw new Error('use a fresh empty output directory')
mkdirSync(out, { recursive: true })
const stage = (n) => { const p = join(out, n); mkdirSync(p, { recursive: true }); return p }
const records = stage('records')
const obs = {}
const record = (n, d) => writeFileSync(join(records, n + '.json'), JSON.stringify(d, null, 2) + '\n')
const hash = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')
const fails = []
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ' - ' + detail : ''}`)
  if (!ok) fails.push(label)
}

// Isolation: an empty DSH_HOME, no local config, no legacy converter, no stale engine roots —
// the same discipline the cloud run used, so nothing resolves from this machine by accident.
process.env.DSH_HOME = stage('isolated-dsh-home')
process.env.DSH_XRD_CONFIG = join(out, 'no-local-config.json')
process.env.DSH_FULLPROF_CIF2PCR = join(out, 'no-legacy-converter', 'CIFs_to_PCR.exe')
// GSAS-II root: resolved through the plugin's OWN chain (env → <plugin>/xrd.local.json → probe).
// A maintainer-specific default would be a machine path, and this file ships inside audit/ — the
// release LEAK AUDIT rejects machine paths in shipped text (2026-11-07: it did exactly that here).
function gsasRootFromPluginConfig() {
  try {
    const p = join(root, 'xrd.local.json')
    if (existsSync(p)) return JSON.parse(readFileSync(p, 'utf8')).gsas_root || null
  } catch { /* ignore */ }
  return null
}
process.env.DSH_GSAS_ROOT = process.env.DSH_GSAS_ROOT || gsasRootFromPluginConfig() || ''
if (!process.env.DSH_GSAS_ROOT) console.log('note: DSH_GSAS_ROOT unresolved — the GSAS-II leg will be skipped')
process.env.PYTHONDONTWRITEBYTECODE = '1'
delete process.env.DSH_FP2K_ROOT

const started = Date.now()
const tools = new Map()
const registry = {
  tools: { register(t) { tools.set(t.name, t) }, guard() {} },
  get() {}, on() {}, inject() {}, logger: { info() {}, warn() {}, error() {}, debug() {} },
}
const { apply } = await import(pathToFileURL(join(root, 'lib/index.js')).href)
apply(registry)
const session = { agent: { session: { header: { cwd: out } } }, signal: new AbortController().signal }

async function call(label, tool, args) {
  const ts = Date.now()
  console.log('START', label, tool)
  try {
    const value = await tools.get(tool).execute(args, session)
    record(label, { tool, args, elapsed_seconds: (Date.now() - ts) / 1000, value })
    console.log('RETURN', label, JSON.stringify({
      ok: value.ok, rwp: value.rwp, generator: value.generator?.kind,
      gate: value.provenance_gate?.waived ? 'waived' : value.provenance_gate?.allow,
      prov: value.provenance?.recorded,
    }))
    return value
  } catch (e) {
    record(label, { tool, args, elapsed_seconds: (Date.now() - ts) / 1000, exception: String(e.message ?? e) })
    throw e
  }
}

try {
  obs.plugin_version = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).version
  obs.tools = [...tools.keys()]
  check('12 tools registered', obs.tools.length === 12, obs.tools.join(', '))

  const cif = join(fixtures, 'Y2O3.cif')
  const dat = join(fixtures, 'Y2O3.dat')
  const refPcr = join(fixtures, 'Y2O3.pcr')
  obs.input_sha_before = { cif: hash(cif), dat: hash(dat), reference: hash(refPcr) }

  // 1. data
  const input = await call('01_data', 'xrd_data_io', { actions: [{ type: 'read', file: dat }], out_dir: stage('01_data') })
  check('data read', input.ok === true, input.error || '')
  const grid = input.datasets[0]
  const range = [grid.x_min, Number(grid.step.toFixed(8)), grid.x_max]
  obs.grid = { n: grid.n_points, range }
  check('7351 points from 3.000 (Ins=10 header intact)', grid.n_points === 7351 && Math.abs(grid.x_min - 3) < 1e-9,
    `n=${grid.n_points} x_min=${grid.x_min}`)

  // 2. generate with the in-tree generator (the 0.10.0 default path)
  const lam = readFileSync(refPcr, 'utf8').match(/!\s*Lambda1\s+Lambda2[^\n]*\n\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)/)
  assert.ok(lam, 'reference pcr carries no wavelength block')
  const wave = lam.slice(1).map(Number)
  const gen = await call('02_generate', 'cif_to_pcr', { cif, data_file: dat, range, wavelength_doublet: wave, out_dir: stage('02_generated') })
  check('in-tree generator produced a pcr', gen.ok === true && gen.generator?.kind === 'python', gen.error || gen.generator?.kind)
  check('provenance sidecar written at birth', existsSync(gen.pcr + '.provenance.json'))
  obs.initial = { pcr: gen.pcr, sha: hash(gen.pcr), kind: JSON.parse(readFileSync(gen.pcr + '.provenance.json', 'utf8')).kind }

  // 3. refine (FULL ladder, the default cycle count)
  const refined = await call('03_refine', 'xrd_refine_auto', { pcr: gen.pcr, ladder: true, cycles: 20, out_dir: stage('03_refined') })
  check('refinement completed', refined.ok === true, refined.error || '')
  check('entry gate accepted the generated pcr', refined.provenance_gate?.allow === true && refined.provenance_gate?.waived !== true)
  check('refinement returned an Rwp', Number.isFinite(refined.rwp), String(refined.rwp))
  // DEFECT A: the driver must have registered the write-back, not "no provenance sidecar".
  const prov = refined.provenance || {}
  check('driver REGISTERED the write-back (defect A)', prov.recorded === true, JSON.stringify(prov).slice(0, 220))
  const side = refined.refined_pcr + '.provenance.json'
  check('delivered pcr carries a sidecar', existsSync(side), side)
  obs.final = { pcr: refined.refined_pcr, sha: hash(refined.refined_pcr) }
  if (existsSync(side)) {
    const rec = JSON.parse(readFileSync(side, 'utf8'))
    obs.final.descendants = (rec.descendants || []).map((d) => d.label || '?')
    check('delivered pcr is on the chain as a descendant',
      (rec.descendants || []).some((d) => d.sha256 === obs.final.sha), obs.final.descendants.join(',') || '(none)')
    check('sidecar kind still a whitelisted origin', ['generated', 'exe_fallback', 'user_provided'].includes(rec.kind), rec.kind)
  }
  // DEFECT B: the plan's NCY and the background panel must be visible on a GENERATOR pcr.
  const workDir = dirname(refined.refined_pcr)
  const resultJson = readdirSync(workDir).filter((f) => /result.*\.json$/i.test(f)).map((f) => join(workDir, f))[0]
  if (resultJson) {
    const res = JSON.parse(readFileSync(resultJson, 'utf8'))
    const steps = Array.isArray(res.steps) ? res.steps : []
    const labels = steps.map((s) => String(s.label || s.name || s.group || ''))
    obs.driver = { result_json: resultJson, n_steps: steps.length, labels: labels.slice(0, 40) }
    check('ladder ran steps at all (defect B: NCY applied)', steps.length > 0, `${steps.length} step(s)`)
    check('a background level is among the accepted steps (defect B: panel found)',
      labels.some((l) => /background|bg/i.test(l)), labels.join(' | ').slice(0, 220))
  } else {
    check('driver result.json found', false, `nothing matching *result*.json in ${workDir}`)
  }

  // 4. verify the DELIVERED pcr through the twin gate — this is the step that failed before.
  const verified = await call('04_verify', 'xrd_verify_fp2k', { pcr: refined.refined_pcr, out_dir: stage('04_verify') })
  check('delivered pcr ACCEPTED by the verify gate (defect A, end to end)', Number.isFinite(verified.rwp), String(verified.rwp))
  check('re-verification gate reports a whitelisted origin', verified.provenance_gate?.allow === true, JSON.stringify(verified.provenance_gate || {}).slice(0, 200))

  // 5. the gate still refuses an untraceable pcr (no regression: the door must stay shut)
  const legacy = join(out, '05_legacy', 'legacy.pcr')
  mkdirSync(dirname(legacy), { recursive: true })
  writeFileSync(legacy, readFileSync(refPcr))
  writeFileSync(join(out, '05_legacy', 'legacy.dat'), readFileSync(dat))
  let refused = null
  try {
    await call('05_legacy', 'xrd_refine_auto', { pcr: legacy, ladder: false, cycles: 5, out_dir: stage('05_legacy_out') })
  } catch (e) { refused = String(e.message ?? e) }
  check('an untraceable pcr is still refused (R1)', !!refused && /no provenance record/.test(refused), (refused || '').split('\n')[0])

  // ---------------------------------------------------------------- MANUAL LEG
  // The manual (bounded-LM) engine is the fallback when the FullProf path is unsuitable. It has
  // its own delivery port (xrd_export_cif), so the leg is refine -> export, not refine -> fp2k.
  const manual = await call('06_manual', 'xrd_refine_manual', {
    data: dat, cif, stage_hint: 'structure',
    hypothesis: 'E2E: manual engine reproduces a converging fit from the same data and CIF',
    out_dir: stage('06_manual'), max_nfev: 4000,
  })
  check('manual engine ran', manual.ok === true || manual.mode !== undefined, manual.error || `mode=${manual.mode}`)
  check('manual engine hypothesis gate allowed it', !manual.gate || manual.gate.decision === 'allow' || manual.gate.allow === true,
    JSON.stringify(manual.gate || {}).slice(0, 160))
  check('manual engine reported an Rwp', Number.isFinite(manual.rwp), String(manual.rwp))
  const manualResult = manual.result_file
    || manual.result
    || (manual.out_files || []).find?.((f) => /result.*\.json$/i.test(f))
  if (manualResult && existsSync(manualResult)) {
    const exported = await call('07_export', 'xrd_export_cif', { result: manualResult, out_dir: stage('07_export') })
    check('xrd_export_cif produced a CIF from the manual result',
      exported.ok === true && Array.isArray(exported.cifs) && exported.cifs.length > 0,
      JSON.stringify({ ok: exported.ok, cifs: exported.cifs, error: exported.error }).slice(0, 220))
    // FIELD NAMES come from the schema, not from memory: xrd_export_cif returns `figures`
    // (plural, array) — an assertion written against a guessed `plot` reported a defect that
    // did not exist (the 155 KB PNG was written). Read the record before believing a red light.
    const figs = exported.figures || (exported.plot ? [exported.plot] : [])
    check('xrd_export_cif wrote a figure', figs.length > 0 && existsSync(figs[0]), figs[0] || '(none)')
  } else {
    check('manual result json located', false, `manualResult=${manualResult || '(none)'}`)
  }

  // ---------------------------------------------------------------- GSAS-II LEG
  // Instrument parameters: E2E_INSTPRM, else the one GSAS-II itself ships
  // (<gsas root>/tests/testinp/INST_XRY.PRM) — no project-specific path in shipped text.
  const instprm = resolve(process.env.E2E_INSTPRM
    || join(process.env.DSH_GSAS_ROOT || '', 'tests', 'testinp', 'INST_XRY.PRM'))
  if (!existsSync(instprm)) {
    check('GSAS instrument parameter file available', false, instprm)
  } else {
    const gsas = await call('08_gsas', 'xrd_refine_gsas', {
      data: dat, instprm, cif, refine: ['background', 'cell', 'profile', 'atoms'],
      n_back: 6, max_cycles: 6, out_dir: stage('08_gsas'),
    })
    check('GSAS-II refinement completed', gsas.ok === true, (gsas.error || '').slice(0, 200))
    check('GSAS-II reported a wR', Number.isFinite(gsas.histograms?.[0]?.wR), JSON.stringify(gsas.histograms || []).slice(0, 160))
    const gpx = gsas.gpx
    check('GSAS-II project written', !!gpx && existsSync(gpx), gpx || '(none)')

    if (gpx && existsSync(gpx)) {
      const ops = await call('09_gsas_ops', 'xrd_gsas_ops', { gpx, actions: [{ type: 'snapshot' }], out_dir: stage('09_gsas_ops') })
      check('xrd_gsas_ops read the project back', ops.ok === true || Array.isArray(ops.parameters), (ops.error || '').slice(0, 160))

      const fin = await call('10_finalize', 'xrd_finalize', { gpx, out_dir: stage('10_final'), sample: 'E2E_Y2O3' })
      check('xrd_finalize packaged the refinement', fin.ok === true && !!fin.package_dir, (fin.error || '').slice(0, 200))
      check('package carries a report', !!fin.report && existsSync(fin.report), fin.report || '(none)')
      check('package carries a fit figure', !!fin.plot && existsSync(fin.plot), fin.plot || '(none)')
      check('package carries per-phase CIFs', Array.isArray(fin.cifs) && fin.cifs.length > 0, JSON.stringify(fin.cifs || []).slice(0, 160))

      // Phase identification on the REMAINING residual of the converged project.
      const screen = await call('11_screen', 'xrd_phase_screen', {
        actions: [{ type: 'residuals', gpx, min_rel: 0.03, top: 20 },
                  { type: 'screen', candidates: [cif], gpx, tol: 0.25, min_rel: 0.02 }],
        out_dir: stage('11_screen'),
      })
      const ranked = screen.ranked || screen.candidates || []
      check('xrd_phase_screen scored the phase against the residual',
        Array.isArray(ranked) && ranked.length > 0, JSON.stringify(ranked).slice(0, 200))

      // ------------------------------------------------ PLOT-EXPORT SKILL LEG
      const exporter = join(root, 'skills', 'xrd-plot-export', 'scripts', 'plot_export.py')
      if (existsSync(exporter)) {
        const plotOut = stage('12_plot_export')
        const run = spawnSync(process.env.E2E_PY || 'python', [exporter, '--src', gpx, '--out', plotOut, '--split-phases'], {
          encoding: 'utf8', env: { ...process.env }, timeout: 300000,
        })
        const produced = readdirSync(plotOut)
        check('plot_export.py exported the two CSV families from the .gpx',
          run.status === 0 && produced.some((f) => /_rietveld\.csv$/.test(f)) && produced.some((f) => /_reflections\.csv$/.test(f)),
          `exit ${run.status}${run.status === 9009 ? ' (bare python not on PATH - set E2E_PY)' : ''}; ${produced.join(',') || (run.stderr || '').slice(-160)}`)
        check('plot_export.py wrote the stats summary', produced.some((f) => f === '_stats_summary.csv'), produced.join(','))
      } else {
        check('plot_export.py present in the tree', false, exporter)
      }
    }
  }

  obs.input_sha_after = { cif: hash(cif), dat: hash(dat), reference: hash(refPcr) }
  check('fixtures were not modified', JSON.stringify(obs.input_sha_before) === JSON.stringify(obs.input_sha_after))
} catch (e) {
  obs.fatal = String(e.message ?? e)
  console.error('STOP', obs.fatal)
  fails.push('fatal: ' + obs.fatal.split('\n')[0])
} finally {
  obs.elapsed_seconds = (Date.now() - started) / 1000
  record('run', obs)
  console.log(`\n${fails.length ? fails.length + ' CHECK(S) FAILED: ' + fails.join('; ') : 'E2E OK'}`)
  console.log(`elapsed ${obs.elapsed_seconds.toFixed(1)} s`)
  process.exitCode = fails.length ? 1 : 0
}
