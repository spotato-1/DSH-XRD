// REAL tool-call verification of the replaced interface: xrd_refine_auto must run
// the bundled headless fp2k driver (not AutoFP) and return a real Rwp, and the
// retired AutoFP-era surface (alias autofp_refine, params path/autoselect, output
// fields par_summary/ladder_summary) must be gone.
//
// Tool-layer paths come from audit/_lib.mjs (default: the tree this audit ships in).
import { mkdirSync, rmSync, copyFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
const WORK = join(WORK_ROOT, 'refine_auto')
const SRC_PCR = join(FIXTURES, 'Y2O3.pcr')      // converter-produced pcr
const DAT = join(FIXTURES, 'Y2O3.dat')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', ENTRY)

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
await (mod.apply ?? mod.default)(ctx)

// ── interface contract assertions ──
const names = tools.map(t => t.name)
const tool = tools.find(t => t.name === 'xrd_refine_auto')
const fails = report.fails
if (!tool) throw new Error('xrd_refine_auto not registered; tools: ' + names.join(','))
if (names.includes('autofp_refine')) fails.push('the retired alias autofp_refine is still registered')
if (names.length !== 12) fails.push(`expected 12 tools (0.8.10 added xrd_refine_multiphase), got ${names.length}: ${names.join(', ')}`)
const props = Object.keys(tool.parameters.properties)
for (const p of ['pcr', 'ladder', 'cycles', 'out_dir']) {
  if (!props.includes(p)) fails.push(`parameter ${p} missing (have: ${props.join(',')})`)
}
for (const gone of ['path', 'autoselect']) {
  if (props.includes(gone)) fails.push(`stale parameter ${gone} still declared`)
}
if (!tool.parameters.required?.includes('pcr')) fails.push('pcr is not required')
const outProps = Object.keys(tool.output.schema.properties)
for (const k of ['ok', 'rwp', 'refined_pcr', 'out_files', 'ladder', 'cycles']) {
  if (!outProps.includes(k)) fails.push(`output field ${k} missing (have: ${outProps.join(',')})`)
}
for (const gone of ['par_summary', 'ladder_summary']) {
  if (outProps.includes(gone)) fails.push(`stale output field ${gone} still declared`)
}
if (!/fp2k_root|DSH_FP2K_ROOT/.test(tool.description)) fails.push('description does not name the new fp2k_root/DSH_FP2K_ROOT locator')
if (!/\.fp2k-work-/.test(tool.description)) fails.push('description does not mention the .fp2k-work-* artifact dir')
console.log('registered tools:', names.length, '|', names.join(', '))
console.log('parameters:', props.join(', '))
console.log('output fields:', outProps.join(', '))

// a converter-produced pcr, paired with its data under the declared name
const pcr = join(WORK, 'Y2O3.pcr')
copyFileSync(SRC_PCR, pcr)
copyFileSync(DAT, join(WORK, 'Y2O3.dat'))

const exec = { agent: { session: { header: { cwd: WORK } } } }
const t0 = Date.now()
let value = null, threw = null
try {
  value = await tool.execute({ pcr, cycles: 20, ladder: true, out_dir: WORK }, exec)
} catch (e) { threw = String(e.message).slice(0, 400) }
const dt = ((Date.now() - t0) / 1000).toFixed(1)

console.log('--- result ---')
console.log('elapsed:', dt, 's')
console.log('threw:', threw || '(no)')
console.log('ok:', value?.ok, '| rwp:', value?.rwp, '| cycles:', value?.cycles)
console.log('ladder:', JSON.stringify(value?.ladder))
console.log('refined_pcr:', value?.refined_pcr)
console.log('error:', value?.error || '(none)')
console.log('out_files:', (value?.out_files || []).length)

// assertions on the run
if (threw) fails.push('tool threw: ' + threw)
if (value?.ok !== true) fails.push('ok is not true')
if (typeof value?.rwp !== 'number') fails.push('no numeric rwp (got ' + value?.rwp + ')')
else if (!(value.rwp > 0 && value.rwp < 100)) fails.push('implausible rwp ' + value.rwp)
if (!value?.ladder) fails.push('no ladder judgement packet')
if (!value?.refined_pcr || !existsSync(value.refined_pcr)) fails.push('refined_pcr missing on disk')
else if (!value.refined_pcr.includes('.fp2k-work-')) fails.push('refined_pcr is not in a .fp2k-work-* dir')
if (!(value?.out_files || []).some(f => f.endsWith('.out'))) fails.push('no .out artifact kept')

// the render must not throw. Wrapped on purpose: an unguarded call turns a render defect
// into an unhandled TypeError + stack trace, which is exactly how the 0.7.2 null-value
// defect hid. When execute THREW there is no value to render, so the null here is an
// artifact of this script rather than a host path — the null-safety of all eleven renders
// is asserted centrally by verify-render-nullsafe.mjs.
let rendered = null
try { rendered = tool.output.render({}, value) } catch (e) { fails.push('render threw: ' + e.message) }
console.log('--- rendered ---')
console.log(rendered ? rendered[0].text.slice(0, 600) : `(no render: ${threw ? 'execute threw, so there is no value to render' : 'render threw'})`)

console.log(fails.length ? 'FAIL: ' + fails.join(' | ') : 'ALL ASSERTIONS PASSED')
process.exit(report.finish('ALL ASSERTIONS PASSED — xrd_refine_auto runs the bundled fp2k driver'))
