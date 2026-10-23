# audit/ — dsh-xrd self-audit module

This module verifies the plugin **from the inside**: it imports the plugin's own tool
registry, calls the real tools, and checks what comes back. It ships with the release so
a transplanted package can be proven intact on the machine it lands on, without trusting
that "it worked on the maintainer's computer".

Everything here is **read-only with respect to the plugin**. The only writes go to
`audit/.work/`, which is disposable.

## Quick start

```bash
node audit/integrity-check.mjs          # the one to run after any transplant or upgrade
node audit/verify-package.mjs           # the release tree still carries what it claims
node audit/verify-cif-edit.mjs          # xrd_cif_edit actions + CIF validation
node audit/verify-e2e.mjs               # build_orbit -> cif_to_pcr -> refine
node audit/verify-autofp-replaced.mjs   # xrd_refine_auto really drives the bundled fp2k
node audit/verify-review-detector.mjs   # the self-review state machine (stub agents, no quota)
node audit/verify-manual-timeout.mjs    # the manual engine's budget/crash/stale-result paths
```

The two `_`-prefixed helpers are Python and are meant to be pointed at a tree:

```bash
python audit/_sample-shape-scan.py [<plugin dir>]   # enumerate sample-code-shaped tokens
python audit/_neg-control-catalog.py                # prove the catalog check can fail
```

## What each script is for

