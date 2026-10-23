# dsh-xrd interface contract

**Version:** 0.9.1 (interface last changed in **0.9.0**, 2026-10-03 — additive: four explicit
wavelength parameters and two new `xrd_data_io` actions; **still 12 tools**, nothing renamed.
**0.9.1 (2026-10-05) changes no tool, no parameter name and no default** — it fixes four defects and
corrects the wording of the λ contract on `xrd_refine_gsas`, whose value was never handed to GSAS-II as
an argument at all: see the CHANGELOG's 0.9.1 entry):

* **Wavelength became an explicit, default-off parameter on the four write entry points** —
  `wavelength` (a single monochromatic value in Å) **or** `wavelength_doublet:[kα1,kα2,ratio]`
  (mutually exclusive; passing both is an error) on:
  * `xrd_refine_manual` — the value goes into the plan as `lambda` (+ `lambda_explicit`); the engine log
    states whether the number was **explicit** or the **engine default**.
  * `cif_to_pcr` — the produced pcr's `! Lambda1 Lambda2 Ratio` **value line** is rewritten after
    conversion (only that line; its trailing Bkpos/Wdt/Cthm/… fields are preserved), and the return
    carries `wavelength:{ok,changed,before,after,kind,value_line_no,warnings}`.
  * `xrd_refine_auto` — the driver sets the value line on its **working copy** of the pcr (the file
    fp2k refines, inside its `.fp2k-work-*` directory) **before** the run; the pcr the caller passed in
    is left untouched, and `result.wavelength` holds the 改前→改后 echo.
  * `xrd_refine_gsas` — written straight into the `Lam`/`Lam1`/`Lam2` value slots (`[1]`) of every
    histogram: for a **new project** right after the histogram is created, for a continued `.gpx` on
    the histograms it already has. (It is deliberately **not** handed to GSAS-II as an argument —
    `add_powder_histogram` has no `wavelength` keyword; 0.9.0 passed one anyway and every new project
    failed.)
  **Omit the parameter and the input is byte-identical** — no writer is reached at all. A caller that
  wants a value must supply it; the tools never infer one (no anode guessing, no parsing of a header,
  no precedence chain).
* **`xrd_data_io` gained two actions**: `header` (a **positional** dump — everything before the data
  starts, located as the first run of ≥3 consecutive rows carrying ≥2 numeric columns; returns the
  line-numbered verbatim text plus `data_start_line`, `data_ncols`, `header_lines`, `line_ending`,
  `encoding_note`, `truncated`) and `evidence` (the same for several files at once). They are **raw
  evidence only**: they do not parse, rank or resolve anything — in particular **no wavelength is
  decided there**.
* **New CLI mode** `cif2pcr_helper.py setwavelength <pcr> <kα1> [<kα2> <ratio>] [--out <json>]` — the
  writer behind `cif_to_pcr` and the fp2k driver; `--out` is a flag and never read as a value.
* **No removals and no tool-count change**: 12 tools before and after; `engine/wavelength.py` (the
  anode→Kα-doublet table) is internal.

**Previous change (0.8.10):**

* **A new tool: `xrd_refine_multiphase`** — the ≥3-phase entry, and the only place a caller can run
  multi-phase orchestration the fixed ladder cannot express. **Hard gate: ≥3 phases** (refused below
  that, at the tool layer and again at run time inside the engine), and a **fresh build without a
  `handoff` is refused** — the two most abundant phases must already be settled by the bounded manual
  engine. `init_engine:true` copies the bundled template to `<out_dir>/gsas_multiphase_custom.py`
  **without running it**, so the caller can edit the `# == EDITABLE ==` orchestration and then execute
  that copy with `engine:"<path>"`. Tool count 11 → 12.
* **`xrd_refine_gsas` gained `phases[]`, `entry`, `rollback`, `rollback_tol`, `handoff`** and the
  `scale` refinement family:
  * `phases:[{...}, {...}]` (index = the order of `cif`/`cif2`) carries per-phase `cell / scale /
    size / mustrain / hstrain / pref / atoms`, with `mustrain` accepting
    `"isotropic" | "uniaxial" | "generalized"`. **Absent ⇒ the 0.8.9 behaviour is unchanged**
    (one global step per family, `cell2` / `size_only` / `scale2_init` / `open2` still work).
  * `entry:"hold" | "direct"` — `hold` (the default **when continuing an existing `.gpx` or when a
    `handoff` is given**) releases one family at a time and **keeps a step only when it improves wR**;
    `direct` is the 0.8.9 single batch with no rollback. A step **GSAS-II itself reports as failed**
    (e.g. `Invalid cell metric tensor for phase #1`) is always rejected and undone, and the delivered
    `.gpx` is the **best accepted state**, not merely the last one.
  * `handoff:"<manual engine result_*.json>"` seeds the two-phase solution across the Ⅱ→Ⅲ boundary:
    the 2nd+ phase ratio (`s2_<i>_scale / scale` → `Scale`), the main-phase cell and the instrument
    `U/V/W/X/Y/Zero` (unit-converted). Everything else is reported in `not_transferred`, never
    invented.
* **New result fields on `xrd_refine_gsas`** (additive): `entry`, `per_phase`,
  `ladder:[{step,label,wR_before,wR_after,accepted,rolled_back,gsas_error?,cell_change_pct?}]` and
  `handoff:{transferred,not_transferred,source_rwp?,warnings?}`. `warning` keeps its 0.8.x meaning
  (problems that leave the DELIVERED state compromised); a failed-but-undone step is reported in
  `ladder` instead and does **not** by itself turn `ok` false.

**Version:** 0.8.9 (interface last changed in **0.8.9**, 2026-09-28 — additive, plus one
behavioural correction a caller can observe):

* **The refinement-review detector no longer switches itself off.** Its "a human message arrived
  mid-refinement" test scans `user/message` session events, and the plugin's own injected reminders
  are `user/message` events too (`role:'user'`, `source.kind:'plugin'`) — so from round 2 onwards the
  detector read its own status-table reminder back as human input, closed itself, and stopped all
  review/E reminders for that sample. Injected events (recognised by `source.kind === 'plugin'`, or
  by carrying both bilingual markers) are now filtered out **before** the newest-seq bookkeeping, so
  `last_user_seq` tracks human messages only. Closures already recorded that are **provably** the
  plugin's own doing (every recorded snippet carries the plugin marker) are repaired on load and
  recorded as `auto-resume`; a closure caused by a real human message — or a mixed history — is
  never released.
* **`xrd_refine_gsas` warns when a sample's review history is split**: `splitLedgerWarning` reports
  sibling ledgers such as `review_mechanical_Y2O3_gsas.json` left behind by 0.8.7 in its `warning`
  list (rounds are never merged automatically; pass `sample` to book into the intended ledger).

**Version:** 0.8.8 (interface last changed in **0.8.8** — additive; three defects found
by a host-level end-to-end run):

* **`xrd_refine_gsas` gained `fmthint` and `sample`**, and reports `review_sample` / `fmthint_used`
  in its result. **The default reader hint is gone**: GSAS-II keeps only importers whose
  `formatName` contains the hint, so the previous `fmthint='GSAS powder'` default made every
  non-GSAS data file fail with `No reader could read file` even though GSAS-II reads it happily
  without a hint (measured: the same two-column `.dat` loads as 7351 points). Pass `fmthint` only
  to pin one reader family. `sample` names the refinement-review ledger
  (`review_mechanical_<sample>.json`); the default derivation is CIF stem → data stem → the
  directory's single existing ledger (gpx continuation) → gpx stem, so one sample no longer gets
  a second ledger when the data file is renamed or converted.
* **`xrd_gsas_ops` also gained `fmthint`** (same reason; its `new_gpx`/`new_from` intake no longer
  defaults to a GSAS-only reader family).
* **Engine stdout/stderr reach the caller again.** `runEngine` read the host's collected output
  through accessors the host reader does not have and fell through to
  `Object.prototype.toString`, so every engine's diagnostics surfaced as the literal
  `[object Object]`; a failure reason now carries the engine's own message.

* **`xrd_refine_auto`**: `ok` is now the **driver's** verdict — a run that reports `ok:false` while
  carrying a numeric Rwp is a FAILURE, with the Rwp still reported and `stopped`/`aborted`/`error`
  passed through. **`ladder:false` now really runs fp2k once** (it ran the baseline three times), and
  the baseline is measured with the same repeats AND the same pinned starting state as the
  candidates (a baseline that cannot complete its repeats now fails loudly instead of comparing a
  mean of one run against a mean of three).
* **`xrd_refine_auto` refuses an untrusted data-file name**: the name carried by the pcr's control
  line is copied only when it is a **bare file name** whose resolved path stays inside the work
  directory; otherwise the second copy is skipped and a `warning` says so (the name is input from a
  file — `../x.dat` used to write outside).
* **`cif_to_pcr`**: the converter — including a caller-supplied `tool_path` — now goes through the
  execution-approval decision, and the user's own same-named CIF is restored in a `finally` **after**
  the occupancy audit has read the converted CIF.
* **`xrd_verify_fp2k`** accepts every data extension the engines do (`.dat/.xra/.fxye/.txt/.xy/.raw`);
  it used to copy only `<stem>.dat`.
* **Engine result files are strict JSON**: `result_io` writes non-finite values as `null` (they were
  emitted as bare `NaN`/`Infinity`, which `JSON.parse` rejects — one bad value made a whole result
  unreadable).
* **The skill mirror is whole-tree and content-based**: `skills/` is mirrored recursively (refs and
  templates included) and a file is upgraded only when its SHA-256 matches what this plugin last
  wrote; a hand-edited file is kept and reported, and a pre-0.8.7 install is upgraded with the old
  copy kept as `<name>.user-backup-<stamp>`.

0.8.6 changed the interface by **addition only** (`cif_copied`, `warning`, `repeat_runs`,
`stopped`/`aborted` on `xrd_refine_auto`); 0.8.5 by addition too. The authoritative contract for every
field is the tool schemas in `lib/xrd-tools.js` and `lib/index.js`. Summary of the earlier additions:

* **`xrd_verify_fp2k`** now reports the **LAST** occurrence of every value it reads (`rwp`, `bragg_r`,
  `density`, `a`), matching `xrd_refine_auto`'s `findall(...)[-1]` rule; `rwp_source` names how many
  occurrences the file held. A run whose `.sum` carries **no R factor and no density** is a failure.
  New optional fields: `rwp_source`, `rwp_sum`, `warning`.
* **A graceful stop is returned, not thrown**: a fresh `{stopped: true}` record produces
  `{ ok:false, stopped:true, timed_out:false }` with `out_dir`/`artifacts` — on the manual tool as well
  as on the tools that pass an engine result through.
* **The engine's own `ok`/`error` are authoritative**: a custom engine that writes `ok:false` while
  carrying a numeric Rwp is no longer reported as a success. `aborted` means the CALL was cancelled and
  `timed_out` stays false for it.
* **An explicit `plan` that does not exist is an error** (`plan not found: <path>`) instead of a silent
  fallback to a default plan.
* **Batch actions report per-action truth**: `failed_actions[].index` is the ACTION's index, an
  **unknown action counts as a failure**, and `partial` means "at least one action succeeded".
* **`cif_to_pcr`** never overwrites a same-named CIF in `out_dir` (it is moved aside and restored) and
  never mistakes a `.pcr` it could not move aside for its own output; it reports `cif_copied`.
* **`xrd_refine_auto`** exposes the driver's `warning[]` and a new `repeat_runs` object (`repeats`,
  `baseline_n_ok`, `baseline_spread`, `unstable_steps`, `repeats_failed`, `background_levels_skipped`).
  A ladder step is accepted **only when every repeat run succeeded**, and the baseline is measured with
  the same repeats as the candidates.
