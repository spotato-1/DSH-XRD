// dsh-xrd extra tools: xrd_refine_manual (python engine driver) and
// xrd_verify_fp2k (FullProf fp2k verification / chemistry).
//
// xrd_refine_manual contract:
//   args: { data, cif?, atoms?, out_dir?, plan?, vary?, stage_hint?,
//           engine?, init_engine? }
//   - plan: optional prebuilt plan JSON path (expert path)
//   - engine: optional custom engine .py path. The bundled engine is a
//     *mutable model template*: AI may first call init_engine:true to copy it
//     into the sample work dir, then edit that copy (model section: structure
//     handling / peak-shape / background / extra components / parameter
//     bounds) to adapt to the material, and finally run with engine=<copy>.
//     init_engine:true copies the template and returns its path without
//     running (the AI then reads/edits the copy before the real run).
//   - otherwise the tool builds a plan: stage_hint selects sensible vary set
//     (calibrate | floor | structure | extras | twophase) which the *caller*
//     (skill/AI) chooses; bounds come from built-in defaults or args.
//   Returns: { rwp, rexp, chi2, result_file, components_file?, out_files }
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync, copyFileSync, readdirSync, rmSync, renameSync, statSync } from 'node:fs'
import { join, resolve, dirname, basename, normalize } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { resolveDir, missingMessage, fp2kRoot, findFp2kBinary, pyExecutable, diagnosePython, localConfig } from './config.js'
// D19/D20 复杂情况审查的机械层（切片 1）：记 GSAS 轮、判 E、组织注入文本。
// 只在**工具内部**注入消息，不改变任何工具返回值（输出 schema 是
// additionalProperties:false，新增返回字段会被宿主拒绝）。
import { onGsasRound } from './review.js'

// probe python (ONE definition for the whole plugin, in lib/config.js)
const PY = pyExecutable()
const PLUGIN_ROOT = normalize(resolve(dirname(fileURLToPath(import.meta.url)), '..'))
const BUNDLED_ENGINE = join(PLUGIN_ROOT, 'engine', 'xrd_engine.py')
const BUNDLED_ENGINE_N = normalize(BUNDLED_ENGINE).toLowerCase()
// EVERY script this plugin ships and runs (0.8.5). Only `xrd_engine.py` used to be
// recognised, so the plugin's own gsas_engine/gsas_ops/data_io/... were classified as
// "external" and asked for execution approval on a host without the central
// dsh-exec-approval service — which is why the code comments already called them
// "plugin-owned (exempt)" while `pluginOwnedScript()` said otherwise.
const BUNDLED_SCRIPTS_N = new Set([
  'xrd_engine.py', 'gsas_engine.py', 'gsas_ops.py', 'gsas_finalize.py',
  'gsas_export.py', 'data_io.py', 'cif_edit.py', 'phase_screen.py',
  'fp2k_driver.py', 'fp2k_guard.py', 'pcr_check.py', 'cif2pcr_helper.py',
  'proc_guard.py', 'result_io.py',
  // 0.8.10: the multiphase template ships with the plugin and is therefore
  // plugin-owned WHEN RUN FROM THE PLUGIN TREE. A copy made by init_engine and
  // edited by the AI is NOT in this set, so it keeps requiring approval — the same
  // contract the manual engine already has (0.8.3).
  'gsas_multiphase_template.py',
].map((f) => normalize(join(PLUGIN_ROOT, 'engine', f)).toLowerCase()))

// External program roots. Nothing machine-specific is hardcoded here; the chain
// is tool argument -> environment variable -> xrd.local.json -> conventional
// probe (see lib/config.js). Failure is explicit and actionable.
function gsasRoot(explicit) {
  const r = resolveDir({
    arg: explicit, env: 'DSH_GSAS_ROOT', key: 'gsas_root',
    names: ['GSAS-II-5.8.1', 'GSAS-II'], prefix: 'GSAS-II',
  })
  if (!r) {
    throw new Error(missingMessage('GSAS-II', 'DSH_GSAS_ROOT', 'gsas_root',
      'Point it at a GSAS-II install directory containing the GSASII/ package.'))
  }
  return r
}

// The FullProf fp2k probe used to be duplicated here (for xrd_verify_fp2k) and in
// index.js (for the refinement driver), each with its own error text. It now
// lives in config.js as fp2kRoot() — one definition, one message — because the
// dependency is a single file (fp2k.exe).

// ---------------------------------------------------------------------------
// Execution-approval policy (user rule 2026-09-06):
//   * scripts bundled inside this plugin (engine/xrd_engine.py) run freely;
//   * ANY other executable script/py (init_engine copies, AI-authored temp
//     scripts, custom engines, ...) requires an explicit
//     sandbox escalation BEFORE it runs;
//   * the CENTRAL dsh-exec-approval service (when composed in the same host
//     tree) owns the whole decision: trusted-session exemption, owned-script
//     exemption, per-session reuse cache, host approval escalation. This
//     plugin delegates to ctx.get('execApproval').request(...) first.
//   * FALLBACK (host without dsh-exec-approval): the legacy inline chain
//     below — per-session cache of approved targets (conversation-end ⇒ auto
//     expiry via process death), fail-open when no approval service exists.
// ---------------------------------------------------------------------------
const _approvedBySession = new Map() // sessionId -> Set(targetKey)

function pluginOwnedScript(p) {
  try {
    const n = normalize(resolve(p)).toLowerCase()
    return n === BUNDLED_ENGINE_N || BUNDLED_SCRIPTS_N.has(n)
  } catch { return false }
}
function approvalTargetKey(kind, target) {
  return `${kind}:${normalize(resolve(target)).toLowerCase()}`
}

async function loadEscalation() {
  // Dynamic, guarded import: @deepseek-ai/dsh-sandbox may be absent in some
  // host compositions; a missing module must never break plugin loading or
  // tool execution. Returns undefined → caller fail-opens (no escalation).
  try {
    const m = await import('@deepseek-ai/dsh-sandbox')
    return typeof m.approveEscalation === 'function' ? m.approveEscalation : undefined
  } catch {
    return undefined
  }
}

async function requestEscalation(ctx, exec, kind, target, justification, effectiveMode) {
  const approver = ctx?.get ? ctx.get('approval') : undefined
  if (!approver) return 'fail-open' // no approval channel (unsandboxed host)
  const approveEscalation = await loadEscalation()
  if (typeof approveEscalation !== 'function') return 'fail-open' // sandbox pkg missing
  // approveEscalation throws on rejected/cancelled/unavailable; returns the
  // granted mode on success. Either way nothing here swallows a rejection.
  await approveEscalation({
    requestedMode: 'danger-full-access',
    justification,
    effectiveMode: effectiveMode ?? 'workspace-write',
    subject: kind === 'script' ? `script ${target}` : 'program',
  }, {
    approver,
    agent: exec.agent,
    callId: exec.callId,
    toolName: exec.toolName || 'xrd',
    signal: exec.signal,
  })
  return 'granted'
}

// Legacy inline approval chain (no central service present).
async function legacyApproval(ctx, exec, kind, target, justification) {
  if (pluginOwnedScript(target)) return true // bundled engine exempt
  const session = exec?.agent?.session
  const sessionId = session?.id
  const policySvc = ctx?.get ? ctx.get('sandboxPolicy') : undefined
  const resolvePolicy = () => {
    try {
      const p = policySvc && typeof policySvc.resolve === 'function'
        ? policySvc.resolve(session ? { session } : {}) : undefined
      return p?.mode ?? 'workspace-write'
    } catch { return 'workspace-write' }
  }
  if (sessionId) {
    const key = approvalTargetKey(kind, target)
    let set = _approvedBySession.get(sessionId)
    if (set && set.has(key)) return true // approved earlier this conversation
    const res = await requestEscalation(ctx, exec, kind, target, justification, resolvePolicy())
    if (res !== 'fail-open') {
      set = _approvedBySession.get(sessionId) || new Set()
      set.add(key)
      _approvedBySession.set(sessionId, set)
    }
    return true
  }
  // no session: per-call approval each time (still fail-open on missing channels)
  await requestEscalation(ctx, exec, kind, target, justification, resolvePolicy())
  return true
}

async function requireExecutionApproval(ctx, exec, kind, target, justification) {
  // Central service first: when dsh-exec-approval is composed in the host
  // tree its decision is authoritative (trusted-session exemption, owned
  // exemption, reuse cache, host escalation; throws on user rejection).
  try {
    const central = ctx?.get ? ctx.get('execApproval') : undefined
    if (central && typeof central.request === 'function') {
      await central.request({
        kind,
        target: resolve(target),
        justification,
        owned: pluginOwnedScript(target),
        pluginRoot: PLUGIN_ROOT,
        agent: exec?.agent,
        callId: exec?.callId,
        signal: exec?.signal,
      })
      return true
    }
  } catch (e) {
    // central rejected / denied ⇒ hard stop (never silently run)
    throw new Error(`execution approval not granted for ${target}: ${e?.message ?? e}`)
  }
  // No central service (host without dsh-exec-approval): legacy inline chain.
  try {
    await legacyApproval(ctx, exec, kind, target, justification)
    return true
  } catch (e) {
    // user rejected / approval denied ⇒ hard stop (never silently run)
    throw new Error(`execution approval not granted for ${target}: ${e?.message ?? e}`)
  }
}

export { requireExecutionApproval }

// default vary sets per stage hint (brain decides; these are conservative defaults)
const STAGE_DEFAULT_VARY = {
  calibrate: ['scale', 'zero', 'a', 'U', 'V', 'W', 'eta', 'bg0', 'bg1', 'bg2', 'bg3', 'bg4'],
  floor: [], // LeBail needs engine support via k_i params - not in v1 engine; caller uses full model as proxy or engine stage
  structure: ['scale', 'zero', 'a', 'U', 'V', 'W', 'eta', 'bg0', 'bg1', 'bg2'],
  extras: ['scale', 'zero', 'bg0', 'bg1', 'bg2', 'ex0_c', 'ex0_H', 'ex0_a', 'ex1_c', 'ex1_H', 'ex1_a'],
  twophase: ['scale', 'zero', 's2_0_scale', 's2_0_k', 's2_0_Y'],
}

// ---------------------------------------------------------------------------
// P0 (0.8.3): run identity + result freshness.
//
// WHY (codex audit of 0.8.1, 2026-09-27; re-verified against this tree): every
// tool read its engine result from a FIXED name inside the artifact dir
// (gsas_result.json, finalize_result.json, …). When this run timed out or
// crashed before writing, an earlier run's file was still sitting there and was
// silently reported as this run's result. Only the manual engine was guarded
// (pre-run mtime snapshot); the other seven were not.
//
// Two gates, both cheap:
//   * mtime must ADVANCE past a snapshot taken BEFORE the child starts — a file
//     from an earlier run can therefore never pass, and a half-written file is
//     impossible because the engines now write atomically (engine/result_io.py);
//   * when the engine echoed a `run_id` (it arrives inside the plan JSON the
//     engine already reads), that id must equal this run's.
// The id is deleted before the payload is returned, so NO tool output schema
// changes.
export function newRunId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** Pre-run mtime of a result file: -1 when it does not exist yet. */
export function resultMtime(path) {
  try { return statSync(path).mtimeMs } catch { return -1 }
}

/** The run id a plan file was written with (null when unreadable/absent). */
export function planRunId(planPath) {
  try { return JSON.parse(readFileSync(planPath, 'utf8')).run_id ?? null } catch { return null }
}

/**
 * Read an engine result that THIS run wrote.
 * @returns {{out: object|null, reason: string|null}} `reason` explains a refusal.
 */
export function readFreshResult(resultFile, { preMtime = -1, runId = null } = {}) {
  let mtime
  try { mtime = statSync(resultFile).mtimeMs } catch {
    return { out: null, reason: 'no result file (the engine wrote nothing)' }
  }
  if (preMtime >= 0 && !(mtime > preMtime)) {
    return { out: null, reason: 'stale result file: not written by this run' }
  }
  let out
  try { out = JSON.parse(readFileSync(resultFile, 'utf8')) } catch {
    return { out: null, reason: 'result file unreadable or half-written' }
  }
  if (!out || typeof out !== 'object' || Array.isArray(out)) {
    return { out: null, reason: 'result file is not a JSON object' }
  }
  if (runId && out.run_id && out.run_id !== runId) {
    return { out: null, reason: `result belongs to another run (${out.run_id})` }
  }
  delete out.run_id   // internal bookkeeping — must never reach the model
  return { out, reason: null }
}

// A5: runCli supports an AbortSignal for clean cancellation. Kept signature
// (cmd, args, cwd, timeoutMs) compatible with all existing call sites; an
// optional 5th arg { signal } (or a plain AbortSignal) cancels the child.
// D18① (2026-09-26): the resolved object now also carries `timedOut` / `aborted`.
// WHY: without the flag the caller cannot tell "the engine finished with a bad
// exit code" from "we killed it at our own budget" — and only the second one is
// the case where an empty result is EXPECTED and the artifacts are the payload.
// ---------------------------------------------------------------------------
// P1/P2 (0.8.3): ONE engine runner, with the liveness policy chosen PER BACKEND.
//
// Why per backend (codex audit 2026-09-27 + measurement 2026-09-28): "is this
// run still alive?" is a different question for our own processes than for a
// black box we merely wait for.
//   * 'heartbeat' — OUR processes (manual engine, GSAS-II, and the short
//     engines) publish `<run>/.xrd-progress.json` while they work. While the
//     ticks advance the run is KEPT ALIVE past its nominal timeout; it is judged
//     stuck only when ticks stop (HEARTBEAT_STALL_MS) or the hard fuse is hit.
//     "Slow but healthy" can therefore never be killed by a fixed number.
//   * 'fixed' — fp2k. The driver only WAITS for it, and fp2k touched its output
//     files 2-3 times per run in every measurement we have (healthy reference
//     run 0.25 s; two real samples then spun at 97% CPU for 600 s AFTER their
//     first output change). Ticks of the waiter prove nothing, so the fixed
//     timeout plus the driver's own stall rule stay the authority for that
//     backend. (Unchanged behaviour, on purpose.)
//
// Every backend also gets a GRACEFUL STOP first: write `<run>/.xrd-stop`, the
// engine raises at its next checkpoint and writes a structured stop result; then
// a grace period; then a TREE kill. The tree kill is `taskkill /T /F` on Windows
// and NEVER a console event — CTRL+C reaching the host console makes DSH exit
// (measured invariant) — while a bare child.kill() would leave grandchildren
// running.
export const LIVENESS = { manual: 'heartbeat', gsas: 'heartbeat' }
// Overridable for tests (a real stall window of 90 s is not something a test can
// wait for; the shipped default is asserted statically in the audit suite).
export const HEARTBEAT_STALL_MS = (() => {
  const v = Number(process.env.DSH_XRD_HEARTBEAT_STALL_MS)
  return Number.isFinite(v) && v > 0 ? v : 90_000
})()
export const HARD_FUSE_FACTOR = 4
export const STOP_GRACE_MS = (() => {
  const v = Number(process.env.DSH_XRD_STOP_GRACE_MS)
  return Number.isFinite(v) && v > 0 ? v : 5_000
})()
export const TICK_FILE = '.xrd-progress.json'
export const STOP_FILE = '.xrd-stop'
// RUN-SCOPED names (0.8.6). Two runs may share an artifact directory (every tool that
// defaults out_dir to the session workspace), and one fixed file name meant they
// overwrote each other's progress tick — a healthy run could look stalled — and shared
// a single stop flag. The run-scoped name is written ALWAYS; the legacy name is kept as
// a fallback so an older engine (or a hand-written flag) still works, and its content
// carries `run_id`, which is what actually decides whose request it is.
export const tickNameFor = (runId) => (runId ? `.xrd-progress-${runId}.json` : TICK_FILE)
export const stopNameFor = (runId) => (runId ? `.xrd-stop-${runId}` : STOP_FILE)

/** Latest tick from a run directory, or null (unreadable / not this run). */
function readTick(tickFile, runId, outDir = null) {
  // run-scoped first, then the legacy fixed name (engines older than 0.8.6 write only
  // that one), and the run_id inside decides whether it belongs to this run.
  const names = []
  if (tickFile) names.push(tickFile)
  if (outDir) for (const n of [TICK_FILE, ...(runId ? [tickNameFor(runId)] : [])]) {
    const p = join(outDir, n)
    if (!names.includes(p)) names.push(p)
  }
  for (const p of names) {
    try {
      const t = JSON.parse(readFileSync(p, 'utf8'))
      if (!t || typeof t !== 'object') continue
      if (runId && t.run_id && t.run_id !== runId) continue
      return t
    } catch { /* try the next name */ }
  }
  return null
}

/**
 * Kill a whole process tree and CONFIRM it is gone.
 *
 * Windows: `taskkill /PID <pid> /T /F`. POSIX: the process group (created by
 * spawning detached), falling back to the pid itself — the group kill silently
 * does nothing when the child never became a group leader, which is how the
 * Linux regression hung (codex review 2026-09-28).
 *
 * @returns {Promise<{ok: boolean, how: string}>}
 */
async function killTree(pid) {
  if (!pid) return { ok: false, how: 'no-pid' }
  if (process.platform === 'win32') {
    const ok = await new Promise((res) => {
      try {
        const k = spawn('taskkill', ['/PID', String(pid), '/T', '/F'],
          { windowsHide: true, stdio: 'ignore' })
        k.on('close', (code) => res(code === 0))
        k.on('error', () => res(false))
      } catch { res(false) }
    })
    return { ok, how: 'taskkill /T /F' }
  }
  let group = false
  try { process.kill(-pid, 'SIGKILL'); group = true } catch { group = false }
  let single = false
  try { process.kill(pid, 'SIGKILL'); single = true } catch { single = false }
  return { ok: group || single, how: group ? 'killpg' : (single ? 'kill' : 'none') }
}

/** Is that pid still alive? (Windows: tasklist; POSIX: signal 0.) */
async function pidAlive(pid) {
  if (!pid) return false
  if (process.platform === 'win32') {
    return await new Promise((res) => {
      try {
        const t = spawn('tasklist', ['/FI', `PID eq ${pid}`, '/NH'],
          { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] })
        let out = ''
        t.stdout.on('data', (d) => { out += d })
        t.on('close', () => res(new RegExp(`\\b${pid}\\b`).test(out)))
        t.on('error', () => res(false))
      } catch { res(false) }
    })
  }
  try { process.kill(pid, 0); return true } catch { return false }
}

/**
 * Best-effort text out of a host-service collect reader.
 *
 * The host's collected-output reader (`@deepseek-ai/dsh-subprocess-local`,
 * `OutputCollector`) exposes `readFrom(fromByte) -> {text, nextOffset, lossy,
 * spillPath?}` (and `finalize() -> {text, truncated, spillPath?}`); the host's own
 * tools call it as `handle.collected.stdout?.readFrom(0)`. 0.8.8: this used to try
 * `text` / `read` / `toString` and, because the real reader is none of them, fell
 * through to the INHERITED `Object.prototype.toString` — which returns the STRING
 * "[object Object]". Every engine's stderr therefore arrived as that literal, so
 * `log_tail` and every "produced no result" failure reason showed `[object Object]`
 * instead of the engine's own traceback (measured end-to-end 2026-09-28; it also
 * swallowed GSAS-II's "No readers matched hint", hiding a second defect).
 *
 * A plain object must now yield '' rather than a fake string: '' keeps the caller's
 * existing `stdout` (`readCollected(x) || stdout`) instead of poisoning it.
 */
function readCollected(reader) {
  if (!reader) return ''
  if (typeof reader === 'string') return reader
  // reader.text as a plain property (an already-read result, or a host variant)
  if (typeof reader.text === 'string') return reader.text
  for (const m of ['readFrom', 'finalize', 'read', 'text']) {
    if (typeof reader[m] !== 'function') continue
    // `readFrom` wants a whole-stream byte offset (0 = first read); the others take
    // nothing, so both call shapes are attempted rather than guessed.
    for (const args of [[0], []]) {
      let v
      try { v = reader[m](...args) } catch { continue }
      if (typeof v === 'string') return v
      if (v && typeof v.text === 'string') return v.text
    }
  }
  // only an OVERRIDDEN toString may be trusted — Object.prototype.toString returns
  // "[object Object]", which is exactly the bug being fixed here
  try {
    if (typeof reader.toString === 'function' && reader.toString !== Object.prototype.toString) {
      const v = reader.toString()
      if (typeof v === 'string' && v !== '[object Object]') return v
    }
  } catch { /* ignore */ }
  return ''
}

/**
 * File-name stem in the form review.js uses for its ledger keys (`safeKey`): the
 * mechanical state is `review_mechanical_<key>.json`, so the two must agree.
 * 0.8.8 — the review sample key is derived here (see xrd_refine_gsas) instead of
 * being taken blindly from whichever path argument happened to be present.
 */
function safeStem(name) {
  return String(name || '').replace(/\.[^.]+$/, '').replace(/[^\w.-]+/g, '_').slice(0, 60)
}

/**
 * The sample key of the ONLY mechanical ledger in `dir`, or null when the directory
 * holds none or more than one — two ledgers means the key is genuinely ambiguous and
 * the caller's own derivation must win (never guess between samples).
 * Used when `xrd_refine_gsas` continues an existing .gpx: the same sample's earlier
 * rounds keep their ledger instead of opening a new one under the project file name.
 */
function soleLedgerSample(dir) {
  try {
    const found = readdirSync(dir).filter((f) => /^review_mechanical(?:_[^.]+)?\.json$/.test(f))
    if (found.length !== 1) return null
    const m = /^review_mechanical_([^.]+)\.json$/.exec(found[0])
    return m ? m[1] : null
  } catch { return null }
}

/**
 * A "your review history is split" warning for the sample key in use (0.8.9).
 *
 * 0.8.7 could book one sample into several ledgers (`Y2O3`, then `Y2O3_gsas` after the
 * pattern was converted, then `gsas_proj` for a project continuation). 0.8.8 unified the
 * derivation, but the ledgers 0.8.7 already wrote stay on disk and nothing told the
 * caller that the history it is appending to is only part of the story. This warns when a
 * sibling ledger looks like the same sample under another file name (one key is the other
 * plus a suffix); the caller can then pass `sample` explicitly or delete the stale file.
 * It never merges silently — merging round counts would invent a history.
 *
 * @returns {string|null} one warning line, or null when nothing looks split
 */
function splitLedgerWarning(dir, key) {
  if (!dir || !key) return null
  try {
    const keys = readdirSync(dir)
      .map((f) => /^review_mechanical_([^.]+)\.json$/.exec(f))
      .filter(Boolean).map((m) => m[1])
    const siblings = keys.filter((k) => k !== key && (k.startsWith(key + '_') || key.startsWith(k + '_')))
    if (!siblings.length) return null
    return `refinement-review history for sample "${key}" looks SPLIT: sibling ledger(s) `
      + `${siblings.join(', ')} exist in the same directory. Pass sample:"${key}" to book this round `
      + 'into the intended ledger, or delete the stale file — rounds are not merged automatically.'
  } catch { return null }
}

/**
 * The refinement-review ledger key for one `xrd_refine_gsas` call (0.8.8).
 *
 * Split out of `execute` so the derivation is testable without GSAS-II and without a
 * host: the previous "stem of gpx OR data" rule silently fragmented ONE sample into
 * several ledgers during a single end-to-end run (measured 2026-09-28: `Y2O3.dat` ->
 * `Y2O3`, then `Y2O3_gsas.fxye` -> `Y2O3_gsas`), splitting the round counter, the
 * streaks, the E lag and any freeze. Order, and why:
 *   1. explicit `sample` — the caller (AI/user) names the sample;
 *   2. the CIF stem — the structure identifies the sample, and `cif_to_pcr` names the
 *      pcr after the same CIF, so the fp2k and GSAS stages agree (`Y2O3.cif` ->
 *      `Y2O3.pcr` -> `Y2O3`);
 *   3. the data stem;
 *   4. for a .gpx continuation: the directory's existing ledger when there is EXACTLY
 *      one (continuation of that sample), otherwise the gpx stem.
 *
 * @returns {string|null} sanitised ledger key, or null when nothing is derivable
 */
export function reviewSampleKey(args = {}, outDir = null) {
  const stem = (p) => safeStem(basename(resolve(p)))
  try {
    if (typeof args.sample === 'string' && args.sample.trim()) return safeStem(args.sample.trim())
    for (const p of [args.cif, args.cif2, args.data]) {
      if (typeof p === 'string' && p.trim()) return stem(p)
    }
    if (typeof args.gpx === 'string' && args.gpx.trim()) {
      if (outDir) {
        const existing = soleLedgerSample(outDir)
        if (existing) return existing
      }
      return stem(args.gpx)
    }
    return null
  } catch { return null }
}

// Remove a stop request that belongs to THIS run (0.8.5). The old code deleted
// `.xrd-stop` whenever it existed, so two runs sharing an artifact directory could
// delete each other's cancel request; the engine side now also ignores a foreign
// run_id, and this keeps the file itself intact for its owner.
function clearStopFile(stopFile, runId) {
  try {
    if (!stopFile || !existsSync(stopFile)) return
    let rid = null
    try { rid = JSON.parse(readFileSync(stopFile, 'utf8')).run_id ?? null } catch { rid = null }
    if (rid && runId && rid !== runId) return      // somebody else's request — leave it
    rmSync(stopFile, { force: true })
  } catch { /* best effort */ }
}

// A timer that never keeps the process alive (0.8.5). A plain setTimeout holds the
// event loop open for its whole delay, so a raced-away exit wait left the engine's OWN
// process alive for up to `fuse + grace + slack` AFTER its work was done — measured on
// the audit suite: it printed "ALL PASS — pass=61 fail=0" and then sat there until the
// per-script cap killed it, which the wrapper reports as TIMEOUT.
function sleepUnref(ms, value = undefined) {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms, value)
    if (t && typeof t.unref === 'function') t.unref()
  })
}

/**
 * Run one engine process with per-backend liveness, graceful stop and tree kill.
 *
 * @param {object} opts
 *   timeoutMs  nominal timeout (heartbeat backends may exceed it while ticking)
 *   fuseMs     ABSOLUTE cap for a heartbeat run; it must stay BELOW the tool's own
 *              timeoutMs so the stop is ours (structured) and not the host's bare
 *              `tool call timed out` — 0.8.4, the `timeoutMs x 4` default was
 *              larger than the tool timeout and therefore unreachable
 *   signal     AbortSignal from the tool call
 *   backend    key of LIVENESS ('manual' / 'gsas' / 'fp2k' / anything else)
 *   runId      this run's id (as written into the plan and the tick file)
 *   outDir     run directory holding .xrd-progress.json / .xrd-stop
 *   ctx        plugin context, used to prefer the host `subprocess` service
 */
