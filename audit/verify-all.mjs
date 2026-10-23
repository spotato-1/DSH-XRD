// verify-all.mjs — run the self-contained part of the audit module in one shot.
//
// WHY a runner: the scripts are deliberately separate (each pins down one thing), but a
// transplant needs one command whose exit code means "this package is intact here". This
// runs the checks that need NO external input — an absent GSAS-II or reference project
// must not make the package look broken, and those scripts already skip cleanly.
//
// It does NOT hide anything: every child's output goes to the terminal, and the summary
// repeats each child's own FAIL/WARN/SKIP line so a skipped check is never read as a pass.
//
// Usage:  node audit/verify-all.mjs [--root <plugin dir>]
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { python } from './_lib.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))

// Python-side controls (py/*.py) are real members of this suite: ctrl-fp2k-orphan
// reproduces the orphan incident and was NOT in the default list until
// 2026-09-28, which is exactly how "the fp2k layer is protected" went unchecked.
let PY = null
try { PY = python() } catch { PY = null }

// Order matters: integrity first (it needs nothing and explains a lot), then the
// self-contained tool-layer checks. `timeout` is in ms and is generous where a real
// refinement runs — the default spawnSync timeout is NONE, and an earlier version of this
// runner let the fp2k ladder step hang indefinitely instead of failing.
const STEPS = [
  ['integrity-check.mjs', 'logic chain + engine integrity', 120_000],
  ['verify-cif-edit.mjs', 'xrd_cif_edit actions + CIF validation', 300_000],
  ['verify-e2e.mjs', 'build_orbit -> cif_to_pcr -> refine chain', 300_000],
  // heavy: a real ladder on the fixture pattern
  ['verify-autofp-replaced.mjs', 'xrd_refine_auto runs the bundled fp2k driver', 900_000],
  ['verify-fp2k-check.mjs', 'xrd_verify_fp2k resolves fp2k', 300_000],
  // these SKIP cleanly without their external inputs (see AUDIT.md)
  ['verify-gsas-ops.mjs', 'gsas_ops two-phase / mixed-site actions', 300_000],
  ['verify-manual-mapping.mjs', 'manual-engine mapping (skips without a prior run)', 60_000],
  // self-contained and quota-free: stub agents only, and the manual engine against real
  // short-lived child processes (the budget is lowered through its own env override)
  ['verify-review-detector.mjs', 'self-review state machine (stub agents, no quota)', 180_000],
  ['verify-manual-timeout.mjs', 'manual-engine budget / crash / stale-result paths', 180_000],
  // P0/P1 (0.8.3): result freshness (run_id + mtime gate) and process supervision
  // (per-backend liveness, graceful stop, tree kill, NaN detection)
  ['verify-result-freshness.mjs', 'only a result THIS run wrote may be reported', 180_000],
  ['verify-proc-supervision.mjs', 'per-backend heartbeat / stop / tree kill / NaN', 300_000],
  // 0.8.8: the two silent failure modes found by the host-level e2e run — collected
  // output read through Object.prototype.toString, and a pinned GSAS reader hint — plus
  // the review-ledger sample key (pure functions, no child processes needed)
  ['verify-review-key.mjs', 'collected output / GSAS reader hint / ledger sample key', 120_000],
  // 0.8.10: the multiphase entry — the >=3-phase gate, the "no fresh build without a hand-off"
  // workflow gate, the init_engine hand-over, and the two one-line DOA regressions the smoke
  // test found (run_guarded_main kwargs, missing GSAS sys.path bootstrap). Tool-layer only:
  // no GSAS-II, no python, no quota.
  ['verify-multiphase.mjs', 'multiphase gates + editable-hook contract (no GSAS needed)', 120_000],
  // 0.9.0: the λ (wavelength) contract of the four write entry points — default-off asserted
  // per writer, the pcr VALUE line rewritten on a real shipped pcr (everything else byte-equal,
  // line endings kept), the target table vs GSAS-II's own values, and `header` staying
  // positional on hostile headers. No fp2k, no GSAS-II, no refinement; python is used directly
  // (the sandbox denies the piped stdio the tool layer needs — that one check says so and falls back).
  ['verify-wavelength.mjs', 'wavelength (λ): four entries, default-off, pcr writer, header', 120_000],
  // python-side control: the orphan incident, reproduced (needs DSH_XRD_SRC in a package)
  ['py/ctrl-fp2k-orphan.py', 'fp2k orphan guard: A/B/C/D discriminable control', 300_000],
]
// NOT in the default list on purpose:
//   verify-package.mjs          — only meaningful for a release tree; run it there
//   verify-manual-fitstatus.mjs — runs the manual engine for several minutes
//   verify-y2o3.mjs / struct-check.mjs — need an external reference project

