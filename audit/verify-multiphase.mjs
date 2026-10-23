// verify-multiphase.mjs — the 0.8.10 multiphase entry: tool-layer gates, the AI-editable
// hook contract, and regression guards for the two defects the smoke test found.
//
// WHY this suite exists: `xrd_refine_multiphase` is the only new TOOL since 0.3.0, and the
// >=3-phase gate + the "no fresh build without a hand-off" workflow gate are the two things
// that keep a 1-2 phase sample out of it. Both are pure tool-layer decisions — they run
// BEFORE any engine is spawned — so they can be pinned here with no GSAS-II, no python and
// no quota. (The engine side was exercised separately by a REAL 3-phase GSAS-II smoke run on
// 2026-10-02; this suite does not replace that, it guards the layer above it.)
//
// The two source-level guards at the end exist because the multiphase template shipped DOA
// twice over: it called `run_guarded_main(main, backend="gsas")` (proc_guard takes one
// argument) and never put gsas_root on sys.path. Both are invisible to a syntax check and
// both are one-line regressions, so they are asserted here.
//
// Usage:  node verify-multiphase.mjs [--root <plugin dir>]
import { mkdirSync, readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, pluginRoot, freshDir, execIn, report, WORK_ROOT } from './_lib.mjs'

const ROOT = pluginRoot()
const WORK = freshDir(join(WORK_ROOT, 'multiphase'))

// The plugin mirrors its skills into $DSH_HOME/skills when it is assembled. Point that at a
// scratch dir BEFORE apply(): a real dsh home would get the bundled skills rewritten by an
// audit run (incident recorded 2026-10-02).
process.env.DSH_HOME = join(WORK, 'fakehome')
mkdirSync(join(process.env.DSH_HOME, 'skills'), { recursive: true })

const { mockContext } = await import('./_lib.mjs')
const mod = await import(new URL('file://' + join(ROOT, 'lib', 'index.js').replace(/\\/g, '/')).href)

const sink = []
mod.apply(mockContext(sink))
const byName = new Map(sink.map((t) => [t.name, t]))

// Deliberately NON-EXISTENT, machine-neutral paths: both gates must decide on the phase
// COUNT and on the presence of a hand-off, i.e. BEFORE anything opens a CIF. If a future
// version starts reading the structures first, this suite fails instead of silently passing
// — and no maintainer path ends up in the shipped audit (the release leak audit rejects
// absolute D:/… paths, and it did catch the first version of this file).
const CIFS = ['phase_a.cif', 'phase_b.cif', 'phase_c.cif'].map((f) => join(WORK, f))
const exec = execIn(WORK)

// ───────────────────────────────────────────────────────────── §1 registration
report.check('§1 the tool family is 12 tools and includes xrd_refine_multiphase',
  sink.length === 12 && byName.has('xrd_refine_multiphase'),
  `got ${sink.length}: ${sink.map((t) => t.name).join(', ')}`)

const mp = byName.get('xrd_refine_multiphase')
const mpParams = mp ? Object.keys(mp.parameters?.properties || {}) : []
report.check('§1 xrd_refine_multiphase exposes the orchestration parameters',
  ['gpx', 'data', 'instprm', 'cifs', 'handoff', 'engine', 'init_engine', 'refine', 'phases', 'out_dir']
    .every((k) => mpParams.includes(k)),
  `got ${mpParams.join(', ')}`)

const gsas = byName.get('xrd_refine_gsas')
const gsasParams = gsas ? Object.keys(gsas.parameters?.properties || {}) : []
report.check('§1 xrd_refine_gsas gained phases[] / entry / rollback / handoff / scale',
  ['phases', 'entry', 'rollback', 'rollback_tol', 'handoff'].every((k) => gsasParams.includes(k))
  && (gsas?.parameters?.properties?.refine?.items?.enum || []).includes('scale'),
  `params: ${gsasParams.join(', ')}`)

const ladderSchema = gsas?.output?.schema?.properties?.ladder?.items?.properties || {}
const outProps = gsas?.output?.schema?.properties || {}
report.check('§1 the ladder/result schema declares gsas_error, cell_change_pct, entry, per_phase, handoff',
  ['gsas_error', 'cell_change_pct'].every((k) => k in ladderSchema)
  && ['entry', 'per_phase', 'handoff'].every((k) => k in outProps),
  `ladder: ${Object.keys(ladderSchema).join(', ')} | top: ${['entry', 'per_phase', 'handoff'].filter((k) => k in outProps).join(', ')}`)