export async function runEngine(cmd, args, cwd, opts = {}) {
  const {
    timeoutMs = 600_000, signal, backend = 'engine', runId = null,
    outDir = null, ctx = null, fuseMs: fuseOpt = null,
  } = opts
  const policy = LIVENESS[backend] || 'fixed'
  const fuseRaw = fuseOpt ?? (policy === 'heartbeat' ? timeoutMs * HARD_FUSE_FACTOR : timeoutMs)
  // A caller-supplied non-positive fuse would fire the watchdog on its first tick, so
  // the value is clamped here as well as in `fuseFor` (0.8.5).
  const fuseMs = Number.isFinite(fuseRaw) && fuseRaw > 0 ? fuseRaw : Math.max(1_000, timeoutMs)
  const tickFile = outDir ? join(outDir, tickNameFor(runId)) : null
  const stopFile = outDir ? join(outDir, stopNameFor(runId)) : null
  const legacyStopFile = outDir ? join(outDir, STOP_FILE) : null
  const sig = signal && typeof signal === 'object' && 'aborted' in signal
    ? signal : (signal?.signal ?? undefined)
  const started = Date.now()

  clearStopFile(stopFile, runId)
  if (legacyStopFile && legacyStopFile !== stopFile) clearStopFile(legacyStopFile, runId)

  // Prefer the host's subprocess service when this host offers it: it already
  // implements tree-scoped termination with grace and whole-tree exit
  // observation. Detect strictly and fall back rather than gamble on an API this
  // plugin cannot see from the maintenance environment.
  let svc = null
  try { svc = ctx?.get ? ctx.get('subprocess') : null } catch { svc = null }
  let useService = !!(svc && typeof svc.spawn === 'function' && typeof spawn !== 'undefined')

  let stdout = ''
  let stderr = ''
  let code = -1
  let beats = 0
  let lastSeq = -1
  let lastBeat = 0
  let timedOut = false
  let aborted = false
  let stopSent = false
  let killReason = null
  let lastTick = null          // newest guard tick, so its self-report reaches the caller
  let transport = useService ? 'host-subprocess' : 'child_process'
  let handle = null
  const finished = { v: false }

  const requestStop = () => {
    if (stopSent || !stopFile) return
    // nothing was ever started ⇒ a stop flag would only confuse a concurrent run that
    // shares this directory (0.8.6)
    if (!child && !handle) return
    stopSent = true
    try { writeFileSync(stopFile, JSON.stringify({ run_id: runId, at: Date.now() })) } catch { /* best effort */ }
    // the legacy fixed name too, for engines that only know that one; its payload still
    // names THIS run, so a concurrent run ignores it
    try {
      if (legacyStopFile && legacyStopFile !== stopFile) {
        writeFileSync(legacyStopFile, JSON.stringify({ run_id: runId, at: Date.now() }))
      }
    } catch { /* best effort */ }
  }

  let child = null
  let donePromise
  // A call that arrives ALREADY cancelled must not start a process at all (0.8.6): the
  // 0.8.5 fix registered the abort listener after the spawn (it needs the termination
  // ladder to exist), so a pre-aborted signal still created the child and only then
  // killed it — the third review measured the child's marker file appearing on disk.
  const preAborted = !!(sig && sig.aborted)
  if (preAborted) {
    useService = false
    transport = 'none (cancelled before start)'
    donePromise = Promise.resolve()
  }
  if (useService && !preAborted) {
    try {
      handle = svc.spawn({
        argv: [cmd, ...args], cwd,
        stdio: { stdin: 'ignore', stdout: { maxBytes: 4_000_000 }, stderr: { maxBytes: 4_000_000 } },
        graceMs: STOP_GRACE_MS, signal: sig,
      })
      if (!handle || typeof handle.terminate !== 'function' || !handle.done) throw new Error('unexpected handle shape')
      donePromise = Promise.resolve(handle.done).then((info) => {
        code = Number(info?.code ?? info?.exitCode ?? -1)
        stdout = readCollected(handle.collected?.stdout) || stdout
        stderr = readCollected(handle.collected?.stderr) || stderr
      }, (err) => { code = -1; stderr += String(err?.message ?? err) })
    } catch (e) {
      // 0.8.4: this is `let`, not `const` — assigning to a const here threw
      // `TypeError: Assignment to constant variable`, so a host service that
      // threw or returned an unexpected shape could not fall back at all
      // (found by the 0.8.3 review, whose repro is now a test of its own).
      useService = false
      transport = 'child_process (host service unusable: ' + String(e?.message ?? e) + ')'
      handle = null
    }
  }
  if (!useService && !preAborted) {
    // POSIX: own process group, so a group kill reaches grandchildren. Windows:
    // taskkill /T does that job (and detached would only detach the console).
    // STDIN IS IGNORED, matching the host-service branch above (2026-10-05): Node's
    // default is a PIPE whose write end we hold open, and an external CLI that prompts
    // ("Press <enter> to finish" — CIFs_to_PCR.exe) then blocks on it until OUR budget
    // expires. Measured on that converter, same cif, async spawn: open pipe ⇒ killed at
    // the 20 s cap with the .pcr already written; stdin ignored ⇒ exits in 211 ms
    // (exit 24, .pcr written). A whole `cif_to_pcr` call cost 117 s because of this.
    // It also means no engine can be wedged forever by an accidental read.
    child = spawn(cmd, args, {
      cwd, windowsHide: true, detached: process.platform !== 'win32',
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    donePromise = new Promise((resolve) => {
      // The fallback path caps what it accumulates, exactly like the host-service branch
      // does with stdout.maxBytes (0.8.6): a chatty engine (a GSAS-II .lst echoed to
      // stdout, say) otherwise grew the string without limit in the host's memory.
      child.stdout.on('data', (d) => {
        if (stdout.length < MAX_CHILD_OUTPUT) stdout += d
      })
      child.stderr.on('data', (d) => {
        if (stderr.length < MAX_CHILD_OUTPUT) stderr += d
      })
      child.on('error', (err) => { code = -1; stderr += String(err?.message ?? err); resolve() })
      child.on('close', (c) => { code = c ?? -1; resolve() })
    })
  }

  // Termination machinery FIRST, abort registration LAST (0.8.5): registering the
  // abort listener before `requestTermination`/`termination` existed meant an
  // ALREADY-ABORTED signal hit a temporal dead zone — `if (sig?.aborted) onAbort()`
  // ran while `const requestTermination` (and `let termination`) were still
  // uninitialised, so it threw
  // `ReferenceError: Cannot access 'requestTermination' before initialization`
  // AFTER the child had been spawned: the call died and nothing terminated the
  // child. Declaration order is the fix, not a guard around it.
  let terminating = false
  let treeKilled = null
  let residual = null
  let termination = null
  const terminate = async () => {
    if (terminating) return                      // idempotent: stall, abort and fuse may all fire
    terminating = true
    // A CANCEL is not a timeout (0.8.5): every stop used to be labelled
    // `timed_out: true`, which told the model the internal budget had expired when
    // in fact the user cancelled the call. The reason stays in `killReason` and
    // `timed_out` now means exactly "a budget/heartbeat/fuse limit was reached".
    timedOut = killReason !== 'aborted'
    requestStop()
    // Nothing was started ⇒ there is nothing to stop and nothing to wait for (0.8.7).
    // A call that arrives ALREADY cancelled used to enter this ladder and sit out the
    // whole stop grace (default 5 s) before returning "cancelled".
    if (!child && !handle) {
      treeKilled = false
      residual = false
      return
    }
    // NOTE: this grace sleep must NOT be unref'd. It is awaited while the engine may
    // already have exited (the graceful-stop path), and an unref'd timer that is the
    // only pending handle lets node empty the event loop and die with
    // "unsettled top-level await" (exit 13) — measured on the supervision suite.
    await new Promise((r) => setTimeout(r, STOP_GRACE_MS))     // let the engine stop cleanly
    if (finished.v) {
      treeKilled = false                       // it exited on its own during the grace period
      residual = false
    } else {
      try {
        if (handle && typeof handle.terminate === 'function') {
          handle.terminate()
          treeKilled = true                        // the service owns whole-tree teardown
          // Confirm it when the handle exposes a pid; otherwise report `null` =
          // "NOT verified by us". The old code wrote `false`, i.e. it claimed a
          // confirmation it had never performed (0.8.5, found by the review).
          const hp = handle.pid
          if (Number.isInteger(hp) && hp > 0) {
            residual = true
            for (let i = 0; i < 10 && residual; i += 1) {
              await new Promise((r) => setTimeout(r, 200))
              residual = await pidAlive(hp)
            }
          } else {
            residual = null
          }
        } else if (child) {
          const k = await killTree(child.pid)
          treeKilled = k.ok
          // CONFIRM it: a kill that returns success while the process lives on is
          // exactly the "no post-timeout verification" gap the review named
          residual = true
          for (let i = 0; i < 10 && residual; i += 1) {
            await new Promise((r) => setTimeout(r, 200))
            residual = await pidAlive(child.pid)
          }
        }
      } catch { treeKilled = treeKilled ?? false }
    }
    // give the exit event a moment, then report what we actually observed
    await Promise.race([donePromise, new Promise((r) => setTimeout(r, STOP_GRACE_MS))])
  }

  // Give the termination ladder a single entry point: stall, abort and fuse can all
  // fire, and the caller must be able to WAIT for the confirmation that the tree is
  // gone before a result is returned (0.8.4 — reporting `residual` while the check
  // was still running was as good as not checking).
  const requestTermination = () => {
    if (!termination) {
      // Never surface a rejection to a caller: the watchdog and the abort listener
      // both fire from contexts that cannot await (0.8.5). The failure is recorded
      // in `killReason` instead, where the caller already looks.
      termination = terminate().catch((e) => {
        killReason = killReason || ('terminate-failed: ' + String(e?.message ?? e))
        treeKilled = treeKilled ?? false
      })
    }
    return termination
  }

  // ABORT is a first-class termination request, not a hint (0.8.4): the 0.8.3 code
  // only wrote the stop flag, so a call cancelled by the user kept running to its
  // timeout — measured by the review as "aborted at 100 ms, the child ran the full
  // 2116 ms and exited 0". Registered HERE, after every name it touches exists, so
  // an already-aborted signal terminates the just-spawned child instead of throwing
  // (0.8.5).
  const onAbort = () => {
    aborted = true
    killReason = killReason || 'aborted'
    const p = requestTermination()
    if (p && typeof p.catch === 'function') p.catch(() => { /* surfaced via treeKilled/residual */ })
  }
  if (sig && !sig.aborted) sig.addEventListener('abort', onAbort, { once: true })
  if (sig?.aborted) onAbort()

  const watchdog = setInterval(() => {
    const now = Date.now()
    const t = readTick(tickFile, runId, outDir)
    if (t) lastTick = t
    if (t && typeof t.seq === 'number' && t.seq !== lastSeq) {
      lastSeq = t.seq
      // Only a tick that CLAIMS progress keeps the run alive. A plain heartbeat
      // (the guard's ticker thread) proves the wrapper is up, which is exactly
      // the thing that must NOT be mistaken for "the work is advancing" — GSAS
      // can stall inside its own loop and fp2k can spin at 100% CPU after
      // writing its first output (both measured).
      if (t.progress === true) { beats += 1; lastBeat = now }
    }
    if (policy === 'heartbeat') {
      const stalled = lastBeat ? (now - lastBeat) > HEARTBEAT_STALL_MS
        : (now - started) > timeoutMs
      if (stalled) { killReason = lastBeat ? 'heartbeat-lost' : 'no-first-beat'; clearInterval(watchdog); requestTermination() }
      else if (now - started >= fuseMs) { killReason = 'hard-fuse'; clearInterval(watchdog); requestTermination() }
    } else if (now - started >= fuseMs) {
      killReason = 'timeout'; clearInterval(watchdog); requestTermination()
    }
  }, 1000)

  // BOUNDED exit wait (0.8.5, corrected in 0.8.6, corrected AGAIN in 0.8.7): `await
  // donePromise` alone had no ceiling, so a transport whose `done` never settles blocked
  // the tool call forever. The 0.8.5 cap was derived from the FUSE, so a run cancelled at
  // 1 s could still sit for ~29 minutes; 0.8.6 switched to `grace + slack` from "the
  // moment" — but re-evaluated it with a fresh `Date.now()` on every loop iteration, so
  // in the terminating state the deadline MOVED WITH THE CLOCK and never arrived at all
  // (fourth review, and a regression introduced by that very fix). The deadline is now
  // frozen ONCE, at the first termination request.
  let terminationDeadline = null
  const exitCeiling = () => {
    if (terminating || aborted) {
      if (terminationDeadline === null) terminationDeadline = Date.now() + STOP_GRACE_MS + EXIT_WAIT_SLACK_MS
      return terminationDeadline
    }
    return started + fuseMs + 2 * STOP_GRACE_MS + EXIT_WAIT_SLACK_MS
  }
  let exitObserved = false
  for (;;) {
    const left = exitCeiling() - Date.now()
    if (left <= 0) break
    const settled = await Promise.race([
      donePromise.then(() => true),
      sleepUnref(Math.min(left, 2_000), false),
    ])
    if (settled) { exitObserved = true; break }
    if ((terminating || aborted) && !termination) await requestTermination()
  }
  if (!exitObserved && !terminating) {
    killReason = killReason || 'exit-not-observed'
    await requestTermination()                 // bounded: terminate() itself races its own grace
    exitObserved = await Promise.race([
      donePromise.then(() => true),
      sleepUnref(EXIT_WAIT_SLACK_MS, false),
    ])
    if (!exitObserved) killReason = 'exit-not-observed'
  }
  finished.v = true
  if (termination) await termination          // the confirmation must finish before we report
  clearInterval(watchdog)
  if (sig) { try { sig.removeEventListener('abort', onAbort) } catch { /* noop */ } }
  clearStopFile(stopFile, runId)
  if (legacyStopFile && legacyStopFile !== stopFile) clearStopFile(legacyStopFile, runId)

  return {
    code, stdout, stderr, timedOut, aborted, stopped: stopSent,
    killReason, beats, policy, transport,
    // what the guard reported about ITSELF (0.8.4): the review found that
    // `self_assigned` never reached the caller, so "the job object failed and we
    // ran unprotected" was invisible. These are text/log facts, NOT new output
    // schema fields — the tool payloads must not change.
    guard: lastTick?.guard ?? null,
    selfAssigned: lastTick?.self_assigned ?? null,
    // `residual` is true (still alive — measured), false (confirmed gone) or null
    // (NOT verified: the host service owns teardown and exposes no pid).
    treeKilled, residual, exitObserved,
    elapsedMs: Date.now() - started,
  }
}

// Kept as the call-site-compatible wrapper used by most tools: same signature as
// before, now backed by runEngine (tree kill + graceful stop for every backend).
function runCli(cmd, args, cwd, timeoutMs = 600_000, signal) {
  return runEngine(cmd, args, cwd, { timeoutMs, signal })
}

// D18①: the manual engine's INTERNAL budget must stay below the tool's own
// timeoutMs so that a long run ends as OUR structured failure — with the
// artifact directory and a file listing — instead of the host's bare
// `tool call timed out after Nms` (measured 2026-09-25 on xrd_refine_manual:
// no path, no R value, no artifact list). 300 s of headroom is left for the
// listing, the log tail and the return trip.
//
// RAISED 2026-09-28 (P2): the manual engine is a HEARTBEAT backend now, so a
// healthy fit is no longer cut at a fixed number — it is judged stuck only when
// its progress ticks stop (90 s) or the hard fuse is reached. The numbers below
// are the CEILING for a fit that keeps ticking, not the expected duration.
const MANUAL_RUN_BUDGET_MS = (() => {
  const v = Number(process.env.DSH_XRD_MANUAL_BUDGET_MS)
  return Number.isFinite(v) && v > 0 ? v : 1_500_000
})()
// Tool timeoutMs for xrd_refine_manual (kept as one name so the pair cannot drift).
// Override exists for tests/debugging (it must stay ABOVE the internal budget,
// otherwise the host wins again and the structured failure never happens).
const MANUAL_TOOL_TIMEOUT_MS = (() => {
  const v = Number(process.env.DSH_XRD_MANUAL_TOOL_TIMEOUT_MS)
  return Number.isFinite(v) && v > 0 ? v : 1_800_000
})()
// Same pair for xrd_refine_gsas, which is also a heartbeat backend (its
// progress is GSAS-II's own .gpx/.lst writes, not the wrapper's liveness).
const GSAS_RUN_BUDGET_MS = (() => {
  const v = Number(process.env.DSH_XRD_GSAS_BUDGET_MS)
  return Number.isFinite(v) && v > 0 ? v : 1_500_000
})()
const GSAS_TOOL_TIMEOUT_MS = (() => {
  const v = Number(process.env.DSH_XRD_GSAS_TOOL_TIMEOUT_MS)
  return Number.isFinite(v) && v > 0 ? v : 1_800_000
})()
// ABSOLUTE FUSE for a heartbeat run, and the one number that MUST stay below the
// tool's own timeout (0.8.4). The 0.8.3 default was `timeoutMs x 4` — 6000 s
// against a 1800 s tool timeout — so a healthy ticking run was cut by the HOST
// long before our fuse could fire, which is the opposite of "the hard timeout is
// the last resort". The margin is the room our own structured failure needs.
const CLEANUP_MARGIN_MS = 60_000
// How long the exit event may lag behind the tree kill before we stop waiting for
// it (0.8.5): `await donePromise` used to be unbounded, so a transport whose `done`
// never settles blocked the tool call forever.
const EXIT_WAIT_SLACK_MS = 30_000
// Cap on what the child_process fallback accumulates (0.8.6). The host-service branch
// already caps each stream at 4 MB via stdout.maxBytes; without this the fallback kept
// every byte an engine echoed.
const MAX_CHILD_OUTPUT = 4_000_000

/**
 * The fuse must sit BETWEEN the engine's own nominal budget and the tool timeout:
 *   * below the nominal budget it would cut a healthy run before the engine can
 *     report anything;
 *   * at or above the tool timeout the HOST kills first and our structured failure
 *     never happens (the 0.8.3 defect).
 * The 0.8.4 rule (`tool timeout - 60 s`) satisfied the second half but ignored the
 * first: with the 60 s tool timeout the timeout test uses, it produced **fuse = 0**,
 * the watchdog fired on its very first tick, and the test still passed because it
 * only looked for the word "budget" in the message (0.8.5, found by the review).
 */
function fuseFor(toolTimeoutMs, nominalBudgetMs = 0) {
  const margin = Math.min(CLEANUP_MARGIN_MS, Math.max(1_000, Math.floor(toolTimeoutMs / 4)))
  const upper = Math.max(1_000, toolTimeoutMs - margin)
  const want = nominalBudgetMs + Math.max(1_000, Math.floor(nominalBudgetMs / 4))
  return Math.max(1_000, Math.min(upper, Math.max(want, 1_000)))
}
const MANUAL_FUSE_MS = fuseFor(MANUAL_TOOL_TIMEOUT_MS, MANUAL_RUN_BUDGET_MS)
const GSAS_FUSE_MS = fuseFor(GSAS_TOOL_TIMEOUT_MS, GSAS_RUN_BUDGET_MS)

// A directory listing for failure reporting: the engine may have written
// background curves, components or a partial log before it was killed, and those
// paths are the whole point of "failed but left the scene intact".
function listArtifacts(dir, cap = 60) {
  const out = []
  const walk = (d, rel, depth) => {
    if (out.length >= cap || depth > 2) return
    let ents = []
    try { ents = readdirSync(d, { withFileTypes: true }) } catch { return }
    for (const e of ents) {
      if (out.length >= cap) return
      const r = rel ? `${rel}/${e.name}` : e.name
      if (e.isDirectory()) walk(join(d, e.name), r, depth + 1)
      else out.push(r)
    }
  }
  walk(dir, '', 0)
  return out
}

// Default artifact dir = session workspace cwd when available, else near the
// primary input file. Mirrors how bash/pwsh tools resolve their workdir.
function sessionDir(exec) {
  try {
    const c = exec?.agent?.session?.header?.cwd
    return c ? resolve(c) : undefined
  } catch { return undefined }
}
function artifactDir(args, exec, fallbackDir) {
  if (args.out_dir) return resolve(args.out_dir)
  const sd = sessionDir(exec)
  return sd || resolve(fallbackDir)
}

// A path that does not exist yet: `name`, else `name-2`, `name-3` … (0.8.5). Used by
// init_engine so a hand-edited engine copy is never silently replaced.
function freeCopyPath(dir, name) {
  const first = join(dir, name)
  if (!existsSync(first)) return first
  const stem = name.replace(/\.py$/i, '')
  const ext = name.slice(stem.length)
  for (let i = 2; i < 1000; i += 1) {
    const cand = join(dir, `${stem}-${i}${ext}`)
    if (!existsSync(cand)) return cand
  }
  return first
}

// A3 fix: schema declares extras/second/occ as JSON *strings*; buildPlan must
// JSON.parse them (Array.isArray was silently dropping every string → those
// features never reached the plan). Accepts either a string or an already
// parsed array/object.
function parseJsonArg(v, what) {
  if (v === undefined || v === null || v === '') return undefined
  if (typeof v === 'string') {
    try { return JSON.parse(v) } catch { throw new Error(`xrd_refine_manual: ${what} is not valid JSON: ${String(v).slice(0, 80)}`) }
  }
  return v
}

async function buildPlan(args, exec) {
  // Caller may pass an explicit plan file - use as-is. A plan that does NOT exist is an
  // error, not a fallback (0.8.6): the old code fell through to the default path, so
  // with `data` present the missing plan was silently IGNORED (a default plan ran and
  // the caller's parameters never applied), and without `data` it died inside
  // `resolve(undefined)` with a bare Node TypeError.
  if (args.plan) {
    const p = resolve(args.plan)
    if (!existsSync(p)) throw new Error(`xrd_refine_manual: plan not found: ${p}`)
    return p
  }
  const data = resolve(args.data)
  if (!existsSync(data)) throw new Error(`xrd_refine_manual: data file not found: ${data}`)
  const outDir = artifactDir(args, exec, dirname(data))
  mkdirSync(outDir, { recursive: true })
  const stage = args.stage_hint || 'calibrate'
  // `atoms`/`lattice` arrive as JSON STRINGS and must be parsed (0.8.5): the raw
  // string went into the plan, and the engine then iterated it character by
  // character (`a["el"]` on a str → TypeError), so the documented nominal-atom path
  // could never work. `lattice` is the cell that path also needs — it was not even
  // an accepted argument before, and without it the engine exits.
  const atoms = parseJsonArg(args.atoms, 'atoms')
  const lattice = parseJsonArg(args.lattice, 'lattice')
  if (atoms && !lattice) {
    throw new Error('xrd_refine_manual: `atoms` needs `lattice` as well — pass the cell as JSON '
      + '({"cubic":5.4}, or {"a":..,"b":..,"c":..,"alpha":..,"beta":..,"gamma":..}); '
      + 'the engine refuses to build a structure without one')
  }
  const plan = {
    run_id: newRunId(),
    data: { file: data },
    out_dir: outDir,
    cif: args.cif ? resolve(args.cif) : null,
    atoms: atoms || null,
    lattice: lattice || null,
    vary: args.vary || STAGE_DEFAULT_VARY[stage] || STAGE_DEFAULT_VARY.calibrate,
    // USER RULE (2026-09-18): background polynomial defaults to order 6.
    background: { n_cheb: 6, center: 40.0, halfwidth: 30.0 },
    max_nfev: args.max_nfev || 8000,
  }
  // Wavelength (0.9.0): EXPLICIT ONLY — omitted means the engine keeps its own
  // default (Cu K-alpha1 1.54056 / K-alpha2 1.54439), and the engine log marks
  // which of the two happened. A single `wavelength` is monochromatic
  // (synchrotron, or K-alpha2 already stripped); `wavelength_doublet` is
  // [K-alpha1, K-alpha2, ratio] for a laboratory tube.
  if (args.wavelength != null && args.wavelength_doublet != null) {
    throw new Error('xrd_refine_manual: pass either `wavelength` (a single value) or '
      + '`wavelength_doublet` ([kalpha1, kalpha2, ratio]) — not both')
  }
  if (args.wavelength != null) {
    const v = Number(args.wavelength)
    if (!Number.isFinite(v) || v <= 0) throw new Error('xrd_refine_manual: `wavelength` must be a positive number (A)')
    plan.lambda = { kalpha1: v, kalpha2: 0, ratio2: 0 }
    plan.lambda_explicit = true
  } else if (args.wavelength_doublet != null) {
    const d = args.wavelength_doublet
    if (!Array.isArray(d) || d.length < 2) {
      throw new Error('xrd_refine_manual: `wavelength_doublet` must be [kalpha1, kalpha2, ratio]')
    }
    const [k1, k2, r] = d.map(Number)
    if (!Number.isFinite(k1) || !Number.isFinite(k2) || k1 <= 0 || k2 <= 0) {
      throw new Error('xrd_refine_manual: `wavelength_doublet` needs two positive wavelengths (A)')
    }
    plan.lambda = { kalpha1: k1, kalpha2: k2, ratio2: Number.isFinite(r) ? r : 0.5 }
    plan.lambda_explicit = true
  }
  // Background ITERATION is opt-in (user rule 2026-09-18, restated): the manual
  // engine and GSAS-II both default to a 6th-order polynomial, and the order
  // iteration is only switched on when every other parameter has been optimised
  // and the fit still misses the target. When it runs, each order's background
  // curve is exported as a two-column file for import into GSAS-II.
  const bgScan = parseJsonArg(args.bg_scan, 'bg_scan')
  if (bgScan === true) {
    plan.bg_scan = { enabled: true, orders: [6, 8, 10, 12, 14, 16, 18, 20], stop_tol: 0.05 }
  } else if (bgScan && typeof bgScan === 'object') {
    plan.bg_scan = bgScan
  }
  const extras = parseJsonArg(args.extras, 'extras')
  const second = parseJsonArg(args.second, 'second')
  const occ = parseJsonArg(args.occ, 'occ')
  const coordVary = parseJsonArg(args.coord_vary, 'coord_vary')
  const bondTargets = parseJsonArg(args.bond_targets, 'bond_targets')
  if (Array.isArray(extras)) plan.extras = extras
  if (Array.isArray(second)) plan.second = second
  if (occ && typeof occ === 'object') plan.occ = occ
  if (coordVary && typeof coordVary === 'object') plan.coord_vary = coordVary
  if (Array.isArray(bondTargets)) plan.bond_targets = bondTargets
  const planPath = join(outDir, `plan_${stage}.json`)
  writeFileSync(planPath, JSON.stringify(plan, null, 1))
  return planPath
}

export function registerXrdManual(ctx) {
  ctx.tools.register({
    name: 'xrd_refine_manual',
    description:
      'Run the generic python XRD fitting engine (pymatgen structure factors + lmfit, Cu Kα1/Kα2 doublet, Pseudo-Voigt, Chebyshev background, Poisson weights) on a 2θ-I data file. Caller supplies data (+ optional CIF or nominal atoms); stage_hint picks a conservative default vary set (calibrate|structure|extras|twophase) - the AI decides the stage per workflow and may pass explicit vary/occ/extras/second. Engine only executes; returns metrics and paths. Use when refinement needs bounded parameters (occupancies, profile, broad peaks) that bare fp2k cannot express. BACKGROUND: the polynomial defaults to order 6 — the same default as GSAS-II AUTOBACK. Raising it is an ESCALATION (bg_scan), used only when every other parameter is already optimised and the fit still misses the target; it then steps by 2 up to a hard cap of 20, judges the R-value boundary effect on THIS engine\'s Rwp (bounded LM, not a GSAS wR), and exports one importable 2theta/background curve per order for GSAS-II. HOW THE BACKGROUND IS FITTED (0.8.5 wording fix — the algorithm itself is unchanged): WITHOUT bg_scan the background coefficients are refined in the SAME joint least-squares as the cell/profile parameters, so it is NOT a separate "fitted last" pass; WITH bg_scan the background ORDER is settled last with every other family frozen, and the run then closes with a joint fit at the chosen order. result.json\'s background block states which of the two happened (`fitted_last` / `final_fit` / `fitted_jointly_with`).',
    parameters: {
      type: 'object',
      properties: {
        data: { type: 'string', description: 'Absolute path to 2-column (2θ I) data file' },
        cif: { type: 'string', description: 'Absolute path to main-phase CIF (preferred) or omit with atoms' },
        atoms: { type: 'string', description: 'Optional JSON string: nominal atoms [{el,xyz,occ,label}...] when no CIF. PARSED as JSON (0.8.5 — it used to be written into the plan as a raw string, which the engine then iterated character by character). Needs `lattice`.' },
        lattice: { type: 'string', description: 'Optional JSON string: the cell for the nominal-atom path, {"cubic":5.4} or {"a":..,"b":..,"c":..,"alpha":..,"beta":..,"gamma":..}. REQUIRED whenever `atoms` is used — the engine exits with "nominal atoms need lattice a (cubic) or a/b/c" when it is missing.' },
        wavelength: { type: 'number', description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): a single monochromatic wavelength in A (synchrotron, or K-alpha2 already stripped). The engine then computes ONE line only. OMIT it and the engine keeps its own default (Cu K-alpha1 1.54056 / K-alpha2 1.54439) — the log line states which of the two was used, so a default can never be mistaken for a declared value. Take the value from the data file the user supplied (xrd_data_io `header` dumps its header for you to read); do not invent one. Mutually exclusive with `wavelength_doublet`.' },
        wavelength_doublet: { type: 'array', items: { type: 'number' }, description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): [K-alpha1, K-alpha2, ratio] for a laboratory tube (e.g. [1.5406, 1.54439, 0.5]). Omit to keep the engine default. Mutually exclusive with `wavelength`.' },
        out_dir: { type: 'string', description: 'Output directory (default: session workspace cwd); result_*.json/components.json written here' },
        stage_hint: { type: 'string', description: 'calibrate (default) | structure | extras | twophase | floor' },
        vary: { type: 'array', items: { type: 'string' }, description: 'Explicit parameter names to vary (overrides stage default)' },
        occ: { type: 'string', description: 'Optional JSON string: {siteLabel: {value,min,max,vary}} occupancy multipliers, each scaling the occupancy the CIF already carries. SCOPE LIMIT (user decision 2026-09-19): these multipliers are INDEPENDENT — this engine has NO total-conservation machinery (no Afrac coupling, no sum constraint). When several occupancies are refined together, nothing stops their total from drifting to a fake gain (measured on a real defect system: K ran to 26 per cell). Do occupancy-coupled work such as [Fe(CN)6] linkage or split-site K with EQUAL occupancies in GSAS-II instead (xrd_gsas_ops), which supports it natively. Also note a site whose CIF occupancy is already ~1 (a mixed site) cannot take a multiplier above 1.' },
        coord_vary: { type: 'string', description: 'Optional JSON string: {"N1":["z"],"O6":["x","z"]} — refine selected fractional coordinates of selected sites. OFF by default: on low-resolution data free ligand coordinates drift to nonsense unless bond_targets are supplied.' },
        bond_targets: { type: 'string', description: 'Optional JSON string: [{"pair":["Fe","C"],"d":1.90,"sigma":0.03,"weight":1.0}] — bond-length SOFT constraints, appended to the residual vector (so Rwp itself is not inflated); use with coord_vary to keep ligand bonds physical.' },
        bg_scan: { type: 'string', description: 'Background-order ITERATION — off by default. Pass the JSON STRING "true" (or {"enabled":true,"orders":[6,8,10,12,14,16,18,20],"stop_tol":0.05}) ONLY when every other parameter has already been optimised and the fit still misses the target: this is an escalation, not a routine step. NOTE the parameter is a string, not a boolean (0.8.6): a bare `true` is rejected by the host schema, so write "true". Default background is a 6th-order polynomial; the iteration raises it in steps of 2 up to a hard cap of 20, one step at a time, and writes one importable 2theta/background curve file PER order (bg_nNN.txt) so each can be handed to GSAS-II. The R-value BOUNDARY EFFECT is judged on THIS engine\'s Rwp (bounded LM) — never on a GSAS wR — and the reported boundary_n_cheb is the first order whose gain fell below stop_tol; orders past that edge are not used to pick the curve. The background is always the LAST parameter family fitted: during the iteration every other parameter is frozen so the trials differ only in background order, and the final state is a joint fit at the chosen order.' },
        extras: { type: 'string', description: 'Optional JSON string: [{center,H,amp,eta}] empirical broad peaks' },
        second: { type: 'string', description: 'Optional JSON string: [{cif,k,scale,Y}] second-phase candidates' },
        plan: { type: 'string', description: 'Optional: absolute path to a prebuilt plan JSON (expert). GOTCHA: when this is given the plan file is used AS-IS and every other argument (vary/occ/extras/second/coord_vary/bond_targets/bg_scan/max_nfev) is IGNORED — put everything you need inside the plan, or leave plan out and pass the individual arguments.' },
        engine: { type: 'string', description: 'Optional: absolute path to a custom engine .py (copy of the bundled template, AI-edited for this material)' },
        init_engine: { type: 'boolean', description: 'true: copy the bundled engine template into out_dir and return its path for AI editing (no run)' },
        max_nfev: { type: 'integer', description: 'lmfit max evaluations (default 8000)' },
      },
      // `data` is NOT universally required (0.8.5): `init_engine` copies a template
      // (it tolerates a missing data path itself) and an explicit `plan` already
      // carries the data file, so a blanket requirement blocked two supported modes.
      // What each mode needs is checked in execute() with a message that says so.
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          mode: { type: 'string', enum: ['init', 'run'], description: "'init' (engine copy returned, nothing run) | 'run' (refinement executed)" },
          rwp: { type: 'number' },
          rexp: { type: 'number' },
          chi2: { type: 'number' },
          result_file: { type: 'string' },
          components_file: { type: 'string' },
          log_tail: { type: 'string' },
          plan_file: { type: 'string' },
          engine: { type: 'string' },
          engine_copy: { type: 'string' },
          health_flags: { type: 'array', items: { type: 'string' }, description: 'Judgment packet: parameters pinned at a bound, or with no/huge stderr — they are NOT measurements. Report them as fixed-by-constraint, not as refined values.' },
          background_order: { type: 'number', description: 'Final background polynomial order used' },
          background_iterated: { type: 'boolean', description: 'true when the background order iteration ran (escalation, not default)' },
          boundary_n_cheb: { type: 'number', description: 'Order at which the R-value boundary effect occurred, judged on THIS engine\'s Rwp' },
          baseline_rwp: { type: 'number', description: 'Rwp with every parameter still at its starting value, measured before any optimisation. Compare it with rwp to tell "the fit improved" apart from "the fit never moved".' },
          fit_status: {
            type: 'object',
            additionalProperties: true,
            description: 'Optimiser status: {success, nfev, hit_max_nfev, n_at_bound, covariance_available, flags[]}. A Rwp from a non-converged run, a run that stopped at max_nfev, or a fit parked on a bound is NOT a minimum — read flags before trusting the number.',
          },
          r_plateau: {
            type: 'object',
            additionalProperties: true,
            description: 'R-value plateau (R 值洼地) verdict for THIS run: {is_plateau, dof, overfit_risk, basis}. Only true when the optimiser converged cleanly and nvar/npts stayed below 0.2; the plateau of a multi-family ladder is judged from xrd_refine_auto\'s per-step ladder trace instead.',
          },
          // D18① (2026-09-26) structured-failure fields. A run killed at the
          // internal budget returns ok:false + these paths instead of throwing,
          // so "no R value" arrives with the scene intact (artifact dir + file
          // list + how to read it) rather than as a bare host timeout.
          ok: { type: 'boolean', description: 'false for a structured failure (the internal run budget expired, or the call was cancelled). Absent on the init path.' },
          timed_out: { type: 'boolean', description: 'true ONLY when a budget/heartbeat/fuse limit was reached — a user cancel reports aborted:true and timed_out:false (0.8.5)' },
          aborted: { type: 'boolean', description: 'true when the CALL was cancelled: the engine was terminated on request, so no fit quality can be concluded (0.8.5)' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point after being asked to (graceful stop), leaving a structured "stopped" result instead of nothing (0.8.5)' },
          error: { type: 'string', description: 'Structured failure reason (present only when ok is false)' },
          hint: { type: 'string', description: 'What the failure does and does not mean, and the next diagnostic step (present only when ok is false)' },
          out_dir: { type: 'string', description: 'Artifact directory (present on the structured-failure path so the kept files are findable)' },
          artifacts: { type: 'array', items: { type: 'string' }, description: 'Relative paths of files already on disk in out_dir when the run failed' },
        },
        // A2 fix: init mode returns NO numeric fields (no run happened). NaN is
        // not lossless JSON → the host rejected the whole tool result. Drop
        // numeric required fields; enforce presence per mode instead.
        // DSH's lossless tool-schema subset (@deepseek-ai/dsh-tools json-schema)
        // supports type/oneOf/properties/required/additionalProperties/items/
        // enum/const — but NOT anyOf. oneOf cannot be combined with a sibling
        // typed object (crash: "schema.anyOf is not a supported keyword" when
        // anyOf was used here). Keep the discriminant with enum + required
        // ['mode']; mode-specific fields stay optional — execute() returns the
        // complete run payload while init mode intentionally omits metrics.
        required: ['mode'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        value = value ?? {}   // a failed call has no value; render must not throw
        if (value.mode === 'init') {
          return [{ type: 'text', text: `Engine template copied to: ${value.engine_copy}\nRead and edit it (model section) to adapt to your material, then run xrd_refine_manual with engine=<this path>.` }]
        }
        // D18①: structured failure (budget expired) — lead with the verdict and
        // the kept artifacts, never with a bare "no result".
        if (value.ok === false) {
          return [{ type: 'text', text: `RESULT: failed${value.timed_out ? ' (timeout, artifacts preserved)' : ''}\n`
            + `${value.error ?? ''}\n`
            + (value.out_dir ? `artifacts kept in: ${value.out_dir}\n` : '')
            + (Array.isArray(value.artifacts) && value.artifacts.length ? `on disk (${value.artifacts.length}): ${value.artifacts.slice(0, 20).join(', ')}\n` : '')
            + (value.hint ? `HINT: ${value.hint}\n` : '')
            + (value.log_tail ? `--- engine ---\n${value.log_tail}` : '') }]
        }
        return [{ type: 'text', text: `Rwp = ${value.rwp}${value.baseline_rwp !== undefined ? ` (baseline ${value.baseline_rwp})` : ''}  Rexp = ${value.rexp}  chi2 = ${value.chi2}\nresult: ${value.result_file}${value.components_file ? `\ncomponents: ${value.components_file}` : ''}${value.plan_file ? `\nplan: ${value.plan_file}` : ''}${value.background_order ? `\nbackground: order ${value.background_order}${value.background_iterated ? ` (iterated${value.boundary_n_cheb ? `, R boundary at order ${value.boundary_n_cheb}` : ''})` : ' (no iteration)'}` : ''}${value.fit_status ? `\nfit: success=${value.fit_status.success} nfev=${value.fit_status.nfev ?? '?'}${value.fit_status.hit_max_nfev ? ' HIT max_nfev' : ''}${value.fit_status.n_at_bound ? ` at_bound=${value.fit_status.n_at_bound}` : ''}${value.r_plateau ? ` plateau=${value.r_plateau.is_plateau}` : ''}` : ''}${value.fit_status?.flags?.length ? `\n⚠ fit_status:\n  - ${value.fit_status.flags.join('\n  - ')}` : ''}${value.health_flags?.length ? `\n⚠ judgment packet:\n  - ${value.health_flags.join('\n  - ')}` : ''}${value.log_tail ? `\n--- engine ---\n${value.log_tail}` : ''}` }]
      },
    },
    timeoutMs: MANUAL_TOOL_TIMEOUT_MS,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      // 0) init_engine mode: copy bundled engine template into the artifact
      //    dir (workspace cwd preferred) and return its path WITHOUT running.
      if (args.init_engine === true) {
        const fb = args.data ? dirname(resolve(args.data)) : process.cwd()
        const outDir = artifactDir(args, exec, fb)
        mkdirSync(outDir, { recursive: true })
        // NEVER overwrite an edited copy (0.8.5): the whole point of init_engine is
        // that the AI then edits the copy, and the old code silently replaced
        // `xrd_engine_custom.py` (and the two helper modules) with the stock
        // template on the next call, destroying that work. A free name is picked
        // instead and the chosen path is reported.
        const copyPath = freeCopyPath(outDir, 'xrd_engine_custom.py')
        copyFileSync(BUNDLED_ENGINE, copyPath)
        // 0.8.4: the template imports two SIBLING modules. Copying only the engine
        // produced a template that cannot even import (`ModuleNotFoundError:
        // result_io`) — it broke the whole "copy the template, edit it, run it"
        // path (found by the 0.8.3 review). The copy is now self-contained.
        const helpers = ['result_io.py', 'proc_guard.py']
        const copied = []
        for (const helper of helpers) {
          const src = join(dirname(BUNDLED_ENGINE), helper)
          if (!existsSync(src)) continue
          const dst = join(outDir, helper)
          // The helper modules are plugin INFRASTRUCTURE, not the user's model code: a
          // stale copy breaks the template (0.8.6 — the third review's "a newer template
          // next to an older proc_guard/result_io"), so they are refreshed when they
          // differ, and the refresh is reported.
          let same = false
          if (existsSync(dst)) {
            try { same = readFileSync(dst, 'utf8') === readFileSync(src, 'utf8') } catch { same = false }
          }
          if (!existsSync(dst)) { copyFileSync(src, dst); copied.push(helper) }
          else if (same) copied.push(`${helper} (unchanged)`)
          else { copyFileSync(src, dst); copied.push(`${helper} (UPDATED to the shipped version)`) }
        }
        return {
          mode: 'init',
          engine_copy: copyPath,
          log_tail: `Engine template copied to ${copyPath}, together with its sibling module(s): ${copied.join(', ') || '(none found)'}. `
            + 'Read and edit it (model section) to adapt to your material, then run xrd_refine_manual '
            + `with engine=<this path>. Keep the copied modules next to it — the template imports them.`
            + (basename(copyPath) === 'xrd_engine_custom.py'
              ? ''
              : ' NOTE: an earlier copy already existed, so this one got a numbered name — nothing was overwritten.'),
        }
      }
      // 1) resolve engine: custom path wins; verify exists
      const engine = args.engine ? resolve(args.engine) : BUNDLED_ENGINE
      if (!existsSync(engine)) throw new Error(`xrd_refine_manual: engine not found: ${engine}`)
      // 1b) non-bundled engine (init_engine copy, AI-authored py, custom) ⇒
      //     explicit approval before it runs (bundled engine is exempt)
      await requireExecutionApproval(ctx, exec, 'script', engine,
        `run XRD fitting engine script: ${engine}`)
      // What this mode needs, said plainly (0.8.5 — the schema no longer demands
      // `data` unconditionally, so the check lives here):
      if (!args.plan && !args.data) {
        throw new Error('xrd_refine_manual: give either `data` (a 2θ/I file) or `plan` (a prebuilt plan JSON)')
      }
      const planPath = await buildPlan(args, exec)
      // ONE directory for plan, result, progress tick and stop file (0.8.5). It used
      // to be `dirname(plan)`, which for an explicit `plan` argument is NOT the
      // artifact directory: the run's result went to one directory while the
      // heartbeat/stop files went to another, so a stop request could not be seen.
      const explicitPlan = !!(args.plan && existsSync(resolve(args.plan)))
      const anchor = explicitPlan ? dirname(resolve(args.plan)) : dirname(resolve(args.data))
      const runDir = artifactDir(args, exec, anchor)
      mkdirSync(runDir, { recursive: true })
      const resultFile = join(runDir, 'result.json')
      const cli = [engine, planPath, '--out', resultFile, '--components']
      // Snapshot the pre-run mtime: the ONLY way to know whether the result file
      // was written by THIS run. A time-window heuristic ("written in the last
      // 2 s") fails exactly when it matters — a stale file from a run seconds
      // earlier looks fresh, and the test measured the old Rwp coming back.
      // P0 (0.8.3): shared with every other engine via readFreshResult below.
      const preMtime = resultMtime(resultFile)
      const { code, stdout, stderr, timedOut, aborted, killReason, guard: guardKind, selfAssigned, residual, exitObserved } = await runEngine(PY, cli, runDir, {
        timeoutMs: MANUAL_RUN_BUDGET_MS,
        fuseMs: MANUAL_FUSE_MS,
        signal: exec?.signal,
        backend: 'manual',
        runId: planRunId(planPath),
        outDir: runDir,
        ctx,
      })
      const tail = (stdout + '\n' + stderr).slice(-2000)
      // A CANCEL is reported as a cancel (0.8.5), not as "the internal budget
      // expired": the review found every stop labelled `timed_out: true`, which is a
      // false statement about what happened (and about what the model may conclude).
      if (aborted) {
        const arts = listArtifacts(runDir)
        return {
          mode: 'run',
          ok: false,
          aborted: true,
          timed_out: false,
          out_dir: runDir,
          artifacts: arts,
          error: `the call was cancelled: termination ladder ran (reason ${killReason || 'aborted'}, exit ${code}, `
            + `guard ${guardKind || 'none'}`
            + `${exitObserved === false ? '; the exit event was NOT observed after the tree kill' : ''}`
            + `${residual === true ? '; a process was still alive after the tree kill' : ''}`
            + `${residual === null ? '; process death not confirmable by the plugin (host-owned teardown)' : ''}`
            + `). ${arts.length} file(s) on disk in ${runDir}.`,
          hint: 'RESULT: cancelled on request. NOTHING can be concluded about fit quality — the run was '
            + 'stopped where it stood. Re-run the same call if the cancellation was accidental.',
          plan_file: planPath,
          engine,
          log_tail: tail.slice(-1200),
        }
      }
      // A STOP is not a timeout (0.8.6): when the caller asked us to stop, a
      // cooperating engine writes a structured `{ok:false, stopped:true}` record on its
      // way out. The 0.8.5 code took the `timedOut` branch FIRST and returned the
      // generic "budget expired" text, throwing that record away — so the stop
      // mechanism existed but the caller never received the structured stop result
      // (the third review's finding, and it also fired the "engine did not produce a
      // result" throw because the stop record carries no metrics).
      if (timedOut || aborted) {
        const stopRead = readFreshResult(resultFile, { preMtime, runId: planRunId(planPath) })
        const sj = stopRead.out
        if (sj && sj.stopped === true) {
          const arts = listArtifacts(runDir)
          return {
            mode: 'run',
            ok: false,
            stopped: true,
            timed_out: false,
            out_dir: runDir,
            artifacts: arts,
            result_file: resultFile,
            error: (typeof sj.error === 'string' && sj.error)
              ? sj.error
              : `engine stopped on request at a safe point (reason ${killReason || 'stop'}, exit ${code})`,
            hint: 'RESULT: stopped on request. No R value is expected — the run ended where it was asked '
              + `to, and its partial state is on disk in ${runDir}. Re-run the same call if the stop was `
              + 'accidental; nothing about fit quality can be concluded from this result.',
            plan_file: planPath,
            engine,
            log_tail: tail.slice(-1200),
          }
        }
      }
      // D18①: our own budget expired ⇒ STRUCTURED failure (same shape of
      // information xrd_refine_auto gives): the artifact directory, what is
      // already on disk, and the explicit statement that a missing R value is not
      // evidence of a bad fit. Returned, not thrown, so the paths survive.
      if (timedOut) {
        const arts = listArtifacts(runDir)
        return {
          mode: 'run',
          ok: false,
          timed_out: true,
          out_dir: runDir,
          artifacts: arts,
          error: `engine stopped at the internal budget of ${MANUAL_RUN_BUDGET_MS / 1000} s `
            + `(tool timeout is ${MANUAL_TOOL_TIMEOUT_MS / 1000} s, fuse ${MANUAL_FUSE_MS / 1000} s; exit ${code}; `
            + `reason ${killReason || 'budget'}; guard ${guardKind || 'none'}`
            + `${selfAssigned === false ? ' — SELF-ASSIGN FAILED, the run was unprotected' : ''}`
            + `${residual ? '; a process was still alive after the tree kill' : ''}). No result.json was produced.`,
          hint: 'RESULT: failed (timeout, artifacts preserved). A MISSING R VALUE IS NOT A BAD FIT — '
            + 'the fit was still running when the budget expired, so nothing can be concluded about '
            + `quality. Inspect ${runDir} (${arts.length} file(s) on disk) and the log tail. Usual causes: `
            + 'the vary set is too large for the data (bounded LM making no progress), a stage that '
            + 'never converges, or a background-order iteration over many orders. Reduce the vary set / '
            + 'fix one parameter family before re-running — do NOT simply retry the same call.',
          plan_file: planPath,
          engine,
          log_tail: tail.slice(-1200),
        }
      }
      // read result
      // D18① (2026-09-26, found by _verify-manual-timeout_): only a result file
      // THIS run wrote may be reported. Without the freshness gate a crashed run
      // silently re-reported the previous run's result.json — the old Rwp came
      // back as if it were the new fit (measured: a stub engine writing nothing
      // returned the earlier run's rwp = 5.0 and ok = true).
      const read = readFreshResult(resultFile, { preMtime, runId: planRunId(planPath) })
      let out = null
      if (read.out) try {
        const j = read.out
        out = {
          mode: 'run',
          rwp: j.metrics?.Rwp, rexp: j.metrics?.Rexp, chi2: j.metrics?.chi2 ?? j.metrics?.Chi2,
          result_file: resultFile,
          plan_file: planPath,
          engine: engine,
          log_tail: tail.slice(-800),
        }
        // A graceful stop leaves a "stopped" result instead of metrics (0.8.5): say
        // so, and declare the field — the host rejects undeclared properties.
        if (j.stopped === true) { out.ok = false; out.stopped = true }
        // `components_file` must be OMITTED when the engine wrote none: the schema
        // declares a string, and `null` is not one (0.8.5 — the same rule this file
        // already applied to health_flags, missed here).
        if (typeof j.components === 'string' && j.components) out.components_file = j.components
        // judgment packet + background provenance (omit when absent: the host
        // schema declares concrete types and must not receive null)
        if (Array.isArray(j.health?.flags) && j.health.flags.length) {
          out.health_flags = j.health.flags
        }
        if (Number.isFinite(j.background?.n_cheb_final)) {
          out.background_order = j.background.n_cheb_final
        }
        if (j.background?.iterated === true) out.background_iterated = true
        if (Number.isFinite(j.background?.boundary_n_cheb)) {
          out.boundary_n_cheb = j.background.boundary_n_cheb
        }
        // baseline R + optimiser status: WITHOUT these a reported "Rwp = 12%"
        // cannot be told apart from "the starting model was already 12%", from a
        // snapshot taken because max_nfev ran out, or from a fit parked on a bound.
        if (Number.isFinite(j.metrics?.baseline_Rwp)) {
          out.baseline_rwp = j.metrics.baseline_Rwp
        }
        const fs = j.fit_status
        if (fs && typeof fs === 'object') {
          out.fit_status = {
            success: fs.success === true,
            nfev: Number.isFinite(fs.nfev) ? fs.nfev : undefined,
            hit_max_nfev: fs.hit_max_nfev === true,
            n_at_bound: Number.isFinite(fs.n_at_bound) ? fs.n_at_bound : undefined,
            covariance_available: fs.covariance_available === true,
            flags: Array.isArray(fs.flags) ? fs.flags : [],
          }
          // drop undefined keys: the host schema must not receive null/undefined
          for (const k of Object.keys(out.fit_status)) {
            if (out.fit_status[k] === undefined) delete out.fit_status[k]
          }
        }
        if (j.r_plateau && typeof j.r_plateau === 'object') {
          out.r_plateau = {
            is_plateau: j.r_plateau.is_plateau === true,
            dof: Number.isFinite(j.r_plateau.dof) ? j.r_plateau.dof : undefined,
            overfit_risk: j.r_plateau.overfit_risk === true,
            basis: typeof j.r_plateau.basis === 'string' ? j.r_plateau.basis : undefined,
          }
          for (const k of Object.keys(out.r_plateau)) {
            if (out.r_plateau[k] === undefined) delete out.r_plateau[k]
          }
        }
      } catch { /* fall through */ }
      // A structured stop that arrived WITHOUT a timeout/cancel (the engine saw a
      // hand-placed flag, or stopped at its own safe point) is still a stop, not an
      // engine fault: return it instead of throwing "did not produce a result".
      if (read.out && read.out.stopped === true) {
        const arts = listArtifacts(runDir)
        return {
          mode: 'run',
          ok: false,
          stopped: true,
          timed_out: false,
          out_dir: runDir,
          artifacts: arts,
          result_file: resultFile,
          error: (typeof read.out.error === 'string' && read.out.error)
            ? read.out.error
            : 'engine stopped on request at a safe point',
          hint: 'RESULT: stopped on request — no R value is expected, and none is missing. '
            + `Partial state is in ${runDir}; re-run the call to get a fit.`,
          plan_file: planPath,
          engine,
          log_tail: tail.slice(-1200),
        }
      }
      if (!out || out.rwp === undefined || out.rwp === null) {
        // Not a timeout: the engine exited without writing a result. Keep the
        // throw (this IS an engine fault), but name the scene like the timeout
        // path does, so the model is not left without paths.
        const arts = listArtifacts(runDir)
        throw new Error(`xrd_refine_manual: engine did not produce a result (exit ${code}).\n`
          + `${diagnosePython(PY)}\n`
          + `artifacts kept in ${runDir} (${arts.length} file(s)): ${arts.slice(0, 20).join(', ')}\n`
          // 双语（2026-09-27）：这句原本只有中文，而同义的英文只出现在**超时路径**的 hint 里
          // （见上面 `RESULT: failed (timeout…)` 那条），两条路径口径不一致。中文原句**保留原样**
          // （verify-manual-timeout.mjs 断言的正是它），英文追加在后面；语言标记的用意与注入行相同：
          // 让主 agent 看出这是**同一条警示的两种语言**，不是两条独立指令。
          + 'language-zh- R 值缺失 ≠ 拟合差：先按 log 判因（撞界 / 模型不合法 / 依赖缺失），'
          + '不要直接当"拟合失败"上报。\n'
          + 'language-en- A missing R value is not a bad fit: read the log first — a parameter sitting on '
          + 'its bound, an invalid model, or a missing dependency — and do not report it as a failed fit.\n'
          + `--- log ---\n${tail}`)
      }
      // The ENGINE'S OWN VERDICT IS AUTHORITATIVE (0.8.6). A custom engine can write
      // `ok:false` (a late write-back or a delivery step failed) while still carrying a
      // numeric Rwp, and this wrapper used to overwrite that with `ok:true` — the third
      // review measured exactly that. Dropping the field keeps the old behaviour.
      if (read.out.ok === false) {
        out.ok = false
        if (typeof read.out.error === 'string' && read.out.error) out.error = read.out.error
      } else if (out.stopped !== true) {
        out.ok = true
      }
      return out
    },
  })
}

export function registerXrdVerify(ctx) {
  ctx.tools.register({
    name: 'xrd_verify_fp2k',
    description:
      'Verify a refinement with FullProf fp2k and compute chemistry/density from a .pcr (occ-normalized). Runs fp2k.exe once on the given pcr (dat must be alongside) in a scratch copy, returns density, cell, Rwp, Bragg R and the refined pcr path. Use for cross-validation of engine results and for publication-grade "refined by FullProf" statements.',
    parameters: {
      type: 'object',
      properties: {
        pcr: { type: 'string', description: 'Absolute path to a FullProf .pcr (occ-normalized; dat in same dir)' },
        out_dir: { type: 'string', description: 'Scratch/output directory (default: session workspace cwd)' },
      },
      required: ['pcr'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          density: { type: 'number' },
          a: { type: 'number' },
          rwp: { type: 'number' },
          rwp_source: { type: 'string', description: 'Which file/definition `rwp` came from (0.8.5). Prefer the .out "Conventional Rietveld" line — the same quantity xrd_refine_auto reports — over the .sum first-match, which is a different number.' },
          rwp_sum: { type: 'number', description: 'The .sum first "Rwp:" value, reported separately so the two definitions can never be confused again (0.8.5)' },
          bragg_r: { type: 'number' },
          out_pcr: { type: 'string' },
          warning: { type: 'array', items: { type: 'string' } },
          log_tail: { type: 'string' },
        },
        required: ['out_pcr'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const v = value ?? {}
        return [{ type: 'text', text: `density = ${v.density ?? 'N/A'} g/cm³  a = ${v.a ?? 'N/A'}  Rwp = ${v.rwp ?? 'N/A'}  BraggR = ${v.bragg_r ?? 'N/A'}`
          + `${v.rwp_source ? `\nRwp source: ${v.rwp_source}` : ''}`
          + `${v.rwp_sum !== undefined && v.rwp_sum !== v.rwp ? `\n(sum reports ${v.rwp_sum} — a different definition, do not compare it with the driver's Rwp)` : ''}`
          + `${v.warning?.length ? `\n⚠ ${v.warning.join('; ')}` : ''}`
          + `\npcr: ${v.out_pcr}${v.log_tail ? `\n--- ---\n${v.log_tail}` : ''}` }]
      },
    },
    timeoutMs: 240_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const pcr = resolve(args.pcr)
      if (!existsSync(pcr)) throw new Error(`xrd_verify_fp2k: pcr not found: ${pcr}`)
      const outDir = artifactDir(args, exec, dirname(pcr))
      mkdirSync(outDir, { recursive: true })
      // scratch: copy pcr + the intensity file with the same stem
      const stem = basename(pcr).replace(/\.pcr$/i, '')
      // EVERY extension the refinement engine accepts (0.8.7). This tool copied only
      // `<stem>.dat`, so verifying a pcr whose pattern is .xra/.fxye/.txt/.xy/.raw failed
      // with "no .sum" even though xrd_refine_auto handles those files fine — the fourth
      // review's finding.
      const VERIFY_DATA_EXTS = ['.dat', '.xra', '.fxye', '.txt', '.xy', '.raw']
      let dat = null
      for (const ext of VERIFY_DATA_EXTS) {
        const cand = join(dirname(pcr), stem + ext)
        if (existsSync(cand)) { dat = cand; break }
      }
      // the same list the engines use for the error message below
      const datForMsg = dat || join(dirname(pcr), stem + '.dat')
      const workDir = join(outDir, `.verify-${stem}`)
      // The previous verification is KEPT, not destroyed (0.8.5): the review found
      // that each call recursively deleted `.verify-<stem>`, so the evidence behind an
      // already-reported number disappeared. One slot is rotated to `.prev-<stem>`
      // (bounded growth), and the fresh directory is what this run's numbers come from.
      const prevDir = join(outDir, `.verify-${stem}.prev`)
      if (existsSync(workDir)) {
        rmSync(prevDir, { recursive: true, force: true })
        try { renameSync(workDir, prevDir) } catch { rmSync(workDir, { recursive: true, force: true }) }
      }
      mkdirSync(workDir, { recursive: true })
      const workPcr = join(workDir, basename(pcr))
      copyFileSync(pcr, workPcr)
      if (dat && existsSync(dat)) copyFileSync(dat, join(workDir, basename(dat)))
      // fp2k binary: shared probe (config.js), so both this tool and the
      // refinement driver resolve the same file with the same error text.
      // findFp2kBinary is platform-aware (fp2k.exe on Windows, fp2k on Linux).
      const fp2k = findFp2kBinary(fp2kRoot())
      await requireExecutionApproval(ctx, exec, 'program', fp2k,
        `run FullProf fp2k for verification: ${fp2k}`)
      const { code, stdout, stderr } = await runCli(fp2k, [stem], workDir, 240_000, exec?.signal)
      const tail = (stdout + '\n' + stderr).slice(-2500)
      // parse .sum for density/cell/Rwp/BraggR — each best-effort and OMITTED
      // from the result when this .sum does not print it (fp2k prints the
      // cell block as "=> Cell parameters :" followed by bare value lines,
      // NOT "a = ...", so cell parsing reads the first value line after the
      // header). Output schema fields stay number-only; absent values are
      // dropped rather than nulled.
      // A6 note: for MULTI-phase .sum only PHASE 1 is parsed (the regexes
      // anchor to "phase 1" / the first Cell parameters / first Rwp / first
      // Bragg R-factor). Multi-phase refinement checks should use the .lst /
      // GSAS-II instead for per-phase metrics.
      const sumPath = join(workDir, stem + '.sum')
      // Everything in workDir was created by THIS call (the directory is fresh), so
      // the .sum either exists because fp2k just wrote it or it does not exist at all.
      const sumExists = existsSync(sumPath)
      if (!sumExists) {
        // A "success" with no .sum was the review's false-success case: exit 0, no
        // numbers, and the tool still returned out_pcr as if verification had passed.
        // This tool exists to produce those numbers, so their absence is a failure.
        throw new Error(`xrd_verify_fp2k: fp2k wrote NO .sum (exit ${code}), so nothing was `
          + `verified — no density, cell, Rwp or Bragg R could be read. The scratch dir is `
          + `kept for inspection: ${workDir}\n--- log ---\n${tail}`)
      }
      let density = null, a = null, rwp = null, bragg = null, rwpSum = null, rwpSource = null
      const outPath = join(workDir, stem + '.out')
      const outTxt = existsSync(outPath) ? readFileSync(outPath, 'utf8') : null
      // LAST occurrence, not the first — the SAME rule the auto driver uses
      // (`fp2k_driver.py`: `m[-1]` for the Rietveld line and `bragg[-1]` for Bragg).
      // 0.8.5 aligned only WHICH FILE is read, and the review of that release measured
      // the remaining half of the defect: one fp2k output whose FINAL Rwp was 16.0 %
      // was reported by this verifier as 29.6 % — its FIRST refinement pass — so a
      // converged run looked like a regression and could flip a rollback decision.
      const lastMatch = (text, re) => {
        const all = [...text.matchAll(new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g'))]
        return all.length ? all[all.length - 1] : null
      }
      const outCount = outTxt ? [...outTxt.matchAll(/Conventional Rietveld Rp,Rwp,Re and Chi2:/g)].length : 0
      if (outTxt) {
        const om = lastMatch(outTxt, /Conventional Rietveld Rp,Rwp,Re and Chi2:\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/)
        if (om) {
          rwp = parseFloat(om[2])
          rwpSource = `out: LAST "Conventional Rietveld Rp,Rwp,Re and Chi2" line (${outCount} present) — `
            + 'the same occurrence xrd_refine_auto reports'
        }
      }
      try {
        const txt = readFileSync(sumPath, 'utf8')
        // every value is the FINAL one, for the same reason as above
        const dm = lastMatch(txt, /density \(volumic mass\) of phase 1 is:\s+([\d.]+)/)
        if (dm) density = parseFloat(dm[1])
        const cm = lastMatch(txt, /=>\s*Cell parameters\s*:\s*\r?\n\s*([\d.]+)/)
        if (cm) a = parseFloat(cm[1])
        const sumCount = [...txt.matchAll(/Rwp:/g)].length
        const rm = lastMatch(txt, /Rwp:\s*([\d.]+)/)
        if (rm) {
          rwpSum = parseFloat(rm[1])
          if (rwp === null) {
            rwp = rwpSum
            rwpSource = `sum: LAST "Rwp:" line (${sumCount} present; no .out Rietveld line found)`
          }
        }
        const bm = lastMatch(txt, /Bragg R-factor:\s*([\d.]+)/)
        if (bm) bragg = parseFloat(bm[1])
      } catch { /* the .sum exists but could not be read — the throw above already covered absence */ }
      const out = { out_pcr: workPcr, log_tail: tail.slice(-800) }
      if (density !== null) out.density = density
      if (a !== null) out.a = a
      if (rwp !== null) out.rwp = rwp
      if (bragg !== null) out.bragg_r = bragg
      if (rwpSource !== null) out.rwp_source = rwpSource
      if (rwpSum !== null) out.rwp_sum = rwpSum
      // A .sum that exists but carries NO metric is not a verification (0.8.5). Measured
      // on the audit fixture: fp2k writes its banner into the .sum even when it cannot
      // open the intensity file, exits 0, and the old code returned that as SUCCESS with
      // every number missing — the review's "false success".
      if (density === null && rwp === null && bragg === null) {
        throw new Error(
          `xrd_verify_fp2k: fp2k ran but reported NO R factor and no density — nothing was `
          + `verified. Its most common cause is that the intensity file could not be opened: `
          + `the pattern must sit next to the .pcr as <stem><ext> (looked for ${basename(datForMsg)}; `
          + `${VERIFY_DATA_EXTS.join('/')} are all accepted). `
          + `Scratch kept for inspection: ${workDir}`
          + `\n--- fp2k log ---\n${tail}`)
      }
      if (code !== 0) {
        out.warning = [`fp2k exited ${code} although it wrote a usable .sum/.out; the numbers below `
          + `come from those files, and the exit code is kept here because a non-zero exit is not a pass`]
      }
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// xrd_refine_gsas — GSAS-II FINAL-OPTIMIZATION driver.
//
// USAGE GATE (user rule 2026-09-06, verbatim intent): "仅作为最终优化使用,
// 不允许在没有使用 fp2k 驱动或手动计算得到前调用 GSAS"。This tool is a
// FINAL-OPTIMIZATION option ONLY: call it only AFTER xrd_refine_auto (fp2k driver)
// or xrd_refine_manual has already produced a converged result for the sample.
// The gate lives at the skill / caller level (documented); the tool itself does
// not hard-gate (user chose document-level constraint).
//
// Drives engine/gsas_engine.py (plan JSON in -> result JSON out, same protocol
// as the manual engine). The GSAS-II install location is resolved at call time
// by gsasRoot(): tool argument -> $DSH_GSAS_ROOT -> xrd.local.json -> probe.
// ---------------------------------------------------------------------------
const BUNDLED_GSAS_ENGINE = join(PLUGIN_ROOT, 'engine', 'gsas_engine.py')
const GSAS_DEFAULT_ROOT = null // resolved on demand by gsasRoot(); see lib/config.js

export function registerXrdGsas(ctx) {
  ctx.tools.register({
    name: 'xrd_refine_gsas',
    description:
      'FINAL-OPTIMIZATION refinement via GSAS-II (scripting interface). HARD USAGE GATE: only after xrd_refine_auto (the headless fp2k driver) or xrd_refine_manual has already converged for this sample — GSAS-II is the final-optimization option, never the first engine (user rule). Drives the bundled gsas_engine.py (plan JSON in / result JSON out). Inputs: raw data file + instrument param file + CIF (creates a project), OR an existing converged .gpx to continue. refine lists parameter families (background/cell/profile/atoms/inst). Extra controls: n_back, inst (e.g. ["U","V","W","X","Y","Zero"]), cif2 (second phase), atoms_map/atoms_flags, size_only, cell2, scale2_init, open2 (post-ladder 2nd-phase scale/cell — never open with main-cell in the same step). Artifacts (.gpx/.lst) kept. Returns { ok, gpx, lst, histograms:[{name,wR}], phases:[{name,cell}], lst_tail, summary, error?, log_tail? }. Requires a GSAS-II install, located via the gsas_root argument / $DSH_GSAS_ROOT / xrd.local.json.',
    parameters: {
      type: 'object',
      properties: {
        data: { type: 'string', description: 'Absolute path to the powder XRD data file (GSAS format .XRA/.dat/.fxye or others readable by fmthint)' },
        instprm: { type: 'string', description: 'Absolute path to the instrument parameter file (.prm / INST_XRY.PRM)' },
        cif: { type: 'string', description: 'Absolute path to the main-phase CIF' },
        cif2: { type: 'string', description: 'Optional second-phase CIF (when a second phase is in the model)' },
        gpx: { type: 'string', description: 'Optional: absolute path of an existing converged GSAS-II project (.gpx) to continue from instead of data+cif' },
        fmthint: { type: 'string', description: 'OPTIONAL reader-family hint passed to GSAS-II as-is (it keeps only importers whose formatName contains this substring, e.g. "GSAS powder", "Topas xye", "FullProf .dat"). LEAVE IT UNSET unless you must pin one reader: GSAS-II then tries every powder reader and keeps the first that works, whereas a WRONG hint makes a readable file fail with "No reader could read file" (measured 2026-09-28: a bare two-column .dat reads with no hint and fails under "GSAS powder").' },
        sample: { type: 'string', description: 'OPTIONAL sample key for the refinement review ledger (review_mechanical_<sample>.json). Set it when the same sample is refined under different file names, so the fp2k and GSAS stages share ONE round counter / freeze state. Default derivation: CIF stem -> data stem -> the existing ledger of the directory when continuing a .gpx -> gpx stem.' },
        refine: { type: 'array', items: { type: 'string', enum: ['background', 'cell', 'profile', 'atoms', 'inst', 'scale'] }, description: 'Parameter families to refine (default: background). FINAL-OPTIMIZATION ladder: background -> inst -> cell -> profile -> scale -> atoms. "scale" = phase fraction (GSAS "Scale"); without a `phases` spec it opens only the 2nd+ phases (legacy), with one it follows the spec per phase.' },
        n_back: { type: 'integer', description: 'Background Chebyshev coefficient count (default 3; backgrounds with real curvature need ~10-15)' },
        inst: { type: 'array', items: { type: 'string' }, description: 'Instrument profile params to refine, e.g. ["U","V","W","X","Y","Zero"] (used with refine containing "inst")' },
        atoms_map: { type: 'string', description: 'Optional JSON string: {atomLabel: flags} e.g. {"Fe1":"XU","K1":"X"} for per-atom control (used with refine "atoms")' },
        atoms_flags: { type: 'string', description: 'Blanket atom flags when no atoms_map (default "XU")' },
        size_only: { type: 'boolean', description: 'profile step refines Size only, not Mustrain (avoid collinear Size/Mustrain with instrument X/Y)' },
        cell2: { type: 'boolean', description: 'true (default): cell refines all phases in one step; false: main phase only' },
        scale2_init: { type: 'number', description: 'Pre-ladder 2nd-phase scale value (refine off until open2)' },
        uiso_floor: { type: 'string', description: 'Optional JSON string for the Uiso LOWER-BOUND clamp: {"enabled":true,"min":0.005,"max_rounds":6}. GSAS has no built-in lower bound, so a refinement can walk a thermal parameter to zero or negative (unphysical, and a classic sign of an over-fitted/badly scaled model). When enabled, after every refinement round each Uiso below "min" (U units, Å²; B = 8π²U so 0.005 U ≈ 0.39 B) is pushed up to "min" and that atom loses its U flag (a clamped value is no longer measured), then the refinement continues until a round finds nothing. DEFAULT OFF: pass {"enabled":true} to switch it on; some models legitimately refine to a very small or negative U (e.g. a split/partially occupied site), so the switch exists to allow that. The final Uiso state is re-measured and reported in uiso_floor.final / n_negative_final, and anomalies raise a warning instead of being silently accepted.' },
        open2: { type: 'array', items: { type: 'string', enum: ['scale2', 'cell2'] }, description: 'POST-ladder: open 2nd-phase scale/cell refinement AFTER main ladder (never in same step as main-cell)' },
        handoff: { type: 'string', description: '0.8.10 — absolute path to a bounded-manual-engine result JSON (result_*.json). Its fitted phase ratio (s2_<i>_scale / scale), refined main-phase cell and the instrument U/V/W/X/Y/Zero it refined are written into the project BEFORE any step runs; Size/Mustrain, background and atom coordinates are NOT copied (different parameterisation / different source of record) and are listed in result.handoff.not_transferred. The design order is: the manual engine settles the two-phase solution, GSAS-II then does the final refinement — without this the manual ratio was silently replaced by 1:1.' },
        entry: { type: 'string', enum: ['hold', 'direct'], description: "0.8.10 — 'hold' (default when continuing an existing gpx or when a hand-off is given): run ONE family first to verify the incoming state, then release the requested families one at a time, KEEPING a step only when it improves wR (result.ladder names what was accepted and what was rolled back). 'direct' = legacy single batch, no rollback. Measured motivation (2026-10-02): a two-phase project entering with a good state was destroyed by the legacy batch (wR 30.42 -> 77.19, cubic cell collapsed)." },
        rollback: { type: 'boolean', description: '0.8.10 — undo a step whose wR does not improve on the best-so-far, and any step GSAS-II itself reports as FAILED (default: true under entry=hold, false under direct). The state is restored from a snapshot COPY of the working .gpx taken before the step — GSAS-II\'s refine() is file-based (Refine(self.filename) + auto-save), so a snapshot written with save(path) would become the working file and be overwritten by the refinement itself (measured 2026-10-02: every rollback then silently restored the rejected state). result.ladder[].rolled_back says what happened, and with rollback on the delivered .gpx is the BEST accepted state, not merely the last one.' },
        rollback_tol: { type: 'number', description: '0.8.10 — tolerance in wR units for "improved" (default 0: any increase is rolled back).' },
        phases: { type: 'string', description: '0.8.10 — JSON string, one entry PER PHASE in cif/cif2 order, e.g. [{"cell":false,"scale":false,"size":false,"mustrain":false}, {"cell":true,"scale":true,"size":true,"mustrain":"isotropic"}]. Fields: cell / scale / size (bool), mustrain (bool or "isotropic"|"uniaxial"|"generalized"), hstrain / pref (bool, declared for the record), atoms ("XU" flags or false). When present every family is emitted PER PHASE so the main phase can be frozen while the second one is released — the GSAS-II tutorial two-phase recipe. ABSENT = legacy behaviour (one global step per family; cell2 / size_only / scale2_init / open2 keep working). NOTE: instrument U/V/W/X/Y/Zero are HISTOGRAM-level in GSAS-II and can never be per phase.' },
        wavelength: { type: 'number', description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): a single monochromatic wavelength in A. Written into the histogram\'s Lam / Lam1+Lam2 slots — for a NEW project right after the histogram is created, and for a continued .gpx on its existing histograms; the engine log always shows 改前 -> 改后. It is NOT a GSAS-II argument: add_powder_histogram has no wavelength keyword (0.9.0 passed one anyway and EVERY new project failed), so the slots are written directly. OMIT it and the project keeps the λ its instrument file carries (byte-identical). Take the value from the data file the user supplied (xrd_data_io `header` dumps its header for you to read). Mutually exclusive with `wavelength_doublet`.' },
        wavelength_doublet: { type: 'array', items: { type: 'number' }, description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): [K-alpha1, K-alpha2, ratio] for a laboratory tube. The histogram must carry Lam1/Lam2 slots — a monochromatic (Lam-only) instrument file takes K-alpha1 only, and the dropped K-alpha2 is reported in the engine log rather than refused. Omit to leave the project\'s own value.' },
        max_cycles: { type: 'integer', description: 'Refinement cycles (default 8)' },
        gsas_root: { type: 'string', description: 'GSAS-II install root. Resolution: this argument, else $DSH_GSAS_ROOT, else "gsas_root" in xrd.local.json, else a conventional probe.' },
        out_dir: { type: 'string', description: 'Output directory for artifacts (default: session workspace cwd)' },
      },
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to, instead of being killed. The result file is a structured "stopped" record, NOT a fit — declare it here or the host rejects the whole payload (0.8.5).' },
          gpx: { type: 'string' },
          lst: { type: 'string' },
          // #1 fix: the engine also reports `residuals` per histogram and
          // `name`/`cell`/`cell_error` per phase; undeclared properties made the
          // host reject the result ("histograms[0].residuals is not a declared
          // property (additionalProperties: false)"). Declare them instead of
          // tightening the schema further.
          histograms: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                wR: { type: 'number' },
                residuals: { type: 'number' },
              },
              additionalProperties: false,
            },
          },
          phases: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                cell: { type: 'array', items: { type: 'number' } },
                cell_keys: { type: 'array', items: { type: 'string' } },
                cell_error: { type: 'string' },
              },
              additionalProperties: false,
            },
          },
          // 0.8.10: entry policy, per-step accept/rollback trace, hand-off report.
          // (null wR values are OMITTED by the engine, never sent as null: the
          // schema grammar here is type/oneOf/... only, so a nullable number would
          // have to be modelled as a nullable type the host may reject.)
          entry: { type: 'string', description: "'hold' = one family first, then stepwise release with rollback; 'direct' = legacy single batch." },
          per_phase: { type: 'boolean', description: 'true when a per-phase `phases` spec was supplied, i.e. every family was emitted per phase.' },
          ladder: {
            type: 'array',
            description: 'One record per refinement step: which family moved, wR before/after, whether it was accepted, and whether a worsening or FAILED step was rolled back. `gsas_error` carries the failure GSAS-II itself reported for that step (a failed step is always rejected and undone, never crowned best), and `cell_change_pct` the per-phase cell parameters that moved more than 2% in it.',
            items: {
              type: 'object',
              properties: {
                step: { type: 'number' },
                label: { type: 'string' },
                wR_before: { type: 'number' },
                wR_after: { type: 'number' },
                accepted: { type: 'boolean' },
                rolled_back: { type: 'boolean' },
                gsas_error: { type: 'string', description: 'Refinement failure reported by GSAS-II for this step (e.g. "Invalid cell metric tensor for phase #1"); present only when the step failed.' },
                cell_change_pct: { type: 'object', additionalProperties: true, description: 'Per phase, the cell parameters that moved more than 2% during this step, as percent change in [a,b,c,alpha,beta,gamma] order.' },
                note: { type: 'string' },
              },
              additionalProperties: false,
            },
          },
          handoff: {
            type: 'object',
            additionalProperties: true,
            description: 'Manual-engine hand-off provenance: source path, what was actually transferred (phase ratio, main-phase cell, instrument parameters), what was deliberately NOT transferred and why, and any warning.',
          },
          lst_tail: { type: 'string' },
          summary: { type: 'string' },
          error: { type: 'string' },
          log_tail: { type: 'string' },
          warning: { type: 'array', items: { type: 'string' } },
          // 0.8.8: which refinement-review ledger this round was booked into
          // (review_mechanical_<review_sample>.json). Surfaced so a split state is
          // visible instead of silent — the e2e run keyed one sample under two names.
          review_sample: { type: 'string', description: 'Sample key of the refinement-review ledger this round was recorded in. Pass `sample` to pin it when the same sample is refined under several file names.' },
          fmthint_used: { type: 'string', description: 'Reader-family hint actually sent to GSAS-II; absent when every powder reader was allowed (the default).' },
          uiso_floor: { type: 'object', additionalProperties: true, description: 'Uiso lower-bound clamp provenance: whether it was enabled, the floor value, per-round clamp events, the FINAL Uiso of every atom (measured, not assumed) and how many are still below the floor or negative.' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? 'GSAS-II refinement OK' : 'GSAS-II FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        if (value.histograms && value.histograms.length) {
          for (const x of value.histograms) lines.push(x.name + ': wR=' + (x.wR ?? '?'))
        } else {
          lines.push('(no histograms)')
        }
        if (value.phases && value.phases.length) {
          for (const p of value.phases) lines.push((p.name || 'phase') + ': ' + JSON.stringify(p.cell ?? p.cell_error ?? ''))
        }
        if (value.summary) lines.push(value.summary)
        // 0.8.10: the per-step ladder, rendered so that a rejected family, an undone
        // step, or a step GSAS-II itself reported as FAILED is visible without
        // digging through the raw ladder array — it is what explains a wR that did
        // not move or a family that was never released.
        if (value.ladder && value.ladder.length) {
          lines.push(`ladder (entry=${value.entry ?? '?'}${value.per_phase ? ', per-phase' : ''}):`)
          for (const s of value.ladder) {
            lines.push(`  ${s.step}. ${s.label}: wR ${s.wR_before ?? '?'} -> ${s.wR_after ?? '?'}`
              + (s.accepted ? ' accepted' : ' rejected')
              + (s.rolled_back ? ' rolled back' : '')
              + (s.gsas_error ? ` | GSAS-II error: ${s.gsas_error}` : ''))
          }
        }
        // The manual-engine hand-off is rendered too: what was seeded, what was
        // deliberately NOT seeded, the manual fit's own Rwp, and any warning (e.g. a
        // phase the manual run fitted to essentially zero, which starts invisible).
        if (value.handoff) {
          lines.push('handoff: ' + ((value.handoff.transferred || []).join('; ') || '(nothing transferred)'))
          if ((value.handoff.not_transferred || []).length) {
            lines.push('  NOT transferred: ' + value.handoff.not_transferred.join('; '))
          }
          if (value.handoff.source_rwp != null) lines.push('  source manual Rwp: ' + value.handoff.source_rwp)
          if ((value.handoff.warnings || []).length) lines.push('  ⚠ ' + value.handoff.warnings.join('; '))
        }
        if (value.uiso_floor && value.uiso_floor.enabled) {
          const uf = value.uiso_floor
          lines.push(`Uiso floor: min=${uf.min} clamped=${uf.n_clamped ?? 0} over ${(uf.rounds || []).length} round(s)`
            + ` | final below floor=${uf.n_below_floor_final ?? '?'} negative=${uf.n_negative_final ?? '?'}`)
        }
        if (value.gpx) lines.push('gpx: ' + value.gpx)
        if (value.lst) lines.push('lst: ' + value.lst)
        // 0.8.8: name the review ledger and the reader hint — a split ledger or a
        // needlessly pinned reader family are both invisible otherwise
        if (value.review_sample) lines.push('review ledger: review_mechanical_' + value.review_sample + '.json'
          + (value.fmthint_used ? ` | fmthint pinned: ${value.fmthint_used}` : ' | fmthint: unset (all readers allowed)'))
        if (value.log_tail) lines.push('--- log ---\n' + String(value.log_tail).slice(-500))
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: GSAS_TOOL_TIMEOUT_MS,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      // usage gate is document-level (skill enforces "after the fp2k driver/manual");
      // engine + skill texts carry it.
      const hasInput = (args.data || args.gpx)
      if (!hasInput) throw new Error('xrd_refine_gsas: need "data" (+instprm/cif) or an existing "gpx" project to continue')
      const outDir = artifactDir(args, exec, dirname(args.data ? resolve(args.data) : resolve(args.gpx)))
      mkdirSync(outDir, { recursive: true })
      // D19 审查子代理的运行参数。**不传 agentOptions**：宿主 resolveChildAgentOptions 以
      // parent.options.{provider,model,maxTokens} 为默认 ⇒ 子代理**继承主对话的路由**
      // （用户："由主对话API直接派生最可用"）。provider 名与超时可用环境变量覆盖。
      // The review state is keyed per SAMPLE (0.8.6): the artifact directory often
      // defaults to the session workspace, so two samples written there shared one
      // round counter and one freeze state.
      //
      // 0.8.8 (e2e 2026-09-28): the key used to be the stem of `gpx` OR `data`, so ONE
      // physical sample produced TWO ledgers inside a single end-to-end run: the call
      // with `data=Y2O3.dat` keyed it `Y2O3`, the next one with the converted
      // `data=Y2O3_gsas.fxye` keyed the SAME sample `Y2O3_gsas`, and continuing from a
      // project would have keyed it `gsas_proj`. Round counts, B/C/D streaks, the E lag
      // and any freeze were therefore split, so the "review every 3 rounds" trigger and
      // the staleness bookkeeping could not work across a real multi-stage run.
      // The derivation itself lives in reviewSampleKey() so it can be tested without
      // GSAS-II; see its doc comment for the order and the reasoning.
      const reviewSample = reviewSampleKey(args, outDir)
      // 0.8.9: a ledger 0.8.7 split off under another file name is reported instead of
      // being silently appended past — see splitLedgerWarning
      const reviewSplitWarning = splitLedgerWarning(outDir, reviewSample)
      const reviewOpts = {
        ctx,
        pluginRoot: PLUGIN_ROOT,
        sample: reviewSample,
        ...(process.env.DSH_XRD_REVIEW_PROVIDER ? { provider: process.env.DSH_XRD_REVIEW_PROVIDER } : {}),
        ...(Number.isFinite(Number(process.env.DSH_XRD_REVIEW_TIMEOUT_MS))
          ? { timeoutMs: Number(process.env.DSH_XRD_REVIEW_TIMEOUT_MS) } : {}),
      }
      const plan = {
        run_id: newRunId(),
        gsas_root: gsasRoot(args.gsas_root),
        // 0.8.8: NO hard-coded fmthint. GSAS-II filters its powder readers by this
        // substring (GSASIIscriptable.import_generic skips every reader whose
        // formatName lacks it), so the old 'GSAS powder' default made EVERY non-GSAS
        // data file unreadable ("No reader could read file") while blaming the file.
        // Measured control (e2e 2026-09-28): the same Y2O3.dat reads as 7351 points
        // through the Topas xye reader with no hint, and FAILS with the hint.
        // Pass `fmthint` only to PIN one reader family.
        data: args.data ? { file: resolve(args.data), fmthint: (typeof args.fmthint === 'string' && args.fmthint.trim()) ? args.fmthint.trim() : null } : null,
        instprm: args.instprm ? resolve(args.instprm) : null,
        cif: args.cif ? resolve(args.cif) : null,
        cif2: args.cif2 ? resolve(args.cif2) : null,
        gpx: args.gpx ? resolve(args.gpx) : null,
        refine: Array.isArray(args.refine) ? args.refine : ['background'],
        n_back: Number.isInteger(args.n_back) ? args.n_back : null,
        inst: Array.isArray(args.inst) ? args.inst : null,
        atoms_map: parseJsonArg(args.atoms_map, 'atoms_map'),
        atoms_flags: args.atoms_flags || null,
        size_only: args.size_only === true ? true : null,
        cell2: args.cell2 === false ? false : null,
        scale2_init: typeof args.scale2_init === 'number' ? args.scale2_init : null,
        uiso_floor: parseJsonArg(args.uiso_floor, 'uiso_floor') || null,
        open2: Array.isArray(args.open2) ? args.open2 : null,
        // ---- 0.8.10: manual-engine hand-off, entry policy, accept/rollback ------
        // `handoff` seeds the project (phase Scale ratio, main-phase cell and the
        // instrument parameters the manual run refined) BEFORE any step runs, and
        // the result reports item by item what crossed and what did not — a phase
        // ratio fitted by the bounded engine used to be silently replaced by 1:1.
        handoff: args.handoff ? resolve(args.handoff) : null,
        // `entry` = 'hold' verifies the incoming state with ONE family first and
        // then releases the requested families one at a time; 'direct' is the
        // legacy single batch. Default is decided in the engine: 'hold' when this
        // call continues an existing .gpx or carries a hand-off, else 'direct'.
        entry: (args.entry === 'hold' || args.entry === 'direct') ? args.entry : null,
        rollback: typeof args.rollback === 'boolean' ? args.rollback : null,
        rollback_tol: Number.isFinite(Number(args.rollback_tol)) ? Number(args.rollback_tol) : null,
        // `phases` = per-phase switches (index = phase order cif → cif2 → ...); when
        // present every family is emitted PER PHASE, so the main phase can be frozen
        // while the second one is released (the GSAS-II tutorial two-phase recipe).
        phases: parseJsonArg(args.phases, 'phases') || null,
        max_cycles: Number.isInteger(args.max_cycles) ? args.max_cycles : 8,
        // 0.9.0 wavelength (EXPLICIT-ONLY): null => the project keeps the λ its
        // instrument file carries. Given => the engine writes the histogram's
        // Lam / Lam1+Lam2 slots itself (a NEW project: right after creation; a
        // CONTINUED gpx: on its existing histograms) and logs 改前 -> 改后 either
        // way. It is deliberately NOT handed to GSAS-II as an argument — that
        // loader has no wavelength keyword (see engine/gsas_engine.py).
        wavelength: (() => {
          if (args.wavelength != null && args.wavelength_doublet != null) {
            throw new Error('xrd_refine_gsas: pass either `wavelength` (a single value) or '
              + '`wavelength_doublet` ([kalpha1, kalpha2, ratio]) — not both')
          }
          if (args.wavelength != null) {
            const v = Number(args.wavelength)
            if (!Number.isFinite(v) || v <= 0) throw new Error('xrd_refine_gsas: `wavelength` must be a positive number (A)')
            return { kalpha1: v, kalpha2: 0, ratio: 0 }
          }
          if (args.wavelength_doublet != null) {
            const d = args.wavelength_doublet
            if (!Array.isArray(d) || d.length < 2) {
              throw new Error('xrd_refine_gsas: `wavelength_doublet` must be [kalpha1, kalpha2, ratio]')
            }
            const [k1, k2, r] = d.map(Number)
            if (!Number.isFinite(k1) || !Number.isFinite(k2) || k1 <= 0 || k2 <= 0) {
              throw new Error('xrd_refine_gsas: `wavelength_doublet` needs two positive wavelengths (A)')
            }
            return { kalpha1: k1, kalpha2: k2, ratio: Number.isFinite(r) ? r : 0.5 }
          }
          return null
        })(),
        out_dir: outDir,
      }
      const planPath = join(outDir, 'gsas_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      // bundled engine script => owned (exempt); still go through approval so
      // a non-whitelisted host keeps the same single-point decision.
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_GSAS_ENGINE,
        `run GSAS-II driver engine: ${BUNDLED_GSAS_ENGINE}`)
      const resultFile = join(outDir, 'gsas_result.json')
      // P0 (0.8.3): snapshot BEFORE the child starts — an earlier run's
      // gsas_result.json must never be reported as this run's result.
      const preMtime = resultMtime(resultFile)
      // A1 fix: runCli(cmd, args, cwd, timeoutMs) — third arg is cwd, NOT signal.
      // Passing exec.signal here crashed spawn ("options.cwd ... AbortSignal").
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_GSAS_ENGINE, planPath, '--out', resultFile], dirname(planPath), {
        timeoutMs: GSAS_RUN_BUDGET_MS,
        fuseMs: GSAS_FUSE_MS,
        signal: exec?.signal,
        backend: 'gsas',
        runId: plan.run_id,
        outDir,
        ctx,
      })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) {
        out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
        // which ledger this round went into + which reader hint was used: both are
        // silent failure modes (split state / over-narrow reader filter) unless named
        if (reviewSample) out.review_sample = reviewSample
        if (plan.data && plan.data.fmthint) out.fmthint_used = plan.data.fmthint
        if (reviewSplitWarning) out.warning = [...(out.warning || []), reviewSplitWarning]
      }
      if (!out) {
        // engine should always write a result; if not, still report + keep artifacts
        onGsasRound({   // 失败也是一轮（计数照记、E 判定照做），但绝不影响上面的返回值
          dir: outDir, agent: exec?.agent ?? null, wR: null, ok: false,
          sample: reviewSample,
        }, reviewOpts)
        return {
          ok: false,
          error: `gsas_engine produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      // D19：一轮 GSAS 结束 ⇒ 记账 + （每 3 轮）标记审查到期 + 注入（E 提示或状态表提醒）；
      // 到期时在**后台**派生审查子代理。注入走 agent.inject(next-step) 送到主对话，
      // **不写进本工具的返回值**（output schema 是 additionalProperties:false）。
      onGsasRound({
        dir: outDir, agent: exec?.agent ?? null,
        wR: (Array.isArray(out.histograms) && out.histograms[0]) ? (out.histograms[0].wR ?? null) : null,
        ok: out.ok !== false,
        sample: reviewSample,
      }, reviewOpts)
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// xrd_refine_multiphase — MULTIPHASE (>=3 phases) GSAS-II entry, with an
// AI-EDITABLE engine template (0.8.10; user decision 2026-10-02).
//
// WHY A SEPARATE TOOL (user's reason, verbatim): "作为新工具，单一工具功能过多AI会
// 错误操作". The 1-2 phase path must stay small and predictable, so the multiphase
// orchestration lives behind its own name instead of adding more branches to
// xrd_refine_gsas.
//
// HARD GATE (user rule): this entry is for >=3 phases only.
//   * tool side: refuses <3 CIFs when building fresh, and refuses any project
//     whose phase count is below 3;
//   * engine side: `engine/gsas_multiphase_template.py` re-checks the count at run
//     time, which is the copy an AI-edited engine cannot bypass.
//   1-2 phases => xrd_refine_gsas (the fixed ladder).
//
// WORKFLOW REQUIREMENT (user, 2026-10-02): before GSAS-II is entered, the TWO most
// abundant phases must already be settled with the bounded manual engine
// (xrd_refine_manual + its `second` block); that result is passed as `handoff`.
// A fresh build without `handoff` is therefore refused, with that reason.
export function registerXrdMultiphase(ctx) {
  const BUNDLED_MULTIPHASE = join(PLUGIN_ROOT, 'engine', 'gsas_multiphase_template.py')
  const MULTIPHASE_MIN_PHASES = 3
  ctx.tools.register({
    name: 'xrd_refine_multiphase',
    description: `MULTIPHASE (>=3 phases) GSAS-II refinement, with an AI-EDITABLE engine. Use it ONLY when the model carries three or more phases; one and two phases must go through xrd_refine_gsas.

WORKFLOW CONTRACT (do not skip): before entering GSAS-II, refine the TWO most abundant phases with the bounded manual engine (xrd_refine_manual, 'second' block) and pass its result JSON as \`handoff\`. A fresh build without \`handoff\` is refused on purpose — starting a 3+ phase least-squares from several unrelated CIFs is the "too many free parameters at once" failure this entry exists to avoid. Continuing an existing project (\`gpx\`) is allowed.

TWO WAYS TO RUN
  * bundled template: omit \`engine\` and the shipped driver runs (it enforces the >=3 phase gate at run time);
  * AI-edited engine: call \`init_engine:true\` first, which copies the template next to your artifacts and returns its path; edit that copy for the material (phase fractions, per-phase profile models, couplings, phases added mid-run), then call again with \`engine:<copy>\`. The copy is NOT plugin-owned, so it asks for execution approval — that is the intended contract, not a bug.

WHAT THE TEMPLATE HANDS BACK: ok/error, phases_count, per-histogram wR, per-phase cells, the step-by-step \`ladder\` (label, wR before/after, accepted, rolled_back) and, when a hand-off was used, exactly what was seeded. Parameter families, n_back, inst, per-phase switches (\`phases\`) and max_cycles are the same vocabulary as xrd_refine_gsas.`,
    parameters: {
      type: 'object',
      properties: {
        gpx: { type: 'string', description: 'Absolute path of an existing GSAS-II project (.gpx) with >=3 phases to continue. Use this instead of data+cifs when a project already exists.' },
        data: { type: 'string', description: 'Powder data file (used when building from scratch, i.e. no gpx)' },
        instprm: { type: 'string', description: 'Instrument parameter file (used when building from scratch)' },
        cifs: { type: 'string', description: 'JSON string: the phase CIF paths, ONE PER PHASE, three or more, e.g. ["a.cif","b.cif","c.cif"]. Fewer than 3 is refused (use xrd_refine_gsas).' },
        handoff: { type: 'string', description: 'REQUIRED for a fresh build — absolute path to the bounded-manual-engine result_*.json produced after refining the TWO most abundant phases. Its phase ratio / main-phase cell / refined instrument parameters are seeded into the project before the ladder; the result reports item by item what crossed.' },
        engine: { type: 'string', description: 'Absolute path of an AI-edited copy of the multiphase template (see init_engine). Omit to run the bundled template.' },
        init_engine: { type: 'boolean', description: 'true = copy the bundled multiphase template into out_dir, return its path for editing and DO NOT refine. Run again with engine=<that path> afterwards.' },
        refine: { type: 'array', items: { type: 'string', enum: ['background', 'cell', 'profile', 'atoms', 'inst', 'scale'] }, description: 'Parameter families (default: background, cell, profile, scale). Emitted PER PHASE.' },
        phases: { type: 'string', description: 'JSON string, one entry per phase (cif order), e.g. [{"cell":false,"scale":false,"size":false}, {"cell":true,"scale":true,"mustrain":"isotropic"}, {"cell":true,"scale":true}]. Same vocabulary as xrd_refine_gsas; omit to let every phase be released.' },
        n_back: { type: 'integer', description: 'Background Chebyshev coefficient count (default 6)' },
        inst: { type: 'array', items: { type: 'string' }, description: 'Instrument profile params to refine, e.g. ["U","V","W","X","Y","Zero"] (histogram-level: never per phase)' },
        max_cycles: { type: 'integer', description: 'Refinement cycles per step (default 8)' },
        rollback_tol: { type: 'number', description: 'Tolerance in wR units for "improved" (default 0: any increase is rolled back).' },
        sample: { type: 'string', description: 'Review-ledger sample key (shared with xrd_refine_gsas so one sample keeps ONE round counter).' },
        fmthint: { type: 'string', description: 'OPTIONAL GSAS-II reader-family hint. Leave unset so every powder reader is tried.' },
        gsas_root: { type: 'string', description: 'GSAS-II install root (argument > $DSH_GSAS_ROOT > xrd.local.json > probe).' },
        out_dir: { type: 'string', description: 'Output directory for artifacts (default: session workspace cwd)' },
      },
      required: [],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          error: { type: 'string' },
          summary: { type: 'string' },
          phases_count: { type: 'number', description: 'Phase count the run actually saw — the hard gate is >=3.' },
          engine_template: { type: 'string', description: 'Path of the AI-editable template copy (returned by init_engine:true; no refinement ran).' },
          gpx: { type: 'string' },
          histograms: {
            type: 'array',
            items: {
              type: 'object',
              properties: { name: { type: 'string' }, wR: { type: 'number' } },
              additionalProperties: false,
            },
          },
          phases: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                cell: { type: 'array', items: { type: 'number' } },
                cell_error: { type: 'string' },
              },
              additionalProperties: false,
            },
          },
          ladder: {
            type: 'array',
            description: 'Step-by-step trace: which family moved, wR before/after, accepted, rolled_back. A step GSAS-II itself reported as FAILED carries `gsas_error`, is always rejected and undone, and can never become the best state.',
            items: {
              type: 'object',
              properties: {
                step: { type: 'number' },
                label: { type: 'string' },
                wR_before: { type: 'number' },
                wR_after: { type: 'number' },
                accepted: { type: 'boolean' },
                rolled_back: { type: 'boolean' },
                gsas_error: { type: 'string', description: 'Refinement failure reported by GSAS-II for this step (e.g. "Invalid cell metric tensor for phase #1"); present only when the step failed.' },
                note: { type: 'string' },
              },
              additionalProperties: false,
            },
          },
          warning: { type: 'array', items: { type: 'string' }, description: 'Problems that leave the DELIVERED state compromised (a failed step that was kept, a rollback that failed, a best-state restore that failed). A step that failed and was undone is reported in `ladder` instead and does not fail the run.' },
          handoff: { type: 'object', additionalProperties: true, description: 'What the manual-engine hand-off seeded, and what it deliberately did not.' },
          review_sample: { type: 'string' },
          fmthint_used: { type: 'string' },
          log_tail: { type: 'string' },
          stopped: { type: 'boolean' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}
        lines.push(value.ok ? 'multiphase GSAS-II refinement OK' : 'multiphase GSAS-II FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.engine_template) {
          lines.push('template for editing: ' + value.engine_template)
          lines.push('(edit it, then call again with engine=<that path>)')
        }
        if (value.phases_count != null) lines.push('phases: ' + value.phases_count)
        if (value.histograms && value.histograms.length) {
          for (const x of value.histograms) lines.push(x.name + ': wR=' + (x.wR ?? '?'))
        }
        if (value.phases && value.phases.length) {
          for (const p of value.phases) lines.push((p.name || 'phase') + ': ' + JSON.stringify(p.cell ?? p.cell_error ?? ''))
        }
        if (value.ladder && value.ladder.length) {
          const kept = value.ladder.filter((r) => r.accepted).length
          lines.push(`ladder: ${value.ladder.length} step(s), ${kept} accepted, ${value.ladder.length - kept} rolled back`)
          for (const r of value.ladder.slice(-6)) {
            lines.push(`  ${r.label}: ${r.wR_before ?? '?'} -> ${r.wR_after ?? '?'}${r.accepted ? '' : (r.rolled_back ? ' (rolled back)' : ' (kept)')}`
              + (r.gsas_error ? ` | GSAS-II error: ${r.gsas_error}` : ''))
          }
        }
        if (value.handoff) {
          lines.push('handoff: ' + ((value.handoff.transferred || []).join('; ') || '(nothing transferred)'))
          if ((value.handoff.not_transferred || []).length) {
            lines.push('  NOT transferred: ' + value.handoff.not_transferred.join('; '))
          }
          if (value.handoff.source_rwp != null) lines.push('  source manual Rwp: ' + value.handoff.source_rwp)
          if ((value.handoff.warnings || []).length) lines.push('  ⚠ ' + value.handoff.warnings.join('; '))
        }
        if (value.summary) lines.push(value.summary)
        return lines
      },
    },
    async execute(args, exec) {
      const outDir = args.out_dir ? resolve(args.out_dir) : (exec?.agent?.session?.header?.cwd || process.cwd())
      mkdirSync(outDir, { recursive: true })

      // ---- init_engine: hand the AI an editable copy, run nothing ------------
      if (args.init_engine === true) {
        const copyPath = join(outDir, 'gsas_multiphase_custom.py')
        copyFileSync(BUNDLED_MULTIPHASE, copyPath)
        return {
          ok: true,
          engine_template: copyPath,
          summary: `multiphase template copied to ${copyPath} — edit it, then call xrd_refine_multiphase with engine=${copyPath}`,
        }
      }

      const cifs = parseJsonArg(args.cifs, 'cifs')
      const gpxIn = args.gpx ? resolve(args.gpx) : null
      // ---- HARD GATE, tool side (1-2 phases => xrd_refine_gsas) --------------
      if (!gpxIn) {
        const n = Array.isArray(cifs) ? cifs.length : 0
        if (n < MULTIPHASE_MIN_PHASES) {
          return {
            ok: false,
            error: `xrd_refine_multiphase is for >=${MULTIPHASE_MIN_PHASES} phases; got ${n} CIF(s). `
              + 'Use xrd_refine_gsas for one or two phases (it owns the fixed 1-2 phase ladder).',
          }
        }
        if (!args.handoff) {
          return {
            ok: false,
            error: 'workflow gate: a fresh multiphase build needs handoff=<manual engine result_*.json>. '
              + 'Refine the TWO most abundant phases with xrd_refine_manual first (its "second" block), '
              + 'then pass that result here — or pass gpx=<existing project> to continue one.',
          }
        }
      }

      const reviewSample = reviewSampleKey(args, outDir)
      const reviewSplitWarning = splitLedgerWarning(outDir, reviewSample)
      const reviewOpts = {
        ctx,
        pluginRoot: PLUGIN_ROOT,
        sample: reviewSample,
        ...(process.env.DSH_XRD_REVIEW_PROVIDER ? { provider: process.env.DSH_XRD_REVIEW_PROVIDER } : {}),
        ...(Number.isFinite(Number(process.env.DSH_XRD_REVIEW_TIMEOUT_MS))
          ? { timeoutMs: Number(process.env.DSH_XRD_REVIEW_TIMEOUT_MS) } : {}),
      }
      const engine = args.engine ? resolve(args.engine) : BUNDLED_MULTIPHASE
      const plan = {
        run_id: newRunId(),
        gsas_root: gsasRoot(args.gsas_root),
        data: args.data ? { file: resolve(args.data), fmthint: (typeof args.fmthint === 'string' && args.fmthint.trim()) ? args.fmthint.trim() : null } : null,
        instprm: args.instprm ? resolve(args.instprm) : null,
        cifs: Array.isArray(cifs) ? cifs.map((c) => resolve(c)) : null,
        gpx: gpxIn,
        handoff: args.handoff ? resolve(args.handoff) : null,
        refine: Array.isArray(args.refine) ? args.refine : ['background', 'cell', 'profile', 'scale'],
        phases: parseJsonArg(args.phases, 'phases') || null,
        n_back: Number.isInteger(args.n_back) ? args.n_back : 6,
        inst: Array.isArray(args.inst) ? args.inst : null,
        max_cycles: Number.isInteger(args.max_cycles) ? args.max_cycles : 8,
        rollback_tol: Number.isFinite(Number(args.rollback_tol)) ? Number(args.rollback_tol) : null,
        out_dir: outDir,
      }
      const planPath = join(outDir, 'multiphase_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      await requireExecutionApproval(ctx, exec, 'script', engine,
        `run the multiphase GSAS-II driver: ${engine}`)
      const resultFile = join(outDir, 'multiphase_result.json')
      const preMtime = resultMtime(resultFile)
      const { code, stdout, stderr } = await runEngine(PY, [engine, planPath, '--out', resultFile], dirname(planPath), {
        timeoutMs: GSAS_RUN_BUDGET_MS,
        fuseMs: GSAS_FUSE_MS,
        signal: exec?.signal,
        backend: 'gsas',
        runId: plan.run_id,
        outDir,
        ctx,
      })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) {
        out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
        if (reviewSample) out.review_sample = reviewSample
        if (plan.data && plan.data.fmthint) out.fmthint_used = plan.data.fmthint
        if (reviewSplitWarning) out.warning = [...(out.warning || []), reviewSplitWarning]
      }
      if (!out) {
        onGsasRound({
          dir: outDir, agent: exec?.agent ?? null, wR: null, ok: false, sample: reviewSample,
        }, reviewOpts)
        return {
          ok: false,
          error: `multiphase engine produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      // a multiphase round IS a GSAS round: same ledger, same 3-round review cadence
      onGsasRound({
        dir: outDir, agent: exec?.agent ?? null,
        wR: (Array.isArray(out.histograms) && out.histograms[0]) ? (out.histograms[0].wR ?? null) : null,
        ok: out.ok !== false,
        sample: reviewSample,
      }, reviewOpts)
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// cif_to_pcr — official FullProf CIF->PCR conversion (user rule 2026-09):
// "从 CIF 生成 pcr 一律经 CIFs_to_PCR 接口，不再手写/拼写结构段"。
// FullProfApp suite ships CIFs_to_PCR.exe next to fp2k.exe:
//   C:\Program Files (x86)\FullProfAPP\resources\fpsuite\windows\CIFs_to_PCR.exe
// It reads a .cif and writes a same-stem .pcr that fp2k can read
// (space group, cell, atoms, site occupancies auto-parsed — verified 2026-09
// on the dsmo example). Exit code 24 is an interactive prompt artifact; the .pcr
// is produced regardless — we verify by file existence.
//
// OCCUPANCY CONVENTION (verified 2026-09-18 on two independent conversions —
// Fm-3m and P 63/m m c):
//     pcr_occ = cif_occ * site_multiplicity / general_position_multiplicity
// e.g. Fm-3m K 0.80 -> 0.03333 (x8/192), C 1.0 -> 0.125 (x24/192); P 63/m m c
// Mn 0.62 -> 0.05167 (x2/24), O 1.0 -> 0.16667 (x4/24). The converter is
// therefore CORRECT and a pcr occupancy far below the CIF value is EXPECTED.
// An earlier version of this audit compared pcr_occ against cif_occ directly and
// reported correct files as "fail" — that was the audit's bug, not the
// converter's, and it is fixed. The audit never rewrites the pcr.
const CIF2PCR_DEFAULT = 'C:\\Program Files (x86)\\FullProfAPP\\resources\\fpsuite\\windows\\CIFs_to_PCR.exe'
const BUNDLED_PCR_CHECK = join(PLUGIN_ROOT, 'engine', 'pcr_check.py')
// 0.9.0: the same module the fp2k side uses; its `setwavelength` mode rewrites the
// pcr's `! Lambda1 Lambda2 Ratio` VALUE line (explicit-only entry).
const BUNDLED_CIF2PCR_HELPER = join(PLUGIN_ROOT, 'engine', 'cif2pcr_helper.py')

function findCif2Pcr() {
  // Resolution chain — now the one the README documents (0.8.5): $DSH_FULLPROF_CIF2PCR
  // -> xrd.local.json "cif2pcr_exe" -> the suite's conventional install path. The
  // local-config step was documented but never read, so a user who followed the README
  // silently got the hardcoded path (the review caught the mismatch).
  const fromEnv = process.env.DSH_FULLPROF_CIF2PCR
  if (fromEnv) return fromEnv
  const fromCfg = localConfig().cif2pcr_exe
  if (fromCfg) return String(fromCfg)
  return CIF2PCR_DEFAULT
}

export function registerCifToPcr(ctx) {
  ctx.tools.register({
    name: 'cif_to_pcr',
    description:
      'Convert a CIF into a FullProf-ready .pcr via the official FullProf CIFs_to_PCR.exe (FullProfApp suite). PREFERRED entry for generating a pcr from a structure — do NOT hand-write or hand-assemble a pcr from scratch when a CIF exists (user rule). Reads the CIF (space group / cell / atoms / site occupancies auto-parsed) and writes a same-stem .pcr next to the CIF (or into out_dir). A site-occupancy AUDIT runs automatically after every conversion and is returned as `occupancy_check`. NOTE the converter writes occupancies in the standard FullProf convention pcr_occ = cif_occ * site_multiplicity / general_multiplicity (verified 2026-09-18: Fm-3m K 0.80 -> 0.03333 = x8/192; P 63/m m c O 1.0 -> 0.16667 = x4/24), so a pcr occupancy far BELOW the CIF value is expected and correct, not an error. The audit compares against that expectation and reports verdict pass/warn/fail (fail = the pcr does not match it and must be reconciled before it is trusted). Returns { ok, pcr, cif_used, occupancy_check, log_tail }. Exit code 24 of the tool is an interactive-prompt artifact; conversion still succeeds — verified by .pcr existence. The subprocess is given an IGNORED stdin (2026-10-05), so that prompt can no longer make the call sit out its budget: measured, an open stdin pipe cost 117 s per call, ignoring it lets the converter exit in ~0.2 s.',
    parameters: {
      type: 'object',
      properties: {
        cif: { type: 'string', description: 'Absolute path to the .cif file' },
        out_dir: { type: 'string', description: 'Output directory for the .pcr (default: same directory as the cif)' },
        tool_path: { type: 'string', description: 'Override CIFs_to_PCR.exe path (default: FullProfApp install; env DSH_FULLPROF_CIF2PCR wins)' },
        wavelength: { type: 'number', description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): a single monochromatic wavelength in A. The pcr\'s `! Lambda1 Lambda2 Ratio` VALUE line is rewritten after conversion and 改前 -> 改后 is returned in `wavelength`. OMIT it and the pcr keeps the converter\'s own default (byte-identical). Take the value from the data file the user supplied (xrd_data_io `header` dumps its header for you to read); never invent one. Mutually exclusive with `wavelength_doublet`.' },
        wavelength_doublet: { type: 'array', items: { type: 'number' }, description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): [K-alpha1, K-alpha2, ratio] for a laboratory tube. Omit to leave the converter\'s value. Mutually exclusive with `wavelength`.' },
      },
      required: ['cif'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          pcr: { type: 'string' },
          cif_used: { type: 'string' },
          cif_copied: { type: 'boolean', description: 'true when the CIF was copied into out_dir under its stem name (the converter resolves the input relative to its cwd). An existing DIFFERENT file of that name is moved aside for the conversion and restored afterwards (0.8.6) — it is never clobbered.' },
          log_tail: { type: 'string' },
          // 0.9.0: present ONLY when `wavelength`/`wavelength_doublet` was passed. It MUST be
          // declared here — the host validates the returned value against this schema with
          // additionalProperties:false, so an undeclared key makes the whole tool call fail
          // ("value.wavelength is not a declared property"). Found by the host end-to-end run
          // on 2026-10-03, i.e. by the one check the isolated suites cannot make.
          wavelength: { type: 'object', additionalProperties: true, description: 'Present ONLY when `wavelength`/`wavelength_doublet` was passed (0.9.0): {ok, changed, before, after, kind, value_line_no, warnings}. before/after are the pcr Lambda1 VALUE lines, so the change is auditable; when the pcr already carried the requested value, changed=false and `note` says so.' },
          occupancy_check: { type: 'object', additionalProperties: true, description: 'Automatic audit after conversion: per-site comparison of the CIF occupancy against the one actually written into the pcr, normalised by site multiplicity (pcr_occ = cif_occ x site_mult / general_mult), with verdict pass/warn/fail/unknown. verdict=fail means the pcr does not match that expectation and must be reconciled before the pcr is trusted; verdict=unknown means the site multiplicity could not be determined, so only the raw factors are reported.' },
        },
        required: ['ok', 'pcr'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        value = value ?? {}   // a failed call has no value; render must not throw
        const chk = value.occupancy_check
        let extra = ''
        if (value.wavelength) {
          const w = value.wavelength
          extra += w.changed
            ? `\nwavelength: pcr Lambda1 value line ${w.before} -> ${w.after} (${w.kind})`
            : `\nwavelength: pcr Lambda1 line NOT changed — ${w.note || (w.warnings || []).join('; ')}`
        }
        if (chk && typeof chk === 'object') {
          // the conversion is correct when factor == expected_factor (site
          // multiplicity / general multiplicity), NOT when factor == 1
          const off = (chk.sites || []).filter(
            (s) => typeof s.relative_deviation === 'number' && s.relative_deviation > 0.02)
          if (chk.verdict === 'pass') {
            extra = `\noccupancy check: pass (${chk.n_cif_sites ?? '?'} site(s) match the CIF, `
              + `multiplicity-normalised${chk.general_multiplicity ? `, general position ${chk.general_multiplicity}` : ''})`
          } else if (chk.verdict === 'warn' || chk.verdict === 'fail') {
            const line = (s) => {
              const exp = (typeof s.cif_occ === 'number' && typeof s.expected_factor === 'number')
                ? (s.cif_occ * s.expected_factor).toPrecision(4) : '?'
              return `  ${s.label}: CIF ${s.cif_occ} -> expected ${exp}` +
                ` (multiplicity factor ${s.expected_factor}), pcr has ${s.pcr_occ} (factor ${s.factor})`
            }
            const rows = off.map(line).join('\n')
            extra = `\n⚠ OCCUPANCY CHECK: ${chk.verdict} — the pcr does not reproduce the CIF `
              + `under the site-multiplicity convention (${(chk.sites || []).length} site(s) compared):\n${rows}\n`
              + '  This site is populated differently from the CIF, so it scatters wrongly. '
              + 'Reconcile the occupancies before trusting this pcr.'
          } else if (chk.verdict === 'unknown') {
            extra = `\noccupancy check: inconclusive — site multiplicities could not be determined`
              + `${chk.general_multiplicity ? ` (general position ${chk.general_multiplicity})` : ''}, `
              + `so only the raw factors are reported (see occupancy_check.sites)`
          } else if (chk.error) {
            extra = `\noccupancy check: unavailable (${chk.error})`
          }
        }
        return [{ type: 'text', text: `${value.ok ? 'CIF -> PCR ok' : 'CIF -> PCR FAILED'}\npcr: ${value.pcr}\ncif: ${value.cif_used}${extra}${value.log_tail ? `\n--- tool output ---\n${value.log_tail.slice(-800)}` : ''}` }]
      },
    },
    timeoutMs: 120_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (typeof args?.cif !== 'string' || !args.cif.trim()) throw new Error('cif_to_pcr: need a "cif" path')
      const cif = resolve(args.cif)
      if (!existsSync(cif)) throw new Error(`cif_to_pcr: cif not found: ${cif}`)
      const tool = args.tool_path || findCif2Pcr()
      if (!existsSync(tool)) throw new Error(`cif_to_pcr: CIFs_to_PCR.exe not found at ${tool} (set DSH_FULLPROF_CIF2PCR or pass tool_path)`)
      const outDir = args.out_dir ? resolve(args.out_dir) : dirname(cif)
      mkdirSync(outDir, { recursive: true })
      // Verified behaviour of CIFs_to_PCR.exe (2026-09-17): it resolves the
      // input relative to its cwd AND writes the .pcr NEXT TO THE INPUT, so the
      // CIF must be present in outDir under its stem name. Handing it a path in
      // another directory produced the misleading "contains nothing for phase
      // # 1" (or left the .pcr in the CIF's own folder and reported failure).
      const stem = basename(cif).replace(/\.cif$/i, '')
      const pcrPath = join(outDir, stem + '.pcr')
      const workCif = join(outDir, stem + '.cif')
      let copiedWork = false
      // A same-named CIF already in out_dir is NOT silently overwritten (0.8.6): if it
      // differs from the CIF being converted it may be an edited copy the user depends
      // on (the third review's finding), so it is moved aside for the conversion and put
      // back afterwards. A failed move is a hard error — clobbering is worse.
      let cifBackup = null
      if (resolve(workCif) !== resolve(cif)) {
        if (existsSync(workCif)) {
          let same = false
          try { same = readFileSync(workCif, 'utf8') === readFileSync(cif, 'utf8') } catch { same = false }
          if (!same) {
            cifBackup = `${workCif}.prev-${Date.now()}`
            try { renameSync(workCif, cifBackup) } catch (e) {
              throw new Error(`cif_to_pcr: ${workCif} already exists, differs from the CIF you passed, `
                + `and could not be moved aside (${String(e).slice(0, 120)}) — refusing to overwrite it. `
                + 'Convert into a different out_dir, or move that file yourself.')
            }
          }
        }
        copyFileSync(cif, workCif)
        copiedWork = true
      }
      const restoreWorkCif = () => {
        if (!cifBackup) return
        const from = cifBackup
        cifBackup = null            // idempotent: also called from a finally (0.8.7)
        try {
          rmSync(workCif, { force: true })
          renameSync(from, workCif)
        } catch { /* keep the .prev- copy rather than losing it */ }
      }
      // A previous .pcr is moved ASIDE, not deleted (0.8.5): the old code removed it
      // before converting, so a conversion that failed (or that CIFs_to_PCR.exe
      // refused) destroyed the file the user already had. If that move FAILS the file is
      // still there and must not be mistaken for this run's output (0.8.6): the third
      // review found that a locked .pcr on Windows then satisfied "a .pcr exists and is
      // non-empty", so a failed conversion reported success. When the move fails we
      // demand PROOF that the file changed (mtime or size) before believing it is new.
      let backup = null
      let backupFailed = false
      const pcrBefore = existsSync(pcrPath) ? statSync(pcrPath) : null
      if (pcrBefore) {
        backup = `${pcrPath}.prev-${Date.now()}`
        try { renameSync(pcrPath, backup) } catch { backup = null; backupFailed = true }
      }
      // The converter is an EXTERNAL executable and `tool_path` lets the caller point at any
      // file (0.8.7): it used to be launched with no approval decision at all, unlike fp2k.
      await requireExecutionApproval(ctx, exec, 'program', tool,
        `run the FullProf CIF->PCR converter: ${tool}`)
      const { code, stdout, stderr } = await runCli(tool, [basename(workCif)], outDir, 110_000, exec?.signal)
      const tail = (stdout + '\n' + stderr).slice(-1500)
      let made = existsSync(pcrPath) && statSync(pcrPath).size > 0
      if (made && backupFailed && pcrBefore) {
        const now = statSync(pcrPath)
        made = now.mtimeMs !== pcrBefore.mtimeMs || now.size !== pcrBefore.size
      }
      if (!made) {
        restoreWorkCif()
        let restored = false
        if (backup) { try { renameSync(backup, pcrPath); restored = true } catch { /* keep the .prev- copy */ } }
        return {
          ok: false, pcr: pcrPath, cif_used: cif, cif_copied: copiedWork,
          log_tail: `tool exit ${code}; no NEW .pcr produced\n${tail}`
            + (backupFailed
              ? `\n(the existing ${basename(pcrPath)} could not be moved aside — it may be locked — and `
                + 'its timestamp and size are unchanged, so it is NOT this run\'s output)'
              : '')
            + (backup ? `\n(previous ${basename(pcrPath)} ${restored ? 'restored' : `kept at ${basename(backup)}`})` : ''),
        }
      }
      if (backup) { try { rmSync(backup, { force: true }) } catch { /* harmless leftover */ } }
      // Occupancy audit (best effort — never blocks a conversion that worked).
      let occupancy
      // 0.9.0 wavelength (EXPLICIT-ONLY): the converter writes its own default
      // Lambda1 value line. When the caller supplies a wavelength we rewrite that
      // one value line (header comment untouched) and report 改前 -> 改后; omitted
      // => the pcr is left exactly as the converter wrote it.
      let wavelengthSet = null
      if (args.wavelength != null || args.wavelength_doublet != null) {
        if (args.wavelength != null && args.wavelength_doublet != null) {
          throw new Error('cif_to_pcr: pass either `wavelength` (a single value) or '
            + '`wavelength_doublet` ([kalpha1, kalpha2, ratio]) — not both')
        }
        let k1; let k2 = null; let ratio = null
        if (args.wavelength != null) {
          k1 = Number(args.wavelength)
          if (!Number.isFinite(k1) || k1 <= 0) throw new Error('cif_to_pcr: `wavelength` must be a positive number (A)')
        } else {
          const d = args.wavelength_doublet
          if (!Array.isArray(d) || d.length < 2) {
            throw new Error('cif_to_pcr: `wavelength_doublet` must be [kalpha1, kalpha2, ratio]')
          }
          k1 = Number(d[0]); k2 = Number(d[1]); ratio = d.length > 2 ? Number(d[2]) : 0.5
          if (!Number.isFinite(k1) || !Number.isFinite(k2) || k1 <= 0 || k2 <= 0) {
            throw new Error('cif_to_pcr: `wavelength_doublet` needs two positive wavelengths (A)')
          }
        }
        const wlJson = join(outDir, stem + '.wavelength.json')
        try { if (existsSync(wlJson)) rmSync(wlJson, { force: true }) } catch { /* best effort */ }
        try {
          await requireExecutionApproval(ctx, exec, 'script', BUNDLED_CIF2PCR_HELPER,
            `set the pcr wavelength line: ${BUNDLED_CIF2PCR_HELPER}`)
          const wlArgs = [BUNDLED_CIF2PCR_HELPER, 'setwavelength', pcrPath, String(k1)]
          if (k2 != null && Number.isFinite(k2)) {
            wlArgs.push(String(k2), Number.isFinite(ratio) ? String(ratio) : '0.5')
          }
          await runCli(PY, [...wlArgs, '--out', wlJson], outDir, 60_000, exec?.signal)
          wavelengthSet = JSON.parse(readFileSync(wlJson, 'utf8'))
        } catch (e) {
          wavelengthSet = { ok: false,
            warnings: [`the wavelength could not be set: ${String(e).slice(0, 200)}`] }
        }
      }
      const checkJson = join(outDir, stem + '.pcr_check.json')
      // 0.8.4: same fixed-name hole as the fp2k .sum above — remove the previous
      // audit so a failed pcr_check cannot leave its verdict to be re-read as this
      // run's (the catch below then reports the failure honestly).
      try { if (existsSync(checkJson)) rmSync(checkJson, { force: true }) } catch { /* best effort */ }
      try {
        // The bundled reader goes through the SAME single-point approval decision as
        // every other script (0.8.5): it used to be launched directly, bypassing the
        // policy entirely (a bundled script is exempt there, so nothing changes for
        // it — but a host that wants to see every execution now sees this one too).
        await requireExecutionApproval(ctx, exec, 'script', BUNDLED_PCR_CHECK,
          `run the bundled pcr reader check: ${BUNDLED_PCR_CHECK}`)
        // THE CONVERTED CIF, not the restored one (0.8.7): the fourth review pointed out
        // that restoring the user's own same-named CIF before this audit made it compare
        // the OLD CIF against the NEW pcr. `workCif` still holds the converted bytes here;
        // the restore happens in the `finally` below.
        await runCli(PY, [BUNDLED_PCR_CHECK, workCif, pcrPath, '--out', checkJson],
          outDir, 90_000, exec?.signal)
        occupancy = JSON.parse(readFileSync(checkJson, 'utf8'))
      } catch (e) {
        occupancy = { ok: false, error: `occupancy audit could not run: ${String(e).slice(0, 200)}` }
      } finally {
        // always put the user's file back, whatever happened above
        restoreWorkCif()
      }
      return { ok: true, pcr: pcrPath, cif_used: cif, cif_copied: copiedWork,
        ...(wavelengthSet ? { wavelength: wavelengthSet } : {}),
        occupancy_check: occupancy, log_tail: tail }
    },
  })
}

// ---------------------------------------------------------------------------
// xrd_finalize — deliverable packager for a CONVERGED GSAS-II project.
//
// This is an OUTPUT MODE, not formatted output: one directory holding a clean
// working .gpx, the refinement .lst, GSAS-generated CIF(s) per phase, a
// publication-style fit figure (de-occluded) and a markdown report. The engine
// never refines and never edits parameters — Uiso / degeneracy repair is a
// planned FUTURE feature and deliberately not part of packaging.
// ---------------------------------------------------------------------------
const BUNDLED_FINALIZE_ENGINE = join(PLUGIN_ROOT, 'engine', 'gsas_finalize.py')

export function registerXrdFinalize(ctx) {
  ctx.tools.register({
    name: 'xrd_finalize',
    description:
      'Package a CONVERGED GSAS-II refinement (.gpx) into a deliverable set — an OUTPUT MODE, not formatted output. Writes <out_dir>/<sample>_final/ containing: a clean working .gpx (stale reflection lists of deleted phases pruned), the .lst refinement log (copied when present; chi**2 and GOF parsed from it), one GSAS-generated CIF per phase (official G2Phase.export_CIF — never hand-written), a publication-style fit figure (observed points / calculated total / background / phase sum, plus a residual panel and one row of per-phase reflection ticks; the strongest peaks carry hkl labels) and a markdown report (metrics, cells with esd, atom parameters, strongest reflections) plus a JSON manifest. OCCLUSION CONTROL: the legend and the statistics box are placed in the emptiest of the four corners by data occupancy, the top of the main panel is padded, and hkl labels are staggered and clipped to avoid mutual overlap. Read-only with respect to the refinement: it never refines and never changes parameters. Returns { ok, package_dir, gpx, lst?, plot, report, cifs, files, metrics, phases, warning, error?, log_tail? }. Requires a GSAS-II install, located via the gsas_root argument / $DSH_GSAS_ROOT / xrd.local.json.',
    parameters: {
      type: 'object',
      properties: {
        gpx: { type: 'string', description: 'Absolute path of the CONVERGED GSAS-II project (.gpx) to package' },
        out_dir: { type: 'string', description: 'Output directory for the package (default: session workspace cwd)' },
        sample: { type: 'string', description: 'Sample name used as the file prefix (default: the gpx file stem). The package is written to <out_dir>/<sample>_final/' },
        title: { type: 'string', description: 'Figure title (default: "<sample> - GSAS-II Rietveld refinement")' },
        formula: { type: 'string', description: 'Chemical formula shown in the statistics box, e.g. Na0.72Ni0.33Mn0.62Mo0.05O2' },
        lst: { type: 'string', description: 'Explicit refinement log (.lst) to copy; default = the same stem as the gpx' },
        dpi: { type: 'integer', description: 'Figure resolution (default 300)' },
        top_peaks: { type: 'integer', description: 'How many strongest reflections get hkl labels (default 6; 0 disables labels)' },
        list_ticks: { type: 'boolean', description: 'Draw the per-phase reflection-tick panel (default true)' },
        xlim: { type: 'array', items: { type: 'number' }, description: 'Optional plot range [2theta_min, 2theta_max]' },
        gsas_root: { type: 'string', description: 'GSAS-II install root. Resolution: this argument, else $DSH_GSAS_ROOT, else "gsas_root" in xrd.local.json, else a conventional probe.' },
        clean_constraints: { type: 'boolean', description: 'Deliverable hygiene: clear stale constraint entries (constraints made from text variable names are stored by ranId and turn into garbage once atoms change, making the GSAS GUI raise "ranId not found"). The project is NOT refined afterwards, so the cached converged wR survives. Default true; pass false to keep them (they are then only reported).' },
      },
      required: ['gpx'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to (0.8.5) — the packager did no work, so nothing can be concluded from a missing package.' },
          package_dir: { type: 'string' },
          gpx: { type: 'string' },
          lst: { type: 'string' },
          plot: { type: 'string' },
          report: { type: 'string' },
          cifs: { type: 'array', items: { type: 'string' } },
          files: { type: 'array', items: { type: 'string' } },
          histogram: { type: 'string' },
          source_gpx: { type: 'string' },
          gsas_version: { type: 'string' },
          constraints: {
            type: 'object',
            properties: {
              hist: { type: 'integer' },
              hap: { type: 'integer' },
              phase: { type: 'integer' },
              cleared: { type: 'boolean' },
            },
            additionalProperties: false,
          },
          reopen: {
            type: 'object',
            properties: {
              phases: { type: 'integer' },
              histograms: { type: 'integer' },
              constraints: { type: 'integer' },
              wR: { type: 'number' },
            },
            additionalProperties: false,
          },
          summary: { type: 'string' },
          error: { type: 'string' },
          log_tail: { type: 'string' },
          warning: { type: 'array', items: { type: 'string' } },
          metrics: {
            type: 'object',
            properties: {
              wR: { type: 'number' },
              R: { type: 'number' },
              Rb: { type: 'number' },
              wRb: { type: 'number' },
              wRmin: { type: 'number' },
              chi2: { type: 'number' },
              gof: { type: 'number' },
            },
            additionalProperties: false,
          },
          phases: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                space_group: { type: 'string' },
                crystal_system: { type: 'string' },
                cell_keys: { type: 'array', items: { type: 'string' } },
                cell: { type: 'array', items: { type: 'number' } },
                cell_sigma: { type: 'array', items: { type: 'number' } },
                bragg_r: { type: 'number' },
                atoms: {
                  type: 'array',
                  items: {
                    type: 'object',
                    properties: {
                      label: { type: 'string' },
                      element: { type: 'string' },
                      refine: { type: 'string' },
                      x: { type: 'number' },
                      y: { type: 'number' },
                      z: { type: 'number' },
                      occ: { type: 'number' },
                      mult: { type: 'integer' },
                      uiso: { type: 'number' },
                    },
                    required: ['label'],
                    additionalProperties: false,
                  },
                },
              },
              required: ['name'],
              additionalProperties: false,
            },
          },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? 'GSAS-II deliverable package OK' : 'GSAS-II finalize FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.summary) lines.push(value.summary)
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        if (value.package_dir) lines.push('package dir: ' + value.package_dir)
        for (const k of ['gpx', 'lst', 'plot', 'report']) {
          if (value[k]) lines.push(k + ': ' + value[k])
        }
        if (value.cifs) for (const c of value.cifs) lines.push('cif: ' + c)
        if (value.metrics) lines.push('metrics: ' + JSON.stringify(value.metrics))
        if (value.log_tail) lines.push('--- log ---\n' + String(value.log_tail).slice(-500))
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 600_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (typeof args?.gpx !== 'string' || !args.gpx.trim()) throw new Error('xrd_finalize: need a "gpx" path')
      const gpx = resolve(args.gpx)
      if (!existsSync(gpx)) throw new Error(`xrd_finalize: gpx not found: ${gpx}`)
      const outDir = artifactDir(args, exec, dirname(gpx))
      mkdirSync(outDir, { recursive: true })
      const plan = {
        run_id: newRunId(),
        gsas_root: gsasRoot(args.gsas_root),
        gpx,
        out_dir: outDir,
        sample: args.sample || null,
        title: args.title || null,
        formula: args.formula || null,
        lst: args.lst ? resolve(args.lst) : null,
        dpi: Number.isInteger(args.dpi) ? args.dpi : null,
        top_peaks: Number.isInteger(args.top_peaks) ? args.top_peaks : null,
        list_ticks: args.list_ticks === false ? false : null,
        clean_constraints: args.clean_constraints === false ? false : null,
        xlim: Array.isArray(args.xlim) ? args.xlim : null,
      }
      const planPath = join(outDir, 'finalize_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      // bundled engine script ⇒ owned; still routed through the single approval
      // decision point so a non-whitelisted host behaves the same way.
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_FINALIZE_ENGINE,
        `run GSAS-II packaging engine: ${BUNDLED_FINALIZE_ENGINE}`)
      const resultFile = join(outDir, 'finalize_result.json')
      const preMtime = resultMtime(resultFile)   // P0 (0.8.3): this run's result only
      // runEngine instead of the legacy runCli wrapper (0.8.6): without outDir the engine
      // had no .xrd-progress.json to publish and no .xrd-stop to watch, and the liveness
      // policy silently fell back to `fixed` — a graceful stop was impossible for this
      // tool even though its engine takes checkpoints.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_FINALIZE_ENGINE, planPath, '--out', resultFile],
        dirname(planPath), {
          timeoutMs: 580_000,
          signal: exec?.signal,
          backend: 'gsas_finalize',
          runId: plan.run_id,
          outDir,
          ctx,
        })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
      if (!out) {
        return {
          ok: false,
          error: `gsas_finalize produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// GSAS TOOL TRIO (user decision 2026-09-17):
//   #1 xrd_refine_gsas  — quick ladder (background -> inst -> cell -> profile -> atoms)
//   #2 xrd_gsas_ops     — every parameter reachable / settable / constrainable
//   #3 xrd_export_cif   — final output: CIF + OPTIONAL physical sanity check
//
// WHY these exist: the PB / layer-oxide campaigns left ~44 one-off GSAS-II
// scripts (gsas_s1..s13, gsas_uequiv, gsas_uclamp, gsas_driver, phys*_save,
// export_s3_cif, ...) - all of them the same few operations. They are now
// declarative ACTIONS/parameters instead of hand-edited script copies.
//
// HARD RULE (user 2026-09-17): unscientific values must always be OUTPUTTABLE.
//   * xrd_gsas_ops never validates values - it reports them;
//   * xrd_export_cif writes the CIF first and only then checks (sanity can be
//     switched off entirely); anomalous values are returned as the diagnostic
//     evidence used to work out why a refinement failed.
// ---------------------------------------------------------------------------
const BUNDLED_GSAS_OPS = join(PLUGIN_ROOT, 'engine', 'gsas_ops.py')
const BUNDLED_GSAS_EXPORT = join(PLUGIN_ROOT, 'engine', 'gsas_export.py')

const OPS_ACTION_HELP =
  'Declarative action list, executed in order. Types: ' +
  '{type:"snapshot"} | {type:"open_uiso",atoms:["all"|labels]} | ' +
  'ATOM SELECTOR of open_uiso / set_adp_mode: "all", ["all"] and omitting "atoms" are all equivalent (= every atom, normalised); a list of labels selects just those, and a label matching nothing is reported back. ' +
  '(Regression 2026-09-25: the documented ["all"] form used to match NO atom and die with "matched no atom".) | ' +
  '{type:"set_adp_mode",mode:"isotropic|anisotropic",atoms:["all"|labels],open_uiso?:true} (WHICH thermal parameter is refined. GSAS-II obeys the per-atom ADP mode MARKER, not the refine flags: with the marker on "A" it drives six Uij even when the flags read "XU", and the Uiso column is only an untouched initial value — so uiso_clamp / fix_uiso / the review block all read the wrong quantity. isotropic also ZEROES the six Uij so a stale negative beta cannot come back; open_uiso (default true) adds the "U" flag) | ' +
  '{type:"set_atoms",all:"XU",atoms:{label:flags},uiso:{label:value},occ:{label:value},pos:{label:[x,y,z]}} | ' +
  '{type:"set_hist",section:"Background|Instrument Parameters|Size|Mustrain|HStrain|Extinction|Pref.Ori.|Limits|Sample Parameters",value:<GSAS value>} (CAVEAT: installing a setting goes through do_refinements(), so ONE refinement step runs as a side effect and the wR it returns belongs to THAT step — a fresh fit of the current state, not a continuation. On a real project consecutive set_* calls were measured jumping 16.56 -> 34.21 -> 19.31 -> 35.11 with no model change; each action record carries wR_kind to mark this. Never read such a jump as "the model got worse") | ' +
  '{type:"add_phase",cif:"<2nd.cif>",scale?:number,vary_scale?:boolean} (add a SECOND PHASE / impurity to the project and link it to every histogram; this is how a two-phase model is built. Its scale is set from "scale" and released for refinement only when vary_scale=true) | ' +
  '{type:"replace_phase",cif:"<clean.cif>",keep_name?:boolean} (swap the project\'s phase(s) for a clean CIF while KEEPING the histograms, background, instrument parameters and size/strain settings — the way to re-enter a structure without redoing the converged setup) | ' +
  '{type:"new_from",save_as:"<path.gpx>",data?:"<other.dat>",fmthint?:string} (derive a NEW project from the current one: save a copy, optionally swap in another data file as the only histogram while keeping the phases — run a new sample against settings that already converged) | ' +
  '{type:"set_site_composition",sites:{label:{element:occ,"..":occ}}} (multi-element site occupancy: GSAS stores one element per atom row, so a mixed site becomes extra rows at the same coordinates — how Mn/Ni/Mo-on-one-site or Fe/Mn disorder is modelled) | ' +
  '{type:"import_background",source:"<bg_nNN.txt | components.json>",n_points:20,vary:false} (import a background curve fitted by the manual engine as GSAS-II\'s native "lin interpolate" background: n equally spaced values across the pattern range, optionally refined. Takes the per-order curve files the background iteration exports, or a components.json) | ' +
  '{type:"auto_background",coeffs:[6,8,10,12,14,16,18,20],iterate:true,with_steps:[<extra GSAS steps per trial>],keep_best:true} (GSAS AUTO background. Default = a SINGLE 6th-order fit; pass iterate:true or an explicit coeffs list ONLY when everything else is optimised and the target is still missed. With iteration it steps by 2 up to 20 and stops at the wR plateau. MUST be the last parameter action) | ' +
  '{type:"set_cell",refine:true} or {type:"set_cell",params:{length_c:{value,vary}}} (same wR caveat as set_hist — the return value is a post-set single-step refit, not comparable with an earlier wR) | ' +
  '{type:"scale",phase:"<name|2nd>",value,vary} | ' +
  '{type:"equiv_uiso",groups:{groupName:[labels]}} | ' +
  '{type:"equiv_afrac",group:[labels],mult:[..]} | ' +
  '{type:"conserve_afrac",group:[labels],total?:number,normalize?:boolean,enable_occupancy_refinement?:boolean} | ' +
  '{type:"check_conservation",groups:[{group:[labels],total?:number}]} | ' +
  '{type:"uiso_clamp",min:0.01,max_rounds:8} | {type:"fix_uiso",atoms:{label:value}} | ' +
  '{type:"clear_constraints"} | {type:"split_site",site:"K2",n:2,frac:0.498,axis:"x"} | ' +
  '{type:"refine",raw:[<raw GSAS recipe>]} | {type:"save"}'

export function registerXrdGsasOps(ctx) {
  ctx.tools.register({
    name: 'xrd_gsas_ops',
    description:
      'GSAS-II FULL-PARAMETER control (tool #2 of the GSAS trio: quick ladder -> full control -> final export). Opens an existing .gpx (or builds one with new_gpx) and applies a DECLARATIVE ACTION LIST: set any histogram section (Background / Instrument Parameters / Size / Mustrain / HStrain / Extinction / Pref.Ori. / Limits / Sample Parameters), any cell parameter, per-atom flags / Uiso / occupancy / coordinates, phase scales, EQUIVALENT-Uiso constraint groups (add_EquivConstr), OCCUPANCY constraints — equiv_afrac TIES occupancies together (add_EquivConstr, the [Fe(CN)6] linkage / split-K pattern) and conserve_afrac HOLDS THE SUM of a group (add_EqnConstr), which is the real fix for "equal Afrac does not constrain the total" (measured: a free 4-K group drifted, the same group conserved held its sum exactly); check_conservation reads the occupancies back after the fact so a held sum is verified rather than assumed, the Uiso-clamping loop, the ADP MODE switch (set_adp_mode — GSAS-II refines whichever thermal parameter the per-atom mode MARKER names, NOT what the refine flags say, so an "A" atom silently refines six Uij while its Uiso column stays stale and escapes every Uiso check; the review block now reports each atom\'s mode and checks the U11/U22/U33 diagonal instead), fixed Uiso, constraint clearing, site splitting (K split-sites), TWO-PHASE work (add_phase links a second phase to every histogram; replace_phase swaps the structure while keeping the converged setup; new_from re-points a project at another data file), multi-element site occupancy (set_site_composition writes a mixed site as extra rows at the same coordinates), importing a manual-engine background curve (import_background → GSAS native "lin interpolate"), and arbitrary raw GSAS refinement recipes. Returns the FULL parameter table (every instrument/background/cell/atom parameter with value + refine flag) plus a reasonableness REVIEW (Uiso/occupancy/wR flags + each atom\'s ADP mode, with anisotropic atoms checked on the U11/U22/U33 diagonal) so that everything is reachable and auditable. BACKGROUND DEFAULTS TO ORDER 6 and background FITTING is done last: auto_background (which fits the background) must be the final parameter action — anything after it is refused; import_background may be followed by other refinements only when vary=false, i.e. the background stays fixed. It NEVER validates values: unscientific results are reported as-is, because that is how a failing refinement is diagnosed. GOTCHA for occupancy constraints: GSAS silently DROPS a constraint whose parameters are not being refined ("Ignoring this constraint; contains no refined parameters"), so both afrac actions set the atom F flag by default (enable_occupancy_refinement:false to keep flags untouched) and warn when the group is already frozen by equivalences. This replaces the one-off gsas_s*.py / gsas_uequiv.py / gsas_uclamp.py / gsas_driver.py scripts.',
    parameters: {
      type: 'object',
      properties: {
        actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: OPS_ACTION_HELP },
        gpx: { type: 'string', description: 'Absolute path of an existing GSAS-II project (.gpx) to open and continue' },
        new_gpx: { type: 'boolean', description: 'true: build a NEW project from data (file + instprm) and cif/cif2 instead of opening an existing one' },
        data: { type: 'string', description: 'Powder data file (used only with new_gpx=true)' },
        fmthint: { type: 'string', description: 'OPTIONAL GSAS-II reader-family hint (keeps only importers whose formatName contains it). Leave UNSET: GSAS-II then tries every powder reader. A wrong hint makes a readable file fail with "No reader could read file" (0.8.8 fix).' },
        instprm: { type: 'string', description: 'Instrument parameter file (used only with new_gpx=true)' },
        cif: { type: 'string', description: 'Main-phase CIF (new_gpx=true)' },
        cif2: { type: 'string', description: 'Optional second-phase CIF (new_gpx=true)' },
        out_dir: { type: 'string', description: 'Output directory (default: session workspace cwd)' },
        save_as: { type: 'string', description: 'Path of the resulting .gpx (default <out_dir>/gsas_ops_out.gpx; an opened project is never overwritten)' },
        max_cycles: { type: 'integer', description: 'GSAS max refinement cycles (default 15)' },
        snapshot_before: { type: 'boolean', description: 'Also return the parameter table as it was BEFORE the actions' },
      },
      required: ['actions'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to (0.8.5). With `partial` this tells "asked to stop, mid-way" apart from "an action failed".' },
          partial: { type: 'boolean', description: 'true when at least one requested action failed (or the final save failed) but the run still produced artifacts. ok=false + partial=true means "some of what you asked for did not happen; inspect actions[].error".' },
          gpx: { type: 'string' },
          converged: { type: 'boolean' },
          summary: { type: 'string' },
          error: { type: 'string' },
          log_tail: { type: 'string' },
          warning: { type: 'array', items: { type: 'string' } },
          histograms: {
            type: 'array',
            items: { type: 'object', additionalProperties: true },
          },
          phases: {
            type: 'array',
            items: { type: 'object', additionalProperties: true },
          },
          actions: {
            type: 'array',
            items: { type: 'object', additionalProperties: true },
          },
          before: { type: 'object', additionalProperties: true },
          review: { type: 'object', additionalProperties: true, description: 'Reasonableness review of the GSAS-II output: verdict pass/warn/fail plus flags for negative/oversized/zero Uiso, out-of-range occupancy and divergent wR. It also carries `adp` (how many atoms are isotropic vs anisotropic) and reports each anisotropic atom\'s U11/U22/U33 diagonal instead of a Uiso it does not have — GSAS-II refines whichever thermal parameter the per-atom mode marker names, so an "A" atom would otherwise escape every Uiso check. verdict=fail means do NOT report this as a finished structure.' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? 'GSAS-II ops OK' : 'GSAS-II ops FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.summary) lines.push(value.summary)
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        if (value.actions) {
          for (const a of value.actions) {
            lines.push(`  ${a.action}: ${a.detail ?? ''}${a.error ? ' [ERROR: ' + a.error + ']' : ''}${a.wR !== undefined ? ' (wR=' + a.wR + ')' : ''}`)
          }
        }
        if (value.phases) {
          for (const p of value.phases) {
            const cell = Array.isArray(p.cell) ? p.cell.map((v) => Number(v).toFixed(4)).join(', ') : '?'
            lines.push(`phase ${p.name} [${p.space_group ?? ''}] cell: ${cell}`)
            for (const at of (p.atoms ?? [])) {
              const u = at.adp === 'A'
                ? `Uij=[${(at.uij ?? []).join(', ')}]`
                : `Uiso=${at.uiso ?? '-'}`
              lines.push(`  ${at.label} ${at.element} flags=${at.flags} adp=${at.adp ?? '?'} occ=${at.occ} ${u}`)
            }
          }
        }
        // The review block is the user-mandated sanity check on what GSAS-II
        // produced; it used to be returned in the JSON but never rendered, so the
        // model could not see the very check it is required to act on.
        if (value.review) {
          const r = value.review
          const adp = r.adp ?? {}
          lines.push(`REVIEW: ${r.verdict ?? '?'} — ADP isotropic=${adp.isotropic ?? '?'} anisotropic=${adp.anisotropic ?? '?'}`)
          for (const f of (r.flags ?? []).slice(0, 20)) {
            lines.push(`  [${f.severity}] ${f.what} @ ${f.where ?? ''}${f.value !== undefined ? ' = ' + f.value : ''}${f.detail ? ' — ' + f.detail : ''}`)
          }
          if ((r.flags ?? []).length > 20) lines.push(`  ... ${r.flags.length - 20} more flag(s)`)
          if (r.note) lines.push('  ' + r.note)
        }
        if (value.gpx) lines.push('gpx: ' + value.gpx)
        if (value.log_tail) lines.push('--- log ---\n' + String(value.log_tail).slice(-400))
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 900_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!Array.isArray(args?.actions) || !args.actions.length) {
        throw new Error('xrd_gsas_ops: "actions" must be a non-empty array')
      }
      if (!args.gpx && args.new_gpx !== true) {
        throw new Error('xrd_gsas_ops: pass "gpx" (open existing) or new_gpx=true + data (+instprm/cif)')
      }
      if (args.gpx) {
        const gpx = resolve(args.gpx)
        if (!existsSync(gpx)) throw new Error(`xrd_gsas_ops: gpx not found: ${gpx}`)
        args = { ...args, gpx }
      }
      const outDir = artifactDir(args, exec, args.gpx ? dirname(resolve(args.gpx)) : process.cwd())
      mkdirSync(outDir, { recursive: true })
      const plan = {
        run_id: newRunId(),
        gsas_root: gsasRoot(args.gsas_root),
        out_dir: outDir,
        gpx: args.gpx ? resolve(args.gpx) : null,
        new_gpx: args.new_gpx === true,
        data: args.data ? { file: resolve(args.data), fmthint: (typeof args.fmthint === 'string' && args.fmthint.trim()) ? args.fmthint.trim() : null } : null,   // 0.8.8: no default hint (see xrd_refine_gsas)
        instprm: args.instprm ? resolve(args.instprm) : null,
        cif: args.cif ? resolve(args.cif) : null,
        cif2: args.cif2 ? resolve(args.cif2) : null,
        save_as: args.save_as ? resolve(args.save_as) : null,
        max_cycles: Number.isInteger(args.max_cycles) ? args.max_cycles : 15,
        snapshot_before: args.snapshot_before === true,
        actions: args.actions,
      }
      const planPath = join(outDir, 'gsas_ops_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_GSAS_OPS,
        `run GSAS-II full-parameter engine: ${BUNDLED_GSAS_OPS}`)
      const resultFile = join(outDir, 'gsas_ops_result.json')
      const preMtime = resultMtime(resultFile)   // P0 (0.8.3): this run's result only
      // runEngine instead of the legacy runCli wrapper (0.8.6): without outDir the engine
      // had no .xrd-progress.json to publish and no .xrd-stop to watch, and the liveness
      // policy silently fell back to `fixed` — a graceful stop was impossible for this
      // tool even though its engine takes checkpoints.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_GSAS_OPS, planPath, '--out', resultFile],
        dirname(planPath), {
          timeoutMs: 880_000,
          signal: exec?.signal,
          backend: 'gsas_ops',
          runId: plan.run_id,
          outDir,
          ctx,
        })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
      if (!out) {
        return {
          ok: false,
          error: `gsas_ops produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      return out
    },
  })
}

export function registerXrdExportCif(ctx) {
  ctx.tools.register({
    name: 'xrd_export_cif',
    description:
      'GSAS-II FINAL OUTPUT (tool #3 of the GSAS trio): export the converged structure to CIF and OPTIONALLY check it. Writes a structured CIF (explicit symmetry operations, Uiso can be made physical via the uiso option) and/or the official GSAS export_CIF, then - only if sanity is enabled - re-reads the CIF with pymatgen to report bond-length statistics, sub-1A contacts, occupancy range violations and optional expected bond windows (e.g. {"C-N":[1.1,1.2], "Fe-C":[1.8,2.05]}). IMPORTANT: the CIF is ALWAYS written before any check, sanity can be switched off (sanity:false), and unscientific values are NEVER blocked - they are returned in "anomalies" (negative/oversized Uiso, out-of-range occupancy, coordinates outside the cell, implausible cell, extreme phase scale, high wR) because those values are exactly the evidence needed to diagnose a failed refinement. A "fail" verdict means "do not report this as a finished structure", not "no output".',
    parameters: {
      type: 'object',
      properties: {
        gpx: { type: 'string', description: 'Absolute path of the .gpx to export from' },
        out_dir: { type: 'string', description: 'Output directory (default: session workspace cwd)' },
        sample: { type: 'string', description: 'File-name prefix (default: gpx stem)' },
        mode: { type: 'string', enum: ['structured', 'official', 'both'], description: 'structured (explicit symmetry ops, Uiso overridable) | official (G2Phase.export_CIF) | both — default structured' },
        sanity: { type: 'boolean', description: 'Run the pymatgen bond/occupancy check (default true). Set FALSE to export without any validation.' },
        diagnostics: { type: 'boolean', description: 'Scan and report anomalous values (default true; independent of sanity)' },
        uiso: { type: 'object', additionalProperties: true, description: '{"mode":"keep"} (default, keeps the refined values even if unphysical) or {"mode":"physical","default":0.01,"by_element":{"K":0.02,"Fe":0.005},"by_atom":{"O6":0.01}}. NOTE: mode=physical rewrites the Uiso in the CIF ONLY — no re-refinement is performed (not practical), so the figure and the wR describe the ORIGINAL gpx model; the returned "calibration" field states exactly what belongs to what, and the ORIGINAL gpx is the project to refine in GSAS-II for the final fit result.' },
        occ_check: { type: 'boolean', description: 'Flag occupancies outside (0,1] (default true)' },
        min_distance: { type: 'number', description: 'Contacts shorter than this (Å) are reported as bad (default 1.0)' },
        bond_expect: { type: 'object', additionalProperties: true, description: 'Optional expected bond windows, e.g. {"C-N":[1.1,1.2],"Fe-C":[1.8,2.05]}; distances outside them are violations' },
        uiso_flag_above: { type: 'number', description: 'Report Uiso above this value as anomalous (default 0.2)' },
        phases: { type: 'array', items: { type: 'string' }, description: 'Phase names to export (default all)' },
        comment: { type: 'string', description: 'Optional comment lines written into the structured CIF header' },
        gsas_root: { type: 'string', description: 'GSAS-II install root. Resolution: this argument, else $DSH_GSAS_ROOT, else "gsas_root" in xrd.local.json, else a conventional probe.' },
        result: { type: 'string', description: 'Source: a manual-engine result.json (from xrd_refine_manual). Its refined "structure" block is turned into a CIF (with a space-group check), the fit figure comes from its components.json, and the same sanity checks run — this is how a manual-engine fit is handed over to GSAS-II or to the delivery path.' },
        components: { type: 'string', description: 'Alternative source: components.json from the manual python engine (x/yobs/ycalc/bg) — use for data that cannot be Rietveld-refined, or to output a manual fit' },
        data: { type: 'string', description: 'Alternative source: raw two-column 2theta-intensity file (plotted without a fit model)' },
        cif: { type: 'string', description: 'Structure file to output / check (a bare CIF works with no project at all)' },
        figure: { type: 'boolean', description: 'Produce the fit figure (default true — a figure is written for EVERY source, so this tool is a complete output port)' },
        title: { type: 'string', description: 'Figure title' },
        formula: { type: 'string', description: 'Formula shown in the statistics box' },
        dpi: { type: 'integer', description: 'Figure resolution (default 300)' },
        top_peaks: { type: 'integer', description: 'How many strongest reflections get hkl labels (default 6; 0 disables)' },
        list_ticks: { type: 'boolean', description: 'Draw the per-phase reflection-tick panel (default true)' },
        xlim: { type: 'array', items: { type: 'number' }, description: 'Optional plot range [2theta_min, 2theta_max]' },
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to (0.8.5) — no figure/report pair was produced for this run.' },
          verdict: { type: 'string', enum: ['pass', 'warn', 'fail', 'skipped'] },
          source: { type: 'string', description: 'gpx | result | components | data | cif' },
          summary: { type: 'string' },
          gpx: { type: 'string', description: 'GSAS project copy written by the port (gpx source only)' },
          figures: { type: 'array', items: { type: 'string' } },
          cifs: { type: 'array', items: { type: 'string' } },
          sanity: { type: 'array', items: { type: 'object', additionalProperties: true } },
          anomalies: { type: 'array', items: { type: 'object', additionalProperties: true } },
          calibration: { type: 'string', description: 'What the figure and the wR actually describe. With uiso.mode=physical the Uiso values are rewritten in the CIF ONLY (no re-refinement is performed), so the figure/wR belong to the ORIGINAL gpx model — report the clamped CIF and the statistics separately, and hand the ORIGINAL gpx to GSAS-II as the refinement project that reaches the final fit result.' },
          warning: { type: 'array', items: { type: 'string' } },
          error: { type: 'string' },
          log_tail: { type: 'string' },
          manual_result: { type: 'object', additionalProperties: true, description: 'result source only: the refined CIF written from the manual-engine result (space_group, n_sites, source_cif, metrics, background provenance).' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? `xrd_export_cif: ${value.verdict ?? 'ok'}` : 'xrd_export_cif FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.summary) lines.push(value.summary)
        if (value.cifs) for (const c of value.cifs) lines.push('cif: ' + c)
        if (value.anomalies && value.anomalies.length) {
          lines.push('anomalies (' + value.anomalies.length + '):')
          for (const a of value.anomalies.slice(0, 25)) {
            lines.push(`  ${a.kind} ${a.phase ?? ''} ${a.atom ?? a.axis ?? a.histogram ?? ''} = ${a.value ?? a.detail ?? ''}`)
          }
        }
        if (value.sanity) {
          for (const s of value.sanity) {
            lines.push(`${s.phase ?? ''} ${s.mode ?? ''}: SG=${s.space_group ?? '?'} atoms=${s.n_atoms ?? '?'}`)
            for (const b of (s.bonds ?? [])) lines.push(`   ${b.pair} n=${b.n} ${b.min}-${b.max} A`)
            for (const b of (s.bad_contacts ?? []).slice(0, 8)) lines.push(`   BAD CONTACT ${b.atoms.join('-')} ${b.d} A`)
            for (const v of (s.bond_violations ?? [])) lines.push(`   BOND VIOLATION ${JSON.stringify(v)}`)
          }
        }
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        if (value.log_tail) lines.push('--- log ---\n' + String(value.log_tail).slice(-400))
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 600_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const hasGpx = typeof args?.gpx === 'string' && !!args.gpx.trim()
      const hasComp = typeof args?.components === 'string' && !!args.components.trim()
      const hasData = typeof args?.data === 'string' && !!args.data.trim()
      const hasCif = typeof args?.cif === 'string' && !!args.cif.trim()
      const hasRes = typeof args?.result === 'string' && !!args.result.trim()
      if (!hasGpx && !hasComp && !hasData && !hasCif && !hasRes) {
        throw new Error('xrd_export_cif: give at least one source — gpx (refined project), result (manual-engine result.json), components (manual-engine json), data (raw pattern) or cif')
      }
      const gpx = hasGpx ? resolve(args.gpx) : null
      if (gpx && !existsSync(gpx)) throw new Error(`xrd_export_cif: gpx not found: ${gpx}`)
      const compPath = hasComp ? resolve(args.components) : null
      const dataPath = hasData ? resolve(args.data) : null
      const cifPath = hasCif ? resolve(args.cif) : null
      const resPath = hasRes ? resolve(args.result) : null
      for (const [k, v] of [['components', compPath], ['data', dataPath], ['cif', cifPath], ['result', resPath]]) {
        if (v && !existsSync(v)) throw new Error(`xrd_export_cif: ${k} not found: ${v}`)
      }
      const outDir = artifactDir(args, exec, dirname(gpx || resPath || compPath || dataPath || cifPath))
      mkdirSync(outDir, { recursive: true })
      const plan = {
        run_id: newRunId(),
        gsas_root: gsasRoot(args.gsas_root),
        gpx,
        result: resPath,
        components: compPath,
        data: dataPath,
        cif: cifPath,
        out_dir: outDir,
        sample: args.sample || null,
        mode: args.mode || 'structured',
        figure: args.figure === false ? false : null,
        title: args.title || null,
        formula: args.formula || null,
        dpi: Number.isInteger(args.dpi) ? args.dpi : null,
        top_peaks: Number.isInteger(args.top_peaks) ? args.top_peaks : null,
        list_ticks: args.list_ticks === false ? false : null,
        xlim: Array.isArray(args.xlim) ? args.xlim : null,
        sanity: args.sanity === false ? false : null,
        diagnostics: args.diagnostics === false ? false : null,
        uiso: args.uiso && typeof args.uiso === 'object' ? args.uiso : null,
        occ_check: args.occ_check === false ? false : null,
        min_distance: typeof args.min_distance === 'number' ? args.min_distance : null,
        bond_expect: args.bond_expect && typeof args.bond_expect === 'object' ? args.bond_expect : null,
        uiso_flag_above: typeof args.uiso_flag_above === 'number' ? args.uiso_flag_above : null,
        phases: Array.isArray(args.phases) ? args.phases : null,
        comment: args.comment || null,
      }
      const planPath = join(outDir, 'export_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_GSAS_EXPORT,
        `run GSAS-II export/sanity engine: ${BUNDLED_GSAS_EXPORT}`)
      const resultFile = join(outDir, 'export_result.json')
      const preMtime = resultMtime(resultFile)   // P0 (0.8.3): this run's result only
      // runEngine instead of the legacy runCli wrapper (0.8.6): without outDir the engine
      // had no .xrd-progress.json to publish and no .xrd-stop to watch, and the liveness
      // policy silently fell back to `fixed` — a graceful stop was impossible for this
      // tool even though its engine takes checkpoints.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_GSAS_EXPORT, planPath, '--out', resultFile],
        dirname(planPath), {
          timeoutMs: 580_000,
          signal: exec?.signal,
          backend: 'gsas_export',
          runId: plan.run_id,
          outDir,
          ctx,
        })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
      if (!out) {
        return {
          ok: false,
          error: `gsas_export produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// xrd_cif_edit - READ / EDIT / BUILD / FETCH CIF files  (user: "关键拟合方法")
//
// Editing a known structure is what cracked the PB case: dropping Sb from the
// literature monoclinic P2_1/c cell took the single-phase fit from 19.4% to
// 14.5%. So this tool lets the AI
//   * READ   a CIF into a readable structure table (space group, cell, sites
//            with occupancies) - so it can reason about someone else's CIF;
//   * EDIT   it - line-level (keeps the original symmetry block, the PB recipe)
//            or structure-level through pymatgen (re-symmetrised on output);
//   * BUILD  a new CIF from parameters taken out of a paper or from a nominal
//            composition (space group + cell + sites), or with build_orbit when
//            the structure deserves a professional construction (one Wyckoff
//            representative per site, expanded by the space group's own orbit);
//   * AUDIT  a hand-built CIF against its own symmetry header (validate) — the
//            case where the database and the literature both failed and the cell
//            had to be typed in by hand (user requirement 2026-09-19);
//   * FETCH  structures from crystal databases: Materials Project (MPRester,
//            needs MP_API_KEY / .pmgrc.yaml) and COD (HTTP search + download).
// The engine script (engine/cif_edit.py) is a FIXED TOOL, deliberately written
// so the AI can edit it: grow validate/build_orbit for the material at hand
// rather than writing a throw-away script. Same idea as the manual-refinement
// engine template. Keep the plan-JSON-in / result-JSON-out protocol when editing.
// Nothing is validated on purpose: a chemically wrong candidate must remain
// writable so it can be tried in a refinement (same red line as xrd_export_cif).
// ---------------------------------------------------------------------------
const BUNDLED_CIF_EDIT = join(PLUGIN_ROOT, 'engine', 'cif_edit.py')

const CIF_ACTION_HELP =
  'Ordered action list. Types: ' +
  '{type:"read",cif} | ' +
  '{type:"fetch_cod",query,max,elements:["Na","Mo","O"]} | ' +
  '{type:"fetch_mp",chemsys:"Na-Mo-O"} or {type:"fetch_mp",formula:"Na2MoO4",max} | ' +
  '{type:"polymorphs",formula:"LiFePO4",max:40,per_polymorph:1} (for highly defective / doped crystals where the phase type cannot be decided: returns ONE CIF per DISTINCT structure type, grouped by (formula, space group), most stable first — then adjudicate them by actually refining, not by theory scores) | ' +
  '{type:"edit_text",cif,out,<changes>} (keeps the original symmetry block) | ' +
  '{type:"edit_structure",cif,out,<changes>} (pymatgen, re-symmetrised) | ' +
  '{type:"build",spacegroup:"P 21/c",cell:{a,b,c,alpha,beta,gamma},sites:[{element,x,y,z,occ}],lattice_from:"cif"} | ' +
  '{type:"build_orbit",spacegroup:"R -3 m",cell:{a,b,c,alpha,beta,gamma},sites:[{element,x,y,z,occ}],symprec} (PROFESSIONAL build: one Wyckoff REPRESENTATIVE per site, the space group\'s own orbit generates the rest; reports the Wyckoff letter + multiplicity pymatgen assigns, so the plan can be checked against the paper before any refinement) | ' +
  '{type:"validate",cif,declared_spacegroup,contact_limit} (AUDIT a hand-written CIF for consistency with the symmetry it declares — use it whenever the databases and the literature both fail and the cell had to be typed in by hand; returns verdict pass|warn with the measured evidence) | ' +
  '{type:"symmetrize",cif,symprec,out}. ' +
  '<changes> = replace_element{"Sb":"Mo"} / rename_sites{"Sb1":"Mo1"} / set_occ{"Sb1":0.5} / ' +
  'set_site_composition{"TM1":{"Mn":0.6,"Ni":0.4}} / set_coord{"O1":[0.1,0.2,0.3]} / ' +
  'set_cell{a,b,c,alpha,beta,gamma} / drop_sites["Sb1"] / add_sites[{label,element,x,y,z,occ}]'

export function registerXrdCifEdit(ctx) {
  ctx.tools.register({
    name: 'xrd_cif_edit',
    description:
      'KEY REFINEMENT METHOD: read, edit, build and fetch CIF files. Use it to (a) READ a CIF into a structure table (space group, cell, sites with occupancy) so the structure can be reasoned about; (b) EDIT an existing structure — element substitution (e.g. drop Sb from a literature cell), occupancy / coordinate / cell changes, site renaming, adding or dropping sites — either line-level (edit_text, which PRESERVES the original symmetry block) or structure-level through pymatgen (edit_structure, re-symmetrised); (c) BUILD a brand-new CIF from parameters read out of a paper or from a nominal composition (space group + cell + sites), or — when the structure must be written more carefully — with build_orbit, which takes ONE WYCKOFF REPRESENTATIVE per site, expands it with the space group\'s own orbit and reports the Wyckoff letter/multiplicity it assigns; (d) FETCH candidate structures from crystal databases: Materials Project (chemsys or formula; needs MP_API_KEY / .pmgrc.yaml) and Crystallography Open Database (free-text query, downloads the CIFs). This is the tool that turns "known structure from literature / database" into a refinable model. For highly defective or doped crystals, where the phase type cannot be decided from the pattern alone, the "polymorphs" action returns one CIF per DISTINCT structure type instead of guessing a single phase. WHEN THE DATABASES AND THE LITERATURE BOTH FAIL and the cell has to be typed in by hand, run the "validate" action on the result BEFORE sending it to a refinement: it audits the file against its own symmetry header and reports verdict pass|warn with measured evidence (a special position typed with a small offset makes the parser fabricate atoms — measured 0.115 A atom overlaps on a real probe). SCOPE OF THAT AUDIT: self-consistency only. NO library can decide whether a structure is chemically right — a cell with a wrong layer spacing is perfectly self-consistent — so only a refinement R value can rule on that. Nothing else is validated on purpose — chemically implausible candidates stay writable so a refinement can test them; check the result afterwards with xrd_export_cif.',
    parameters: {
      type: 'object',
      properties: {
        actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: CIF_ACTION_HELP },
        out_dir: { type: 'string', description: 'Output directory (default: session workspace cwd); fetched database CIFs go to <out_dir>/ref/' },
        sample: { type: 'string', description: 'Prefix for written CIF names (default: derived from the input CIF)' },
        gsas_root: { type: 'string', description: 'Unused placeholder kept for interface symmetry with the other xrd tools' },
      },
      required: ['actions'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to (0.8.5)' },
          // P0 (0.8.5): a batch action that raises no longer leaves ok:true behind.
          // ok=false + partial=true = "some actions ran, some did not";
          // ok=false + partial=false = "nothing that was asked for happened".
          partial: { type: 'boolean', description: 'true when at least one action succeeded while at least one failed (ok is false in that case too). Inspect `failed_actions` and actions[].error.' },
          failed_actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'One entry per action that raised: {index, type, error}. Present only when something failed.' },
          cif: { type: 'string', description: 'Last CIF written or read' },
          summary: { type: 'string' },
          structures: { type: 'array', items: { type: 'object', additionalProperties: true } },
          fetched: { type: 'array', items: { type: 'object', additionalProperties: true } },
          actions: { type: 'array', items: { type: 'object', additionalProperties: true } },
          validations: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'One entry per "validate" action: verdict, declared symbol/number, per-site orbit size, expanded-cell atom count and the measured minimum interatomic contact.' },
          warning: { type: 'array', items: { type: 'string' } },
          error: { type: 'string' },
          log_tail: { type: 'string' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? 'xrd_cif_edit OK' : 'xrd_cif_edit FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.summary) lines.push(value.summary)
        for (const a of (value.actions ?? [])) {
          lines.push(`  ${a.action}: ${a.detail ?? ''}${a.error ? ' [ERROR: ' + a.error + ']' : ''}`)
        }
        for (const s of (value.structures ?? [])) {
          lines.push(`structure ${s.source ?? ''} ${s.space_group ?? '?'} ${s.formula ?? ''} (${s.n_sites ?? '?'} sites)`)
          for (const t of (s.sites ?? []).slice(0, 40)) {
            lines.push(`   ${t.label} ${t.element} occ=${t.occ} (${t.x?.toFixed?.(4)}, ${t.y?.toFixed?.(4)}, ${t.z?.toFixed?.(4)})`)
          }
          if (s.path) lines.push('   file: ' + s.path)
        }
        for (const f of (value.fetched ?? [])) {
          lines.push(`fetched ${f.source} ${f.id} ${f.formula ?? ''} -> ${f.path}`)
        }
        for (const v of (value.validations ?? [])) {
          const ec = v.checks?.expanded_cell ?? {}
          lines.push(`validate ${String(v.file ?? '').split(/[\\/]/).pop()}: ${v.verdict} | declared ${v.declared_symbol ?? '?'} (#${v.declared_number ?? '?'}) | ${v.n_sites_listed ?? '?'} site(s) listed | ${ec.n_atoms ?? '?'} atom(s) in the expanded cell | min contact ${ec.min_contact_A ?? '?'} A`)
          for (const w of (v.warnings ?? [])) lines.push('   ! ' + w)
        }
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        if (value.cif) lines.push('cif: ' + value.cif)
        if (value.log_tail) lines.push('--- log ---\n' + String(value.log_tail).slice(-400))
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 900_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!Array.isArray(args?.actions) || !args.actions.length) {
        throw new Error('xrd_cif_edit: "actions" must be a non-empty array')
      }
      const outDir = artifactDir(args, exec, process.cwd())
      mkdirSync(outDir, { recursive: true })
      const plan = {
        run_id: newRunId(),
        out_dir: outDir,
        sample: args.sample || null,
        actions: args.actions,
      }
      const planPath = join(outDir, 'cif_edit_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_CIF_EDIT,
        `run CIF edit/build/fetch engine: ${BUNDLED_CIF_EDIT}`)
      const resultFile = join(outDir, 'cif_edit_result.json')
      const preMtime = resultMtime(resultFile)   // P0 (0.8.3): this run's result only
      // runEngine instead of the legacy runCli wrapper (0.8.6): without outDir the engine
      // had no .xrd-progress.json to publish and no .xrd-stop to watch, and the liveness
      // policy silently fell back to `fixed` — a graceful stop was impossible for this
      // tool even though its engine takes checkpoints.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_CIF_EDIT, planPath, '--out', resultFile],
        dirname(planPath), {
          timeoutMs: 880_000,
          signal: exec?.signal,
          backend: 'cif_edit',
          runId: plan.run_id,
          outDir,
          ctx,
        })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
      if (!out) {
        return {
          ok: false,
          error: `cif_edit produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// xrd_data_io - the "can we even read the data?" gap (user 2026-09-17: #3
// Mo.raw/.fxye reading, #8 Rigaku raw parsing, peak-region cutting).
// Vendor raw / two-column text / GSAS .fxye / FullProf .dat were previously
// converted by fresh one-off scripts (parse_sampleA.py, convert_electrode_engine.py,
// cut_electrode_engine.py, align_dat.py, fix_step.py, measure_fwhm.py, ...).
// ---------------------------------------------------------------------------
const BUNDLED_DATA_IO = join(PLUGIN_ROOT, 'engine', 'data_io.py')

const DATA_IO_HELP =
  'Ordered action list. Types: ' +
  '{type:"head",file,n} (sniff the format) | ' +
  '{type:"read",file,format:"auto|rigaku|fxye|twocol"} (report points/range/step) | ' +
  '{type:"convert",file,to:"twocol|fxye|dat",out,pad_lines} | ' +
  '{type:"cut",file,ranges:[[lo,hi],...],confirm:true,reason:"<use case>",auto_al:true,out} (data-level peak cutting — GSAS scripted masking does not work. GATED: it really deletes data points, so confirm=true plus a one-line reason are required; never use it as preventive clean-up) | ' +
  '{type:"fwhm",file,peaks:[[lo,hi],...],wavelength} (FWHM per window; >=3 peaks also solves Caglioti U/V/W) | ' +
  '{type:"scherrer",file,peaks:[[lo,hi],...],wavelength:1.54056,K:0.9,inst_broadening:null,caglioti:{U,V,W},inst_mode:"gaussian|lorentzian"} (Scherrer size D = K*lambda/(beta*cos(theta)) per window — the width of an amorphous hump and of an impurity peak are read the same way. With NO instrumental broadening the value is flagged APPARENT; pass inst_broadening=<deg> or caglioti={U,V,W} to subtract it. Every assumption (lambda / K / subtraction mode / Kalpha2 / strain) is echoed back with the number. BOUNDARY: micro-strain separation (Williamson-Hall) is NOT implemented, Kalpha2 is not stripped, and on an amorphous hump D is an apparent coherence length — not a crystallite size) | ' +
  '{type:"align",file,drop_lines:6,out} (FullProf skips a fixed 6 comment lines) | ' +
  '{type:"header",file,max_lines:2000} (POSITIONAL header dump — everything BEFORE the data starts, located as the first run of >=3 consecutive rows carrying >=2 numeric columns; never keyword matching, because real vendor headers look nothing like ours. Returns line-numbered verbatim text plus data_start_line / data_ncols / line_ending / encoding_note / truncated; reading it is the caller\'s job) | ' +
  '{type:"evidence",files:[...],max_lines:400} (the same header regions for SEVERAL files at once — data file + reference + any other input — as raw evidence, one block each, no judgement made)'

export function registerXrdDataIo(ctx) {
  ctx.tools.register({
    name: 'xrd_data_io',
    description:
      'Read, convert, cut and inspect powder-XRD data files — closes the "cannot even read the vendor data" gap. Formats auto-detected: Rigaku RAW/txt (";RAW4.00"/"[RawHeader]" header then a "[Data]" block of "2theta, count" rows), GSAS FXY ("BANK n FXY", centidegrees), and any tolerant two-column text. Actions: head (sniff), read (points/range/step), convert (to twocol / fxye / FullProf dat, with pad_lines for the fixed-6-comment-line skip), cut (GATED: confirm=true plus a one-line reason are required, because it really deletes data points and changes every downstream number — the only working way to remove Al-foil peaks, since GSAS scripted masking is ineffective; auto_al=true uses the usual Al positions), fwhm (half-maximum width per window; with >=3 peaks it also solves the Caglioti U/V/W starting values), scherrer (crystallite/coherence size from those widths, D = K*lambda/(beta*cos(theta)) — the value is flagged APPARENT unless an instrumental broadening is supplied, either as inst_broadening in degrees or as caglioti {U,V,W}; gaussian quadrature subtraction by default, linear for lorentzian; all assumptions are echoed back and micro-strain separation is deliberately out of scope), align (FullProf comment alignment). header/evidence dump a file\'s header POSITIONALLY (everything before the data starts — found by position, not by keywords) so a real vendor header can be read as-is: they are RAW EVIDENCE ONLY, they never parse, interpret or rank anything, and no wavelength is resolved here — the wavelength comes from the input the user supplied (read it from the dumped header) or from an engine default that must be labelled as such. Values pass through unchanged.',
    parameters: {
      type: 'object',
      properties: {
        actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: DATA_IO_HELP },
        out_dir: { type: 'string', description: 'Output directory (default: session workspace cwd)' },
        sample: { type: 'string', description: 'Prefix for written files (default: input file stem)' },
      },
      required: ['actions'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to (0.8.5)' },
          // P0 (0.8.5): a missing input file used to come back as ok:true with zero
          // results — the failure lived only in actions[].error and a warning line.
          partial: { type: 'boolean', description: 'true when at least one action succeeded while at least one failed (ok is false in that case too). Inspect `failed_actions` and actions[].error.' },
          failed_actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'One entry per action that raised: {index, type, error}. Present only when something failed.' },
          file: { type: 'string', description: 'Last file written' },
          summary: { type: 'string' },
          datasets: { type: 'array', items: { type: 'object', additionalProperties: true } },
          files: { type: 'array', items: { type: 'string' } },
          actions: { type: 'array', items: { type: 'object', additionalProperties: true } },
          warning: { type: 'array', items: { type: 'string' } },
          error: { type: 'string' },
          log_tail: { type: 'string' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? 'xrd_data_io OK' : 'xrd_data_io FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.summary) lines.push(value.summary)
        for (const a of (value.actions ?? [])) {
          lines.push(`  ${a.action}: ${a.detail ?? ''}${a.error ? ' [ERROR: ' + a.error + ']' : ''}`)
          for (const l of (a.lines ?? [])) lines.push('     | ' + l)
          for (const f of (a.fwhm ?? [])) lines.push(`     peak ${f.center} FWHM=${f.fwhm ?? 'n/a'} bg=${f.background ?? ''}`)
          if (a.caglioti) lines.push(`     Caglioti U=${a.caglioti.U} V=${a.caglioti.V} W=${a.caglioti.W}`)
          for (const s of (a.scherrer ?? [])) {
            lines.push(`     peak ${s.center} FWHM=${s.fwhm_deg ?? 'n/a'} deg  D=${s.D_nm != null ? s.D_nm + ' nm' : 'n/a'}${s.corrected ? ' [corrected: ' + (s.inst_mode ?? '') + ']' : ' [APPARENT]'}${s.d_spacing ? ' d=' + s.d_spacing + ' A' : ''}`)
            if (s.note) lines.push(`       note: ${s.note}`)
          }
          if (a.assumptions) lines.push(`     assumptions: lambda=${a.assumptions.wavelength_A} A K=${a.assumptions.K} inst=${a.assumptions.inst_source}`)
          if (a.header) {
            const h = a.header
            lines.push(`     header: ${h.header_lines} line(s) before the data` +
              (h.data_found ? ` (data starts at line ${h.data_start_line}, ${h.data_ncols} column(s))` : ' (no data run found — the whole file counts as header)') +
              `${h.truncated ? ' [TRUNCATED at max_lines]' : ''} | ${h.line_ending} | ${h.encoding_note}`)
          }
          for (const e of (a.evidence ?? [])) {
            if (e.error) { lines.push(`     --- ${e.file}: ERROR ${e.error}`); continue }
            lines.push(`     --- ${e.file} — ${e.header_lines} header line(s)` +
              (e.data_found ? `, data starts at line ${e.data_start_line} (${e.data_ncols} col)` : ', no data run found'))
            for (const l of (e.lines ?? [])) lines.push('     | ' + l)
          }
        }
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        if (value.file) lines.push('file: ' + value.file)
        // the engine's own stderr: without it a failed run reports "produced no result"
        // and nothing else (fixed 2026-09-23 — the other nine tools always printed it)
        if (value.log_tail) lines.push('--- engine ---\n' + value.log_tail)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 300_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!Array.isArray(args?.actions) || !args.actions.length) {
        throw new Error('xrd_data_io: "actions" must be a non-empty array')
      }
      const outDir = artifactDir(args, exec, process.cwd())
      mkdirSync(outDir, { recursive: true })
      const plan = { run_id: newRunId(), out_dir: outDir, sample: args.sample || null, actions: args.actions }
      const planPath = join(outDir, 'data_io_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_DATA_IO,
        `run data I/O engine: ${BUNDLED_DATA_IO}`)
      const resultFile = join(outDir, 'data_io_result.json')
      const preMtime = resultMtime(resultFile)   // P0 (0.8.3): this run's result only
      // runEngine instead of the legacy runCli wrapper (0.8.6): without outDir the engine
      // had no .xrd-progress.json to publish and no .xrd-stop to watch, and the liveness
      // policy silently fell back to `fixed` — a graceful stop was impossible for this
      // tool even though its engine takes checkpoints.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_DATA_IO, planPath, '--out', resultFile],
        dirname(planPath), {
          timeoutMs: 280_000,
          signal: exec?.signal,
          backend: 'data_io',
          runId: plan.run_id,
          outDir,
          ctx,
        })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
      if (!out) {
        return {
          ok: false,
          error: `data_io produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      return out
    },
  })
}

// ---------------------------------------------------------------------------
// xrd_phase_screen - "which phase explains the leftover peaks?"  The residual
// extraction + candidate scoring that the PB / layer-oxide campaigns did with
// extract_residual_engine.py + score_candidates_engine.py + mp_residual_screen.py.
// ---------------------------------------------------------------------------
const BUNDLED_PHASE_SCREEN = join(PLUGIN_ROOT, 'engine', 'phase_screen.py')

const PHASE_SCREEN_HELP =
  'Ordered action list. Types: ' +
  '{type:"residuals",gpx,min_rel:0.03,merge:0.2,top:30} (leftover obs-calc peaks of a converged project) | ' +
  '{type:"peaks",file,smooth:15} (peak picking on a raw pattern) | ' +
  '{type:"fetch",mp:{chemsys:"Na-Mo-O",max:10}} or {type:"fetch",cod:{query:"Na2MoO4",max:10,elements:["Na","Mo","O"]}} | ' +
  '{type:"screen",candidates:[...]|dir:...,residuals:[...]|gpx:...,tol:0.25,wavelength:1.54056,min_rel:0.02}'

export function registerXrdPhaseScreen(ctx) {
  ctx.tools.register({
    name: 'xrd_phase_screen',
    description:
      'Find which candidate phase explains the peaks a refinement failed to model (phase identification, workflow stage 3). Extracts the residual (observed - calculated) peaks from a converged GSAS project — or picks peaks from a raw pattern when no model exists — then scores candidate CIFs against that list: every theoretical reflection (pymatgen XRDCalculator, relative intensity >= min_rel) matched to a residual peak within tol degrees contributes rel_intensity * (residual_height / max_residual). Candidates can be given directly, taken from a directory, or FETCHED from Materials Project / COD in the same call. Returns the ranked table (score / n_matches / per-reflection matches) plus the residual peak list. The ranking is evidence, not a verdict: a good theoretical match does NOT guarantee a two-phase refinement will converge, and a weak match set means the hypothesis is unsupported.',
    parameters: {
      type: 'object',
      properties: {
        actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: PHASE_SCREEN_HELP },
        out_dir: { type: 'string', description: 'Output directory (default: session workspace cwd); fetched CIFs go to <out_dir>/ref/' },
        gsas_root: { type: 'string', description: 'GSAS-II install root. Resolution: this argument, else $DSH_GSAS_ROOT, else "gsas_root" in xrd.local.json, else a conventional probe.' },
      },
      required: ['actions'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          stopped: { type: 'boolean', description: 'true when the engine stopped itself at a safe point because it was asked to (0.8.5)' },
          // P0 (0.8.5): pick-peaks on a missing file used to return ok:true with no
          // peaks; the failure was visible only in actions[].error.
          partial: { type: 'boolean', description: 'true when at least one action succeeded while at least one failed (ok is false in that case too). Inspect `failed_actions` and actions[].error.' },
          failed_actions: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'One entry per action that raised: {index, type, error}. Present only when something failed.' },
          summary: { type: 'string' },
          residual_peaks: { type: 'array', items: { type: 'object', additionalProperties: true } },
          candidates: { type: 'array', items: { type: 'object', additionalProperties: true } },
          actions: { type: 'array', items: { type: 'object', additionalProperties: true } },
          warning: { type: 'array', items: { type: 'string' } },
          error: { type: 'string' },
          log_tail: { type: 'string' },
        },
        required: ['ok'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        const lines = []
        value = value ?? {}   // a failed call has no value; render must not throw
        lines.push(value.ok ? 'xrd_phase_screen OK' : 'xrd_phase_screen FAILED')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.summary) lines.push(value.summary)
        if (value.residual_peaks && value.residual_peaks.length) {
          lines.push('residual peaks: ' + value.residual_peaks
            .map((p) => `${p.two_theta}(${p.residual})`).join(' '))
        }
        for (const a of (value.actions ?? [])) {
          lines.push(`  ${a.action}: ${a.detail ?? ''}${a.error ? ' [ERROR: ' + a.error + ']' : ''}`)
        }
        for (const c of (value.candidates ?? [])) {
          lines.push(`  score=${c.score} n=${c.n_matches} ${c.formula ?? ''} ${c.cif ?? ''}`)
          for (const m of (c.matches ?? []).slice(0, 6)) {
            lines.push(`     ${m.pred_2theta} (rel ${m.pred_rel_intensity}) -> residual ${m.residual_2theta} h=${m.residual_height} d=${m.delta}`)
          }
        }
        if (value.warning && value.warning.length) lines.push('WARNING: ' + value.warning.join('; '))
        // the engine's own stderr: without it a failed run reports "produced no result"
        // and nothing else (fixed 2026-09-23 — the other nine tools always printed it)
        if (value.log_tail) lines.push('--- engine ---\n' + value.log_tail)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 900_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (!Array.isArray(args?.actions) || !args.actions.length) {
        throw new Error('xrd_phase_screen: "actions" must be a non-empty array')
      }
      const outDir = artifactDir(args, exec, process.cwd())
      mkdirSync(outDir, { recursive: true })
      // GSAS-II is needed ONLY by the actions that read a .gpx (residuals, or a screen
      // step given a gpx). Demanding it at plan-build time made the pattern-only peak
      // picker fail with "cannot find GSAS-II" before the engine ever saw the plan —
      // the third review's finding; the engine side was made lazy in 0.8.5, the tool
      // layer was missed. Now it is resolved only when such an action is present.
      const needsGsas = (args.actions || []).some(
        (a) => a && (a.type === 'residuals' || (a.type === 'screen' && a.gpx)))
      const plan = {
        run_id: newRunId(),
        out_dir: outDir,
        gsas_root: needsGsas ? gsasRoot(args.gsas_root) : (args.gsas_root ? resolve(args.gsas_root) : null),
        actions: args.actions,
      }
      const planPath = join(outDir, 'phase_screen_plan.json')
      writeFileSync(planPath, JSON.stringify(plan, null, 1))
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_PHASE_SCREEN,
        `run phase-screening engine: ${BUNDLED_PHASE_SCREEN}`)
      const resultFile = join(outDir, 'phase_screen_result.json')
      const preMtime = resultMtime(resultFile)   // P0 (0.8.3): this run's result only
      // runEngine instead of the legacy runCli wrapper (0.8.6): without outDir the engine
      // had no .xrd-progress.json to publish and no .xrd-stop to watch, and the liveness
      // policy silently fell back to `fixed` — a graceful stop was impossible for this
      // tool even though its engine takes checkpoints.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_PHASE_SCREEN, planPath, '--out', resultFile],
        dirname(planPath), {
          timeoutMs: 880_000,
          signal: exec?.signal,
          backend: 'phase_screen',
          runId: plan.run_id,
          outDir,
          ctx,
        })
      const tail = (stdout + '\n' + stderr).slice(-3000)
      const fresh = readFreshResult(resultFile, { preMtime, runId: plan.run_id })
      const out = fresh.out
      if (out) out.log_tail = (out.log_tail || '') + '\n' + tail.slice(-1500)
      if (!out) {
        return {
          ok: false,
          error: `phase_screen produced no result (exit ${code}; ${fresh.reason})\n${diagnosePython(PY)}`,
          log_tail: tail.slice(-2000),
        }
      }
      return out
    },
  })
}

/**
 * Internals exposed for the audit suites only (0.8.8). `readCollected` and the ledger
 * key derivation are pure functions whose failure modes are invisible from the tool
 * boundary — a wrong stdout coercion shows up only as the string "[object Object]" in a
 * render, and a wrong ledger key only as a second state file on disk. Both are now
 * regression-tested directly (verify-review-key.mjs) instead of by eyeballing output.
 */
export const _internals = { readCollected, safeStem, soleLedgerSample, reviewSampleKey, splitLedgerWarning }
