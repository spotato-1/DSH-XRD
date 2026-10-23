// Definitive structural attribution: keep a known-good pcr (one fp2k can read) and
// replace ONLY the atom block with the structure build_orbit produced. If Rwp stays at
// the reference level, the CIF-derived structure is right and any earlier bad Rwp was
// purely a pcr-format/reader problem.
//
// NEEDS A REFERENCE PROJECT: the known-good pcr + its data are not shipped (they carry
// a measured pattern). Set DSH_XRD_Y2O3_REF=<dir> containing Y2O3.pcr and Y2O3.dat.
// Without it this script SKIPs — an absent input is not a plugin defect.
//
// run verify-y2o3.mjs first: it writes .work/y2o3/Y2O3_orbit.pcr which this consumes.
import { mkdirSync, readFileSync, writeFileSync, copyFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { importRoot, pluginEntry, mockContext, WORK_ROOT, report } from './_lib.mjs'

const WORK = join(WORK_ROOT, 'structcheck')
const REFDIR = process.env.DSH_XRD_Y2O3_REF && existsSync(process.env.DSH_XRD_Y2O3_REF)
  ? process.env.DSH_XRD_Y2O3_REF
  : null
const OURPCR = join(WORK_ROOT, 'y2o3', 'Y2O3_orbit.pcr')

if (!REFDIR || !existsSync(join(REFDIR, 'Y2O3.pcr'))) {
  report.skip('the structural comparison: no reference project (set DSH_XRD_Y2O3_REF=<dir '
    + 'containing Y2O3.pcr and Y2O3.dat>)')
  process.exit(report.finish('skipped — nothing to compare against'))
}
if (!existsSync(OURPCR)) {
  report.skip(`the structural comparison: ${OURPCR} is missing — run verify-y2o3.mjs first`)
  process.exit(report.finish('skipped — run verify-y2o3.mjs first'))
}
mkdirSync(WORK, { recursive: true })
console.log('tool layer under test:', pluginEntry(importRoot()))

const tools = []
const ctx = mockContext(tools)
const mod = await import(pathToFileURL(pluginEntry(importRoot())).href)
await (mod.apply ?? mod.default)(ctx)
const refine = tools.find(t => t.name === 'xrd_refine_auto')
const exec = { agent: { session: { header: { cwd: WORK } } } }

const refLines = readFileSync(join(REFDIR, 'Y2O3.pcr'), 'utf8').replace(/\r\n/g, '\n').split('\n')
const ourLines = readFileSync(OURPCR, 'utf8').replace(/\r\n/g, '\n').split('\n')

// atoms from OUR pcr: lines starting with the label at column 0
const ourAtoms = ourLines.filter(l => /^[A-Z][A-Za-z0-9]*\s+[A-Z][a-z]?\s+[\d.]/.test(l))
console.log('our atom rows:', ourAtoms.length)
for (const r of ourAtoms) console.log('   |' + r + '|')

// the reference phase block: !Nat line, then atoms; each atom has 1 main line
// followed by 2 or 4 continuation lines. Locate them and rebuild the block.
const natIdx = refLines.findIndex(l => /^!Nat Dis Ang/.test(l))
console.log('ref !Nat line', natIdx, ':', refLines[natIdx])
console.log('ref counts  ', natIdx + 1, ':', refLines[natIdx + 1])
const atomHdr = refLines.findIndex(l => /^!Atom/.test(l))
const profIdx = refLines.findIndex((l, i) => i > atomHdr && /Profile Parameters/.test(l))
console.log(`atom header line ${atomHdr} | phase end ${profIdx}`)
const nat = Number(refLines[natIdx + 1].trim().split(/\s+/)[0])
// each atom occupies 4 lines: main / zeros / beta / zeros
const BLOCK = 4
console.log(`ref Nat=${nat}, ${BLOCK} lines per atom, phase block ${atomHdr + 2}..${profIdx - 1}`)

const patched = refLines.slice()
let p = atomHdr + 2
for (let a = 0; a < nat; a++) {
  const ourRow = ourAtoms[a]
  const refMain = refLines[p]
  const m = ourRow.trim().match(/^(\S+)\s+(\S+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)/)
  const label = refMain.slice(0, 7)
  const typ = refMain.slice(7, 11)
  // rebuild the main line with OUR coordinates and occupancy, keeping the
  // reference's Biso and the trailing flag columns
  patched[p] = `${label}${typ}${m[3].padStart(10)}${m[4].padStart(9)}${m[5].padStart(9)}` +
               `${refMain.slice(36, 45)}${m[7].padStart(10)}${refMain.slice(55)}`
  console.log(`   atom ${a + 1}: ${patched[p].trim()}`)
  p += BLOCK
}

const dir = join(WORK, 'struct')
mkdirSync(dir, { recursive: true })
writeFileSync(join(dir, 'struct.pcr'), patched.join('\n'))
copyFileSync(join(REFDIR, 'Y2O3.dat'), join(dir, 'struct.dat'))
const r = await refine.execute({ pcr: join(dir, 'struct.pcr'), out_dir: dir, cycles: 3 }, exec)
console.log(`\nRESULT: tool Rwp=${r.rwp} | ok=${r.ok} | err=${r.error || '-'}`)
console.log('ladder:', JSON.stringify(r.ladder || {}).slice(0, 300))
console.log('EXPECTED: Rwp at the reference level if the build_orbit structure is right')
process.exit(r.ok ? 0 : 1)