// ─────────────────────────────────────────────────────── §2 the >=3 phase gate
{
  const outA = join(WORK, 'gate-two')
  mkdirSync(outA, { recursive: true })
  const r = await mp.execute({ cifs: JSON.stringify(CIFS.slice(0, 2)), out_dir: outA }, exec)
  const left = readdirSync(outA)
  report.check('§2 two CIFs are refused with a message naming the 1-2 phase tool',
    r.ok === false && /1-2 phase|one or two phases/i.test(r.error || '') && /2 CIF/.test(r.error || ''),
    `ok=${r.ok} error=${r.error}`)
  report.check('§2 the refusal spawns nothing (no artifacts written)',
    left.length === 0, `out_dir contains: ${left.join(', ')}`)
}

// ─────────────────────────────── §3 a fresh 3-phase build needs the hand-off
{
  const outB = join(WORK, 'gate-nohandoff')
  mkdirSync(outB, { recursive: true })
  const r = await mp.execute({ cifs: JSON.stringify(CIFS), out_dir: outB }, exec)
  const left = readdirSync(outB)
  report.check('§3 a fresh >=3-phase build without handoff is refused, naming the manual engine',
    r.ok === false && /handoff/.test(r.error || '') && /xrd_refine_manual|TWO most abundant/i.test(r.error || ''),
    `ok=${r.ok} error=${r.error}`)
  report.check('§3 the workflow refusal spawns nothing', left.length === 0,
    `out_dir contains: ${left.join(', ')}`)
}

// ────────────────────────────────────── §4 init_engine hands over a copy, runs nothing
{
  const outC = join(WORK, 'init-engine')
  mkdirSync(outC, { recursive: true })
  const r = await mp.execute({ init_engine: true, out_dir: outC }, exec)
  const copy = r.engine_template
  const bundled = join(ROOT, 'engine', 'gsas_multiphase_template.py')
  const same = existsSync(copy) && readFileSync(copy, 'utf8') === readFileSync(bundled, 'utf8')
  report.check('§4 init_engine returns a byte-identical copy of the bundled template',
    r.ok === true && same, `ok=${r.ok} copy=${copy} identical=${same}`)
  report.check('§4 init_engine runs nothing (no gpx/result in out_dir)',
    readdirSync(outC).filter((f) => /\.(gpx|json)$/.test(f)).length === 0,
    `out_dir: ${readdirSync(outC).join(', ')}`)
}

// ─────────────────────────────── §5/§6 the engine side: gate + the two DOA regressions
{
  const src = readFileSync(join(ROOT, 'engine', 'gsas_multiphase_template.py'), 'utf8')
  report.check('§5 the template carries the run-time >=3 phase gate (MIN_PHASES = 3)',
    /^MIN_PHASES\s*=\s*3/m.test(src) && /multiphase gate: the project carries/.test(src),
    'MIN_PHASES or the run-time gate text is missing')
  report.check('§5 the template refuses a fresh build without a hand-off (workflow gate)',
    /multiphase entry needs a settled starting point/.test(src), 'workflow-gate text missing')

  // 2026-10-02 defect #1: proc_guard.run_guarded_main(main_fn) takes ONE argument; the
  // permissive local shim (**kw) made an invalid `backend=` keyword survive every review.
  const badCall = /run_guarded_main\(\s*main\s*,/.test(src)
  report.check('§6 the template calls run_guarded_main(main) with no extra keyword',
    !badCall && /run_guarded_main\(main\)/.test(src),
    badCall ? 'found run_guarded_main(main, ...) — proc_guard takes one argument'
      : 'run_guarded_main(main) call not found')

  // 2026-10-02 defect #2: GSAS-II is not on the interpreter's sys.path, so the root has to
  // be resolved and inserted inside the template (it was missing → ModuleNotFoundError).
  report.check('§6 the template resolves gsas_root and inserts it into sys.path',
    /sys\.path\.insert\(0,\s*gsas_root\)/.test(src) && /GSASIIscriptable\.py/.test(src),
    'gsas_root bootstrap missing (ModuleNotFoundError on every call)')

  report.check('§6 the template keeps the instrument slot/unit handling and the >10x refusal',
    /ent\[1\] = val/.test(src) && /implausible seed/.test(src) && /1e4/.test(src),
    'instrument transfer handling changed')
}

report.finish('multiphase entry (tool gates + engine gate + DOA regressions)')
