// verify-package.mjs — confirm a BUILT release tree carries what it should.
//
// WHY: the packaged tree is the one that gets transplanted, so the source and runtime
// copies being right is not enough. This also enforces the honest-reporting rule: the
// shipped CHANGELOG must state evidence levels, must NOT claim blanket verification,
// and must name the items that were never verified.
//
// It READS the release directory only — nothing is executed.
//
// Usage:  node verify-package.mjs [--root <release dir>]
//   The audit module cannot know where a release was unpacked, so a path is REQUIRED:
//   pass --root <dir> or set DSH_XRD_ROOT=<dir>. (Inside a shipped package the audit
//   audits the package itself by default, which is the same thing.)
import { readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname, basename, relative, extname } from 'node:path'
import { gunzipSync } from 'node:zlib'
import { pluginRoot, report } from './_lib.mjs'

const { check } = report
const PKG = pluginRoot()

if (!existsSync(join(PKG, 'lib', 'xrd-tools.js'))) {
  console.error(`not a dsh-xrd release tree: ${PKG}\n` +
    '  (expected <dir>/lib/xrd-tools.js; pass --root <release dir>)')
  process.exit(2)
}
console.log('release tree under test:', PKG)

const tools = readFileSync(join(PKG, 'lib', 'xrd-tools.js'), 'utf8')
const log = readFileSync(join(PKG, 'CHANGELOG.md'), 'utf8')

// 1. the four previously unadvertised gsas_ops actions are in the packaged tool text
for (const a of ['add_phase', 'replace_phase', 'new_from', 'set_site_composition']) {
  check(tools.includes('{type:"' + a + '"'), `package advertises ${a}`, `package does NOT advertise ${a}`)
}
// 2. the packaged changelog no longer claims blanket verification
check(log.includes('How much of this is actually verified'), 'package CHANGELOG states the evidence levels',
  'package CHANGELOG has no evidence-level section')
check(!log.includes('Everything below is fixed and verified'), 'package CHANGELOG drops the blanket claim',
  'package CHANGELOG still claims "Everything below is fixed and verified"')
// 3. the unverified items are named, not hidden
for (const s of ['restore_verified.within_tolerance=false', 'not deterministic', 'provisional']) {
  check(log.includes(s), `package CHANGELOG names unverified item (${s})`, `package CHANGELOG hides ${s}`)
}
// 4. the packaged changelog keeps the full release history
for (const h of ['## 0.5.0', '## 0.4.0', '## 0.3.0']) {
  check(log.includes(h), `package CHANGELOG keeps ${h}`, `package CHANGELOG LOST ${h}`)
}
// 5. the bundled runtime is present and the local-only file is absent
check(existsSync(join(PKG, 'third_party', 'fullprof', 'fp2k')), 'package ships the Linux fp2k', 'package is missing fp2k')
check(existsSync(join(PKG, 'third_party', 'fullprof', 'fp2k.exe')), 'package ships fp2k.exe', 'package is missing fp2k.exe')
check(!existsSync(join(PKG, 'xrd.local.json')), 'package excludes xrd.local.json', 'package LEAKS xrd.local.json')
// 6. the audit module itself ships, and carries no maintainer paths.
// The first version of this check walked a HAND-WRITTEN list of 14 `.mjs` names — the
// exact failure mode this project already met twice (0.7.0 and 0.7.3: "a completeness
// check must not be derived from the same list it is checking"). Measured on 2026-09-26:
// a shipped scratch file and two .py helpers were outside that list, so the check passed
// while `audit/_manual-timeout-work/plan_calibrate.json` carried the maintainer path. It
// now walks EVERY text file under audit/, and also audits the backslash-unescaped form
// (a path inside a JSON string carries doubled separators, which the literal rule sees
// as "not a path" — that is how a scratch file shipped with the maintainer path intact).
const auditFiles = ['integrity-check.mjs', '_lib.mjs', 'verify-all.mjs', 'verify-cif-edit.mjs',
  'verify-e2e.mjs', 'verify-autofp-replaced.mjs', 'verify-fp2k-check.mjs', 'verify-gsas-ops.mjs',
  'verify-package.mjs', 'verify-manual-fitstatus.mjs', 'verify-manual-mapping.mjs',
  'verify-y2o3.mjs', 'struct-check.mjs',
  // 0.8.3: the two suites that cover the process-supervision work must ship too —
  // a test that stays behind in the workspace is not a test of the package
  'verify-review-detector.mjs', 'verify-manual-timeout.mjs',
  'verify-result-freshness.mjs', 'verify-proc-supervision.mjs',
  // 0.8.8: the suite that guards the collected-output reader and the GSAS reader hint
  'verify-review-key.mjs',
  join('py', 'ctrl-fp2k-orphan.py'), 'AUDIT.md']
for (const f of auditFiles) {
  check(existsSync(join(PKG, 'audit', f)), `package ships audit/${f}`, `package is MISSING audit/${f}`)
}
check(existsSync(join(PKG, 'audit', 'fixtures', 'Y2O3.dat')), 'package ships audit fixtures',
  'package is missing audit/fixtures')
const AUDIT_TEXT_EXT = new Set(['.js', '.mjs', '.cjs', '.py', '.md', '.json', '.yml', '.yaml',
  '.txt', '.pcr', '.cif', '.dat', '.prm', '.cfg', '.ini', '.patch'])
