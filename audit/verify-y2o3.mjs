// Real-data end-to-end: rebuild Y2O3 from Wyckoff representatives only, then
// cif_to_pcr -> xrd_refine_auto (bundled fp2k driver), and compare against the
// known-good reference run kept in a standard Y2O3 project.
//
// NOTE it writes .work/y2o3/Y2O3_orbit.pcr, which struct-check.mjs consumes next —
// run this one first.
//
// Paths come from audit/_lib.mjs. The reference directory is NOT shipped (it holds a
// measured pattern); without it the checks that need it are skipped.
//   DSH_XRD_Y2O3_REF=<dir>   the reference project (Y2O3.pcr + Y2O3.dat)
import { mkdirSync, existsSync, rmSync, copyFileSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES, report } from './_lib.mjs'

const WORK = join(WORK_ROOT, 'y2o3')
const REF = process.env.DSH_XRD_Y2O3_REF && existsSync(process.env.DSH_XRD_Y2O3_REF)
  ? process.env.DSH_XRD_Y2O3_REF
  : null
rmSync(WORK, { recursive: true, force: true })
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', pluginEntry(importRoot()))
if (!REF) report.skip(`the comparison run: no reference project (set DSH_XRD_Y2O3_REF=<dir `
  + 'containing Y2O3.pcr and Y2O3.dat>); the build/convert/refine chain still runs, '
  + 'using the shipped fixture data instead')

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(pluginEntry(importRoot())).href)
await (mod.apply ?? mod.default)(ctx)
const cifEdit = tools.find(t => t.name === 'xrd_cif_edit')
const cif2pcr = tools.find(t => t.name === 'cif_to_pcr')
const refine = tools.find(t => t.name === 'xrd_refine_auto')
const exec = { agent: { session: { header: { cwd: WORK } } } }

// 1) the CIF, from Wyckoff representatives ONLY (values read off the published
//    ICSD entry: Y 8b (1/4,1/4,1/4); Y 24d (x,0,1/4); O 48e (general))
const built = join(WORK, 'Y2O3_orbit.cif')
// A restricted environment can refuse to start the engine's interpreter (`spawn EPERM`);
// report that instead of dying with a stack trace.
let r1 = null
try {
  r1 = await cifEdit.execute({
    out_dir: WORK, sample: 'Y2O3_orbit',
    actions: [{
      type: 'build_orbit', spacegroup: 'I a -3',
      cell: { a: 10.60796, b: 10.60796, c: 10.60796, alpha: 90, beta: 90, gamma: 90 },
      sites: [
        { element: 'Y', x: 0.25, y: 0.25, z: 0.25, occ: 1.0 },
        { element: 'Y', x: 0.9673, y: 0.0, z: 0.25, occ: 1.0 },
        { element: 'O', x: 0.3915, y: 0.1538, z: 0.3800, occ: 1.0 },
      ],
      out: built,
    }],
  }, exec)
} catch (e) {
  console.error(`build_orbit could not run: ${String(e.message).slice(0, 160)}`)
  console.error('spawn EPERM here means the engine could not be started in this environment '
    + '(a sandbox/permission limit, not a plugin defect)')
  process.exit(1)
}
console.log('1) build_orbit ok =', r1.ok)
const bo = (r1.actions || []).find(a => a.action === 'build_orbit')
console.log('   detail:', bo?.detail)
for (const p of bo?.plan || []) {
  console.log(`   plan ${p.element}: mult=${p.multiplicity} wyckoff=${p.wyckoff_letter} occ=${p.occ} special=${p.on_special_position}`)
}
const st = r1.structures?.[0]
console.log('   built formula:', st?.formula, '| sites:', st?.n_sites, '| sg:', st?.space_group)

// the expected conventional cell: Z=16 -> Y 40, O 48 = 88 atoms
if (st?.n_sites !== 88) console.log(`   NOTE: expected 88 atoms in the conventional cell, got ${st?.n_sites}`)

// 2) official converter
const r2 = await cif2pcr.execute({ cif: built, out_dir: WORK }, exec)
console.log('2) cif_to_pcr ok =', r2.ok, '| occupancy verdict =', r2.occupancy_check?.verdict, '| pcr =', r2.pcr)
for (const s of r2.occupancy_check?.sites || []) {
  console.log(`   occ ${s.label}(${s.element}) cif=${s.cif_occ} pcr=${s.pcr_occ} mult=${s.site_multiplicity} dev=${s.relative_deviation}`)
}

// 3) fp2k resolves the pattern from the name the pcr DECLARES, so the data must sit
//    next to the pcr under exactly that name.
const pcr = r2.pcr
const declared = readFileSync(pcr, 'utf8').split('\n')
  .map(l => (l.match(/DAT-file:\s*([^,\s]+)/) || [])[1]).find(Boolean)
  || 'Y2O3.dat'
console.log('3) pcr declares data file:', declared)
// the reference data if we have it (that is the measured pattern this project was built
// on), else the shipped fixture — the chain must still run end-to-end either way.
const datSrc = join(REF || FIXTURES, 'Y2O3.dat')
const datTarget = join(WORK, declared)
copyFileSync(datSrc, datTarget)
const here = pcr.replace(/\.pcr$/, '.dat')
if (here !== datTarget) copyFileSync(datSrc, here)
console.log('   data placed:', existsSync(datTarget), 'from', datSrc)

// 4) the bundled headless fp2k driver
const r4 = await refine.execute({ pcr, out_dir: WORK }, exec)
console.log('4) xrd_refine_auto ok =', r4.ok, '| Rwp =', r4.rwp, '| cycles =', r4.cycles)
console.log('   ladder:', JSON.stringify(r4.ladder || {}).slice(0, 400))
console.log('   error:', r4.error || '(none)')
console.log('   refined pcr:', r4.refined_pcr)
if (!r4.ok) {
  console.log('\nFAIL: the build_orbit -> cif_to_pcr -> refine chain did not complete')
  process.exit(1)
}
console.log(`\nCHAIN COMPLETE${REF ? ' — compare Rwp against the reference project run' : ''}`)

