// REAL tool-call verification for the new xrd_cif_edit actions.
//
// Mirrors the harness pattern used for the other xrd verification scripts: import the
// plugin entry, apply() with a minimal mock ctx, grab the registered tool, execute().
//
// Paths come from audit/_lib.mjs: by default it exercises the tree it ships in, so in
// a transplanted package this tests the package itself. Redirect with
//   DSH_XRD_PROFILE=<.../node_modules/dsh-xrd>   (the copy dsh actually loads), or
//   DSH_XRD_ROOT=<plugin root> / --root <dir>
import { mkdirSync, writeFileSync, existsSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, report } from './_lib.mjs'

const ENTRY = pluginEntry(importRoot())
const WORK = join(WORK_ROOT, 'cif_edit')

rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', ENTRY)

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(ENTRY).href)
const apply = mod.apply ?? mod.default
await apply(ctx)

const tool = tools.find(t => t.name === 'xrd_cif_edit')
if (!tool) throw new Error('xrd_cif_edit not registered (tools: ' + tools.map(t => t.name).join(',') + ')')
console.log('registered tools:', tools.length, '| xrd_cif_edit found')

const exec = { agent: { session: { header: { cwd: WORK } } } }
const built = join(WORK, 'probe_orbit.cif')
const bad = join(WORK, 'probe_bad.cif')
const good = join(WORK, 'probe_good.cif')

writeFileSync(bad, `data_bad
_symmetry_space_group_name_H-M   'R -3 m'
_cell_length_a   2.8700
_cell_length_b   2.8700
_cell_length_c   14.3000
_cell_angle_alpha  90.0
_cell_angle_beta   90.0
_cell_angle_gamma  120.0
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
Na1 Na 0.0000 0.0000 0.0000 1.0
O1  O  0.0400 0.0000 0.2300 1.0
`)
writeFileSync(good, `data_good
_symmetry_space_group_name_H-M   'R -3 m'
_cell_length_a   2.8700
_cell_length_b   2.8700
_cell_length_c   14.3000
_cell_angle_alpha  90.0
_cell_angle_beta   90.0
_cell_angle_gamma  120.0
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
Na1 Na 0.0000 0.0000 0.0000 1.0
O1  O  0.0000 0.0000 0.2300 1.0
`)

// A restricted environment can refuse to start the engine's interpreter at all
// (`spawn EPERM`). In that case there is nothing to assert, so report it as an unrunnable
// check rather than letting an exception kill the script with a stack trace.
let value = null
try {
  value = await tool.execute({
    out_dir: WORK,
    sample: 'probe',
    actions: [
      { type: 'build_orbit', spacegroup: 'R -3 m',
        cell: { a: 2.87, b: 2.87, c: 14.3, alpha: 90, beta: 90, gamma: 120 },
        sites: [
          { element: 'K', x: 0, y: 0, z: 0, occ: 0.8 },
          { element: 'Mn', x: 0, y: 0, z: 0.5, occ: 1.0 },
          { element: 'O', x: 0, y: 0, z: 0.23, occ: 1.0 },
        ], out: built },
      { type: 'validate', cif: built },
      { type: 'validate', cif: bad },
      { type: 'validate', cif: good },
    ],
  }, exec)
} catch (e) {
  report.fail(`xrd_cif_edit could not run: ${String(e.message).slice(0, 160)}`
    + ' — spawn EPERM here means the engine could not be started in this environment '
    + '(a sandbox/permission limit, not a plugin defect)')
}
if (!value) process.exit(report.finish('NOT RUN — the engine could not be started here'))

// ---- the tool result is validated against output.schema: an UNDECLARED field
// would reject the ENTIRE result, so assert the new field survived.
console.log('ok =', value.ok)
console.log('has validations in result:', Array.isArray(value.validations), '->', (value.validations || []).length)

const bo = (value.actions || []).find(a => a.action === 'build_orbit')
console.log('build_orbit detail:', bo?.detail)
console.log('build_orbit plan:', JSON.stringify((bo?.plan || []).map(p => [p.element, p.multiplicity, p.wyckoff_letter, p.occ])))

const vs = value.validations || []
const byName = Object.fromEntries(vs.map(v => [v.file.split(/[\\/]/).pop(), v]))
for (const [n, v] of Object.entries(byName)) console.log(`  ${n}: ${v.verdict} | minContact=${v.checks?.expanded_cell?.min_contact_A} | warnings=${v.warnings.length}`)

// ---- assertions
const fails = report.fails
if (!value.ok) fails.push('result not ok')
if (!existsSync(built)) fails.push('built CIF missing')
if (byName['probe_orbit.cif']?.verdict !== 'pass') fails.push('br> built CIF did not validate as pass')
if (byName['probe_good.cif']?.verdict !== 'pass') fails.push('correct hand-written CIF did not pass')
if (byName['probe_bad.cif']?.verdict !== 'warn') fails.push('broken CIF was not flagged')
const plan = bo?.plan || []
// plan entries are OBJECTS: {element, input, occ, multiplicity, wyckoff_letter, ...}
const letterOf = el => plan.find(p => p.element === el)?.wyckoff_letter
if (letterOf('K') !== '3a') fails.push('K not labelled 3a (got ' + letterOf('K') + ')')
if (letterOf('Mn') !== '3b') fails.push('Mn not labelled 3b (got ' + letterOf('Mn') + ')')
if (letterOf('O') !== '6c') fails.push('O not labelled 6c (got ' + letterOf('O') + ')')

// ---- render must not throw
const rendered = tool.output.render({}, value)
console.log('--- rendered ---')
console.log(rendered[0].text)

console.log(fails.length ? 'FAIL: ' + fails.join(' | ') : 'ALL ASSERTIONS PASSED')
process.exit(report.finish('ALL ASSERTIONS PASSED — xrd_cif_edit actions behave'))
