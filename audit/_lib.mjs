// audit/_lib.mjs — shared path resolution for the dsh-xrd audit module.
//
// WHY THIS FILE EXISTS
// The audit scripts started life in the maintainer's workspace and hardcoded absolute
// drive paths everywhere (a top-level work directory, and per-user profile paths under
// the Windows Users tree). That made them useless in the package for two reasons: they
// could not run on another machine, and shipping them as-is would have leaked the
// maintainer's directory layout into the release.
//
// So every path is DERIVED, never typed in:
//   * the plugin under audit defaults to THIS module's parent directory — i.e. the
//     package audits itself, which is the normal case in a transplant;
//   * anything that genuinely varies per machine (the dsh profile, the Python
//     interpreter, the GSAS-II root) is resolved from environment variables first,
//     then probed, and only THEN falls back to a clear error telling you what to set.
//
// Nothing here is allowed to contain a machine-specific literal.
import { existsSync, readFileSync, statSync, rmSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve, basename } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

export const AUDIT_DIR = dirname(fileURLToPath(import.meta.url))
// This module ships INSIDE the package as <pkg>/audit/_lib.mjs, but it is DEVELOPED
// at <workspace>/xrd-plugin-checks/. So the parent directory is the plugin only when
// it actually looks like one; otherwise the audit must be told where to look.
export const PKG_ROOT = basename(dirname(AUDIT_DIR)) === 'audit' || looksLikePlugin(dirname(AUDIT_DIR))
  ? resolve(AUDIT_DIR, '..')
  : null
export const FIXTURES = join(AUDIT_DIR, 'fixtures')

/**
 * Scratch directory.
 *
 * A package can legitimately live somewhere the running process may not write — a
 * read-only mount, Program Files, or (verified 2026-09-21) a file sandbox that permits
 * writes only inside a workspace. There the audit used to die with
 * `EPERM: operation not permitted, mkdir ...\audit\.work\cif_edit`, i.e. it became
 * unusable exactly where a self-check matters most.
 *
 * So probe a list of candidates in order and take the first one that actually accepts a
 * write. IMPORTANT: a single fallback is not enough — measured on this machine, a
 * workspace-write sandbox denies BOTH `<audit>/.work` (when the tree is outside the
 * workspace) AND the OS temp dir, so the first version of this fix failed straight back
 * to the broken path. Each candidate is probed by creating and deleting a real child.
 */
function resolveWorkRoot() {
  const candidates = [
    join(AUDIT_DIR, '.work'),          // preferred: next to the audit itself
    PKG_ROOT ? join(PKG_ROOT, '.audit-work') : null,
    join(process.cwd(), '.xrd-audit-work'),
    join(tmpdir(), 'dsh-xrd-audit'),
  ].filter(Boolean)

  const tried = []
  for (const c of candidates) {
    try {
      const probe = join(c, 'probe')
      mkdirSync(probe, { recursive: true })
      rmSync(probe, { recursive: true, force: true })
      if (c !== candidates[0]) console.log(`note: ${candidates[0]} is not writable — using ${c} for scratch output`)
      return c
    } catch (e) {
      tried.push(`${c} (${e.code || e.message})`)
    }
  }
  // Nothing is writable. Say exactly what was tried instead of failing later with a
  // confusing EPERM from deep inside a check.
  console.error('no writable scratch directory for the audit. Tried:\n  ' + tried.join('\n  ')
    + '\n  Set TMPDIR/TEMP to a writable path, or run the audit from a writable directory.')
  process.exit(2)
}
export const WORK_ROOT = resolveWorkRoot()

function looksLikePlugin(dir) {
  return existsSync(join(dir, 'lib', 'index.js')) && existsSync(join(dir, 'package.json'))
}

/** `--root <dir>` anywhere on the command line, if present. */
export function rootFlag() {
  const i = process.argv.indexOf('--root')
  if (i < 0) return null
  const v = process.argv[i + 1]
  if (!v || v.startsWith('--')) throw new Error('--root needs a directory argument')
  return resolve(v)
}