* **`bg_scan` is a JSON STRING** (`"true"`), not a boolean — a bare `true` is rejected by the host.
* **Run-scoped file names**: `.xrd-progress-<run_id>.json` and `.xrd-stop-<run_id>` are written
  alongside the legacy fixed names (which are still honoured), so two runs sharing an artifact
  directory cannot overwrite each other's heartbeat or stop each other.

0.8.5 changed the interface by **addition only** (all fields additive and declared, so a caller written
against 0.8.4 keeps working):

* **new optional return fields**, added because an independent review showed the old payloads could
  not express what had happened: `aborted` (the CALL was cancelled — `timed_out` no longer pretends a
  cancel was a budget expiry), `stopped` (the engine stopped itself at a safe point, on the six tools
  that pass an engine result through), `partial` + `failed_actions` (a batch action failed: `ok` is now
  **false** whenever any action raised, where `xrd_cif_edit` / `xrd_data_io` / `xrd_phase_screen` used
  to return `ok: true` with an empty result set), `rwp_source` + `rwp_sum` + `warning` on
  `xrd_verify_fp2k` (which Rwp definition the number is, and a non-zero fp2k exit).
* **`xrd_refine_manual` input schema**: `data` is no longer unconditionally required — `init_engine`
  and an explicit `plan` do not need it — and a new optional **`lattice`** argument is accepted (it was
  already required by the engine's nominal-atom path, which therefore could never be used). `atoms` is
  now JSON-parsed instead of being passed through as a raw string.
* **`xrd_verify_fp2k` behaviour**: a run that produces no R factor and no density is now a **failure**
  rather than a success with every number missing, and `rwp` comes from the `.out` "Conventional
  Rietveld" line — the same definition `xrd_refine_auto` reports — with the `.sum` value kept separately
  in `rwp_sum` because the two numbers differ.
* **`cif_to_pcr`**: an existing `.pcr` is moved aside and restored if the conversion fails, instead of
  being deleted before the attempt.
* **Fuse semantics**: the hard fuse is now clamped to sit **between** the engine's nominal budget and the
  tool timeout (0.8.4's `tool timeout - 60 s` evaluated to 0 for the 60 s test override, so the watchdog
  fired on its first tick); defaults are unchanged — `xrd_refine_manual` / `xrd_refine_gsas`: tool
  **1800 s** > fuse **1740 s** > nominal budget **1500 s**.
* **Stop-flag contract**: `.xrd-stop` carries `{run_id, at}` and is honoured only for the run it names
  (a hand-written flag with no `run_id` still counts), so two runs sharing an artifact directory can no
  longer stop each other.

0.8.4 changed no schema at all: `init_engine`'s copy brings the template's sibling modules with it,
`abort` terminates through the same ladder as a stall, the host `subprocess` fallback works, and the hard
fuse was moved **below** the tool timeout. In 0.8.3 the
interface text said the run's contract changed; it now also states the timing hierarchy explicitly.
The 0.8.3 change itself: every result is stamped with a per-run `run_id` (carried inside the plan the engine
already reads, and stripped again before any payload is returned), a result whose mtime did not advance
or whose `run_id` does not match is refused instead of reported, and an engine run now stops
gracefully through a `<run>/.xrd-stop` flag before any tree kill. The **tool timeouts of the two
long-running heartbeat backends were raised** — `xrd_refine_manual` 660 s → **1800 s** (internal budget
620 s → **1500 s**), `xrd_refine_gsas` 900 s → **1800 s** (internal 880 s → **1500 s**) — because those
runs are now kept alive while they keep reporting progress instead of being cut at a fixed number.
0.8.0, 2026-09-26: `xrd_refine_manual`'s output
gained the structured-failure fields `ok` / `timed_out` / `error` / `hint` / `out_dir` / `artifacts` —
a run killed at the tool's internal budget now **returns** that payload instead of letting the host's
timeout erase the scene. 0.7.0, 2026-09-22: `xrd_gsas_ops` gained the
`set_adp_mode` action; 0.6.0 added the `scherrer` action to `xrd_data_io`. **0.8.10 raised the tool
family from 11 to 12** by adding `xrd_refine_multiphase` (the first new tool since 0.3.0; the
≥3-phase entry described at the top of this file); nothing was renamed. **0.9.0 changed no tool count
and renamed nothing**: it added the `wavelength`/`wavelength_doublet` parameters to four existing tools
and the `header`/`evidence` actions to `xrd_data_io` (both described at the top of this file). 0.4.0/0.5.x changed behaviour and
reporting only — see CHANGELOG.md. **0.7.1, 0.7.2 and 0.7.3 changed no schema at all**: the
interpreter chain moved to this plugin's own keys, the *text* of the "produced no result" errors
now names the interpreter, every render tolerates a value-less call, and 0.7.3 is a packaging
fix only — same fields, better words, no crashes.)
**Status of this file:** authoritative. If a skill, a description, or a habit
disagrees with this file, this file wins.

