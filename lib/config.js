/**
 * Shared path / environment resolution for dsh-xrd.
 *
 * NO machine-specific path is baked into this plugin. Every external dependency
 * (GSAS-II, the fullprof fp2k engine, the Python interpreter, CIFs_to_PCR.exe) is
 * located through one chain:
 *
 *   1. an explicit tool argument          — e.g. { gsas_root: "D:/GSAS-II" }
 *   2. an environment variable            — DSH_GSAS_ROOT / DSH_FP2K_ROOT / ...
 *   3. xrd.local.json next to the plugin  — {"gsas_root": "...", ...}
 *      (or the file named by $DSH_XRD_CONFIG)
 *   4. a conventional location, probed    — plugin dir, cwd, their parents,
 *                                           $DSH_HOME
 *
 * If nothing matches we return null and the caller raises an actionable error.
 * We never silently fall back to a directory layout that only exists on the
 * maintainer's machine — that is exactly the class of leak a release must avoid.
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'

/** Absolute path of the plugin root (the directory holding lib/ and engine/). */
export const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

let _local = null

/** Contents of the optional local config file ({} when absent/unreadable). */
export function localConfig() {
  if (_local !== null) return _local
  _local = {}
  const p = process.env.DSH_XRD_CONFIG || join(PLUGIN_ROOT, 'xrd.local.json')
  try {
    if (existsSync(p)) _local = JSON.parse(readFileSync(p, 'utf8')) || {}
  } catch {
    _local = {}
  }
  return _local
}

function probeBase(base, names, prefix, nested = []) {
  if (!base) return null
  for (const n of names) {
    const p = join(base, n)
    try { if (existsSync(p)) return p } catch { /* unreadable */ }
  }
  // nested candidates: ship-with-the-package runtimes live one level down (e.g.
  // <plugin>/third_party/fullprof), and the direct-child probe above cannot see
  // them. Measured 2026-09-21: without this, the bundled fp2k was NOT found.
  for (const n of nested) {
    const p = join(base, n)
    try { if (existsSync(p)) return p } catch { /* unreadable */ }
  }
  if (prefix) {
    try {
      for (const e of readdirSync(base)) {
        if (e.startsWith(prefix)) {
          const p = join(base, e)
          if (existsSync(p)) return p
        }
      }
    } catch { /* unreadable base */ }
  }
  return null
}

/**
 * Resolve a directory through the chain above. Returns an absolute path or null.
 *
 * @param {object} o
 * @param {string} [o.arg]       explicit value from the tool call
 * @param {string} [o.env]       environment variable name
 * @param {string} [o.key]       key inside xrd.local.json
 * @param {string[]} [o.names]   conventional directory names to look for
 * @param {string} [o.prefix]    also accept any sibling starting with this
 * @param {string[]} [o.nested]  relative paths inside each base (must contain a
 *                               separator; for runtimes bundled with the package)
 */
export function resolveDir({ arg, env, key, names = [], prefix = null, nested = [] } = {}) {
  if (arg) return resolve(arg)
  if (env && process.env[env]) return resolve(process.env[env])
  const cfg = key ? localConfig()[key] : null
  if (cfg) return resolve(cfg)
  const bases = [
    PLUGIN_ROOT,
    process.cwd(),
    dirname(process.cwd()),
    dirname(PLUGIN_ROOT),
    process.env.DSH_HOME || join(homedir(), '.dsh'),
  ]
  for (const b of bases) {
    const hit = probeBase(b, names, prefix, nested)
    if (hit) return hit
  }
  return null
}

/** Actionable error text for a dependency that could not be located. */
export function missingMessage(label, env, key, extra = '') {
  return `${label} could not be located. Provide it in one of these ways:\n`
    + `  * pass the explicit tool argument\n`
    + `  * set the environment variable ${env}\n`
    + `  * add "${key}" to ${join(PLUGIN_ROOT, 'xrd.local.json')}\n`
    + (extra ? `  ${extra}\n` : '')
}