// pass through --root unless the caller already set DSH_XRD_ROOT
const passArgs = []
const i = process.argv.indexOf('--root')
if (i >= 0 && process.argv[i + 1]) passArgs.push('--root', process.argv[i + 1])
const env = { ...process.env }
// 2026-10-05: a full gate run left `engine/__pycache__` behind (fp2k_guard/proc_guard/
// result_io) because the python steps — and the python engines a JS step spawns — were
// started without `-B`. `-B` on one interpreter cannot cover the children it spawns, but
// this env var is INHERITED all the way down, so one setting keeps the plugin tree clean
// for every step (a dirty tree is what makes three-copy-check and the packagers lie).
env.PYTHONDONTWRITEBYTECODE = '1'
if (passArgs.length) env.DSH_XRD_ROOT = process.argv[i + 1]

const results = []
// Sentinel for "this runner skipped it", deliberately NOT a code a child could return:
// a child exiting 2 (e.g. those scripts' own "no plugin tree" guard) used to be reported
// as MISSING, which sent the reader looking for a file that was right there (measured
// 2026-10-02 in a package tree). A child's own exit codes are now reported as such.
const RUNNER_SKIPPED = 42
for (const [script, what, timeout] of STEPS) {
  const p = join(HERE, script)
  if (!existsSync(p)) { results.push({ script, code: RUNNER_SKIPPED }); continue }
  const isPy = script.endsWith('.py')
  if (isPy && !PY) { results.push({ script, code: 4 }); continue }
  console.log(`\n${'═'.repeat(72)}\n▶ ${script} — ${what}\n${'═'.repeat(72)}`)
  const r = spawnSync(isPy ? PY : process.execPath, [p, ...passArgs],
    { stdio: 'inherit', env, timeout })
  // a timeout kills the child: report it as a failure, never as a pass
  const timedOut = r.error && r.error.code === 'ETIMEDOUT'
  if (timedOut) console.log(`\n!! ${script} TIMED OUT after ${timeout / 1000}s`)
  results.push({ script, code: timedOut ? 3 : r.status })
}

console.log(`\n${'═'.repeat(72)}\n▶ SUMMARY\n${'═'.repeat(72)}`)
let failed = 0
for (const { script, code } of results) {
  const verdict = code === 0 ? 'ok'
    : code === 3 ? 'TIMEOUT'
    : code === RUNNER_SKIPPED ? 'MISSING'
    : code === 4 ? 'NO-PYTHON'
    : code === 2 ? 'EXIT-2 (the script refused to start — read its own message above)'
    : 'FAILED'
  if (code !== 0 && code !== 4) failed++
  console.log(`  ${verdict.padEnd(8)} ${script}`)
}
console.log(failed
  ? `\n${failed} of ${results.length} script(s) did not pass — see their output above.`
  : `\nAll ${results.length} script(s) passed on this machine.`)
console.log('Note: scripts that reported SKIP did not run every check — read their output;')
console.log('"no FAIL" is not the same as "everything was exercised".')
process.exit(failed ? 1 : 0)