---

## 1. What changed in 0.3.0, and why

The plugin used to drive FullProf through **AutoFP** (the PyQt wrapper
`shautofp.py` plus its `diffpy/pyfullprof` pcr reader). That layer could not read
a `.pcr` written by the official `CIFs_to_PCR.exe`: its reader dies in the
background block (`pcrfilereader.py:749`) and every call ended in
*"refinement did not report Rwp"* even though `fp2k.exe` itself had refined the
file correctly.

AutoFP is therefore **gone from this plugin**. What replaced it:

* the refinement is driven by the bundled **`engine/fp2k_driver.py`**, which runs
  `fp2k.exe` directly and reads the R-factors from the `.out` fp2k writes;
* AutoFP's *auto-refinement strategy* — the parameter order, and the
  "enable one group → run → keep only if Rwp improves, otherwise roll back" loop —
  was **ported** into that engine, so the behaviour is preserved without the
  wrapper;
* the only thing taken from a FullProf installation is **`fp2k.exe`**.

Old → new, at a glance:

| 0.2.x | 0.3.0 |
|---|---|
| `autofp_refine` (alias) + `xrd_refine_auto` | **`xrd_refine_auto`** only |
| runs `shautofp.py` (AutoFP + GUI-class deps) | runs bundled `engine/fp2k_driver.py` |
| Rwp from AutoFP's `par.txt` | Rwp parsed from fp2k's `.out` |
| `path`, `cycles`, `autoselect`, `out_dir` | `pcr`, `cycles`, `ladder`, `out_dir` |
| `rwp, refined_pcr, out_files, par_summary, log_tail` | `ok, rwp, refined_pcr, out_files, ladder, cycles, error?, log_tail?` |
| `.autofp-work-*` artifact dir | `.fp2k-work-*` artifact dir |
| `DSH_AUTOFP_ROOT` / `autofp_root` (**still honoured as a fallback**) | **`DSH_FP2K_ROOT` / `fp2k_root`** |
| needed `shautofp.py` present | needs only `fp2k.exe` present |