/** The plugin tree under audit: --root, then $DSH_XRD_ROOT, then self-audit. */
export function pluginRoot() {
  const flag = rootFlag()
  if (flag) return flag
  const e = env('DSH_XRD_ROOT')
  if (e) return e
  if (PKG_ROOT) return PKG_ROOT
  throw new Error('cannot tell which plugin tree to audit: this audit copy lives in a '
    + `workspace (${AUDIT_DIR}), so pass --root <dir> or set DSH_XRD_ROOT=<plugin root>`)
}

/** A dsh profile's node_modules/dsh-xrd, for auditing the RUNTIME copy. */
export function profileRoot() {
  return env('DSH_XRD_PROFILE') || null
}

/**
 * Where the tool layer gets imported from. Prefers an explicit --root / $DSH_XRD_ROOT
 * (that is what you want when auditing a dev tree or an extracted package), then a
 * named profile (the copy node actually loads), else the tree this module lives in.
 */
export function importRoot() {
  const flag = rootFlag()
  if (flag) return flag
  return profileRoot() || pluginRoot()
}

/** entry point of the plugin's tool registry */
export function pluginEntry(root = importRoot()) {
  return join(root, 'lib', 'index.js')
}

function env(name) {
  const v = process.env[name]
  return v && v.trim() ? resolve(v.trim()) : null
}

/**
 * Python interpreter. Order: $DSH_XRD_PYTHON, then conventional per-user/system
 * install locations, then `python3`/`python` on PATH.
 *
 * Two environment traps this deliberately avoids:
 *   1. `where python` on this machine resolves ONLY to the Microsoft Store stub under
 *      WindowsApps, which is not a real interpreter — so the PATH probe is a last
 *      resort, not the first choice;
 *   2. capturing a child's output via `stdio:'pipe'` is DENIED by the file sandbox
 *      (named pipe), so every probe here either uses existsSync or runs the candidate
 *      with `stdio:'ignore'` — never a pipe.
 */
let _py = null
export function python() {
  if (_py) return _py
  const explicit = env('DSH_XRD_PYTHON')
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`DSH_XRD_PYTHON points at nothing: ${explicit}`)
    return (_py = explicit)
  }
  const candidates = []
  const home = process.env.LOCALAPPDATA
  if (home) {
    for (const v of ['Python314', 'Python313', 'Python312', 'Python311', 'Python310']) {
      candidates.push(join(home, 'Programs', 'Python', v, 'python.exe'))
    }
  }
  candidates.push('C:\\Python313\\python.exe', 'C:\\Python312\\python.exe')
  if (process.platform !== 'win32') candidates.push('/usr/bin/python3', '/usr/local/bin/python3')
  for (const p of ['python3', 'python']) candidates.push(p)   // PATH — last resort

  for (const c of candidates) {
    // a bare command name must be looked up on PATH; a path must exist
    const isPath = /[\\/]/.test(c)
    if (isPath && !existsSync(c)) continue
    if (!isPath && existsSync(c)) continue
    try {
      // stdio:'ignore' — no pipe, so the sandbox allows it; a non-zero exit throws
      execFileSync(c, ['-c', 'import sys; assert sys.version_info[0] == 3'],
        { stdio: 'ignore' })
      return (_py = c)
    } catch { /* not usable — next candidate */ }
  }
  throw new Error('no Python 3 interpreter found: set DSH_XRD_PYTHON=<python.exe> '
    + '(checked %LOCALAPPDATA%\\Programs\\Python\\Python3xx\\python.exe, C:\\Python3xx, PATH)')
}

/**
 * GSAS-II install root: $DSH_GSAS_ROOT, else xrd.local.json. Returns null when unknown —
 * use this from a script that can SKIP, so a machine without GSAS-II does not crash.
 */
export function tryGsasRoot() {
  const e = env('DSH_GSAS_ROOT')
  if (e) return e
  const cfg = join(pluginRoot(), 'xrd.local.json')
  if (existsSync(cfg)) {
    try {
      const j = JSON.parse(readFileSync(cfg, 'utf8'))
      if (j.gsas_root) return resolve(j.gsas_root)
    } catch { /* fall through */ }
  }
  return null
}

