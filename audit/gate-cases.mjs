// T-07 case table, run against the PURE gateDecision() of the plugin source tree.
// No engine, no sidecar, no I/O: this checks the decision table itself.
import { gateDecision, GATE_KINDS_OK, GATE_TEXT } from '../../dsh-xrd/lib/xrd-tools.js'

const CASES = [
  // id, verify doc, opts, expected {allow, waived, messageId}
  ['a  no provenance', { ok: false, reason: 'no provenance' }, {}, { allow: false, waived: false, messageId: 'R1' }],
  ['b  head match', { ok: true, kind: 'generated', position: 'head' }, {}, { allow: true, waived: false, messageId: null }],
  ['c  descendant', { ok: true, kind: 'generated', position: 'descendant:fp2k_run1' }, {}, { allow: true, waived: false, messageId: null }],
  ['d1 bytes mismatch', { ok: false, reason: 'pcr bytes do not match any recorded hash' }, {}, { allow: false, waived: false, messageId: 'R2' }],
  ['d2 chain broken', { ok: false, reason: 'chain broken at descendant:fp2k_run1 (parent sha missing)' }, {}, { allow: false, waived: false, messageId: 'R2' }],
  ['e  sidecar unreadable', { ok: false, reason: 'sidecar unreadable: JSONDecodeError: Expecting property name' }, {}, { allow: false, waived: false, messageId: 'R2' }],
  ['f  user_provided ok', { ok: true, kind: 'user_provided', position: 'head' }, {}, { allow: true, waived: false, messageId: null }],
  ['g  user_provided edited', { ok: false, kind: 'user_provided', reason: 'pcr bytes do not match any recorded hash' }, {}, { allow: false, waived: false, messageId: 'R3' }],
  ['h  waiver + reason', { ok: false, reason: 'no provenance' }, { allowUnprovenanced: true, reason: 'legacy batch from 2026-11-06' }, { allow: true, waived: true, messageId: null }],
  ['i  waiver no reason', { ok: false, reason: 'no provenance' }, { allowUnprovenanced: true }, { allow: false, waived: false, messageId: 'R3' }],
  ['j  adopted kind', { ok: true, kind: 'adopted', position: 'head' }, {}, { allow: false, waived: false, messageId: 'R2' }],
  // extra hard cases the table does not spell out but the code must get right
  ['x  waiver cannot cover bytes mismatch', { ok: false, reason: 'pcr bytes do not match any recorded hash' }, { allowUnprovenanced: true, reason: 'trust me' }, { allow: false, waived: false, messageId: 'R2' }],
  ['y  waiver cannot cover broken chain', { ok: false, reason: 'chain broken at descendant:x' }, { allowUnprovenanced: true, reason: 'trust me' }, { allow: false, waived: false, messageId: 'R2' }],
  ['z  blank reason is no reason', { ok: false, reason: 'no provenance' }, { allowUnprovenanced: true, reason: '   ' }, { allow: false, waived: false, messageId: 'R3' }],
  ['w  exe_fallback is accepted', { ok: true, kind: 'exe_fallback', position: 'head' }, {}, { allow: true, waived: false, messageId: null }],
  ['v  verify without kind cannot pass', { ok: true }, {}, { allow: false, waived: false, messageId: 'R2' }],
]

let pass = 0
for (const [id, verify, opts, want] of CASES) {
  const got = gateDecision(verify, opts)
  const ok = got.allow === want.allow && got.waived === want.waived && got.messageId === want.messageId
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${id.padEnd(38)} allow=${got.allow} waived=${got.waived} msg=${got.messageId}`)
  if (!ok) console.log(`       expected allow=${want.allow} waived=${want.waived} msg=${want.messageId}`)
  if (ok) pass++
}
// the wording constants must be the frozen T-07 text, three lines each
for (const k of ['R1', 'R2', 'R3']) {
  const lines = GATE_TEXT[k].split('\n')
  const ok = lines.length === 3 && /^refused:/.test(lines[0]) && /^why:/.test(lines[1]) && /^next:/.test(lines[2])
  console.log(`${ok ? '[PASS]' : '[FAIL]'} wording ${k}: ${lines.length} line(s), segments ok=${ok}`)
  if (ok) pass++
}
const ascii = /^[\x20-\x7e\n]*$/.test(GATE_TEXT.R1 + GATE_TEXT.R2 + GATE_TEXT.R3 + GATE_TEXT.UNVERIFIED_ORIGIN)
console.log(`${ascii ? '[PASS]' : '[FAIL]'} wording is pure ASCII (tools print English)`)
if (ascii) pass++
console.log(`\nwhitelist = ${JSON.stringify(GATE_KINDS_OK)}`)
console.log(`GATE CASES ${pass}/${CASES.length + 4} passed`)
process.exit(pass === CASES.length + 4 ? 0 : 1)
