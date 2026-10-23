// verify-render-nullsafe.mjs — every tool's render() must survive a value-less call.
//
// WHY (0.7.2, found by the packaged self-audit on 2026-09-23): verify-autofp-replaced.mjs
// calls `tool.output.render({}, value)` after a `try { value = await execute(...) } catch`,
// so when execute THROWS the value is `null` — and xrd_refine_auto's render dereferenced
// `value.rwp` unguarded and died with
//
//     TypeError: Cannot read properties of null (reading 'rwp')
//
// That surfaced as an unhandled TypeError with a stack trace instead of a named failure.
// No host path was proven to render after a throw, so this is hardening rather than a
// reported bug — but the audit's own comment said "the render must not throw", so the
// plugin should actually hold that line, and a render is the one place a crash is
// guaranteed to hide the real error.
//
// This control is CENTRAL on purpose: eleven renders across two files, each with its own
// value shape, so a per-script assertion would only ever cover the tool that script uses.
//
// Static: assembles the plugin with a mock context. No engine is spawned.
//
// Negative control (2026-09-23): run it against `dist/dsh-xrd-0.7.1` — the build made
// BEFORE the guards — and it must FAIL. `--root` selects the tree.
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
console.log('tool layer under test:', ENTRY)

const tools = []
const mod = await import(pathToFileURL(ENTRY).href)
;(mod.apply ?? mod.default)(mockContext(tools))
report.check(tools.length === 11, `the plugin registered 11 tools`,
  `expected 11 tools, got ${tools.length}`)

// the values a render must survive: what the host could hand over when a call produced
// nothing (null), and the "no value at all" shape.
const CASES = [['null', null], ['undefined', undefined]]

for (const tool of tools) {
  const render = tool.output?.render
  report.check(typeof render === 'function', `${tool.name}: has a render`,
    `${tool.name}: no render function`)
  if (typeof render !== 'function') continue
  for (const [label, v] of CASES) {
    let blocks = null
    let err = null
    try {
      blocks = render({}, v)
    } catch (e) {
      err = e
    }
    const shaped = Array.isArray(blocks) && blocks.length > 0 && typeof blocks[0]?.text === 'string'
    const text = shaped ? blocks[0].text : ''
    report.check(!err && shaped, `${tool.name}: render(${label}) returns text without throwing`,
      `${tool.name}: render(${label}) — ${err ? `${err.name}: ${err.message}` : `not a text card: ${JSON.stringify(blocks)?.slice(0, 120)}`}`)
    // an empty card is a silent failure: it reports nothing and looks like a pass
    if (!err && shaped) {
      report.check(text.trim().length > 0, `${tool.name}: render(${label}) text is non-empty`,
        `${tool.name}: render(${label}) returned empty text`)
    }
  }
}

const ok = report.fails.length === 0
console.log(ok
  ? `\nRENDER NULL-SAFETY OK — ${tools.length} tool(s) x ${CASES.length} value-less call(s)`
  : `\n${report.fails.length} RENDER CHECK(S) FAILED`)
process.exit(report.finish(ok
  ? `render null-safety OK — ${tools.length} tool(s) x ${CASES.length} value-less call(s)`
  : `${report.fails.length} render check(s) failed`))