/**
 * Locate the FullProf suite directory that holds `fp2k.exe` (the refinement
 * engine this plugin drives).
 *
 * ONE definition for the whole plugin. It used to be duplicated (index.js for the
 * refinement driver, xrd-tools.js for xrd_verify_fp2k) because both wanted their
 * own error text; the duplication is gone now that the dependency is a single
 * file.
 *
 * The old names DSH_AUTOFP_ROOT / "autofp_root" are still honoured as a fallback
 * so an existing xrd.local.json keeps working, but they are no longer the
 * documented interface: the plugin no longer uses anything else from that
 * directory (AutoFP's own wrapper and its pcr reader are not used at all).
 *
 * @param {string} [explicit] optional explicit directory from the tool call
 * @returns {string} absolute path to the directory containing fp2k.exe
 */
export function fp2kRoot(explicit) {
  const r = resolveDir({
    arg: explicit, env: 'DSH_FP2K_ROOT', key: 'fp2k_root',
    names: ['fullprof', 'fpsuite', 'autofp'],
    // runtime shipped inside this package (see third_party/fullprof/PROVENANCE.md)
    nested: ['third_party/fullprof'],
  }) || resolveDir({
    // legacy chain, kept only so existing installs do not break
    env: 'DSH_AUTOFP_ROOT', key: 'autofp_root',
  })
  if (!r || !findFp2kBinary(r)) {
    throw new Error(missingMessage('FullProf fp2k', 'DSH_FP2K_ROOT', 'fp2k_root',
      'That directory must contain the fp2k executable — `fp2k.exe` on Windows, '
      + '`fp2k` on Linux/macOS. It is the ONLY file this plugin needs from the '
      + 'FullProf suite; refinement is driven by the bundled engine/fp2k_driver.py, '
      + 'not by FullProfApp or AutoFP.'))
  }
  return r
}

/**
 * The fp2k executable inside a resolved directory, or null.
 *
 * Platform-aware on purpose: the package ships BOTH `fp2k.exe` (FullProf 2017,
 * Windows) and `fp2k` (FullProf 2021, Linux) under third_party/fullprof, and the
 * old code hard-coded `fp2k.exe` — so on Linux the bundled runtime was unusable
 * and fp2kRoot() rejected a perfectly good directory (found by audit 2026-09-21).
 * On Windows `.exe` is preferred when both are present.
 */
export function findFp2kBinary(dir) {
  if (!dir) return null
  const names = process.platform === 'win32'
    ? ['fp2k.exe', 'fp2k']
    : ['fp2k', 'fp2k.exe']
  for (const n of names) {
    const p = join(dir, n)
    try { if (existsSync(p)) return p } catch { /* unreadable */ }
  }
  return null
}

/**
 * Locate the Python interpreter the bundled engines are run with.
 *
 * ONE definition for the whole plugin — index.js and xrd-tools.js each used to carry
 * their own copy of this probe.
 *
 * Precedence — the SAME chain every other external dependency uses (see the header),
 * which this function used to ignore:
 *   1. `DSH_XRD_PY`    — this plugin's OWN name; the documented interface.
 *   2. `python` in xrd.local.json (or the file named by `$DSH_XRD_CONFIG`) — the key
 *      the README example has shown all along, which nothing actually read until now.
 *   3. probe `%LOCALAPPDATA%\Programs\Python\Python3*`, newest first.
 *   4. `python` on PATH — last resort; a Microsoft Store stub answers here, and the
 *      caller's error text tells the user to set the variable instead.
 *
 * No per-call argument on purpose: "which python" is a machine-level answer, not a
 * per-invocation choice, so it does not belong in nine tool schemas.
 *
 * REMOVED 2026-09-23 — this chain used to end with an environment variable inherited from
 * an unrelated plugin distribution (private, and never published alongside this one). It
 * was never a *code* dependency: nothing here imports, spawns or calls that plugin, and
 * with no variable set the probe below still finds an interpreter. But a published plugin
 * must not carry another distribution's variable name in its interface — a reader of this
 * file concludes "this depends on that plugin" — and a value set for it silently
 * redirected THESE engines: one without pymatgen/gemmi/lmfit makes every engine die at its
 * top-level `import numpy`, which surfaced as "produced no result" with no hint that the
 * interpreter was the cause (measured 2026-09-23; see diagnosePython below).
 *
 * @returns {string} path to a Python interpreter (never null; the probe is best-effort)
 */