**Nothing else in the tool family changed.** All other tools keep their names,
parameters and outputs.

---

## 2. Tool family (12 tools)

| Tool | Purpose |
|---|---|
| `xrd_data_io` | read/convert/cut XRD data, FWHM→Caglioti, Scherrer size, FullProf alignment |
| `xrd_cif_edit` | read/edit/build CIF (`build_orbit`, `validate`, `polymorphs`, MP/COD fetch) |
| `cif_to_pcr` | official `CIFs_to_PCR.exe` conversion + occupancy audit |
| `xrd_phase_screen` | residual-peak phase screening |
| **`xrd_refine_auto`** | **headless FullProf fp2k refinement (this contract)** |
| `xrd_refine_manual` | bounded-LM python engine (AI-editable) |
| `xrd_refine_gsas` | GSAS-II ladder: per-phase switches, `entry`/rollback, `handoff` (gated: run after fp2k/manual) |
| **`xrd_refine_multiphase`** | **≥3-phase orchestration + AI-editable engine hook (hard gate ≥3 phases)** |
| `xrd_gsas_ops` | GSAS-II full parameter/constraint control |
| `xrd_export_cif` | unified output port (gpx/result/components/data) |
| `xrd_finalize` | deliverable packaging |
| `xrd_verify_fp2k` | fp2k cross-check + chemistry/density |