| script | verifies | notes |
|---|---|---|
| `integrity-check.mjs` | **logic chain + engine integrity**: tool assembly; every declared engine exists and compiles; the advertised action vocabulary against what each engine actually dispatches (both directions); closed output schemas; the host JSON-schema keyword subset; six named cross-tool chains; **and the defect catalog** (14 classes, the two `reachability` marks, footnote homes, the 10 per-sample field names against the skill text, per-sample `defect_table.json` ids) | the one to run after any code change or transplant. Exit 0 = chain intact |
| `verify-package.mjs` | a built release tree: advertised actions, the CHANGELOG's evidence levels and no-blanket-claim rule, the bundled fp2k binaries, `xrd.local.json` absent, both release archives and the executable bit inside the `.tar.gz`, the audit module itself present and free of maintainer paths (**every text file under `audit/`, raw and backslash-unescaped**) | point it at a release dir with `--root` |
| `verify-cif-edit.mjs` | `xrd_cif_edit`: `build_orbit` completes Wyckoff orbits; `validate` passes a good CIF and flags a broken one | self-contained |
| `verify-e2e.mjs` | the chain `build_orbit -> cif_to_pcr -> xrd_refine_auto` | needs the official `CIFs_to_PCR.exe` |
| `verify-autofp-replaced.mjs` | `xrd_refine_auto` runs the bundled headless fp2k driver and the retired AutoFP-era interface is gone; produces a real Rwp | needs fp2k (bundled) |
| `verify-fp2k-check.mjs` | `xrd_verify_fp2k` resolves fp2k through the shared `fp2kRoot()` probe | needs fp2k (bundled) |
| `verify-manual-fitstatus.mjs` | the manual engine's judgement packet (`baseline_rwp` / `fit_status` / `r_plateau`) crosses the tool layer | slow (runs to its evaluation cap on purpose) |
| `verify-manual-mapping.mjs` | isolates the tool-layer mapping from that slow run | skips unless the previous script has produced `.work/manual/result.json` |
| `verify-manual-timeout.mjs` | the manual engine's failure paths, against **real** child processes: budget expiry returns a structured failure (artifact dir + file list + verdict), an engine fault still throws and names the scene, and a stale `result.json` from an earlier run is never reported as this run's result | lowers the budget through `DSH_XRD_MANUAL_BUDGET_MS`; needs `DSH_XRD_PYTHON` (or a conventional install); sandbox must allow spawning a child with piped stdio |
| `verify-review-detector.mjs` | the self-review state machine (`lib/review.js`): round ledger, every-3-rounds cadence, streak/level escalation, what each level injects, E reminders, release detection, one in-flight review per sample, human takeover and explicit resume | **stub agents only — no model, no engine, zero quota**; pass `--root` to audit a profile copy |
| `verify-result-freshness.mjs` | 0.8.3 P0: only a result THIS run wrote may be reported — every refusal branch of `readFreshResult` (missing / stale / wrong `run_id` / unparsable), a real `data_io` run whose plan carries a `run_id` the engine echoes back, and a negative control that reproduces the old bug on the same fixture | real engine child process; pass `--root` |
| `verify-proc-supervision.mjs` | 0.8.3 P1/P2: the per-backend liveness policy (`manual`/`gsas` = heartbeat, `fp2k` = fixed), a slow-but-ticking run that must OUTLIVE its nominal timeout, a run whose ticks stop that must be killed as `heartbeat-lost`, the graceful stop (the engine exits itself and leaves a result), the host-subprocess branch and its fallback, and the fp2k NaN probe | real child processes; `DSH_XRD_HEARTBEAT_STALL_MS` / `DSH_XRD_STOP_GRACE_MS` shorten the windows; pass `--root` |
| `verify-gsas-ops.mjs` | the four `xrd_gsas_ops` actions that were implemented but unadvertised (`add_phase`, `replace_phase`, `new_from`, `set_site_composition`), on a real converged project, reading the project back with GSAS-II | needs GSAS-II and a converged `.gpx` (see below) |
| `verify-multiphase.mjs` | **0.8.10** the multiphase entry: the tool family is 12 and `xrd_refine_multiphase` advertises the orchestration parameters; **the ≥3-phase gate** refuses two CIFs (naming the 1-2 phase tool) and **spawns nothing**; a fresh ≥3-phase build **without `handoff`** is refused (naming the manual engine); `init_engine` returns a **byte-identical** copy of the bundled template and runs nothing; and two source-level guards for the defects the 2026-10-02 smoke test found — the template must call `run_guarded_main(main)` with **no** extra keyword (`proc_guard` takes one argument; the permissive local shim hid a `backend=` keyword) and must resolve `gsas_root` into `sys.path` (without it every call died with `ModuleNotFoundError`) | **tool layer only — no GSAS-II, no python, no quota**; sets its own scratch `DSH_HOME` because assembling the plugin mirrors the skills |
| `py/ctrl-fp2k-orphan.py` | the fp2k orphan incident as a discriminable control: without the guard the grandchild survives, with it the child dies with the driver, and the sweep only kills parentless processes | needs `DSH_XRD_SRC` (or a repo tree) and a writable `DSH_XRD_CTRL_WORK` |
| `verify-y2o3.mjs`, `struct-check.mjs` | real-data end-to-end and structural attribution | need a reference project (see below); `struct-check` must run after `verify-y2o3` |
| `ctrl-plateau-basis.mjs` | the R-value plateau verdict's `basis` field (why a run is or is not a plateau) | pairs with the manual-engine scripts |
| `_sample-shape-scan.py` | enumerates every `<two letters><two digits>` token in a tree and reports the ones the release scrub does **not** cover — the same rule the release audit applies, run **before** a build | dev tree: the scrub keys are read out of the builder; in a package the ignore list is empty (after scrubbing, any match is a finding) |
| `_neg-control-catalog.py` | **negative control**: damages one catalog row, runs `integrity-check.mjs`, and requires section 7 to FAIL — then restores the file and requires it byte-identical | a check that cannot fail is not a check; run it after touching the catalog |


## What is NOT bundled, and how to supply it

Nothing here downloads anything. Three inputs are deliberately absent because they carry
measured data or third-party binaries; each script **skips with an explanation** rather
than failing, so an absent input is never mistaken for a passed check.

| input | needed by | how to supply |
|---|---|---|
| a converged GSAS-II project (`.gpx` with ≥1 histogram) | `verify-gsas-ops.mjs` | `DSH_XRD_GPX=<path>` |
| a reference Y2O3 project (`Y2O3.pcr` + `Y2O3.dat`) | `verify-y2o3.mjs`, `struct-check.mjs` | `DSH_XRD_Y2O3_REF=<dir>` |
| GSAS-II itself | `verify-gsas-ops.mjs` | `DSH_GSAS_ROOT=<dir>`, or `gsas_root` in `xrd.local.json` |
| the official `CIFs_to_PCR.exe` (FullProfApp) | `verify-e2e.mjs` | `DSH_FULLPROF_CIF2PCR=<exe>`, or the public default install path |

## Environment variables