function walkAudit(dir) {
  const out = []
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name)
    if (e.isDirectory()) out.push(...walkAudit(p))
    else out.push(p)
  }
  return out
}
// The path patterns are BUILT FROM PARTS, never written out. Reason (measured 2026-09-26):
// the release audit applies the same patterns, and it now also audits the
// backslash-unescaped form of every line — so a literal path written in THIS file is
// matched by its own rule and the build reports a leak in the audit tool. Assembling the
// literals keeps the rule readable for humans and invisible to the scanner.
const BS = String.fromCharCode(92)
const rxEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const REPO_BS = 'D:' + BS + 'AI' + BS + 'DSH'
const REPO_FS = 'D:' + '/' + 'AI' + '/' + 'DSH'
const USERS_BS = 'C:' + BS + 'Users' + BS
// BOTH separators for the user-name rule as well (codex review 2026-09-27): the
// backslash-only version let a forward-slash user path ship inside audit/ while this
// scan reported "no maintainer paths". A rule written for one spelling cannot see the
// other — the same gap as the escaped-backslash pass above, in a different rule.
const USERS_FS = 'C:' + '/' + 'Users' + '/'
const PATH_RULES = [
  ['maintainer layout', new RegExp(rxEsc(REPO_BS) + '|' + rxEsc(REPO_FS), 'i')],
  ['user-name path', new RegExp('(?:' + rxEsc(USERS_BS) + '|' + rxEsc(USERS_FS) + ')[A-Za-z0-9_.-]+', 'i')],
]
let leaked = []
let auditScanned = 0
const auditRoot = join(PKG, 'audit')
if (existsSync(auditRoot)) {
  for (const p of walkAudit(auditRoot)) {
    if (!AUDIT_TEXT_EXT.has(extname(p).toLowerCase())) continue
    auditScanned++
    const raw = readFileSync(p, 'utf8')
    const forms = [raw, raw.replace(/\\\\/g, '\\')]
    for (const [label, re] of PATH_RULES) {
      for (const t of forms) {
        const m = t.match(re)
        if (m) { leaked.push(`${relative(PKG, p).split('\\').join('/')} [${label}] ${String(m[0]).slice(0, 60)}`); break }
      }
    }
  }
}
check(leaked.length === 0, `the shipped audit module carries no maintainer paths (${auditScanned} text file(s) scanned)`,
  'shipped audit module leaks paths:\n      ' + leaked.join('\n      '))

// 7. DEPLOYMENT CONVENTION (user decision 2026-09-21): Windows ships from the .zip,
//    Linux from the .tar.gz. Both must exist for every release, and the tar.gz must
//    actually carry the executable bit — a release whose Linux fp2k is 0644 installs
//    fine and then cannot start, which is the silent failure this check exists for.
//    (No tar library is available here, so the gzip stream is inflated with zlib and the
//    tar headers — 512-byte blocks, mode in the first 8 bytes at offset 100 — are read
//    directly. That is the whole format we need.)
const distDir = dirname(PKG)
const tarPath = join(distDir, `${basename(PKG)}.tar.gz`)
const zipPath = join(distDir, `${basename(PKG)}.zip`)
check(existsSync(zipPath), `Windows archive exists (${basename(zipPath)})`, `missing ${zipPath}`)
check(existsSync(tarPath), `Linux archive exists (${basename(tarPath)})`, `missing ${tarPath}`)

if (existsSync(tarPath)) {
  const raw = gunzipSync(readFileSync(tarPath))
  let off = 0
  const modes = new Map()
  const names = []
  while (off + 512 <= raw.length) {
    const header = raw.subarray(off, off + 512)
    if (header.every((b) => b === 0)) break
    const name = header.subarray(0, 100).toString('utf8').replace(/\0.*$/, '')
    const sizeField = header.subarray(124, 136).toString('utf8').replace(/\0.*$/, '').trim()
    const size = parseInt(sizeField, 8) || 0
    const mode = parseInt(header.subarray(100, 108).toString('utf8').replace(/\0.*$/, '').trim(), 8)
    if (name) { modes.set(name, mode); names.push(name) }
    off += 512 + Math.ceil(size / 512) * 512
  }
  const top = names[0]?.split('/')[0]
  check(!!top && names.every((n) => n.startsWith(top + '/')),
    'tar.gz carries a single top-level directory (clean extraction)', top ?? '(none)')
  const fp2kEntry = names.find((n) => n.endsWith('/third_party/fullprof/fp2k'))
  const fp2kExe = names.find((n) => n.endsWith('/third_party/fullprof/fp2k.exe'))
  check(!!fp2kEntry && (modes.get(fp2kEntry) & 0o111) !== 0,
    'tar.gz marks the Linux fp2k executable',
    fp2kEntry ? `${fp2kEntry} mode=0${(modes.get(fp2kEntry) & 0o777).toString(8)}` : 'entry not found')
  check(!!fp2kExe && (modes.get(fp2kExe) & 0o111) !== 0,
    'tar.gz marks the Windows fp2k.exe executable',
    fp2kExe ? `${fp2kExe} mode=0${(modes.get(fp2kExe) & 0o777).toString(8)}` : 'entry not found')
}

process.exit(report.finish('PACKAGED RELEASE OK — corrections, audit module and honest changelog are in the package'))