---

## 3. `xrd_refine_auto` — the contract

### Parameters

| name | type | required | meaning |
|---|---|---|---|
| `pcr` | string | **yes** | absolute path to the `.pcr`. Its pattern data — the filename **declared inside the pcr** — must sit in the same directory. |
| `ladder` | boolean | no (default `true`) | `true` runs the full refinement ladder; `false` runs a single baseline refinement (use it to score a structure quickly). |
| `cycles` | integer | no (default `20`, clamped 5–200) | fp2k refinement cycles per call. |
| `out_dir` | string | no | where the `.fp2k-work-*` artifact directory is created (default: the session workspace). |

### Returns

| field | meaning |
|---|---|
| `ok` | `true` when an Rwp was obtained. |
| `rwp` | **the value that belongs to the delivered pcr** — the Rwp that run measured from the state handed over. This is the *flow* value: it enters the next stage together with the pcr. Absent when none was reported. |
| `rwp_mean` | **the mean of the repeat runs — the *decision* value.** Use it to choose between candidate CIFs and to judge whether the result is good enough to go straight into GSAS-II. It belongs to the set of runs, not to any single file, so it is reported separately from `rwp`. Omitted when only one run was made. |
| `refined_pcr` | the delivered `.pcr` inside the `.fp2k-work-*` directory. It is the **start state** of the delivered step — the state whose refinement produced `rwp` — *not* the state fp2k wrote back. |
| `out_files` | kept artifacts: delivered pcr, per-step start and backup pcrs, `.out/.sum/.prf`, `fp2k_plan.json`, `fp2k_result.json`. |
| `ladder` | **judgement packet** — the groups that were accepted, each with the Rwp it reached, e.g. `scale=80.1, zero=74.9, simple background/L1=52.1, W=35.3, Pref,Bov=16.0`. Empty when `ladder:false`. |
| `cycles` | cycles actually used. |
| `error`, `log_tail` | present on failure. |