| variable | meaning |
|---|---|
| `DSH_XRD_ROOT` | the plugin tree to audit; same as `--root <dir>`. Default: the tree this module lives in |
| `DSH_XRD_PROFILE` | a dsh runtime copy (`.../node_modules/dsh-xrd`) to test instead of the local tree |
| `DSH_XRD_PYTHON` | Python 3 interpreter. Probed automatically when unset |
| `DSH_XRD_GPX` | converged GSAS-II project for `verify-gsas-ops.mjs` |
| `DSH_XRD_Y2O3_REF` | reference Y2O3 project directory |
| `DSH_GSAS_ROOT` / `DSH_FP2K_ROOT` / `DSH_FULLPROF_CIF2PCR` | third-party program locations |

Where the reference project lives is the plugin's own convention: `xrd.local.json` next to
the plugin (`gsas_root`, `fp2k_root`), which is per-machine and therefore never shipped.

## How to read the output

Every script prints `PASS` / `FAIL` / `SKIP` lines and ends with
`FAIL n | WARN n | SKIP n`, then a verdict, and exits non-zero if anything failed.

- **FAIL** — a real defect in the plugin. Do not ignore.
- **SKIP** — an input this machine does not have. Not a defect, and **not** a pass.
- A run with skips says so explicitly; do not read "no FAIL" as "everything worked".

**Two environment errors that are NOT plugin defects** — the scripts name them, but they
are worth recognising:

| symptom | what it means |
|---|---|
| `spawn EPERM` | the process could not start a child at all. This is a sandbox or permission limit (typically a restricted-stdio policy), not a bug in the plugin. Re-run where child processes may be spawned. |
| `EPERM: ... mkdir ...\.work\...` | the scratch directory is not writable; the module now probes several candidates and says which one it used. If none works it exits with the list it tried. |

## What was exercised, and what was not

The module reports what actually ran. Recorded so a later reader can tell "checked" from
"assumed":

