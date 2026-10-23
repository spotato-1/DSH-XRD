// integrity-check.mjs — dsh-xrd logic-chain and engine integrity audit.
//
// WHY (user request 2026-09-21): many tools had their parameters, action lists and
// return fields changed in one session. The failure mode that matters is a SILENT
// chain break: a tool that advertises an action no engine implements, an engine file
// that is referenced but missing, a return field that is not declared in the output
// schema (the host rejects the whole result), or a bundled engine that no longer
// compiles. Every one of those is checked here.
//
// By default it audits the tree it SHIPS IN — so in a transplanted package it audits
// the package itself, assuming nothing else on the machine exists.
//
// Usage:
//   node integrity-check.mjs                 audit the tree this audit module lives in
//   node integrity-check.mjs --root <dir>    audit another plugin tree (an extracted
//                                            release zip, a dev checkout, ...)
//   node integrity-check.mjs --profile       audit the dsh runtime copy named by
//                                            $DSH_XRD_PROFILE
//   DSH_XRD_ROOT=<dir> node integrity-check.mjs     same as --root, via the environment
import { pathToFileURL } from 'node:url'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, basename } from 'node:path'
import { execFileSync } from 'node:child_process'
import { pluginRoot, profileRoot, python, report } from './_lib.mjs'

// `--profile` is a convenience for auditing the copy dsh actually loads; it needs
// DSH_XRD_PROFILE because a package cannot know where the host keeps its profile.
const ROOT = (process.argv.includes('--profile')
  ? requireProfile()
  : pluginRoot()).replace(/\\/g, '/').replace(/\/+$/, '')
const PY = python()

function requireProfile() {
  const p = profileRoot()
  if (!p) {
    console.error('--profile needs $DSH_XRD_PROFILE=<.../node_modules/dsh-xrd>\n' +
      '  (a shipped package cannot know the host\'s profile location)')
    process.exit(2)
  }
  return p
}

const info = []
const pass = report.pass
const fail = report.fail
const warn = report.warn

// Fail fast and legibly on a bad --root: otherwise a wrong path only surfaces as a
// confusing "Cannot find module .../lib/index.js" from the dynamic import below.
if (!existsSync(join(ROOT, 'package.json')) || !existsSync(join(ROOT, 'lib', 'index.js'))) {
  console.error(`not a dsh-xrd plugin tree: ${ROOT}\n` +
    '  (expected <root>/package.json and <root>/lib/index.js; check the --root path)')
  process.exit(2)
}

// ───────────────────────────────────────────────────────────── 1. assemble
console.log('== 1. plugin assembly (auditing: ' + ROOT + ') ==')
const tools = []
const ctx = { tools: { register: t => tools.push(t), guard: () => {} },
  inject() {}, on() {}, get: () => undefined,
  logger: { info() {}, warn() {}, error() {}, debug() {} } }
try {
  const mod = await import(pathToFileURL(join(ROOT, 'lib', 'index.js')).href)
  await (mod.apply ?? mod.default)(ctx)
  pass(`assembled: ${tools.length} tool(s)`)
} catch (e) {
  fail('assembly threw: ' + String(e.message).slice(0, 200))
}
const names = tools.map(t => t.name)
// 0.8.10: xrd_refine_multiphase joined the family (multiphase entry, >=3 phases).
if (tools.length !== 12) fail(`expected 12 tools, got ${tools.length}: ${names.join(', ')}`)
else pass('tool count = 12: ' + names.join(', '))