/** GSAS-II install root, or a clear error naming both ways to set it. */
export function gsasRoot() {
  const r = tryGsasRoot()
  if (r) return r
  throw new Error('GSAS-II root unknown: set DSH_GSAS_ROOT, or put "gsas_root" in '
    + join(pluginRoot(), 'xrd.local.json'))
}

/** fp2k root (only needed by scripts that call fp2k outside the tool layer). */
export function fp2kRoot() {
  const e = env('DSH_FP2K_ROOT')
  if (e) return e
  const cfg = join(pluginRoot(), 'xrd.local.json')
  if (existsSync(cfg)) {
    try {
      const j = JSON.parse(readFileSync(cfg, 'utf8'))
      if (j.fp2k_root) return resolve(j.fp2k_root)
    } catch { /* fall through */ }
  }
  return null
}

/**
 * The official FullProf CIFs_to_PCR.exe. A PUBLIC default install path (it is where
 * the FullProfApp suite puts it) — not a machine-specific secret.
 */
export function cif2Pcr() {
  const e = env('DSH_FULLPROF_CIF2PCR')
  if (e) return e
  return 'C:\\Program Files (x86)\\FullProfAPP\\resources\\fpsuite\\windows\\CIFs_to_PCR.exe'
}

// ------------------------------------------------------------------ reporting
const fails = []
const warns = []
const skips = []

// Defined as free functions and only COLLECTED into `report`, so destructuring
// (`const { check } = report`) cannot lose `this` — an earlier version used `this.pass`
// and threw "Cannot read properties of undefined (reading 'pass')" the moment a caller
// destructured a method.
function pass(m) { console.log('  PASS  ' + m) }
function fail(m) { fails.push(m); console.log('  FAIL  ' + m) }
function warn(m) { warns.push(m); console.log('  WARN  ' + m) }
/**
 * A dependency this machine does not have. NOT a failure: an absent third-party
 * program is an environment fact, not a defect in the plugin. Reported separately so
 * "could not run" is never mistaken for "passed".
 */
function skip(m) { skips.push(m); console.log('  SKIP  ' + m) }
function check(cond, good, bad) { if (cond) pass(good); else fail(bad) }
/**
 * Exit code + final line; returns the code so callers can `process.exit(finish(...))`.
 * Pass `{ alreadyMentionsSkips: true }` when the message itself explains the skips, so
 * the suffix does not say the same thing twice.
 */
function finish(okMessage, { alreadyMentionsSkips = false } = {}) {
  console.log('')
  console.log(`  FAIL ${fails.length} | WARN ${warns.length} | SKIP ${skips.length}`)
  if (skips.length) for (const s of skips) console.log('  not run: ' + s)
  if (fails.length) {
    console.log('\nFAILED (' + fails.length + '):')
    for (const f of fails) console.log('  - ' + f)
    return 1
  }
  const suffix = skips.length && !alreadyMentionsSkips ? ' (with skipped checks — see above)' : ''
  console.log('\n' + okMessage + suffix)
  return 0
}

export const report = {
  pass, fail, warn, skip, check, finish,
  get fails() { return fails },
  get warns() { return warns },
  get skips() { return skips },
}

// ------------------------------------------------------------------ small utils
/** minimal host context so a plugin can be assembled outside dsh */
export function mockContext(sink = []) {
  return {
    tools: { register: t => sink.push(t), guard: () => {} },
    inject() {}, on() {}, get: () => undefined,
    logger: { info() {}, warn() {}, error() {}, debug() {} },
  }
}

export function freshDir(dir) {
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  return dir
}

/** the exec object the tools expect (they only read session.header.cwd) */
export function execIn(cwd) {
  return { agent: { session: { header: { cwd } } } }
}

export function isDir(p) {
  try { return statSync(p).isDirectory() } catch { return false }
}

export { basename, join, resolve, dirname, existsSync }
