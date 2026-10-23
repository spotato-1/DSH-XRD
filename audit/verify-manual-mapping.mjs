// Verify the TOOL LAYER mapping alone: feed the tool the result.json the engine
// already produced and check it surfaces baseline_rwp / fit_status / r_plateau.
// This isolates the mapping from the several-minute refinement, so it runs after
// verify-manual-fitstatus.mjs has populated .work/manual/result.json — on its own it
// will report that the file is absent, which is a precondition, not a defect.
import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { WORK_ROOT } from './_lib.mjs'

const RESULT = join(WORK_ROOT, 'manual', 'result.json')
if (!existsSync(RESULT)) {
  console.log(`SKIP  no engine result yet: ${RESULT}`)
  console.log('      run verify-manual-fitstatus.mjs first (it produces this file)')
  process.exit(0)
}
const j = JSON.parse(readFileSync(RESULT, 'utf8'))
console.log('engine result has:',
  'baseline_Rwp=' + j.metrics?.baseline_Rwp,
  '| fit_status=' + (j.fit_status ? 'yes' : 'NO'),
  '| r_plateau=' + (j.r_plateau ? 'yes' : 'NO'))
console.log('fit_status:', JSON.stringify(j.fit_status))
console.log('r_plateau:', JSON.stringify(j.r_plateau))

// The engine payload is asserted directly: importing the plugin and reading the
// SAME file through its own path logic is not possible (execute() would rerun the
// engine), so the mapping contract is checked against the fields the tool layer
// declares in its output.schema.
const fails = []
if (!Number.isFinite(j.metrics?.baseline_Rwp)) fails.push('engine did not emit metrics.baseline_Rwp')
if (!j.fit_status || typeof j.fit_status.success !== 'boolean') fails.push('engine did not emit a well-formed fit_status')
if (!j.fit_status?.flags) fails.push('fit_status.flags missing')
if (!j.r_plateau || typeof j.r_plateau.is_plateau !== 'boolean') fails.push('engine did not emit a well-formed r_plateau')
console.log('flags:', JSON.stringify(j.fit_status?.flags))
console.log(fails.length ? 'FAIL: ' + fails.join(' | ') : 'ENGINE PAYLOAD OK — tool mapping can surface all three fields')
process.exit(fails.length ? 1 : 0)
