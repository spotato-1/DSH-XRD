// Regression: xrd_verify_fp2k uses the SHARED fp2kRoot() probe from config.js
// (its own duplicate was removed), so it must still resolve fp2k and run.
//
// Tool-layer paths come from audit/_lib.mjs (default: the tree this audit ships in).
import { mkdirSync, rmSync, copyFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
const WORK = join(WORK_ROOT, 'verify_fp2k')
const SRC_PCR = join(FIXTURES, 'Y2O3.pcr')
const DAT = join(FIXTURES, 'Y2O3.dat')
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', ENTRY)

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
await (mod.apply ?? mod.default)(ctx)
const tool = tools.find(t => t.name === 'xrd_verify_fp2k')
if (!tool) throw new Error('xrd_verify_fp2k not registered')

// fp2k resolves the pattern from the name DECLARED inside the pcr, so both the
// pcr stem and the declared data name must be present next to each other.
const pcr = join(WORK, 'Y2O3.pcr')
copyFileSync(SRC_PCR, pcr)
copyFileSync(DAT, join(WORK, 'Y2O3.dat'))

const exec = { agent: { session: { header: { cwd: WORK } } } }
let value = null, threw = null
try { value = await tool.execute({ pcr, out_dir: WORK }, exec) } catch (e) { threw = String(e.message).slice(0, 300) }
console.log('threw:', threw || '(no)')
console.log('value:', JSON.stringify(value).slice(0, 500))
const fails = report.fails
if (threw) fails.push('threw: ' + threw)
if (!value || value.ok === false) fails.push('verify did not succeed')
else {
  if (typeof value.rwp !== 'number' && typeof value.Rwp !== 'number') fails.push('no Rwp in verify result')
  if (!value.density && !value.cell) fails.push('no density/cell parsed')
}
process.exit(report.finish('xrd_verify_fp2k OK — shared fp2kRoot() resolves fp2k and runs'))
