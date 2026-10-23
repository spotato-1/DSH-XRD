// verify-review-key.mjs — 0.8.8 regression suite (two silent failure modes found by the
// host-level end-to-end run on Y2O3, 2026-09-28):
//
//   (1) runEngine read the host's collected output through the WRONG accessor and fell
//       through to Object.prototype.toString, so every engine's stdout/stderr surfaced as
//       the literal string "[object Object]". Nothing crashed — the diagnostics simply
//       became useless, and GSAS-II's "No readers matched hint" (the clue to defect 2)
//       was swallowed by it.
//   (2) GSAS-II was always called with fmthint='GSAS powder'. import_generic keeps only
//       readers whose formatName contains the hint, so EVERY non-GSAS data file failed
//       with the misleading "No reader could read file".
//   (3) the refinement-review ledger key came from `gpx OR data`, so one physical sample
//       was booked into two ledgers inside one run (Y2O3 -> Y2O3_gsas -> gsas_proj),
//       splitting the round counter, the streaks, the E lag and any freeze.
//
// usage: node verify-review-key.mjs [--root <plugin root>]
//   DSH_XRD_GSAS_INTAKE=1 additionally runs the REAL GSAS-II intake control (slow,
//   needs GSAS-II + an unsandboxed environment that can spawn a child process).
import { readFileSync, existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { join, dirname, resolve, basename } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'

const HERE = dirname(fileURLToPath(import.meta.url))
const argv = process.argv.slice(2)
const rootIdx = argv.indexOf('--root')
function defaultRoot() {
  if (process.env.DSH_XRD_SRC) return resolve(process.env.DSH_XRD_SRC)
  const sibling = resolve(HERE, '..', '..', 'dsh-xrd')
  return existsSync(sibling) ? sibling : resolve(HERE, '..')
}
const ROOT = rootIdx >= 0 ? argv[rootIdx + 1] : defaultRoot()
const WORK = join(HERE, '.work', 'review-key')

let pass = 0, fail = 0, skipped = 0
const check = (ok, label, detail = '') => {
  if (ok) { pass++; console.log('  PASS  ' + label) }
  else { fail++; console.log('  FAIL  ' + label + (detail ? '\n        ' + detail : '')) }
}
const skip = (why) => { skipped++; console.log('  SKIP  ' + why) }

console.log('plugin root :', ROOT)
const tools = await import(pathToFileURL(join(ROOT, 'lib', 'xrd-tools.js')).href)
const review = await import(pathToFileURL(join(ROOT, 'lib', 'review.js')).href)
// The interpreter must come from the plugin's OWN resolution chain (DSH_XRD_PY ->
// xrd.local.json -> newest LOCALAPPDATA Python -> PATH). A bare 'python' from PATH is a
// Microsoft Store stub on this machine: it prints nothing and every child fails, which is
// exactly how the first version of section 6 failed all three of its checks.
const config = await import(pathToFileURL(join(ROOT, 'lib', 'config.js')).href)
const { readCollected, safeStem, soleLedgerSample, reviewSampleKey } = tools._internals

const fresh = (name) => {
  const d = join(WORK, name)
  rmSync(d, { recursive: true, force: true })
  mkdirSync(d, { recursive: true })
  return d
}

// ── 1. readCollected：宿主真实 API 与那个陷阱 ─────────────────────────────────
console.log('== 1. collected output is read through the host\'s real API ==')
{
  // the real @deepseek-ai/dsh-subprocess-local OutputCollector shape
  const real = { readFrom: (fromByte) => ({ text: 'engine stderr ' + fromByte, nextOffset: 9, lossy: false }) }
  check(readCollected(real) === 'engine stderr 0', 'readFrom(0).text is returned', JSON.stringify(readCollected(real)))
  check(readCollected({ finalize: () => ({ text: 'finalized', truncated: false }) }) === 'finalized',
    'a finalize()-only collector works')
  check(readCollected({ text: 'already read' }) === 'already read', 'an already-read result works')
  check(readCollected({ text: () => 'legacy accessor' }) === 'legacy accessor', 'the legacy text() accessor still works')
  check(readCollected('plain string') === 'plain string', 'a plain string passes through')
  check(readCollected(null) === '' && readCollected(undefined) === '', 'null/undefined yield an empty string')
  check(readCollected({}) === '', 'a bare object yields EMPTY, never a fake string', JSON.stringify(readCollected({})))
  check(!/\[object Object\]/.test(readCollected({})), 'THE REGRESSION: no "[object Object]" for a bare object')
  // a poisoned object whose only "toString" is the inherited one
  check(readCollected(Object.create({ readFrom: undefined })) === '',
    'an object with a shadowed/absent reader is not stringified')
  check(readCollected({ toString: () => 'custom text' }) === 'custom text',
    'an OVERRIDDEN toString is still honoured')
  check(!/\[object Object\]/.test(readCollected({ toString: Object.prototype.toString })),
    'an explicitly inherited Object.prototype.toString is refused')
}

// ── 2. 机械栏键：与 review.js 的 safeKey 必须一致 ─────────────────────────────
console.log('== 2. the ledger key matches review.js\' own sanitiser ==')
{
  check(safeStem('Y2O3_gsas.fxye') === 'Y2O3_gsas', 'stem drops the extension')
  check(safeStem('a b/c.dat') === 'a_b_c', 'illegal characters collapse to _', JSON.stringify(safeStem('a b/c.dat')))
  check(safeStem('').length === 0 && safeStem(null).length === 0, 'empty input stays empty')
  const dir = fresh('keys')
  const mech = basename(review._internals.mechPath(dir, safeStem('a b/c.dat')))
  check(mech === 'review_mechanical_a_b_c.json',
    'review.js writes exactly the file safeStem predicts (no third spelling)', mech)
  // idempotent: sanitising a ledger key never renames the ledger
  check(safeStem('Y2O3_gsas') === 'Y2O3_gsas', 'safeStem is idempotent on an already-safe key')
}

// ── 3. 目录里唯一的机械栏 ────────────────────────────────────────────────────
console.log('== 3. soleLedgerSample reads back the only ledger of a directory ==')
{
  const none = fresh('ledger-none')
  check(soleLedgerSample(none) === null, 'no ledger -> null')
  const one = fresh('ledger-one')
  writeFileSync(join(one, 'review_mechanical_Y2O3.json'), '{}')
  check(soleLedgerSample(one) === 'Y2O3', 'one ledger -> its sample key')
  const unkeyed = fresh('ledger-unkeyed')
  writeFileSync(join(unkeyed, 'review_mechanical.json'), '{}')
  check(soleLedgerSample(unkeyed) === null, 'the legacy UNKEYED ledger -> null (no bogus key)')
  const two = fresh('ledger-two')
  writeFileSync(join(two, 'review_mechanical_Y2O3.json'), '{}')
  writeFileSync(join(two, 'review_mechanical_other.json'), '{}')
  check(soleLedgerSample(two) === null, 'two ledgers -> null: never guesses between samples')
  const junk = fresh('ledger-junk')
  writeFileSync(join(junk, 'review_judgment.md'), '# not a ledger')
  check(soleLedgerSample(junk) === null, 'a judgement file is not mistaken for a ledger')
}

// ── 4. reviewSampleKey：一轮 e2e 只能有一个键 ────────────────────────────────
console.log('== 4. one sample keeps ONE ledger across the whole chain ==')
{
  const dir = fresh('samplekey')
  const D = { data: 'D:/e2e/Y2O3.dat' }
  const D2 = { data: 'D:/e2e/Y2O3_gsas.fxye' }
  const C = { cif: 'D:/e2e/Y2O3.cif' }
  check(reviewSampleKey({ ...D, ...C }, dir) === 'Y2O3', 'CIF stem wins over the data stem',
    String(reviewSampleKey({ ...D, ...C }, dir)))
  check(reviewSampleKey({ ...D2, ...C }, dir) === 'Y2O3',
    'THE REGRESSION: a converted data file no longer opens a second ledger',
    String(reviewSampleKey({ ...D2, ...C }, dir)))
  check(reviewSampleKey({ ...D2, ...C }, dir) === reviewSampleKey({ ...D, ...C }, dir),
    'the bare .dat and the converted .fxye key the SAME sample')
  check(reviewSampleKey(D, dir) === 'Y2O3', 'data stem is used when no CIF is given')
  check(reviewSampleKey({ ...D2, ...C, sample: 'my sample 2' }, dir) === 'my_sample_2',
    'an explicit sample wins and is sanitised', String(reviewSampleKey({ ...D2, ...C, sample: 'my sample 2' }, dir)))
  check(reviewSampleKey({ gpx: 'D:/e2e/gsas_proj.gpx' }, dir) === 'gsas_proj',
    'a gpx with no ledger present keys on the project name')
  writeFileSync(join(dir, 'review_mechanical_Y2O3.json'), '{}')
  check(reviewSampleKey({ gpx: 'D:/e2e/gsas_proj.gpx' }, dir) === 'Y2O3',
    'THE REGRESSION: continuing a project joins the existing ledger', String(reviewSampleKey({ gpx: 'D:/e2e/gsas_proj.gpx' }, dir)))
  writeFileSync(join(dir, 'review_mechanical_other.json'), '{}')
  check(reviewSampleKey({ gpx: 'D:/e2e/gsas_proj.gpx' }, dir) === 'gsas_proj',
    'two ledgers make it ambiguous again -> the derived key is used (never guess)')
  check(reviewSampleKey({}, dir) === null, 'nothing derivable -> null')
  check(reviewSampleKey({ data: 42 }, dir) === null, 'a non-string path does not throw')
}

// ── 4b. 0.8.9：0.8.7 留下的分裂账本要能被看见 ─────────────────────────────────
console.log('== 4b. a ledger split by 0.8.7 is reported, never merged silently ==')
{
  const dir = fresh('split')
  const { splitLedgerWarning } = tools._internals
  check(splitLedgerWarning(dir, 'Y2O3') === null, 'no other ledger -> no warning')
  writeFileSync(join(dir, 'review_mechanical_Y2O3.json'), '{}')
  check(splitLedgerWarning(dir, 'Y2O3') === null, "only the sample's own ledger -> no warning")
  writeFileSync(join(dir, 'review_mechanical_Y2O3_gsas.json'), '{}')
  const w = splitLedgerWarning(dir, 'Y2O3')
  check(!!w && /SPLIT/.test(w) && /Y2O3_gsas/.test(w),
    'the legacy suffixed ledger is named in the warning', String(w))
  check(!!w && /sample:"Y2O3"/.test(w), 'and the warning says how to book into the intended ledger')
  const wRev = splitLedgerWarning(dir, 'Y2O3_gsas')
  check(!!wRev && /Y2O3\b/.test(wRev),
    'the reverse direction is reported too (the derived key extends an existing one)')
  writeFileSync(join(dir, 'review_mechanical_other_sample.json'), '{}')
  const wUnrelated = splitLedgerWarning(dir, 'Y2O3')
  check(wUnrelated !== null && !/other_sample/.test(wUnrelated),
    'an unrelated sample in the same directory is NOT reported as a split', String(wUnrelated))
  check(splitLedgerWarning(null, 'Y2O3') === null && splitLedgerWarning(dir, null) === null,
    'missing inputs are handled without throwing')
  // the tool must actually surface it (a helper nobody calls would be dead code)
  const toolsSrc = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
  check(/reviewSplitWarning/.test(toolsSrc)
    && /out\.warning = \[\.\.\.\(out\.warning \|\| \[\]\), reviewSplitWarning\]/.test(toolsSrc),
    'xrd_refine_gsas pushes the split warning into its result warning list')
}

// ── 5. 源码级守卫（(c) 级证据：只证明没有把默认值写回去） ─────────────────────
console.log('== 5. no default hint / no hard-coded ledger key is put back ==')
{
  const js = readFileSync(join(ROOT, 'lib', 'xrd-tools.js'), 'utf8')
  const eng = readFileSync(join(ROOT, 'engine', 'gsas_engine.py'), 'utf8')
  const ops = readFileSync(join(ROOT, 'engine', 'gsas_ops.py'), 'utf8')
  // The guard targets a LIVE default only — the fix itself documents the old value in
  // comments, docstrings and schema descriptions, and a literal anywhere in prose is not
  // a regression. Note `add_phase(..., fmthint='CIF')` is legitimate: the CIF reader's
  // formatName really does contain "CIF"; what must never come back is a default for the
  // POWDER DATA import.
  const jsDefault = js.split(/\r?\n/).map((l, i) => ({ l: l.trim(), n: i + 1 }))
    .filter(({ l }) => /fmthint:\s*['"][^'"]*['"]/.test(l))
  check(jsDefault.length === 0, 'xrd-tools.js: the data plan carries no literal fmthint default', JSON.stringify(jsDefault))
  for (const [name, src] of [['gsas_engine.py', eng], ['gsas_ops.py', ops]]) {
    const callDefault = src.split(/\r?\n/).map((l, i) => ({ l: l.trim(), n: i + 1 }))
      .filter(({ l }) => /add_powder_histogram\(/.test(l) && /fmthint\s*=\s*['"]/.test(l))
    const getDefault = [...src.matchAll(/get\(\s*['"]fmthint['"]\s*,\s*['"][^'"]*['"]\s*\)/g)]
      .map((m) => m[0])
    check(callDefault.length === 0, `${name}: add_powder_histogram gets no literal hint`, JSON.stringify(callDefault))
    check(getDefault.length === 0, `${name}: no data.get('fmthint', <default>)`, JSON.stringify(getDefault))
  }
  const tools = js
  for (const key of ['fmthint', 'sample', 'review_sample', 'fmthint_used']) {
    check(new RegExp(`\\b${key}: \\{`).test(tools), `the tool schema still exposes "${key}"`)
  }
  check(/_internals = \{ readCollected, safeStem, soleLedgerSample, reviewSampleKey, splitLedgerWarning \}/.test(tools),
    'the internals this suite imports are exported on purpose')
}

// ── 6. 真实的 GSAS-II 数据入口对照（需要 GSAS-II + 非沙箱环境） ────────────────
console.log('== 6. real GSAS-II intake control (gated) ==')
if (process.env.DSH_XRD_GSAS_INTAKE !== '1') {
  skip('set DSH_XRD_GSAS_INTAKE=1 to run the real GSAS-II intake control (spawns python)')
} else {
  // The fixtures ship WITH the audit module (audit/fixtures), so this control needs no
  // external input — the first version pointed at a CIF that never existed, which made
  // the branch permanently unreachable.
  const FIXTURES = join(HERE, 'fixtures')
  const dat = join(FIXTURES, 'Y2O3.dat')
  const cif = join(FIXTURES, 'Y2O3.cif')
  const gsas = process.env.DSH_GSAS_ROOT || ''
  const py = config.pyExecutable()
  // GSAS-II needs instrument parameters to BUILD a histogram; the file lives in the
  // GSAS-II installation's own test directory (absent on some installs, hence the
  // explicit override). Without it the engine fails for a reason that has nothing to do
  // with the reader filter under test.
  const instCandidates = [
    process.env.DSH_XRD_INSTPRM,
    gsas ? join(gsas, 'tests', 'testinp', 'INST_XRY.PRM') : null,
  ].filter(Boolean)
  const instprm = instCandidates.find((p) => existsSync(p)) || null
  if (!existsSync(dat) || !existsSync(cif)) {
    skip(`fixtures missing (${dat} / ${cif})`)
  } else if (!instprm) {
    skip(`no instrument parameter file found (set DSH_XRD_INSTPRM; tried ${instCandidates.join(', ') || 'nothing'})`)
  } else {
    mkdirSync(WORK, { recursive: true })
    console.log('  python   :', py)
    console.log('  instprm  :', instprm)
    const run = (fmthint) => {
      const tag = fmthint ? 'hint' : 'nohint'
      const plan = join(WORK, `plan_${tag}.json`)
      writeFileSync(plan, JSON.stringify({
        run_id: 'intake-' + tag, out_dir: WORK, gsas_root: gsas,
        data: { file: dat, fmthint }, instprm, cif, refine: [], max_cycles: 1,
      }, null, 1))
      // A confined sandbox refuses piped stdio (EPERM) — that is the environment, not a
      // defect, so it must degrade to a skip instead of a failure.
      const r = spawnSync(py, ['-B', join(ROOT, 'engine', 'gsas_engine.py'), plan,
        '--out', join(WORK, `res_${tag}.json`)], { encoding: 'utf8', windowsHide: true })
      if (r.error) throw r.error
      let res = null
      try { res = JSON.parse(readFileSync(join(WORK, `res_${tag}.json`), 'utf8')) } catch { /* none */ }
      return { out: (r.stdout || '') + (r.stderr || ''), res }
    }
    try {
      const a = run(null)
      check(a.res && a.res.ok === true && (a.res.histograms || []).length === 1,
        'a bare two-column .dat IS imported with no hint (the fix)',
        a.out.slice(-300))
      const b = run('GSAS powder')
      const msg = (b.res && b.res.error) || b.out
      check(/No reader could read file/i.test(msg) && /fmthint='GSAS powder'/.test(msg),
        'the SAME file fails under the old pinned hint — the control that proves the cause',
        String(msg).slice(-300))
      check(/xrd_data_io/.test(String(msg)),
        'and the failure names a way forward (0.8.8 error text)', String(msg).slice(-200))
    } catch (err) {
      skip(`the intake control could not spawn a child here: ${err?.message ?? err}`)
    }
  }
}

console.log(`\n  ${fail === 0 ? 'ALL PASS' : 'FAILURES'} — pass=${pass} fail=${fail} skipped=${skipped}`)
rmSync(WORK, { recursive: true, force: true })
process.exit(fail === 0 ? 0 : 1)