// ─────────────────────────────────────────────── 2. bundled engine paths exist
console.log('\n== 2. bundled engine paths (declared -> on disk -> parses) ==')
const libFiles = ['lib/index.js', 'lib/xrd-tools.js', 'lib/config.js']
const declared = new Map()          // engine file -> declared by
for (const f of libFiles) {
  const txt = readFileSync(join(ROOT, f), 'utf8')
  // Any 'engine/<name>.py' literal counts, whatever join() shape is used — matching
  // only join(PLUGIN_ROOT,'engine',...) made a differently-built path look undeclared.
  for (const m of txt.matchAll(/['"]engine['"]\s*,\s*['"]([^'"]+\.py)['"]/g)) {
    if (!declared.has(m[1])) declared.set(m[1], f)
  }
  for (const m of txt.matchAll(/engine\/([A-Za-z0-9_]+\.py)/g)) {
    if (!declared.has(m[1])) declared.set(m[1], f)
  }
}
// Syntax check. Deliberately NOT `python -m py_compile`: that WRITES __pycache__ next to
// the source, so under a sandbox that denies writes outside the workspace it fails with a
// permission error and the audit reports "FAILS to compile" for ten perfectly good
// engines — a read-only check turning into a false plugin defect (measured 2026-09-21
// against the dsh profile copy). `ast.parse` reads the file and writes nothing, so the
// result depends on the plugin and not on where this audit is allowed to write.
const SYNTAX_CHECK = 'import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read(),filename=sys.argv[1])'
for (const [eng, by] of [...declared].sort()) {
  const p = join(ROOT, 'engine', eng)
  if (!existsSync(p)) { fail(`engine MISSING on disk: engine/${eng} (declared in ${by})`); continue }
  try {
    execFileSync(PY, ['-c', SYNTAX_CHECK, p], { stdio: 'ignore' })
    pass(`engine/${eng} exists + parses`)
  } catch (e) {
    fail(`engine/${eng} FAILS to parse (declared in ${by})`)
  }
}
// engines present on disk but never declared (dead weight or a wiring slip)
// KNOWN_LEGACY: superseded files kept on purpose (user rule 2026-09-21: do not touch
// engines that already work). engine/cif2pcr_helper.py's two repairs (phase-name
// sanitising, Chebyshev -> polynomial background) now live in engine/fp2k_driver.py,
// and its occupancy audit is engine/pcr_check.py; the file is unreachable from the
// tool layer but deleting it is not worth the risk of a stale caller elsewhere.
const KNOWN_LEGACY = new Set(['cif2pcr_helper.py'])
const onDisk = readdirSync(join(ROOT, 'engine')).filter(f => f.endsWith('.py'))
for (const f of onDisk) {
  if (declared.has(f) || f === '__init__.py') continue
  if (KNOWN_LEGACY.has(f)) { info.push(`engine/${f}: KNOWN LEGACY — superseded, kept on purpose`); continue }
  warn(`engine/${f} on disk but not referenced by any lib file`)
}

// ─────────────────────────────────────── 3. action vocabulary: declared vs implemented
console.log('\n== 3. action vocabulary (tool schema vs engine source) ==')
const ENGINE_OF = {
  xrd_cif_edit: 'cif_edit.py',
  xrd_data_io: 'data_io.py',
  xrd_phase_screen: 'phase_screen.py',
  xrd_gsas_ops: 'gsas_ops.py',
}
for (const [toolName, engFile] of Object.entries(ENGINE_OF)) {
  const t = tools.find(x => x.name === toolName)
  if (!t) { fail(`tool ${toolName} not registered`); continue }
  const props = (t.parameters?.properties ?? {})
  const actProp = props.actions
  if (!actProp) {
    fail(`${toolName}: has no "actions" parameter, so its engine cannot be driven at all`)
    continue
  }
  // The action vocabulary lives in the `actions` parameter description (a long help
  // string). Cross-check every `{type:"..."}` form in it against what the engine
  // dispatches, ignoring JSON-Schema type names ({type:"string"} etc.).
  const desc = String(actProp.description ?? '') + ' ' + String(t.description ?? '')
  const src = readFileSync(join(ROOT, 'engine', engFile), 'utf8')
  const impl = new Set()
  // four dispatch shapes occur in this plugin: `atype == 'x'`, `atype in ('x','y')`,
  // `def act_x(`, and a dict table {..., 'x': act_x, ...}
  for (const m of src.matchAll(/atype\s*[=!]=\s*['"]([a-z_0-9]+)['"]/g)) impl.add(m[1])
  for (const m of src.matchAll(/atype\s+in\s*\(([^)]*)\)/g)) {
    for (const q of m[1].matchAll(/['"]([a-z_0-9]+)['"]/g)) impl.add(q[1])
  }
  for (const m of src.matchAll(/^\s*def act_([a-z_0-9]+)\(/gm)) impl.add(m[1])
  for (const m of src.matchAll(/['"]([a-z_0-9]+)['"]\s*:\s*act_/g)) impl.add(m[1])
  const SCHEMA_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'object',
    'array', 'null'])
  const declaredActs = new Set()
  for (const m of desc.matchAll(/\{type:\s*"([a-z_0-9]+)"/g)) {
    if (!SCHEMA_TYPES.has(m[1])) declaredActs.add(m[1])
  }
  for (const m of desc.matchAll(/"(?:type|action)"\s*:\s*"([a-z_0-9]+)"/g)) {
    if (!SCHEMA_TYPES.has(m[1])) declaredActs.add(m[1])
  }
  const missing = [...declaredActs].filter(a => !impl.has(a))
  if (declaredActs.size === 0) {
    warn(`${toolName}: its actions description carries no {type:"..."} form — ` +
         `cannot cross-check against engine/${engFile}`)
  } else if (missing.length) {
    fail(`${toolName}: advertised action(s) not implemented in engine/${engFile}: ${missing.join(', ')}`)
  } else {
    pass(`${toolName}: ${declaredActs.size} advertised action(s) all implemented ` +
         `(${[...declaredActs].sort().join(', ')})`)
  }
  // reverse direction: actions implemented but never advertised are unreachable from
  // the tool contract (dead code or a forgotten description update)
  const unadvertised = [...impl].filter(a => declaredActs.size && !declaredActs.has(a))
  if (declaredActs.size && unadvertised.length) {
    info.push(`${toolName}: engine/${engFile} implements action(s) the tool text does not ` +
              `advertise: ${unadvertised.sort().join(', ')}`)
  }
}

// ────────────────────────────────── 4. return fields declared in output schemas
console.log('\n== 4. output schemas are closed (additionalProperties:false) ==')
for (const t of tools) {
  const s = t.output?.schema
  if (!s) { fail(`${t.name}: no output schema`); continue }
  if (s.additionalProperties !== false) { warn(`${t.name}: output schema is OPEN (undeclared fields would pass)`); continue }
  const req = s.required ?? []
  if (!req.length) warn(`${t.name}: closed schema but declares no required field`)
  else pass(`${t.name}: closed schema, ${Object.keys(s.properties).length} field(s), requires ${req.join(',')}`)
}

// ───────────────────────────────────────────── 5. schema keyword subset (host)
console.log('\n== 5. host JSON-schema subset (type/oneOf/properties/required/items/enum/const) ==')
const ALLOWED = new Set(['type', 'oneOf', 'properties', 'required', 'additionalProperties',
  'items', 'enum', 'const', 'description', 'title', 'default', 'examples'])
function walkSchema(node, path, toolName) {
  if (!node || typeof node !== 'object') return
  for (const k of Object.keys(node)) {
    if (!ALLOWED.has(k)) fail(`${toolName} ${path}: unsupported keyword "${k}"`)
  }
  if (node.properties) for (const [k, v] of Object.entries(node.properties)) walkSchema(v, path + '.' + k, toolName)
  if (node.items) walkSchema(node.items, path + '[]', toolName)
  if (node.oneOf) node.oneOf.forEach((v, i) => walkSchema(v, path + `|oneOf[${i}]`, toolName))
}
for (const t of tools) {
  walkSchema(t.parameters, 'params', t.name)
  walkSchema(t.output?.schema, 'output', t.name)
}
pass('schema keyword scan complete (findings above, if any)')

// ───────────────────────────────────────── 6. logic chains the tools must support
console.log('\n== 6. logic chains (declared capability vs actual engine support) ==')
const CHAINS = [
  ['raw pattern -> refinement', ['xrd_data_io', 'xrd_refine_auto']],
  ['structure -> pcr -> refinement', ['xrd_cif_edit', 'cif_to_pcr', 'xrd_refine_auto']],
  ['phase identification -> refinement', ['xrd_phase_screen', 'xrd_refine_auto']],
  ['refinement -> GSAS ladder -> deliverable', ['xrd_refine_auto', 'xrd_refine_gsas', 'xrd_finalize']],
  ['manual engine -> unified export', ['xrd_refine_manual', 'xrd_export_cif']],
  ['cross-check', ['xrd_refine_auto', 'xrd_verify_fp2k']],
]
for (const [label, need] of CHAINS) {
  const missing = need.filter(n => !names.includes(n))
  if (missing.length) fail(`chain "${label}": missing tool(s) ${missing.join(', ')}`)
  else pass(`chain "${label}" complete: ${need.join(' -> ')}`)
}
// the fp2k driver must be reachable from the refinement tool's engine
const drv = join(ROOT, 'engine', 'fp2k_driver.py')
if (!existsSync(drv)) fail('engine/fp2k_driver.py missing (xrd_refine_auto cannot run)')
else {
  const txt = readFileSync(drv, 'utf8')
  for (const fn of ['run_fp2k', 'run_fp2k_mean', 'set_atom_code', 'pcr_refine_fingerprint',
                    'polynomialise_background', 'sanitise_phase_name']) {
    if (!new RegExp(`def ${fn}\\(`).test(txt)) fail(`fp2k_driver.py: ${fn}() missing`)
  }
  pass('fp2k driver exposes the expected entry points')
}

// ─────────────────────────────── 7. 缺陷总表（附录 A）与每样品检查表的一致性
// 附录 A 是 xrd-workflow §6 的只读底表；每样品 defect_table.json 引用的 id 必须存在于它的骨架里，
// 否则"逐类判 适用/不适用/待定"就是对着空气判。字段名更是一致性陷阱：骨架、正文 §6.9、
// 附录正文三处各写一份，任何一处漂了都会让 AI 按错的键名落档（实测已犯过一次）。
console.log('\n== 7. defect catalog (appendix A) + per-sample table consistency ==')
const CAT = join(ROOT, 'skills', 'xrd-workflow', 'refs', 'defect_catalog.json')
const CATMD = join(ROOT, 'skills', 'xrd-workflow', 'refs', 'defect-catalog.md')
const WFSKILL = join(ROOT, 'skills', 'xrd-workflow', 'SKILL.md')
if (!existsSync(CAT)) fail('skills/xrd-workflow/refs/defect_catalog.json missing (appendix A skeleton)')
else {
  let cat = null
  try { cat = JSON.parse(readFileSync(CAT, 'utf8')) } catch (e) { fail(`defect_catalog.json does not parse: ${e.message}`) }
  if (cat) {
    const rows = cat.rows || [], foot = cat.footnotes || []
    if (rows.length !== 14) fail(`defect_catalog.json: expected 14 defect classes, found ${rows.length}`)
    else pass(`catalog carries 14 defect classes (${rows.map(r => r.id).join(',')})`)
    const deep = rows.filter(r => r.family === '位点·占位').length
    const geo = rows.filter(r => r.family === '几何').length
    const mic = rows.filter(r => r.family === '扩展·微结构').length
    if (deep + geo + mic !== rows.length) fail(`catalog families inconsistent: 位点·占位=${deep} 几何=${geo} 扩展·微结构=${mic} total=${rows.length}`)
    else pass(`3 families covered: 位点·占位=${deep} / 几何=${geo} / 扩展·微结构=${mic}`)
    const ids = rows.map(r => r.id)
    const dup = ids.filter((v, i) => ids.indexOf(v) !== i)
    if (dup.length) fail(`duplicate defect id(s): ${[...new Set(dup)].join(', ')}`)
    else pass('defect ids unique')
    for (const id of ['D-06', 'D-09']) {
      const r = rows.find(x => x.id === id)
      if (!r) fail(`${id} missing (reachability marker has no home)`)
      else if (r.reachability !== true) fail(`${id} must carry reachability=true (start-point dependency: CIF screening must be confirmed)`)
    }
    if (rows.find(x => x.id === 'D-06')?.reachability && rows.find(x => x.id === 'D-09')?.reachability) {
      pass('reachability marked exactly where designed (D-06 ordering/supercell, D-09 distortion/tilt)')
    }
    // 骨架是**最小三字段**（id/family/defect，2026-09-25 定）；8 个必需字段落在附录正文，
    // 所以逐节核对的是 md，而不是 json（把 8 字段抄进 json = 两处真相，必漂）。
    const mdTxt = existsSync(CATMD) ? readFileSync(CATMD, 'utf8') : ''
    const detail = ['structural_form', 'xrd_signature', 'judge', 'params_backend', 'pitfall']
    const sections = mdTxt.split(/^### /m).slice(1).filter(s => /^D-\d\d /.test(s))
    if (sections.length !== 14) fail(`defect-catalog.md has ${sections.length} D-xx section(s), expected 14`)
    const thin = sections
      .map(s => [s.slice(0, 4), detail.filter(f => !s.includes('**' + f + '**'))])
      .filter(([, miss]) => miss.length)
      .map(([id, miss]) => `${id}: missing ${miss.join('/')}`)
    if (thin.length) fail(`defect-catalog.md row(s) incomplete — ${thin.join('; ')}`)
    else pass(`all 14 catalog rows carry the detail fields (${detail.join('/')}) in prose`)
    const need = cat.fields_required || []
    const stray = rows.flatMap(r => Object.keys(r).filter(k => !['id', 'family', 'defect', 'reachability'].includes(k)))
    if (stray.length) fail(`catalog skeleton row carries unexpected key(s): ${[...new Set(stray)].join(', ')} (skeleton is id/family/defect[+reachability] only)`)
    else pass(`skeleton stays minimal (id/family/defect[+reachability]); rich fields live in the prose (${need.join('/')})`)
    if (foot.length !== 3) fail(`expected 3 footnotes (N-01..N-03), found ${foot.length}`)
    else {
      const bad = foot.flatMap(f => (f.home || []).filter(h => !ids.includes(h)).map(h => `${f.id}->${h}`))
      if (bad.length) fail(`footnote home id(s) not in rows: ${bad.join(', ')}`)
      else pass('footnote homes all resolve to catalog rows')
      if (foot.every(f => f.backend === 'gsas')) pass('all 3 footnotes route to GSAS-II (manual engine cannot judge them)')
      else fail('a footnote does not declare backend=gsas')
    }
    // 骨架字段名必须与 SKILL.md §6.9 的 10 字段逐字一致
    const skill = existsSync(WFSKILL) ? readFileSync(WFSKILL, 'utf8') : ''
    const sec69 = skill.slice(skill.indexOf('### 6.9'), skill.indexOf('## 7.'))
    const declaredFields = (cat.per_sample_table?.fields) || []
    const missingInSkill = declaredFields.filter(f => !new RegExp('`' + f.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '`').test(sec69))
    if (!sec69) fail('xrd-workflow SKILL.md: §6.9 not found (per-sample checklist spec)')
    else if (declaredFields.length !== 10) fail(`per_sample_table.fields should be the 10-field checklist, found ${declaredFields.length}`)
    else if (missingInSkill.length) fail(`§6.9 does not name field(s) ${missingInSkill.join(', ')} (field names drifted from defect_catalog.json)`)
    else pass('§6.9 names all 10 per-sample fields exactly as defect_catalog.json declares')
    // §6 的引用块必须指到这两个 ref 文件，否则 AI 读不到底表
    for (const f of ['refs/defect-catalog.md', 'refs/defect_catalog.json']) {
      if (!skill.includes(f)) fail(`xrd-workflow SKILL.md §6 does not point at ${f}`)
    }
    if (skill.includes('refs/defect-catalog.md') && skill.includes('refs/defect_catalog.json')) {
      pass('§6 reference block points at both the prose and the skeleton')
    }
    // 每样品检查表：扫描树里真实存在的 defect_table.json，核对 id 合法性
    const tables = []
    const scan = d => {
      let ents = []
      try { ents = readdirSync(d, { withFileTypes: true }) } catch { return }
      for (const e of ents) {
        if (e.name === 'node_modules' || e.name === '.git' || e.name === '__pycache__') continue
        const p = join(d, e.name)
        if (e.isDirectory()) scan(p)
        else if (e.name === 'defect_table.json') tables.push(p)
      }
    }
    scan(ROOT)
    let rowCount = 0, illegal = []
    for (const t of tables) {
      try {
        const j = JSON.parse(readFileSync(t, 'utf8'))
        const list = Array.isArray(j) ? j : (j.rows || [])
        for (const r of list) {
          rowCount++
          const id = r.id || ''
          if (!ids.includes(id) && !/^X-\d{2}$/.test(id)) illegal.push(`${basename(t)}:${id}`)
        }
      } catch (e) { fail(`defect_table.json does not parse: ${t} (${e.message})`) }
    }
    if (illegal.length) fail(`per-sample defect_table id(s) outside the catalog (use X-nn for off-table hypotheses): ${illegal.join(', ')}`)
    else pass(`per-sample defect tables scanned: ${tables.length} file(s), ${rowCount} row(s), all ids legal`)
  }
}
if (!existsSync(CATMD)) fail('skills/xrd-workflow/refs/defect-catalog.md missing (appendix A prose)')
else {
  const md = readFileSync(CATMD, 'utf8')
  const missing = ['D-01', 'D-02', 'D-03', 'D-04', 'D-05', 'D-06', 'D-07', 'D-08',
                   'D-09', 'D-10', 'D-11', 'D-12', 'D-13', 'D-14'].filter(id => !md.includes(`### ${id} `))
  if (missing.length) fail(`defect-catalog.md lacks a section for: ${missing.join(', ')}`)
  else pass('defect-catalog.md documents all 14 classes (one section each)')
  for (const id of ['N-01', 'N-02', 'N-03']) {
    if (!md.includes(id)) fail(`defect-catalog.md does not mention footnote ${id}`)
  }
  if (['N-01', 'N-02', 'N-03'].every(id => md.includes(id))) pass('defect-catalog.md carries the 3 footnotes')
  if (!/reachability|可达性/.test(md)) fail('defect-catalog.md does not explain the reachability marker')
  else pass('defect-catalog.md explains the reachability (start-point) marker')
}

// ─────────────────────────────────────────────────────────────────── summary
console.log('\n== SUMMARY ==')
console.log(`  tools          : ${tools.length}`)
console.log(`  engines checked: ${declared.size}`)
console.log(`  FAIL           : ${report.fails.length}`)
console.log(`  WARN           : ${report.warns.length}`)
for (const i of info) console.log('  note           : ' + i)
if (report.fails.length) { console.log('\nCHAIN INTEGRITY: BROKEN'); process.exit(1) }
console.log('\nCHAIN INTEGRITY: OK' + (report.warns.length ? ' (with warnings)' : ''))