**At 0.8.7 (2026-09-28, maintainer's Windows machine)** — the fourth-review fix; run against the
source tree, the runtime copy under the dsh profile, and the built release:

- `verify-all.mjs` — **12/12 script(s) passed, no SKIP.** `verify-proc-supervision.mjs` is now
  **181/181**; its new §23 turns all twelve findings of that review into controls: the review's own
  never-settling-`done` probe (returns in ~32 s with `exitObserved:false` instead of hanging), a
  pre-aborted call (0 ms), a real progress tick right after a plain one, the **four skill-mirror
  states** (fresh install mirrors the whole tree incl. `refs/` + `templates/`; idempotent; a
  user-edited file is KEPT; a file we wrote is upgraded; a pre-0.8.7 install is upgraded with
  `.user-backup`), strict-JSON engine results round-tripped through `JSON.parse`, a `../` name carried
  by a pcr writing **nothing** outside the work dir, and score-only running fp2k **exactly once** with
  `ok:true` and its Rwp.
- Two items were checked by real runs rather than fakes: a full ladder (background steps capped at the
  panel's real field count → `background_levels_skipped: 18`, "no such field index" warnings 18 → 1,
  Rwp 15.4) and a score-only run (`ok:true`, Rwp 81.1, `delivered_from` present, one `.out`).
- **A defect this release introduced and the suite caught**, kept as evidence: keying the in-flight
  review map by dir+sample was done as `reviewInFlight(inflightKey(dir, sample))`, i.e. the key was
  built twice, so the in-flight check never matched and `verify-review-detector.mjs` reported
  `calls=2` ("a second due round does not stack a second in-flight review"). Fixed by passing the raw
  arguments; the suite is back to **138/138**.
- Release chain on `dist/dsh-xrd-0.8.7`: `LEAK AUDIT : clean`, `.zip`/`.tar.gz` **104 entries** each,
  **THREE COPIES AGREE** (source == profile; package == `sanitise(source)`), `verify-package.mjs`
  **FAIL 0 / WARN 0 / SKIP 0**.
- **Not exercised here, stated rather than implied:** Windows job-object acceptance under a killed
  parent; the real host `subprocess` service (stub only); the Windows `CIFs_to_PCR.exe` path. A cloud
  reviewer's environment had no GSAS-II, which is consistent with ours — their supervision run showed
  158 pass + 2 skipped where this machine shows 181/0.

**At 0.8.6 (2026-09-28, maintainer's Windows machine)** — the third-review fix; run against the source
tree, the runtime copy under the dsh profile, and the built release:

- `verify-all.mjs` — **12/12 script(s) passed, with NO skip**: `verify-gsas-ops.mjs` ran its
  real-project half against a converged PbSO4 `.gpx` (`FAIL 0 | WARN 0 | SKIP 0`), now including a
  **real two-step refinement** (`refine:['background','cell']`) that asserts `ok:true`, a numeric `wR`
  (4.60), the reported cell after step 2, and the guard's tick naming `gsas refine step 1/2` with
  `self_assigned:true` — the 0.8.5 per-step restructure measured on a real project.
- `verify-proc-supervision.mjs` — **162/162**, including the four sections added for this release:
  **§19** fp2k's LAST-occurrence Rwp (the fixture's own `.out` holds **4** conventional-Rietveld lines
  and its `.sum` **8** `Rwp:` lines: the tool now reports **81.1 %** where 0.8.5 reported the first
  values **83.6 %** / **75.7 %**), **§20** an engine's `ok:false` surviving the wrapper, **§21** a
  missing explicit `plan` being reported, **§22** distinct per-action failure indices, an unknown
  action counting as a failure, and `partial` meaning "some action succeeded".
- A real fp2k driver run is stopped by its own `.xrd-stop` (**exit 97** plus a structured
  `{stopped:true}` result) while a flag naming **another** run leaves it alone — this is what exposed
  `EngineStopped` being swallowed by the engines' `except Exception`. It derives from `BaseException`
  now, and all nine long engines take a checkpoint (asserted as a set, not as a sentence).
- **Measured, and recorded because it first looked like a defect:** the same real two-step GSAS call
  that succeeds in **9.3 s** in a clean directory failed under the supervision suite, because that
  suite shortens the heartbeat stall window to 2.5 s for its stall controls. The real-run assertion
  therefore lives in `verify-gsas-ops.mjs` (shipped 90 s window).
- Release chain on `dist/dsh-xrd-0.8.6`: `LEAK AUDIT : clean`, `.zip`/`.tar.gz` **104 entries** each,
  **THREE COPIES AGREE** (source == profile; package == `sanitise(source)`), `verify-package.mjs`
  **FAIL 0 / WARN 0 / SKIP 0**.

**At 0.8.5 (2026-09-28, maintainer's Windows machine)** — the second-review fix; run against the
source tree, the runtime copy under the dsh profile, and the built release:

- `verify-all.mjs` — **12/12 script(s) passed.** `verify-proc-supervision.mjs` grew from 103 to
  **111/111**: the newly asserted behaviours are the pre-aborted-signal crash (0.8.4 threw
  `Cannot access 'requestTermination' before initialization` AFTER spawning the child), the fuse
  staying ABOVE the nominal budget (the 60 s override used to produce fuse = 0 and cut a healthy run
  on the first tick), `stopped` being a declared field on the seven tools that pass an engine result
  through, `xrd_verify_fp2k` failing when its `.sum` carries no R factor, the rotated
  `.verify-<stem>.prev` scratch, and the sweep's new path check. `verify-result-freshness.mjs` —
  **61/61**; `verify-manual-timeout.mjs` — **37/37** (it now checks WHICH reason fired, not just that
  the word "budget" appears); `verify-review-detector.mjs` — **138/138**; `integrity-check.mjs` —
  **0 FAIL**.
- `py/ctrl-fp2k-orphan.py` — **ALL PASS**, including new case **C2**: a parentless process with the
  same NAME but a different image path is reported under `skipped_unverified` and left running, while
  the genuine orphan (path = the binary the driver runs) is killed and a parented one is spared.
- **Measured, not asserted:** the sweep control prints the pids it killed/skipped; the abort control
  reports how long the terminated run actually took; the fp2k probe records that fp2k writes a
  `.sum` containing only its banner when it cannot open the intensity file — which is why "a .sum
  exists" was replaced by "an R factor or a density exists" as the acceptance test.
- Release chain on `dist/dsh-xrd-0.8.5`: `LEAK AUDIT : clean`, `.zip`/`.tar.gz` **104 entries** each
  (tar with one top-level directory and the executable bits set), **THREE COPIES AGREE** (source ==
  profile 34/34; package == `sanitise(source)`), `verify-package.mjs` **FAIL 0 / WARN 0 / SKIP 0**.
- **Environment boundary met here, worth knowing:** in a confined sandbox that denies piped stdio, the
  whole module fails with `spawn EPERM` inside the tools — that is the sandbox, not the plugin. The
  numbers above were taken with child spawning permitted, and the suites say so in their own output
  rather than silently skipping.
- **Self-inflicted defect found by the suite during this release (kept as evidence):** after bounding
  the engine exit wait with a timer, `verify-result-freshness.mjs` printed
  `ALL PASS — pass=61 fail=0` and then hung until the per-script cap killed it (reported as
  `TIMEOUT`), because a plain `setTimeout` holds node's event loop open for its whole delay. The
  raced cap timers are `sleepUnref(...)` now; the AWAITED grace/poll timers must NOT be unref'd
  (unref'ing those empties the loop and node dies with `unsettled top-level await`, exit 13).

**At 0.8.4 (2026-09-28, maintainer's Windows machine)** — the release-blocker fix; run against
the source tree:

- `verify-all.mjs` — **12/12 script(s) passed**. `verify-proc-supervision.mjs` grew from 52 to
  **103/103**: every 0.8.4 blocker now has a control that FAILS on 0.8.3 — the `init_engine` copy
  must be importable, `abort` must actually terminate (checked by a still-running child, not by a
  written flag), the host-subprocess fallback must not throw on the previously-`const` variable,
  and the fuse must sit below the tool timeout. `verify-result-freshness.mjs` **61/61**. The same
  honest **SKIP** remains (real-project half of `verify-gsas-ops.mjs`).
- Measured, not asserted: an aborted run is gone in **~1.9 s** with `residual === false` (the abort
  path awaits the termination it requested); a stalled run is still cut as `heartbeat-lost`; a
  throwing service falls back to the direct-spawn branch instead of failing the tool.
- Release chain on `dist/dsh-xrd-0.8.4`: `LEAK AUDIT : clean`, `.zip`/`.tar.gz` **104 entries**
  each, **THREE COPIES AGREE** (source == profile 34/34; package == `sanitise(source)`),
  `verify-package.mjs` **FAIL 0 / WARN 0 / SKIP 0**.
- **Known limitation, not a gap in the controls:** the third-party executables the plugin invokes
  (`fp2k.exe`, `CIFs_to_PCR.exe`) are outside the job-object guard, so a hard host death can still
  orphan one — recorded in the 0.8.4 CHANGELOG rather than papered over.

**At 0.8.3 (2026-09-28, maintainer's Windows machine)** — the process-supervision release; run
against the source tree:

- `verify-all.mjs` — **12/12 script(s) passed** (child processes permitted). New members:
  `verify-result-freshness.mjs` **61/61** and `verify-proc-supervision.mjs` **52/52**, plus the
  python control `py/ctrl-fp2k-orphan.py`, which reproduces the orphan incident and had never been
  in the default list. One honest **SKIP** remains: the real-project half of `verify-gsas-ops.mjs`
  (no converged `.gpx` supplied).
- `verify-manual-timeout.mjs` — **35/35** (the static assertions moved with the raised timeout pair:
  internal 1500 s below the tool's 1800 s, so a stuck run still fails structurally).
- `verify-review-detector.mjs` — **138/138**; `integrity-check.mjs` — **0 FAIL**.
- The heartbeat behaviour is measured, not asserted: a ticking run **finished at 4.9 s although its
  nominal timeout was 1.5 s**; a run whose ticks stopped was stopped at ~5.5 s as `heartbeat-lost`
  (not at its 60 s nominal timeout); a cooperating engine **exited by itself with code 0** and left a
  structured result behind.
- **Not exercised here:** the host-`subprocess` branch (stub only — a real host belongs to the
  deployment environment), and the same external-input scripts as before
  (`verify-manual-fitstatus.mjs`, `verify-y2o3.mjs`, `struct-check.mjs`, real-project `verify-gsas-ops.mjs`).

**At 0.8.0 (2026-09-26, maintainer's Windows machine)** — run against the source tree, the
runtime copy under the dsh profile, and the built release:

- `verify-all.mjs` — **9/9 script(s) passed** once child processes were permitted. What
  really ran: `verify-e2e.mjs` executed the whole `build_orbit -> cif_to_pcr ->
  xrd_refine_auto` chain (the CIF was accepted by the official converter and fp2k found its
  pattern file); `verify-autofp-replaced.mjs` ran a **real fp2k ladder — Rwp 16 in 64.1 s,
  57 artifacts kept**; `verify-fp2k-check.mjs` ran fp2k directly. Two checks honestly
  reported **SKIP** (the real-project half of `verify-gsas-ops.mjs` — no converged `.gpx`
  supplied here — and `verify-manual-mapping.mjs`, which needs a prior slow run).
- **Run it twice, and read the difference:** the same command inside this session's file
  sandbox failed **4 of 9** scripts with `spawn EPERM` — every one of them a script that
  starts a child process, i.e. an environment limit the scripts name explicitly, not a
  plugin defect. A run that cannot spawn says so; it never reports a pass.
- `integrity-check.mjs` — **0 FAIL on all three trees** (11 tools, 10 engines, 6 chains,
  and section 7's catalog checks). The one WARN is a documented legacy engine kept on
  purpose.
- `verify-review-detector.mjs` — **108/108** on the source tree and on the profile copy
  (stub agents only; no model, no engine, no quota).
- `verify-manual-timeout.mjs` — **31/31** (real python children: budget expiry, engine
  fault, stale result, plus a fast stub as the negative control).
- `verify-package.mjs` — green on `dist/dsh-xrd-0.8.0`, including both archives and the
  audit-module path scan (61 text files).
- `_sample-shape-scan.py` — 0 un-scrubbed sample-shaped tokens in the source tree and,
  run from inside a copy of the package with no ignore list, 0 there too.
- `_neg-control-catalog.py` — damaged a row ⇒ section 7 FAILs; restored ⇒ passes and the
  file is byte-identical.
- `three-copy-check.mjs` — source == profile byte for byte; package == sanitise(source).
- **Not run at 0.8.0:** `verify-manual-fitstatus.mjs`, `verify-y2o3.mjs`,
  `struct-check.mjs`, and the real-project half of `verify-gsas-ops.mjs` (they need
  several minutes or an external reference project/GSAS-II; 0.8.0 touched neither the fp2k
  driver nor the GSAS engines). Their last green run was on the 0.7.x trees, where the
  GSAS-II two-phase build was exercised.

## What this module has already caught

Worth knowing, because they are the failure mode it exists for:

1. **`ladder:false` ran the entire refinement ladder anyway** — a `or 1` turned the
   caller's `0` into `1`. With the bug, "score this structure quickly" cost ~19 fp2k runs
   (18.6 s) instead of 1 (0.4 s).
2. **Actions implemented but never advertised** — `add_phase`, `replace_phase`,
   `new_from` and `set_site_composition` existed in `engine/gsas_ops.py` and were
   mentioned nowhere in the tool's own help, i.e. unreachable in practice, including the
   entry point for two-phase work. The reverse-direction half of the vocabulary check is
   what found them.
3. **A crashed run re-reported the previous run's Rwp** (2026-09-26, found by
   `verify-manual-timeout.mjs` section 5 and then by section 6): the manual engine's driver
   read `result.json` without checking who wrote it, so a stub engine that wrote nothing
   came back `ok: true` with the earlier run's number. The fix is a pre-run mtime snapshot.
   It was found because a *negative control* (a fast stub engine writing a valid result)
   left a file behind for the next case.
4. **The leak audit could not see an escaped path** (2026-09-26): the shipped test scratch
   `audit/_manual-timeout-work/plan_calibrate.json` carried the maintainer directory path
   **with its backslashes doubled** (JSON escaping), and every rule was written against the
   single-backslash form — so a build reported **clean** while the package leaked. The
   audit now also scans a backslash-unescaped copy of every line **and** the forward-slash
   spelling, and `verify-package.mjs` walks the whole `audit/` tree instead of a
   hand-written file list. Both gaps are the same class this file already warns about:
   **a check must not be derived from the list it is checking.** The control for the new
   rule is a planted probe file, which the build must report as exactly one finding.
   (This very paragraph is written without the literal path, so it does not trip the rule
   it describes.)

## Extending it

Add `import { pluginRoot, importRoot, pluginEntry, mockContext, WORK_ROOT, FIXTURES,
gsasRoot, python, report } from './_lib.mjs'` and never type a machine path — the whole
point of `_lib.mjs` is that the module stays portable and leak-free. Use `report.skip()`
for anything optional, so "could not run" stays distinguishable from "passed".
