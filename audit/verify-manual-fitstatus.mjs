// Verify the manual engine's judgement fields end-to-end through the tool layer
// (output.schema is additionalProperties:false — an undeclared field would reject
// the whole result). Takes a few minutes: the engine runs to its max_nfev cap on
// purpose, which is exactly the state we want to see reported honestly.
//
// Tool-layer paths come from audit/_lib.mjs (default: the tree this audit ships in).
import { mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
const WORK = join(WORK_ROOT, 'manual')
const DATA = join(FIXTURES, 'Y2O3.dat')
const CIF = join(FIXTURES, 'Y2O3.cif')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', ENTRY)

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
await (mod.apply ?? mod.default)(ctx)
const tool = tools.find(t => t.name === 'xrd_refine_manual')
if (!tool) throw new Error('xrd_refine_manual not registered')

const exec = { agent: { session: { header: { cwd: WORK } } } }
const t0 = Date.now()
let value = null, threw = null
try {
  value = await tool.execute({ data: DATA, cif: CIF, stage_hint: 'structure', out_dir: WORK }, exec)
} catch (e) { threw = String(e.message).slice(0, 500) }

console.log('elapsed:', ((Date.now() - t0) / 1000).toFixed(1), 's')
console.log('threw:', threw || '(no)')
console.log('rwp:', value?.rwp, '| baseline_rwp:', value?.baseline_rwp)
console.log('fit_status:', JSON.stringify(value?.fit_status))
console.log('r_plateau:', JSON.stringify(value?.r_plateau))

const fails = report.fails
if (threw) fails.push('threw: ' + threw)
if (typeof value?.rwp !== 'number') fails.push('no numeric rwp')
if (typeof value?.baseline_rwp !== 'number') fails.push('baseline_rwp missing (the whole point of the addition)')
if (!value?.fit_status || typeof value.fit_status.success !== 'boolean') fails.push('fit_status missing/ill-formed')
if (!value?.r_plateau || typeof value.r_plateau.is_plateau !== 'boolean') fails.push('r_plateau missing/ill-formed')
const rendered = tool.output.render({}, value)
console.log('--- rendered ---')
console.log(rendered[0].text.slice(0, 700))
process.exit(report.finish('ALL ASSERTIONS PASSED — manual-engine judgement fields cross the tool layer'))