**Why two R values.** Up to GSAS-II the workflow is R-driven, so the number entering a
stage must belong to the file entering it — that is `rwp`. The choice between candidates
and the "good enough for GSAS-II" gate, on the other hand, must be made on a quantity
that is not single-run noise — that is `rwp_mean`. Reporting one number for both jobs is
what produced the long-standing "the best snapshot is not reproducible" confusion.

**Why the delivered pcr is a start state.** fp2k reports the Rwp of the parameter set it
*evaluated*, but what it leaves in the pcr is the *next* update, which it has not
evaluated yet. Refining the written-back file therefore does not return the reported
number. Measured on real data with one fresh fp2k run each: from the run's own start
state the refinement ends at **16.0** (the value the driver recorded); from the post-run
written-back file it ends at **17.5**. The write-back is still kept — it holds the refined
parameter values — as a labelled backup, and a delivery never re-measures anything: the
cached start state is *copied*, so the reported number and the delivered file are one
measurement by construction.

**Failure policy:** never a bare throw. On failure `ok:false`, `rwp` is absent,
`error` explains, and **all artifacts are kept**.

### Ladder order (ported from AutoFP)

`scale → zero → simple background (1–2 terms) → cell a,b,c → W → complex
background (remaining terms) → V,U → Y,X → atom x y z → Pref,Bov → Biso-Atom →
GausS,1G → Occ-Atom → …`

Within a group each level stacks on the previous one; a level that worsens Rwp is
rolled back on its own. **The order is not cosmetic** — opening the cell before the
background/profile are stable destroys the fit (measured: Rwp 21 → 100).

Two AutoFP groups are deliberately **not** ported: `Asym` and `S_L,D_L`. Their
fields are not refinable codes in the pcr panel this driver writes, so enabling
them would be guesswork. They are reported as skipped, not silently ignored.

### What the driver repairs on the way in

Two defects the official converter leaves behind, without which `fp2k.exe` cannot
even open the file:

1. **phase-name line** — the converter copies the CIF filename in (`Y2O3.cif`);
   the dot makes fp2k abort with `FindFMT: invalid character in a numeric field`.
2. **background panel** — the converter writes Chebyshev (24 coefficients, 8 rows
   × 6), whose coefficients are invisible to the refine codes; it is rewritten as a
   polynomial, with the Job row's `Npr`/`Nba` corrected.
   *Gotcha:* the compact Job row **must not be edited by column name** — the two
   pcr generations label it differently, and a name-based edit silently moved `Nex`
   from 2 to 5, after which fp2k stopped right after its banner.

### Environment / configuration

```
1. explicit tool argument (where a tool accepts one)
2. DSH_FP2K_ROOT          (legacy: DSH_AUTOFP_ROOT, still honoured)
3. "fp2k_root" in xrd.local.json   (legacy: "autofp_root")
4. conventional probe: <plugin>/fullprof | fpsuite | autofp, cwd, parents, $DSH_HOME
```

The resolved directory **must contain `fp2k.exe`**; nothing else from it is used.

---

## 4. Reading R values correctly

* FullProf's **Rwp** and GSAS-II's **wR** use different weightings — they are
  **not comparable**. "GSAS improved the fit" must mean a clear improvement on the
  same data with the same model, not a difference between two conventions.
* A missing R-factor line in `.out` means **fp2k did not complete** — that is not
  the same as a bad fit, and it must not be reported as one.