export function pyExecutable() {
  if (process.env.DSH_XRD_PY) return process.env.DSH_XRD_PY
  const cfg = localConfig().python
  // deliberately NOT resolve()d: a bare command name (`python`) is a legitimate value,
  // and resolving it against cwd would turn it into a path that cannot exist.
  if (typeof cfg === 'string' && cfg) return cfg
  const base = join(process.env.LOCALAPPDATA || '', 'Programs', 'Python')
  try {
    if (existsSync(base)) {
      // NUMERIC version order, newest first (0.8.6). A plain string sort put Python39
      // ahead of Python313 ("9" > "3" at that position), so the probe could pick an
      // older interpreter than the one installed — the third review's finding.
      const ver = (d) => {
        const m = /^Python(\d)(\d+)$/.exec(d)
        return m ? Number(m[1]) * 1000 + Number(m[2]) : -1
      }
      const dirs = readdirSync(base).filter((d) => /^Python3\d+$/.test(d))
        .sort((a, b) => ver(b) - ver(a))
      for (const d of dirs) {
        const exe = join(base, d, 'python.exe')
        if (existsSync(exe)) return exe
      }
    }
  } catch { /* fall through */ }
  return 'python'
}

/**
 * The site-packages imports every engine needs. GSAS-II is deliberately NOT listed:
 * the engines add the GSAS-II root to `sys.path` themselves (from `gsas_root`), so a
 * bare `import GSASII` fails even under a perfectly good interpreter — probing it
 * would report a false fault (measured 2026-09-23).
 */
export const PY_PROBE_MODULES = ['numpy', 'pymatgen', 'gemmi', 'lmfit', 'matplotlib']

/**
 * One-line diagnosis of an interpreter, for the FAILURE path only.
 *
 * This exists because "engine produced no result" was the most misleading failure the
 * plugin had. Measured 2026-09-23 with an interpreter whose site-packages are empty
 * (same CPython, same engine, same plan — only the interpreter differed): the engine
 * dies at its top-level `import numpy`, nothing is written to the result file, and the
 * tool returns `produced no result (exit 1)`. xrd_data_io and xrd_phase_screen did not
 * even render the engine's stderr, so the model saw two lines with no mention of numpy,
 * of the interpreter, or of a traceback — a clean-looking plugin defect.
 *
 * Appending this line to the `error` string fixes that for EVERY tool at once, because
 * every render prints `ERROR: …`; it also keeps the output schemas unchanged (error is
 * already a string).
 *
 * Runs once per failure and never on the success path, so it costs nothing in normal
 * use; `find_spec` is used instead of import so no engine dependency is actually loaded.
 *
 * @param {string} py interpreter as resolved by pyExecutable() (may be a bare name)
 * @returns {string} a short `[python] …` line; never throws
 */
export function diagnosePython(py) {
  const script = [
    'import json, sys',
    'import importlib.util as u',
    `mods = ${JSON.stringify(PY_PROBE_MODULES)}`,
    'miss = []',
    'for m in mods:',
    '    try:',
    '        found = u.find_spec(m) is not None',
    '    except Exception:',
    '        found = False',
    '    if not found:',
    '        miss.append(m)',
    "print(json.dumps({'exe': sys.executable, 'ver': sys.version.split()[0], 'miss': miss}))",
  ].join('\n')
  try {
    const r = spawnSync(py, ['-c', script], { encoding: 'utf8', timeout: 20_000, windowsHide: true })
    if (r.error) {
      return `[python] "${py}" could not be started (${r.error.message}) — the engines need a `
        + 'CPython 3.13 carrying numpy/pymatgen/gemmi/lmfit/matplotlib.'
    }
    if (r.status !== 0) {
      const last = String(r.stderr || '').trim().split('\n').filter(Boolean).pop() || `exit ${r.status}`
      return `[python] "${py}" is not a usable interpreter (${last}).`
    }
    const line = String(r.stdout || '').trim().split('\n').filter(Boolean).pop() || '{}'
    const j = JSON.parse(line)
    if (Array.isArray(j.miss) && j.miss.length) {
      return `[python] ${j.exe} (${j.ver}) is MISSING ${j.miss.join(', ')} — the engines cannot run on `
        + 'it. Point the plugin at a full interpreter: set DSH_XRD_PY, or put "python" in xrd.local.json.'
    }
    return `[python] ${j.exe} (${j.ver}) carries every engine import — the failure above is NOT a `
      + 'missing interpreter dependency.'
  } catch (e) {
    return `[python] "${py}" could not be diagnosed (${e && e.message ? e.message : e}).`
  }
}
