# Changelog

All notable interface changes are recorded here. The authoritative interface
contract lives in [INTERFACE.md](./INTERFACE.md).

## 0.9.1 — 2026-10-05 — **four defects from real work: the GSAS-II writer never worked on a new project, the hand-off seeded the wrong width, the review subagent died in 15 ms, and one converter waited out a prompt**

All four were reported by the user from actual runs (the third was diagnosed for them by a separate
review; its stated cause was re-checked here before anything was changed, and its §5 was re-measured —
see the correction at the end). **Still 12 tools; no parameter was added, renamed or removed, and no
default changed.** Three of the four were invisible to the entire audit chain, so what changed in the
tests matters as much as what changed in the code.

### 1. `xrd_refine_gsas`: every NEW project failed — `wavelength=` does not exist on that loader

The 0.9.0 λ writer handed `wavelength=` to `G2Project.add_powder_histogram`, whose signature is
`(datafile, iparams, phases, fmthint, databank, instbank, multiple, URL)` — **no `wavelength`, and no
`**kwargs`** (`GSASIIscriptable.py:1044`). `wavelength=` belongs to `add_simulated_powder_histogram`
(`:1197`), which builds a *calculated* pattern. And because the argument was passed **unconditionally**
(`None` when no λ was requested), **every new-project GSAS refinement raised `TypeError`** — surfaced as
the misleading `GSAS-II could not import powder data '<file>'`.

Fixed by writing the λ into the histogram's **slots** (`Lam` / `Lam1`+`Lam2`, index 1 — the value GSAS
reads): for a new project right after the histogram is created, for a continued `.gpx` on the histograms
it already has, through **one** `_wl_write()` that both paths share. A mono request against a
Lam-only instrument file now logs the dropped Kα2 instead of relying on a raise that no longer happens.

**Why nothing caught it:** the host end-to-end run continued an **existing** `.gpx` (that path always
wrote the slots), `verify-wavelength` asserted the writer on the **source** only, and the one real
new-project control sat behind `DSH_XRD_GSAS_INTAKE=1` — an opt-in gate is coverage that was deleted.
`verify-wavelength.mjs` now carries **§2b** (a negative assertion: no `wavelength=` may reach that call,
with explanatory prose stripped first so the rule is not tripped by its own comment) and **§6**, which
runs the engine **for real** whenever a GSAS-II install is discoverable and reads the λ **back out of the
saved `.gpx`** (not out of our own in-memory write). **Measured control:** the same suite against the
shipped 0.9.0 tree reports **5 FAIL**, including `unexpected keyword argument 'wavelength'`; against this
tree, **0 FAIL**.

### 2. `apply_handoff`: U/V/W seeded 5.545× too wide — FWHM² was passed as a Gaussian variance

The manual engine's `U/V/W` are the coefficients of **FWHM² in degrees²** (`xrd_engine.hwidth` returns the
full width at half maximum, and that is the `H` its Gaussian `g_area` consumes). GSAS-II's `U/V/W` are the
coefficients of the **Gaussian variance in centidegrees²**: `GSASIIpwd.py:926` defines
`sig = sqrt(U tan²+V tan+W)`, and `:909-910` turns it into degrees (`sqrt(sig)/100`) *before*
`fwhm = 2.355*sigma + 2*gamma`. The hand-off multiplied by 1e4 alone, i.e. it omitted the FWHM↔σ factor —
seeding a profile **8·ln2 = 5.545×** too broad. That is below the existing "10× ⇒ refuse" implausible-seed
guard, so it was never refused; it just started the refinement from a visibly wrong width.

The factor is now `1e4/8ln2 = 1803.37` (`_FWHM2SIG`); `Zero` stays ×1. The `X`/`Y` rows are **inert** (no
manual result carries them) and their own GSAS-II convention (Lorentzian terms, HWHM vs FWHM) is **not**
verified here — the table now says so instead of implying it was measured. Verified on a real project:
`U = 2.097 (manual 0.001163 ×1803.37)`, `V = −0.2272`, `Zero = −0.1`, and `W` was refused by the existing
guard (44× the instrument file's value) — i.e. the guard now sees the right numbers.

### 3. The review subagent never ran: a missing `signal`, reported as a timeout

`lib/review.js` called `subagents.start(provider, {...})` **without `signal`**, and the host's in-process
driver reads `request.signal.aborted` unconditionally
(`dsh-subagent-in-process-driver/lib/index.js:160-162`) → `TypeError: Cannot read properties of undefined
(reading 'aborted')` **15–18 ms** after the round was booked. Both registered providers (`spawn`, `fork`)
funnel into that same driver, so no environment variable could avoid it. The plugin then rendered *every*
failure as one sentence — "the reviewer did not return within its budget" — which is exactly why this read
as a 120 s timeout for two versions while the budget never started.

Now the plugin supplies its **own** `AbortController` (never `exec.signal`: a review is an independent
background task and must not be cancelled when the tool call that spawned it returns) and aborts only at
the moment it stops waiting, so "we gave up" and "the child stops" coincide. The one-size text is gone:
`invalidText()` separates **not run** (service/provider unavailable), **threw** (names the error) and
**timed out** — and the old `TEXT.invalid` was removed rather than left behind unused.
`verify-review-detector.mjs` now drives a **strict stub that enforces the host contract** (it throws when
`signal` is not readable), so a regression fails there instead of in the field; **measured control**:
deleting the `signal:` line from a copy reproduces the exact recorded `TypeError`. Suite: **158/158**.

### 4. `cif_to_pcr` waited ~117 s per call for an interactive prompt

`runEngine`'s `child_process` fallback spawned with Node's default stdio — a **stdin pipe whose write end
we hold open** — while the host-service branch already used `stdin: 'ignore'`. `CIFs_to_PCR.exe` ends with
`Press <enter> to finish`, so the converter blocked on that pipe until **our own 110 s budget** expired
(measured on a real call: **117 084 ms**, `log_tail` ending with that prompt). The fallback now uses
`stdio: ['ignore','pipe','pipe']`, matching the service branch: the same call takes **715 ms**, same `.pcr`
(4097 B). It also means no engine can be wedged forever by an accidental read.

**Correction, stated as such:** the first isolated control for this item reported "NOT CONFIRMED" — the
converter exited in <1 s under both stdio shapes. That control was wrong: **`spawnSync` closes the child's
stdin for you**, so it cannot produce the blocking condition. With an async `spawn` (what `runEngine`
actually does) the default pipes block past a 20 s cap, while `stdin:'ignore'` exits in **211 ms**.

### Not changed

* No tool added, removed or renamed (**12**), no parameter added or removed; the λ parameters keep their
  **default-off** contract, and a λ still reaches exactly the same two artifacts as before — through the
  mechanism GSAS-II actually has.
* The manual engine's **numerics are untouched**: both ① and ② are fixes on the GSAS-II side.
* `three-copy-check` already ignores `__pycache__`/`.pyc`; `verify-all.mjs` now exports
  `PYTHONDONTWRITEBYTECODE=1` to every step (the env is inherited, `-B` is not), so a full gate run no
  longer leaves bytecode inside the plugin tree.
* **Audit result for this release:** the full gate on this tree is **ALL PASS** (FAIL 0, tools 12,
  engines 14, two pre-existing "engine on disk but referenced only by other engines" warnings).

## 0.9.0 — 2026-10-03 — **the wavelength (λ) you were given: the input decides, nothing is written unless you say so — and one of the four writers was writing nothing at all**

> **Corrected in 0.9.1.** Entry ④ below describes "GSAS-II's own `wavelength=` when the histogram is
> created". That argument **does not exist** on `add_powder_histogram`, and it was passed unconditionally,
> so from 0.9.0 a **new** GSAS-II project failed outright; only the "continue an existing `.gpx`" path
> ever worked. The parameter names, defaults and the other three entries are unchanged. See the 0.9.1
> entry above.

This release is a question from the user about real work — "**实际工作中 XRD 插件能否确认正确波长并修正**" —
that turned out to need an interface, not a lookup. The design went through several rounds and the user
corrected it twice, both times on a point of principle:

* "**不是，不需要 GSAS-II 读取，我们自己的插件能读就行了**" — reading the header is the plugin's own job.
* Authority is decided by **what the input IS, not what format it has**: a partially refined `.gpx` handed over
  *as an input* is authoritative; **our own upstream products never are**. The first design (a precedence chain
  with conflict bands and automatic parsing of `.pcr`/`.prm`/CIF) was built and then **deleted**, because that
  premise was wrong.
* "**facts 为什么要作为独立组件不计入工作日志或假设里的固定信息，而作为一个单独文件维护**" — so no new
  component or file was added: the fact lives in `workflow_state.json`'s `inputs` and one `journal.jsonl` line.

**Still 12 tools** — λ is four *explicit* parameters on tools that already existed. Every one of them is
**default-off**: omit it and the input is byte-identical, because a tool that silently "corrects" a wavelength
would destroy the one thing a refinement is measured against. A doublet-vs-monochromatic choice is **not a
question** (a laboratory tube has Kα2, a synchrotron does not); only when the data *and* the user's input are
both silent does the workflow ask — and then it says what the engine default will be.

### What is new

1. **Four explicit λ write entry points** (all with `wavelength` for a single monochromatic value, or
   `wavelength_doublet: [kα1, kα2, ratio]`):
   * `xrd_refine_manual` — the plan's `lambda`, plus a `lambda_explicit` flag; the engine log states
     `[explicit]` or `[engine default]`, so a default can never be read as a declared value.
   * `cif_to_pcr` — the pcr's `! Lambda1 Lambda2 Ratio` **value line** is rewritten after conversion
     (only that line; the trailing Bkpos/Wdt/Cthm/… fields are preserved), and 改前→改后 is returned.
   * `xrd_refine_auto` — the driver sets that value line on its **working copy** of the pcr (the file
     fp2k actually refines, inside its `.fp2k-work-*` directory) **before** the run; the pcr the caller
     passed in is left untouched.
   * `xrd_refine_gsas` — GSAS-II's own `wavelength=` when the histogram is created, or the
     `Lam`/`Lam1`/`Lam2` value slots when an existing `.gpx` is continued.
2. **`xrd_data_io` gained `header` and `evidence`** — a **positional** header dump (everything before the data
   starts, located as the first run of ≥3 consecutive rows with ≥2 numeric columns) so a real vendor header can
   be read as it is. They are **raw evidence only**: they never parse, rank or resolve anything, and in
   particular **no wavelength is decided there**. Measured on a real Rigaku RAW: **141 header lines, data at
   line 142**, with the `Angle, PSD,` column-label row correctly kept out of the data run.
3. **`engine/wavelength.py` is now a pure lookup table** (the Kα doublet of Cu/Ti/Cr/Fe/Co/Ga/Mo/Ag/In), taken
   from GSAS-II's own `GSASIIElem.waves` so the plugin and the GSAS engine agree. A synchrotron source has **no
   entry** — there the value must come from the user. The classic tabulation this plugin used to default to
   (Cu 1.54056/1.54439) is kept **separately** and flagged, because it differs from the table by ≈5e-5 and
   **which convention was used has to be reported**.
4. **Three skills updated**: `xrd-tools` (the λ contract and the `header`/`evidence` semantics),
   `xrd-preflight` (λ source reconciliation at data intake: `Anode`/`Alpha1`/`Alpha2`/`AlphaRatio`/
   `Monochromator`/`ActuallyUsedLambda`, and the trap that `Monochromator=0` makes `ActuallyUsedLambda` a
   **Kα average, not Kα1**), `xrd-workflow` (`inputs.wavelength` + the record/one-question/label-the-assumption
   rules).
5. **A new audit suite, `audit/verify-wavelength.mjs`** (in `verify-all.mjs`): the four tools' schemas, the
   mutual-exclusion refusal, **one presence-test assertion per writer** (so a refactor that drops the guard
   fails), the real-pcr writer (mono / doublet / idempotent / no-banner), the table against GSAS-II's numbers,
   and `header` on hostile headers (duplicate keys, a base64 value containing `=`, a line with no `=`, a
   multi-value line) — no fp2k, no GSAS-II, no refinement.

### Three defects this feature's own code contained (all three fixed)

The first two were found by the new audit suite; the third **only** by the host end-to-end run — which is the
point of running one.

* **The `--out` flag was read as a wavelength value.** The helper's `main()` took `sys.argv[4]` as Kα2, but the
  tool layer calls `setwavelength <pcr> <k1> --out <json>` — so `float('--out')` raised, the exception was
  caught into `ok:false` plus a warning, and **the monochromatic wavelength was never written** on that path.
  An earlier unit test had only used the bare CLI form, which is why it passed. `--out` is now stripped before
  the mode arguments are read, and the values are positional — the discipline that follows is in the suite:
  **a writer test must pass `--out`, because that is how the tool calls it.**
* **Line endings were rewritten.** `_read()` normalises CRLF to LF, so writing back with `newline='\n'` turned a
  CRLF pcr into a mixed-ending file (and, after the fix, `'\r\n' in raw` could never be true — the separator has
  to be probed from the **bytes**). The shipped templates are CRLF, so this was reachable in normal use. The
  value-line writer now writes the whole file back with its own separator, and the suite asserts that **only the
  value line differs** from the template.
* **`cif_to_pcr` returned a key its own output schema did not declare** — and the host validates a tool's return
  value against that schema with `additionalProperties: false`, so the whole call failed with
  `tool "cif_to_pcr" returned invalid output: "value.wavelength" is not a declared property`. This is the one
  class of defect an isolated suite cannot see: the handler was right, the λ was written, and the *interface*
  rejected the result on the way out. `xrd_refine_auto` (the other tool that returns a λ echo) had been declared
  from the start; `cif_to_pcr` had not. `audit/verify-wavelength.mjs` now asserts, for both, that the output
  schema declares `wavelength` — **found by the post-restart host run on 2026-10-03**, not by the suite it was
  written into.

Also fixed here: `engine/xrd_engine.py`'s `load_data()` used the platform default encoding, so a UTF-8 data file
with a Chinese `#` header died with `UnicodeDecodeError: 'gbk' codec can't decode byte 0x80` (hit on a real
test-set file). It now reads UTF-8 with `errors='replace'`.

### Evidence grades

* **(a)** entry ① on real data: the same pattern (Y₂O₃, 7351 points) and structure, only λ changed — engine
  default Cu doublet **Rwp 41.53 %**, explicit monochromatic 1.5406 **Rwp 44.04 %**. The worsening is the
  expected direction (the pattern contains Kα2 and the monochromatic model does not), and the two log lines
  name their own source.
* **(a)** `header` boundary on a real Rigaku RAW (141/142 above); **(b)** `header` on the hostile-header
  fixtures in the suite.
* **(b)** the pcr writer on a real pcr: value line only, echo, `.prewavelength` backup, idempotent second call
  reporting `changed=false`, and a pcr without the banner reporting `ok:false` plus a warning instead of
  pretending it wrote.
* **(b)** the target table against GSAS-II's own values (Cu/Mo/Co) and the no-entry case for a synchrotron.
* **(c)** (withdrawn by the run below) the `fp2k_driver` and `gsas_engine` write paths started out pinned by
  the suite's presence tests only — the real-project run in the next bullet replaced this grade.

* **(a)** entries ③ and ④ on a real in-house project (a milled Prussian-blue-analogue,
  KxFe0.3Mn0.7[Fe(CN)6], the same campaign the S-series standard samples came from): the λ decision came
  from the user's own measurement data (the Rigaku RAW header: `Anode=Cu`, `Alpha1=1.5406`, `Alpha2=1.54439`,
  `AlphaRatio=0.5`, `Monochromator=0` — i.e. Kα2 is present, so the tube value is a **doublet**), and it was
  passed explicitly as `[1.5406, 1.54439, ratio]`.
  * `cif_to_pcr` on the project's CIF: without the parameter the produced pcr carries the **converter's own**
    `1.540560 1.544390 0.50000`; with it, `1.540600 1.544390 0.50000` — and **line 11 is the ONLY line that
    differs** between the two pcrs (measured by comparing both files line by line). The 4e-5 difference
    between the two Cu conventions is exactly the thing the workflow requires to be reported.
  * `xrd_refine_auto` (real fp2k run, `ladder:false`, 5 cycles): the driver's log shows
    `wavelength: pcr Lambda1 line set to [ 1.540600 1.544390 0.49700 …] (was [ … 0.50000 …])`, and the
    work-directory pcrs (`…_baseline_start.pcr`, `…_baseline_end.pcr`) carry `0.49700` — i.e. it was set
    **before** fp2k ran. The caller's pcr still reads `0.50000`: the write is to the working copy. (That
    baseline run's own Rwp is 414 % — a fresh converter pcr with an unrefined scale, and **not** a
    fit-quality claim; what is being verified here is where the λ landed, not the fit.)
  * `xrd_refine_gsas` continuing a **copy** of the project's `.gpx`: read back with GSAS-II itself, the
    instrument slots went from `Lam1 = [1.5405, 1.5405]`, `Lam2 = [1.5443, 1.5443]` (the instrument file's
    own values) to **`Lam1 = [1.5406, 1.5406]`, `Lam2 = [1.54439, 1.54439]`** (the vendor doublet), in both
    the delivered project and the ladder-best snapshot, while the project handed in is unchanged
    (non-destructive by construction). `wR 14.78 %` on that continued project.

### Unverified / open

* The `header`/`evidence` **tool-layer** call cannot run under a restricted sandbox that denies piped stdio
  (`spawn EPERM`); the suite says so and drives the engine directly instead. With a wider sandbox the same
  check runs through the tool layer and passes (`FAIL 0 | WARN 0 | SKIP 0` in the packaged run).
* Only the **Cu** end of the table has been exercised on real data; every other anode is a table lookup.
* The **classic-vs-table Cu convention** (1.54056/1.54439 vs 1.54051/1.54433) is a real choice that must be
  stated wherever a λ value is reported; nothing in the plugin decides it for the user.
* Carried over untouched: `xrd_data_io` cannot read a GSAS `.XRA` and mis-handles an `.fxye` centidegree axis.
* `cif2pcr_helper.normalise()`/`assemble()` still write with `newline='\n'`, so a CRLF pcr passed through them
  comes back LF-only (not mixed, but changed). Only the λ writer was fixed here — minimal change.

### End-to-end status (2026-10-03)

**A host-level end-to-end run of 0.9.0 was made after a dsh restart** (the same day, after the profile copy was
synced): three calls through the tool layer on real data — `xrd_data_io` `header` on the in-house Rigaku RAW
(**141 header lines, data at line 142** — the λ keys `Alpha1=1.5406`, `Alpha2=1.54439`, `AlphaRatio=0.5`,
`Monochromator=0` are all in the dump), `xrd_refine_manual` with the explicit vendor doublet on that project's
real pattern and model (**Rwp 16.88 %**, baseline 96.54 %; `plan_calibrate.json` carries
`lambda = {kalpha1:1.5406, kalpha2:1.54439, ratio2:0.5}` and `lambda_explicit: true`), and `cif_to_pcr` with the
same value — which is how the output-schema defect above was caught. Two observations from that run, reported
as-is: the engine's liveness tick failed twice with `WinError 5` when renaming its temp file (the run itself
completed; no cause is claimed), and the manual engine's `result.json` does **not** echo λ — the record is
`plan_calibrate.json`'s `lambda_explicit`, by design (the manual engine is deliberately left alone).

The 0.8.9 host status (Y2O3: fp2k Rwp 16, GSAS-II wR 11.81 %, `xrd_export_cif` verdict `pass`) is unchanged and
is not extended by this entry.

**This build must not be used for a publication or a formal delivery** on the strength of this entry: the host
run covers three calls on one project, the λ convention question is left to the user, and the `xrd_data_io`
intake defects above are open.

## 0.8.10 — 2026-10-02 — **the Ⅱ→Ⅲ hand-off: per-phase switches, a ladder that can undo itself, and a ≥3-phase hook — all of it exposed by trying to reproduce a GSAS-II standard sample**

This release is the two-phase / multiphase path. It exists because a two-phase project that entered GSAS-II
from an already-good state was destroyed by the one-shot ladder (30.42 % → 77.19 %, cell collapsed), and
because the user corrected the diagnosis: the design order is "**settle the two phases with the bounded manual
engine first, then let GSAS-II do the final refinement**", so the real gap was never GSAS-II's ability to
bootstrap a two-phase fit — it was that **nothing crossed the Ⅱ→Ⅲ boundary** (a fitted phase ratio had no way
in, so GSAS-II restarted from 1:1 and absorbed the difference into background and peak shape).
**Tool count 11 → 12** (the first new tool since 0.3.0), one new skill, and five defects that the verification
runs exposed — two of them fatal to the very feature being verified.

### What is new

1. **`xrd_refine_multiphase` — the ≥3-phase entry with an AI-editable engine** (grade (a): a real 3-phase
   GSAS-II project built from three CIFs, with a real manual-engine hand-off, ran the template end to end —
   4 ladder steps, `phases_count: 3`; grade (b): the tool-layer gates are pinned by
   `audit/verify-multiphase.mjs`). Hard gate **≥3 phases** at the tool layer *and* again at run time inside the
   engine (`MIN_PHASES = 3`), and a **fresh build without `handoff` is refused** ("refine the TWO most abundant
   phases with `xrd_refine_manual` first"). `init_engine:true` copies the bundled template next to the
   artifacts and runs nothing, so a caller can edit the `# == EDITABLE ==` orchestration and execute that copy
   with `engine:"<path>"`.
2. **`xrd_refine_gsas` gained `phases[]`, `entry`, `rollback`, `rollback_tol`, `handoff` and the `scale`
   family** (grade (a): every claim below was measured on real GSAS-II runs of the S3 standard sample).
   `phases:[{...}]` carries per-phase `cell/scale/size/mustrain/hstrain/pref/atoms` — absent ⇒ the 0.8.9
   behaviour is bit-for-bit what it was. `entry:"hold"` (the default when continuing a `.gpx` or when a
   `handoff` is given) releases one family at a time and keeps a step only when wR improves; `"direct"` stays
   the old single batch. **The delivered `.gpx` is the best accepted state, not the last one** (same principle
   as the fp2k driver, which delivers its cached start state).
3. **`handoff:"<manual result_*.json>"` — the Ⅱ→Ⅲ contract** (grade (a)): the 2nd+ phase ratio
   (`s2_<i>_scale / scale` → `Scale`), the main-phase cell and the instrument `U/V/W/X/Y/Zero` (unit-converted)
   are seeded before the ladder; everything else is reported in `not_transferred` instead of being invented.
   Two guards: a phase the manual run fitted to ≈0 raises a warning (it starts invisible), and an instrument
   value more than 10× away from the project's is **refused**.
4. **A new skill `xrd-multiphase`**, plus updated `xrd-refinement` (§4.5.3, the design order and the 2-phase
   flow), `xrd-tools` (12 tools, the new parameters and result fields) and `xrd-workflow` (count) — and a new
   audit suite `verify-multiphase.mjs` (**13 checks, FAIL 0**) wired into `verify-all.mjs`.

### Also (the self-audit module)

* **The audit module was updated for the twelfth tool, and now proves the release from inside the package.**
  The new suite `audit/verify-multiphase.mjs` (**13 checks**) covers the ≥3-phase gate, the missing-`handoff`
  refusal, the `init_engine` hand-over and the two DOA regressions above, and it is wired into
  `verify-all.mjs`. Four tracking assertions were stale *by design* and moved with the change that
  invalidated them: the tool count in `verify-autofp-replaced.mjs` (11 → 12), the `onGsasRound()` call sites
  in `verify-review-detector.mjs` (2 → 4 = two GSAS-family tools × success + failure), the explicit-fuse
  sites in `verify-proc-supervision.mjs` (2 → 3 backends) and the plan-`run_id` count in
  `verify-result-freshness.mjs` (8 → 9, with the `readFreshResult` assertion now tied to that number so a
  new plan builder cannot slip through without a gate).
* **Two defects in the audit module itself were fixed** (grade (a): both were found by running the
  *package's own* `verify-all`). Three suites resolved the plugin tree through a hardcoded `../../dsh-xrd`
  fallback that only ever worked in the maintainer's dev layout; inside a package they exited with
  `no plugin tree at …\dist\dsh-xrd`, so a release tree could not self-verify without `--root` — they now
  use the package-aware `pluginRoot()`. And `verify-all.mjs` mapped **any** child exit code 2 to `MISSING`,
  which sent the reader hunting for a file that was right there; its own skip sentinel is now distinct, and
  a child's own exit codes are reported as such.

### The defects the verification found

5. **The rollback restored exactly the state it was supposed to undo** (grade (a): the S3 two-phase project;
   grade (c): the GSAS-II source that explains it). Every step logged `rolled_back:true`, yet each step's
   `wR_before` equalled the *previous* step's rejected `wR_after`, and the delivered project carried the
   rejected CuO cell **4.4989 / 3.0618 / 4.7171 Å, β 108.26°** while its input cell was
   **4.6795 / 3.4206 / 5.1253 Å, β 99.83°**. Cause: `G2Project.refine()` is **file-based**
   (`GSASIIstrMain.Refine(self.filename)` plus `index_ids()` auto-save) and `save(other_path)` **rebinds
   `self.filename`** — so the "snapshot" became the working file and the next refinement overwrote it.
   Snapshots are now **file copies** with `self.filename` pinned to the delivered project: re-measured, all
   three rejected steps report `wR_before = 28.646699` (the best) and the delivered cell is intact.
6. **A step GSAS-II itself reports as failed was accepted like any other** (grade (a): two independent runs;
   grade (b): a targeted reproduction). `GSASIIstrMath.py` prints *"Invalid cell metric tensor for phase #1
   (CuO)"* and **returns normally**, leaving the parameters half-updated; acceptance was decided on wR alone,
   so a broken state could be crowned "best". Each step's stdout is now tapped, a failure forces
   reject + rollback, and the step carries `gsas_error`. The reproduction (a deliberately corrupted project)
   returns `gsas_error = "Invalid cell metric tensor for phase #1 (CuO)"` and the summary
   *"0 of 1 step(s) accepted: the delivered project is the state it entered with"*.
7. **`entry` came back as a phase record instead of `"hold"`** (grade (a)): the per-phase loop reused the name
   `entry`, shadowing the policy variable. Renamed; the field now reports the policy.
8. **The hand-off's instrument transfer was dead code — three faults deep** (grade (a) + grade (b): a
   three-variant controlled probe on one project).
   * `h.data['Instrument Parameters']` is a **list of per-bank dicts**, so `key in ip` never matched and the
     transfer silently did nothing while reporting "nothing usable".
   * The value lives at **index 1** of an entry (`[reference, value, refine-flag]`; GSASIIstrIO reads
     `inst['Lam'][1]`). Measured: baseline wR 25.849063 with initial χ² 47812; writing `U` into slot 0 gave
     **identical** wR and χ²; writing it into slot 1 changed the initial χ² to **2.9175e+05** and the
     refinement diverged.
   * Units are mixed inside GSAS-II: manual **degrees** → `U/V/W` **×1e4** (centidegrees²), `X/Y` **×100**
     (centidegrees), but `Zero` **×1** (degrees — `G2lat.Dsp2pos` adds it to `2·asind(...)` in degrees).
     Measured: seeding `Zero` as if it were centidegrees (−0.028) collapsed the fit from **wR 25.90 % to
     74.54 %**.
   * The guards are measured too: the same hand-off's instrument seed was 217× / 587× / 170× the 11-BM
     calibration values. With the seed accepted the ladder ended at **71.82 %** (against **25.53 %** for the
     identical run with no instrument transfer at all); with the seed **refused** — and the `Zero` unit
     corrected — the same hand-off ended at **25.03 %**. The multiphase template's smoke test showed it
     independently: 62.19 % with the seed accepted, **25.34 %** with it refused.
9. **The multiphase template was dead on arrival, twice over** (grade (a): its first smoke run).
   It called `run_guarded_main(main, backend="gsas")` while `proc_guard.run_guarded_main` takes one argument
   (`TypeError` before a single line of the fit — hidden until then by the template's own permissive
   `**kw` shim), and its `run()` imported GSAS-II without ever putting `gsas_root` on `sys.path`
   (`ModuleNotFoundError` on every call). Both are now asserted in `verify-multiphase.mjs` §6.

### How much of this is actually verified

* **(a) real data, end to end** — items 1, 2 (container-level), 3, 5, 6, 7, 8 and 9 above, all measured on
  **real GSAS-II runs** against the S3 standard sample (CuCr2O4 + CuO, the 7 K frame `OH_00.fxye`,
  `# Temp (K) = 6.778`) and, for item 1, on a real 3-phase project. GSAS-II **is** installed on the machine
  these runs were made on and was used for every one of them; nothing here is inferred from a machine that
  lacks it.
* **(b) isolated reproduction / unit level** — the slot probe of item 8 (one project, three variants, one
  variable), the failure-detector reproduction of item 6, and the tool-layer gates of item 1
  (`verify-multiphase.mjs`, 13 checks, no GSAS-II needed).
* **(c) source-level review only** — the GSAS-II internals quoted in items 5/6/8 (read from
  `GSASIIscriptable.py` / `GSASIIstrIO.py` / `GSASIIlattice.py` / `GSASIIstrMath.py`); their *behaviour* is
  grade (a)/(b), the mechanism is read from source.
* **Package self-proof (2026-10-02, grade (a))** — the audit module that ships **inside** the release tree
  passes **14/14 suites** there (`node audit/verify-all.mjs`, including the real fp2k ladder and real
  GSAS-II runs), and the release chain is green end to end: `LEAK AUDIT: clean (0 findings)`, 108 archive
  entries with both fp2k executables marked executable, three copies agreeing byte for byte
  (source == profile, package == sanitise(source)) and `verify-package.mjs` reporting
  `PACKAGED RELEASE OK`. This is what the package can prove about itself — it is **not** a host run
  (see below).

### What was NOT verified, and what was not achieved

* **`xrd_refine_multiphase` has never been called through the host tool layer.** Its gates are pinned at the
  tool layer (grade (b)) and its engine ran on a real 3-phase project (grade (a)), but the host needs the
  sync + a dsh restart before the twelfth tool exists for the running host.
* **The ≥3-phase engine path was exercised as a machinery smoke test only**: the third phase was a
  placeholder CIF (Y2O3) on the S3 frame, because no genuine 3-phase standard sample exists here. It proves
  the plumbing (gate, hand-off, per-phase steps, ladder, result), **not** that a 3-phase scientific fit
  converges.
* **The S3 sample does not reach the tutorial's precision**: best measured **22.19 %** vs the official
  **13.859 %** (Rw < 14 %, GOF 1.08). Measured causes: CuO's cell refinement fails with
  `Invalid cell metric tensor` every time it is released; CuO's profile is only switchable at **model** level
  here while the tutorial adds a **single term** (D13); and CuO carries only ≈**1 %** of the integrated
  intensity (sumInt 1230 vs 82811). **User ruling (2026-10-02): this sample is not usable as a two-phase
  benchmark — the second phase is too weak and gets swallowed.** It is kept as a documented limitation, not
  as a target the engine is claimed to meet.
* **S2 (fluoroapatite, single phase) measured wR 9.32 % vs the official 10.38 %** — better, but **not a
  like-for-like claim**: our ladder released more families than the tutorial's recipe (10 background terms,
  six instrument parameters, all atoms `XU`), and our `a` differs from the official value by ≈7σ, i.e. it is
  **not the same solution**. "Lower R" is not "better structure".
* **Known unfixed data-intake defects remain** (carried over, not touched here): `xrd_data_io` cannot read a
  GSAS `.XRA` and converts an `.fxye` centidegree axis only above x > 180 — both **silently**, with wrong
  point counts. Every standard-sample case in the test set routes around it by letting GSAS-II read the data.

### End-to-end status (2026-10-02)

**No host-level end-to-end run of 0.8.10 has been made yet** — the host still loads 0.8.9 until the profile
copy is synced and dsh restarted, so the new tool and the new parameters are, at this moment, verified at
engine level and audit-suite level only. The 0.8.9 host status stands unchanged and is not extended by this
entry: one standard sample (Y2O3), fp2k ladder **Rwp 16**, GSAS-II **wR 11.81 %**, `xrd_export_cif` verdict
`pass` — one sample on one machine.

**This build must not be used for a publication or a formal delivery** on the strength of this entry: the
twelfth tool has no host-level run, the ≥3-phase path has only a machinery smoke test, the S3 two-phase
target is not met, and the two `xrd_data_io` intake defects above are open.

## 0.8.9 — 2026-09-28 — **the detector that switched itself off: the plugin's own reminder counted as a human takeover, plus a split review ledger nobody could see**

Found by the **0.8.8 end-to-end acceptance run** on the host (the same run that graded 0.8.8's three
fixes as passing), not by an external review. One behavioural defect, one migration, one new
warning. Still **11 tools**; no signature changed.

### The defect

1. **The refinement-review detector read its own injection back as human input** (grade (a): the
   state file after a real run; grade (b): six new controls in `verify-review-detector.mjs` §12).
   `scanHumanTakeover()` decides "the human took over" from `user/message` session events whose
   sequence number is above the last one seen — but the plugin's reminders are injected with
   `agent.inject(...)`, i.e. as `role:'user'` messages carrying `source.kind:'plugin'`, and the host
   records them as `user/message` events like any other. On the next round the detector therefore
   treated **its own status-table text** as the human speaking: measured on Y2O3,
   `takeover_history[0].snippet` was literally the injected reminder and `detector_enabled` went to
   `false` from round 2 onwards. Because round 1 has a deliberate "absorb the baseline" early return,
   this was not a corner case — it was **inevitable on the second round of any sample**, after which
   D19/D20 went silent for that sample (no E reminder, no scheduled review, no freeze). Injected
   events are now filtered out **before** the newest-seq bookkeeping — recognised by
   `source.kind === 'plugin'` (the contract is the host's `UserMessage`) or, for a host that does not
   surface `source`, by carrying **both** bilingual markers — so `last_user_seq` still tracks the
   newest HUMAN message. Controls: the real `injectedMessage()` object does not close the detector; a
   marker-only event (no `source`) does not either; a genuine human message still does.

### The migration

2. **Closures the plugin caused itself are repaired on load** (grade (b): the repair and both
   controls are new checks). A sample already closed by 0.8.8 would otherwise stay closed for the
   rest of its life — and "resume" requires a human sentence that may never come. `loadState()` now
   clears a closure **only** when every recorded `close` snippet carries the plugin's bilingual
   marker (a real human message does not, and a MIXED history is left alone: the plugin does not
   guess), resets the streak/level/E counters, and appends an `auto-resume` record so the repair is
   auditable instead of silent. The repair is written immediately: a state that is only ever loaded
   would otherwise keep the detector off.

### The warning

3. **A review history split by 0.8.7 is now visible** (grade (b): new checks). 0.8.8 unified the
   ledger key, but the ledgers 0.8.7 already wrote stay on disk, so a caller could keep appending to
   one file while earlier rounds sat in another. `xrd_refine_gsas` now pushes a `splitLedgerWarning`
   into its `warning` list when a sibling ledger looks like the same sample under another file name
   (`Y2O3` / `Y2O3_gsas`), naming the sibling and how to book into the intended ledger. Rounds are
   **never** merged automatically — merging would invent a history.

### The follow-on defect, found while re-running the gate

4. **A per-step checkpoint could be rate-limited away** (grade (a): the tick file on disk; grade (b):
   a new isolated probe). `Guard.checkpoint()` went through the same `tick_s` (5 s) rate limit as a
   heartbeat, so in a run that finished inside one tick window only the initial `phase:"start"` was
   ever written: measured 2026-09-28, a real two-step GSAS refinement (~5 s) left
   `phase:"start", seq:1` on disk while both `gsas refine step 1/2` and `2/2` checkpoints had run —
   which is exactly the evidence `audit/verify-gsas-ops.mjs` reads, so that suite had been passing
   or failing on **timing** rather than on behaviour. A checkpoint is a step boundary, not a
   heartbeat, and the number of them is bounded by the number of steps, so it is now written
   unconditionally (`force=True`). The new probe asserts the phase of the last checkpoint survives
   two ticks inside one 5 s window.

### Also

* `audit/verify-review-detector.mjs` §12 (+12 checks, **150/150**),
  `audit/verify-review-key.mjs` §4b (+8 checks, **52/52**, the gated real GSAS-II intake control
  still included) and the checkpoint probe in `audit/verify-proc-supervision.mjs` (**186/186**).
### End-to-end status (2026-09-28, on the host, after a dsh restart)

**0.8.9 has been run end-to-end on the host and the plugin is working normally.** After restarting dsh
so the 0.8.9 JavaScript was actually loaded, one standard sample (Y2O3) went through all eight stages
with the real tools and the real engines — `xrd_cif_edit` (Ia-3, 80 sites) → `xrd_data_io`
(7351 points, 3.000–150.000°) → `cif_to_pcr` (occupancy audit `pass`) → `xrd_refine_auto` (fp2k
ladder, **Rwp = 16**, 11 accepted steps) → `xrd_verify_fp2k` (**Rwp 16.0 / Rp 15.4 / Re 4.59**,
ρ = 5.026 g/cm³, a = 10.60796 Å) → `xrd_refine_gsas` (**wR = 11.81 %** on the bare two-column
pattern, a = 10.600267 Å, gof 3.79) → `xrd_finalize` (deliverable package with gpx / lst / fit figure /
report / CIF) → `xrd_export_cif` (**verdict `pass`**, O–Y 2.232–2.349 Å, 0 anomalous values). Every
tool's engine log reached the caller as real text (no `[object Object]`).

All four fixes in this release were re-verified **in the host**, not only in the suite: the injected
reminder no longer closes the detector (a clean control directory ran two rounds in one turn — round 1
injected, round 2 read that injection back and stayed ON with an empty `takeover_history`); a closure
0.8.8 had already recorded was repaired on load with an `auto-resume` record; the split-ledger warning
fired; and the tick file now carries `"phase":"gsas refine step 4/4","seq":8`.

**What that statement is not.** It is one standard sample, on one machine, today: it says the shipped
tools work end to end, not that every parameter path, every data format or any other sample has been
exercised. Two known gaps stay open and are **not** covered by it — the `write_fxye` empty-`title`
edge case (a caller-supplied empty title would put `BANK` on line 1, and no shipped path does that) and
the split ledger left behind by 0.8.7 (`review_mechanical_<sample>_gsas.json`), which this release
reports as a warning instead of merging. **A dsh restart is still required for these JavaScript changes
to take effect in a running host** (they are loaded at plugin start).

## 0.8.8 — 2026-09-28 — **a host-level end-to-end run (Y2O3): the diagnostics that were swallowed, the GSAS-II reader filter that blamed the data file, and one sample booked into two review ledgers**

Not a review round: this release comes from running the **shipped 0.8.7 tools on the host**, in
order, on one simple standard sample (CIF → pcr → fp2k ladder → verify → GSAS-II → finalize →
export). The chain itself worked (fp2k Rwp ≈ 16, GSAS-II wR = 11.81 %, `xrd_export_cif` verdict
`pass`, O–Y 2.232–2.349 Å), and it exposed **three defects** that no unit test in this repository
could see — all three are silent: nothing crashes, the result just loses its meaning. Still **11
tools**; two tools gained optional arguments.

### The defects

1. **Every engine's stdout/stderr arrived as the literal string `[object Object]`** (grade (a): the
   end-to-end run showed it in four different tools; grade (b): the fix is now a control in
   `verify-proc-supervision.mjs` §6). `runEngine` probed the host's collected-output reader for
   `text` / `read` / `toString`, but the real reader
   (`@deepseek-ai/dsh-subprocess-local`'s `OutputCollector`) exposes `readFrom(byte) -> {text,…}` —
   so the probe fell through to the **inherited** `Object.prototype.toString` and returned
   `"[object Object]"` as a perfectly valid string. Consequences: `log_tail` was useless in all
   eleven tools, and a failure reason that should have carried the engine's own traceback carried
   two lines of `[object Object]` instead. It also **hid defect 2**: GSAS-II had printed
   `No readers matched hint 'GSAS powder'`, and that sentence never reached anyone.
2. **GSAS-II was always called with `fmthint='GSAS powder'`** (grade (b): isolation control, one
   variable). `GSASIIscriptable.import_generic` skips every reader whose `formatName` does not
   contain the hint (`if fmthint is not None and fmthint not in reader.formatName: continue`), and
   only `G2pwd_fxye` is named *"GSAS powder data"* — so **any** non-GSAS data file (bare two-column
   text, Topas `.xye`, FullProf `.dat`, Bruker RAW, csv) failed with `No reader could read file`,
   which blames the file and not the filter. Measured with the same file otherwise untouched: no
   hint → read as **7351 points** through the Topas xye reader; `'GSAS powder'` → **no reader**.
   The hint is now **unset by default** (GSAS-II tries every powder reader and keeps the first that
   works) in the tool layer, in `gsas_engine.py` and in `gsas_ops.py` (two intake paths), and it is
   exposed as an optional `fmthint` argument for pinning one family deliberately. A failing import
   now also names the file, the hint in force and — **matched to the cause the error actually shows**
   — either the format families GSAS-II does read or the missing instrument-parameter file
   (`instprm`); the first version of that message appended the format advice unconditionally, so a
   pattern that had read perfectly well was told to convert its file.
3. **One sample was booked into two refinement-review ledgers** (grade (a): two state files on disk
   after one run). The ledger key was `stem(gpx ?? data)`, so the same physical sample became
   `Y2O3` for `data=Y2O3.dat`, `Y2O3_gsas` after converting the pattern to `Y2O3_gsas.fxye`, and
   would have become `gsas_proj` when continuing from the project — splitting the round counter, the
   B/C/D streaks, the E staleness lag and any freeze, so "review every 3 rounds" could never fire
   across a real multi-stage run. The key is now derived in one place (`reviewSampleKey`, exported
   for tests): explicit `sample` → **CIF stem** (the structure identifies the sample, and
   `cif_to_pcr` names the pcr after the same CIF) → data stem → the directory's **single** existing
   ledger when continuing a `.gpx` → gpx stem. The resolved key travels back as `review_sample`,
   and `fmthint_used` reports the hint actually in force, so neither failure can hide again.

### Retracted in the same round (my own false positive)

An intermediate finding claimed the plugin's own `.fxye` writer produced a file GSAS-II cannot read
(`BANK` on line 1, which the GSAS reader treats as a comment). That was an artefact of **my
hand-copied reproduction**: `write_fxye` writes a `#` title line whenever a title is given, and the
real product of `xrd_data_io {convert, to:'fxye'}` does start with `# Y2O3.dat`. Re-tested with the
real file: GSAS-II reads it — 7351 points, with and without a hint. The claim is withdrawn; only the
`title=''` edge case remains (a caller-supplied empty title would put `BANK` on line 1), and no
shipped path does that.

### Also

* Two `xrd_refine_gsas` schema fields the callers can rely on: `review_sample` and `fmthint_used`
  (both optional, both appear only when known).
* New audit suite `audit/verify-review-key.mjs` (**44 checks** — 41 that always run, plus a
  **gated real GSAS-II intake control**: `DSH_XRD_GSAS_INTAKE=1` imports the shipped two-column
  fixture with no hint and then asserts the SAME file still fails under the old pinned hint; it
  resolves python through the plugin's own chain and needs no external input). Registered in
  `verify-all.mjs` and asserted in `verify-package.mjs`'s shipped-file list. The suite covers the
  collected-output reader contract including the `[object Object]` trap, the ledger-key derivation
  table, and a source guard that no default reader hint returns. `verify-proc-supervision.mjs` §6
  additionally exercises the real `readFrom` collector shape and an unreadable collector (which must
  yield `''`, never a fake string).
* `xrd_refine_gsas`'s `sample` argument is also how a caller merges ledgers that an earlier version
  already split (name the sample once and the next round joins it).
* **The shipped audit module now proves itself FROM the package** (measured 2026-09-28 by extracting
  `dsh-xrd-0.8.8.zip` into a clean directory and running its own `audit/verify-all.mjs` there):
  `audit/py/ctrl-fp2k-orphan.py` resolves the tree through `DSH_XRD_ROOT` (the value
  `verify-all.mjs` exports for `--root`) and the package layout, so a received package reports
  **13/13** instead of "1 of 13 did not pass" for a control that only could not find the tree. The
  extracted tree passes every suite — 138/138 detector, 44/44 review-key (with the real GSAS-II
  intake control), 185/185 process supervision, the orphan A/B/C/D control included.

## 0.8.7 — 2026-09-28 — **a fourth review (of 0.8.6): the wait that could never end, a name that could write anywhere, fp2k results the wrapper could flip to success, and a skill mirror that never upgraded**

A fourth independent review (codex, 2026-09-28, of **0.8.6**) reported 4 release blockers, 3
high-priority findings and 5 smaller ones. All twelve were checked against this tree and **all twelve
hold** — no false positive. Still **11 tools**; the schema grew only by `cif_copied` (0.8.6),
`stopped`/`aborted` on `xrd_refine_auto`, and `rwp_sum`/`rwp_source` (signature unchanged).

### Release blockers

1. **The bounded wait could never end — a regression introduced by 0.8.6's own fix** (grade (b): the
   review's probe is now a control). 0.8.6 changed the exit ceiling to "`grace + slack` from the
   moment a termination is requested" but recomputed it with a fresh `Date.now()` **on every loop
   iteration**, so in the terminating state the deadline moved with the clock and never arrived: a
   host transport whose `done` never settles still hung the call forever. The deadline is now frozen
   ONCE, at the first termination request. Measured with the shipped slack: the call returns in
   **32 s** with `exitObserved:false` instead of never. An already-cancelled call also no longer sits
   out the stop grace: with no child and no handle the ladder returns at once (**0 ms** where it
   waited 5 s).
2. **A name carried by the pcr could write anywhere** (grade (b): new control with a `../` name).
   `xrd_refine_auto` copies the pattern under the `<stem><ext>` name fp2k looks for AND, when the pcr
   body declares a different name, under **that** name — and that name is untrusted input. The review
   showed `../escaped-copy.dat` landing a full copy of the pattern outside the work directory. Only a
   bare file name is accepted now, the resolved target must still be inside the work dir, and a
   refusal is reported as a warning instead of being silent.
3. **The wrapper could turn a failed fp2k run into a success** (grade (b): `ok:false` + a numeric Rwp
   is asserted) — and the statistics behind the number were inconsistent. Six linked defects:
   * `index.js` built its result with `ok: true` whenever the Rwp was numeric, so a driver that
     reported `ok:false, Rwp:9.9` came back as a finished refinement. **`driver.ok` is authoritative
     now**, and `stopped`/`aborted`/`error` travel with the payload.
   * the **baseline** was measured with `run_fp2k_mean(..., trial_repeats)` but WITHOUT
     `restore_lines`, so its three runs were a write-back chain rather than three measurements from
     one state — while the candidates did pass `restore_lines`. Both sides are pinned to one state.
   * the baseline was allowed to continue on 1 of 3 successful runs while a candidate required 3/3.
     **Both now demand the full repeat count** (the baseline fails loudly with what to do about it).
   * `ladder:false` promised "score the structure once" but ran the baseline three times. Score-only
     now runs **exactly one** fp2k call (verified: one `.out` in the work dir).
   * score-only used a hand-built `first` dict with no `ok`, no representative run and no cached
     start state, so a SUCCESSFUL score-only run wrote `ok:false` and delivered `null`. It now keeps
     the full baseline object (`delivered_from` present, `ok:true`, Rwp 81.1 in the control).
4. **The skill mirror deployed only `SKILL.md` and never upgraded** (grade (b): four-state control).
   `syncBundledSkills()` copied `<skill>/SKILL.md` alone, so `xrd-workflow/refs/*` (the defect
   catalogue) and `fullprof-pcr/templates/*.pcr` never reached the user skill root, and it decided by
   **mtime** — a copy the plugin had just written always looked newer than the next release's source
   file, so every later upgrade was skipped forever. Now: the **whole tree** is mirrored recursively,
   the decision is by **SHA-256** against a record of what WE last wrote
   (`~/.dsh/skills/.dsh-xrd-skills.json`), and
   * equal to the shipped file → nothing to do;
   * missing → written (new skills, new refs);
   * differs but equals our record → **upgraded**;
   * differs and is NOT our record → the user edited it: **kept**, and reported;
   * installed before this record existed → upgraded with the previous copy kept as
     `<name>.user-backup-<stamp>`.

### High priority

5. **The background review wrote into the wrong state file** (grade (c): the two call sites).
   `onGsasRound()` loaded the state with the sample key, but `runReviewInBackground()` loaded it
   without — so a per-sample review's verdict landed in the unkeyed file and the sample's own history
   stayed empty. Both use the same key now, and the in-flight map is keyed by **dir + sample** (two
   samples refining into one directory used to look like one review already running).
6. **The background fix had not taken effect** (grade (b): a real ladder run). The polynomial panel
   exposes exactly `background_order` coefficient fields (6 by default) while the ladder's
   "complex background" group asks for terms 3–24, and 0.8.6's guard only fired when the rewrite had
   FAILED — so on a successful rewrite the run still issued ~18 dead steps and reported
   `background_levels_skipped: 0`. The cap is now computed from the real field count: a live run
   reports **18 levels skipped** and the "no such field index" warnings fall from 18 to **1**
   (Rwp 15.4 on the fixture).
7. **A real progress tick could be swallowed by a plain one** (grade (b): guard probe). `tick()`
   rate-limits on a single `_last_tick`, so the manual engine's `progress=True` call right after an
   ordinary heartbeat was dropped — the one tick that proves the fit is advancing was suppressed by
   the one that proves nothing, which can make a healthy fit look stalled. A real progress assertion
   now bypasses the rate limit.

### Smaller findings

8. The fp2k **success** path now carries `assigned` / `alive_after_kill` too (only the failure paths
   did), so a failed job-object assignment is visible on a normal run and the warning can fire.
9. `xrd_verify_fp2k` accepts every data extension the engines do (`.dat/.xra/.fxye/.txt/.xy/.raw`);
   it copied only `<stem>.dat`, so verifying such a pcr failed with "no .sum".
10. `cif_to_pcr`: the converter (and a caller-supplied `tool_path`) now goes through the same
    execution-approval decision as every other program; the user's same-named CIF is restored in a
    `finally`; and the occupancy audit runs **before** that restore, so it compares the CONVERTED CIF
    against the new pcr (it used to check the old CIF against the new pcr).
11. `result_io.write_result()` no longer defaults to `allow_nan=True`: NaN/Infinity are not valid
    JSON, and the JS side reads results with `JSON.parse`, so one non-finite value could make a whole
    run's result unreadable. Non-finite values are written as `null` (counted and named on stderr) —
    verified round-trip through the JS parser.

### Verification recorded for this release

* `audit/verify-proc-supervision.mjs` — **181/181**, §23 being the twelve findings as controls: the
  never-settling `done` probe, the pre-aborted call, the progress tick, the four skill-mirror states,
  strict-JSON results, the `../` name refused, and score-only running fp2k exactly once.
* Real runs for the two items that cannot be faked: a full ladder (background cap, 18 levels
  skipped) and a score-only run (one fp2k call, `ok:true`, Rwp 81.1, `delivered_from` present).
* **Not exercised here, stated rather than implied:** the Windows job-object acceptance under a killed
  parent and the real host `subprocess` service still belong to the deployment environment; a cloud
  reviewer's "no GSAS-II installed" note is consistent with ours (their 158+2 SKIP vs our 181/0).

## 0.8.6 — 2026-09-28 — **a third review (of 0.8.5): one scientific-correctness defect this file had claimed fixed, plus the stop path, the fp2k statistics and the Windows process guard**

A third independent review (codex, 2026-09-28, of **0.8.5**) reported ~30 findings. Each was checked
against this tree: **one was already fixed** (an explicit `plan` no longer splits the run directory),
**two were narrower than stated**, and **the rest held**. Still **11 tools**; the output schema again
grew only by optional, declared fields (`cif_copied`, `warning`, `repeat_runs`).

### The defect this file got wrong, and the claim it must withdraw

**The verifier read the FIRST Rwp, the driver the LAST — and 0.8.5's note said the definitions were
aligned.** They were not: 0.8.5 aligned *which file* is read (`.out` instead of the `.sum`) but left
`.match()` (first occurrence) where the driver uses `re.findall(...)[-1]` (last). Measured on the audit
fixture: fp2k wrote **4** conventional-Rietveld lines and **8** `Rwp:` lines, and the tool reported
**83.6 %** (first) / **75.7 %** (first `.sum`) where the final values are **81.1 %** in both — the
review's own numbers. Every value the verifier reports is now the LAST occurrence, taken by one shared
`lastMatch()`, and `rwp_source` names the occurrence count. `audit/verify-proc-supervision.mjs` §19
re-parses the fixture's own `.out`/`.sum` and compares, so the rule cannot silently regress.
**The 0.8.5 sentence "the same definition xrd_refine_auto reports" is withdrawn as an overclaim.**

### Process control: the stop path was not connected everywhere

1. **`EngineStopped` was swallowed by the engines' own error handling** (grade (b): the fp2k stop
   control now runs a real driver). It derived from `Exception`, and every engine wraps its work in
   `try/except Exception` — a per-action loop, a per-run block, a per-step loop — so a stop request
   raised at a checkpoint was CAUGHT, recorded as "this action failed", and the engine carried on and
   finished normally. Measured: with a stop flag in place the fp2k driver exited **0** with an ordinary
   error result. It now derives from **`BaseException`** (control flow, like KeyboardInterrupt), so no
   `except Exception` can swallow it while `finally:` cleanup still runs.
2. **Five engines had no checkpoint at all** (`cif_edit`, `phase_screen`, `gsas_finalize`,
   `gsas_export`, `xrd_engine`) — so "every engine honours a stop at a safe point", which 0.8.5 stated,
   was true of four of nine. All nine now take one, and the suite asserts the set instead of trusting
   the sentence.
3. **Six tools never passed an `outDir`** (`xrd_finalize`, `xrd_gsas_ops`, `xrd_export_cif`,
   `xrd_cif_edit`, `xrd_data_io`, `xrd_phase_screen` used the legacy `runCli` wrapper): no
   `.xrd-progress.json` was published, the JS stop file landed outside the engine's directory, and the
   liveness policy silently fell back to `fixed`. All six now go through `runEngine` with `outDir`,
   `runId`, `backend` and `ctx`.
4. **The tick and stop files now carry the run id in their NAME** (`.xrd-progress-<run_id>.json`,
   `.xrd-stop-<run_id>`), with the legacy fixed names still written and still honoured, so two runs in
   one directory can no longer overwrite each other's heartbeat (0.8.5 scoped only the file's
   *content*).
5. **A cancel no longer waits for the fuse** (grade (b): the abort control measures it). 0.8.5 bounded
   the exit wait by `fuse + grace + slack`, so a run cancelled at 1 s could still sit for ~29 minutes;
   after a termination request the ceiling is `grace + 30 s` from THAT moment, re-evaluated in 2 s hops.
6. **An already-cancelled call no longer starts a process at all** (grade (b)): 0.8.5 registered the
   abort listener after the spawn (it needs the termination ladder), so a pre-aborted signal still
   created the child and killed it afterwards — the review saw the child's marker file appear.
7. **`assigned` / `alive_after_kill` reach the caller**, and the fp2k guard keys its job-object kill on
   `assigned`, not on the API's return value: an EMPTY job object accepts `TerminateJobObject` and
   returns TRUE, so a failed assignment used to look like a successful whole-tree kill.

### Return semantics

8. **A structured stop result is returned, not thrown away** (grade (b)): the manual tool took its
   `timedOut` branch first (returning the generic "budget expired" text) and otherwise threw "engine
   did not produce a result", because a stop record carries no metrics. A fresh `{stopped: true}`
   result now returns a `stopped` payload with the artifact directory, and the throw is reserved for a
   genuine engine fault.
9. **The engine's `ok:false` is authoritative** (grade (b): a stub engine reports it with a numeric
   Rwp). The wrapper ended with `out.ok = true` unconditionally, so a late write-back or delivery
   failure was reported as a success. The engine's own `ok`/`error` now survive.
10. **An explicit `plan` that does not exist is reported**: it used to fall through to the default path
    (with `data` present the plan was silently ignored and a default plan ran) or die in
    `resolve(undefined)` with a bare Node TypeError.
11. **Batch-action indices are the action's, not the argv loop's** (the three engines reused the argv
    index, so three failed actions were all reported as index 1). **An unknown action is a failure**
    (it used to leave `ok:true` with only an "ignored" note). **`partial` means "some action
    succeeded"**, not "some artifact exists" — a successful peak pick followed by a failed screening
    step used to report `partial:false`.
12. **`raw peaks` no longer demands GSAS-II in the tool layer** (the engine was made lazy in 0.8.5, the
    JS side was missed), and **a `fetch` whose fetchers cannot be imported fails** instead of
    returning 0 candidates with `ok:true`.

### Science: how fp2k results are measured and reported

13. **The baseline uses the same repeats as the candidates** (grade (c): the code paths, asserted).
    The baseline ran ONCE against candidate means of three — with a measured run-to-run spread of
    ~1.7 percentage points, either direction could decide the whole ladder — and a candidate could be
    accepted on a mean where only one of three runs had succeeded. Both now use the same statistics,
    a step is accepted **only when every repeat succeeded**, and the spread is reported.
14. **The driver's warnings are part of the result**: 34 warnings (failed repeats, unstable spreads, a
    baseline measured over fewer runs, skipped background levels) never reached the caller. They do
    now, alongside `repeat_runs` (`repeats`, `baseline_n_ok`, `baseline_spread`, `unstable_steps`,
    `repeats_failed`, `background_levels_skipped`).
15. **Background levels that cannot work are skipped and SAID SO**: every level past the first reports
    "no such field index" while the panel is still Chebyshev; when the polynomial rewrite did not
    happen the higher levels are dropped and the reason is recorded, instead of running steps that
    change nothing and burying the log in warnings.

### Files, interfaces and the smaller findings

16. `cif_to_pcr` **never clobbers a same-named CIF** in `out_dir` (a differing file is moved aside for
    the conversion and restored), and **a `.pcr` that could not be moved aside is not mistaken for the
    new output** — the file must have changed (mtime or size) before it counts as this run's.
17. **`xrd_verify_fp2k` keeps the previous scratch** (0.8.5) and now reports `rwp_sum`, `rwp_source`
    and a `warning` when fp2k exited non-zero with usable numbers.
18. **`xrd.local.json` `cif2pcr_exe`** is read (0.8.5) — unchanged here, listed because the review
    re-checked it.
19. **Python interpreter selection sorts by version NUMBER**: `Python39` used to win over `Python313`
    (a string sort), so the probe could pick an older interpreter.
20. **The `bg_scan` parameter documents that it is a JSON STRING** (`"true"`), since a bare `true` is
    rejected by the host schema.
21. **`init_engine` refreshes its helper modules** when they differ from the shipped ones and says so,
    instead of leaving a stale `proc_guard`/`result_io` beside a newer template (the user's own engine
    copy is still never overwritten).
22. **The review detector's state is keyed by directory AND sample**: the artifact directory often
    defaults to the session workspace, so several samples shared one round counter and one freeze
    state.
23. **The review subagent's `start()` is bounded** (it was awaited without a ceiling, so a hanging
    provider blocked the tool forever) and **`dispose()` is raced against 10 s**.
24. **The `child_process` fallback caps what it accumulates** (4 MB per stream, like the host-service
    branch's `maxBytes`).
25. **Skill resources are documented as location-independent**: if a host installs only `SKILL.md`,
    `templates/` and `refs/` may be missing from the skill root — the texts now say the same files ship
    under the plugin's own `skills/` tree and give that path, instead of letting a reader conclude the
    template or defect catalogue does not exist.
26. **`refined_pcr` is described as what it is** — the best seed the ladder reached, not necessarily a
    file carrying the final write-back parameters.

### Verification recorded for this release

* `audit/verify-proc-supervision.mjs` — **162/162**, including §19 (last-occurrence Rwp, checked
  against the fixture's own files), §20 (`ok:false` survives), §21 (missing plan), §22 (per-action
  indices, unknown-action failure, `partial`).
* `audit/verify-gsas-ops.mjs` — **FAIL 0 / WARN 0 / SKIP 0**, now including a **real two-step
  refinement** with the shipped 90 s stall window (the supervision suite's 2.5 s window is for its stall
  controls and would cut a legitimate run — measured: the same call succeeds in 9.3 s in a clean
  directory).
* `audit/py/ctrl-fp2k-orphan.py` — ALL PASS; `verify-result-freshness.mjs` — 61/61;
  `verify-manual-timeout.mjs` — 37/37; `verify-review-detector.mjs` — 138/138.

## 0.8.5 — 2026-09-28 — **a second review of 0.8.4: four P0 defects it did not fix, eight more it found, and three claims this file had to withdraw**

A second independent review (codex, 2026-09-28, of **0.8.4**) reported ~30 findings. **Every one was
checked against this tree before anything was changed**; the triage is recorded here rather than the
outcome alone, because "the review said so" is not evidence. **No finding turned out to be a false
positive**, but five were **narrower than stated** and are marked as such. Still **11 tools**; the
output schema grew only by **optional, declared** fields (`aborted`, `stopped`, `partial`,
`failed_actions`, `rwp_source`, `rwp_sum`, `warning`) — see [INTERFACE.md](./INTERFACE.md).

### P0 — defects that made a call crash, lie, or hang

1. **A call cancelled BEFORE it started crashed, leaving the child running** (grade (b): the abort
   control now covers a pre-aborted signal). `onAbort` was registered *before*
   `const requestTermination` and `let termination` existed, so an already-aborted signal hit the
   temporal dead zone: `ReferenceError: Cannot access 'requestTermination' before initialization`,
   thrown **after** the child had been spawned in the same function body — the tool call died and
   nothing terminated the process it had just started. Declaration order is the fix (the listener is
   registered after the ladder exists); the exception is gone, not caught.
2. **A transport whose `done` never settles blocked the call forever** (grade (b): source-read +
   the exit path is exercised). `await donePromise` had no ceiling at all, so the review's probe
   (`settled:false`) hung the tool even after the watchdog had fired. The wait is now bounded by
   `fuse + 2 x grace + 30 s`, and a wait that ends without an observed exit is **reported**
   (`exitObserved:false`), not silently treated as an exit.
3. **`ok: true` survived a batch in which every action failed** (grade (b): an action is made to
   raise, and the return value is checked). `xrd_cif_edit`, `xrd_data_io` and `xrd_phase_screen`
   wrote `ok: True` unconditionally after the action loop; a missing input file produced
   `ok:true`, zero results, and the real error only inside `actions[i].error` and a warning line.
   `ok` now means "every action succeeded", with `partial` (something did) and `failed_actions`
   (index/type/error per failure) alongside it.
4. **The background provenance said something untrue** (grade (c): the code path is asserted, and the
   numeric behaviour was deliberately left alone). On the default path the background coefficients are
   refined in the **same** bounded least-squares as the cell/profile parameters, yet `result.json`
   hard-coded `"fitted_last": true` and the tool description claimed "BACKGROUND IS FITTED LAST (hard
   rule)". **User decision (2026-09-28): "如果是手动引擎的部分就不要动了"** — the fitting is NOT
   changed; only the record is now exact: `fitted_last` is true only when the `bg_scan` path really
   settled the background order last (with every other family frozen), plus `final_fit` and
   `fitted_jointly_with` name what actually happened. The skill text that states the rule is untouched
   and flagged to the user, since changing a stated rule is not a code fix.

### P1 — process control and the stop path

5. **`.xrd-stop` was honoured on mere existence** (grade (b): three cases in one control). Two runs
   sharing an artifact directory (every tool that defaults `out_dir` to the session workspace) could
   stop each other, and the JS layer deleted the flag regardless of whom it belonged to. The flag
   carries `{run_id, at}`; the engine ignores a flag naming another run (a hand-written flag with no
   id still counts), and the JS side only clears its own.
6. **The fp2k backend had no graceful stop at all** (grade (c): wiring asserted). The fp2k plan
   carried no `out_dir`, so `start_guard()` fell back to `None` ⇒ no stop file was watched and no
   progress tick was written; the driver now passes the result file's directory explicitly and the
   plan carries `out_dir`. (The manual engine was already wired — its guard takes the `--out` path.)
7. **GSAS-II could not be stopped BETWEEN refinement steps** (grade (c) for the restructure, (b) for
   the surrounding suite). All steps were handed to one `do_refinements(steps)` call, with a single
   checkpoint *before* it — while the comment claimed "a stop request lands BETWEEN refinement
   steps". Steps are now issued one at a time with a checkpoint before each (the same sequence, same
   order); `xrd_gsas_ops`' `refine` recipe and its `auto_background` order scan got the same
   treatment.
8. **`align`'s `drop_lines` did nothing and the report said it had** (grade (b): file contents
   checked). The code called `write_dat(..., pad_lines=0)` and then claimed `pad_lines=6 prepends
   dummy rows`: a 16-line input produced 16 lines while the text described padding. Dropping and
   padding are now two real operations (`drop_lines` strips that many leading comment/blank lines
   through a text pass-through, `pad_lines` prepends dummy rows), and the record reports what the
   file really contains. Non-text containers fall back to the parsed-points writer and say so.
9. **`raw peaks` demanded GSAS-II** (grade (b): the peaks-only path runs without a GSAS root). The
   GSAS root was resolved before ANY action, in the tool and again in the engine, so the pattern-only
   peak picker — pure numpy — was unusable on a machine without GSAS. Resolution is now lazy and only
   the actions that read a `.gpx` ask for it.
10. **The nominal-atom path could never work** (grade (c): interface vs engine contract, asserted).
    `atoms` was documented as a JSON string but written into the plan **unparsed** (the engine then
    iterated a `str`), and the cell the engine requires (`lattice`) was not an accepted argument at
    all — the engine exits with "nominal atoms need lattice a (cubic) or a/b/c". `atoms` is parsed,
    `lattice` exists, and using `atoms` without it fails with that sentence instead of a silent
    `SystemExit`.
11. **`data` was required even in modes that do not need it, and a run could split in two
    directories** (grade (b): both modes exercised). `required: ['data']` blocked `init_engine` (whose
    own code tolerates a missing path) and an explicit `plan`; separately, `runDir` was
    `dirname(plan)` while `result.json` went to the artifact directory, so with an explicit plan the
    heartbeat/stop files and the result lived in different places. `required` is now empty with a
    per-mode check that says what is missing, and plan/result/stop/tick share one directory.
12. **The graceful-stop result could be rejected by the host** (grade (c): schema vs producer,
    asserted). `proc_guard.write_stop_result()` writes `{"ok": false, "stopped": true, ...}`, and
    seven tools return the engine's JSON as-is under `additionalProperties: false` — `stopped` was not
    declared anywhere, so the honest "stopped on request" record could be refused as a schema
    violation. It is declared on all seven (`xrd_refine_gsas`, `xrd_finalize`, `xrd_gsas_ops`,
    `xrd_export_cif`, `xrd_cif_edit`, `xrd_data_io`, `xrd_phase_screen`).
13. **A cancel was reported as a timeout** (grade (b): the abort control checks the flags). Every stop
    set `timed_out: true`, so a user cancellation was described as "the internal budget expired" and
    the model was told a budget had run out. `timed_out` now means exactly that; a cancel reports
    `aborted: true` with its own message and hint.
14. **A zero or negative fuse killed healthy runs** (grade (b): the timeout control now asserts the
    reason). `MANUAL_FUSE_MS = tool timeout - 60 s` is **0** for the 60 s tool timeout that control
    uses, so the watchdog fired on its first tick — and the test still passed because it only looked
    for the word "budget" in the message. The fuse is now clamped **between** the nominal budget and
    the tool timeout (`fuseFor`), a non-positive caller value is clamped inside `runEngine` too, and
    the control asserts the reason that actually fired is not `hard-fuse`.
15. **`init_engine` overwrote a hand-edited engine** (grade (b): the second call's result is checked).
    The copy replaced `xrd_engine_custom.py` (and the two helper modules) unconditionally, destroying
    exactly the work the mode exists for. A free name is chosen instead, and the note says so.
16. **Nothing but `xrd_engine.py` counted as the plugin's own script** (grade (c): asserted). Every
    other bundled engine was classified as external and went to the approval hub as `owned:false` —
    while the comments beside those call sites already called them "plugin-owned (exempt)". The set now
    lists all fourteen engine scripts, and `pcr_check.py` (which bypassed the decision entirely) is
    routed through it as well.

### P1 — results and evidence

17. **`xrd_verify_fp2k` could report "success" with no numbers** (grade (b): the audit fixture now
    fails). Requiring a fresh `.sum` was not enough: measured on that fixture, fp2k writes its banner
    into the `.sum` even when it cannot open the intensity file, and the tool returned `out_pcr` with
    density/cell/Rwp/BraggR all missing. A run that produces **no R factor and no density** is now a
    failure that names the intensity file it looked for.
18. **The verifier's Rwp was a different quantity from the driver's** (grade (c): both readers
    quoted). `xrd_refine_auto` reads the `.out` "Conventional Rietveld Rp,Rwp,Re and Chi2" line; the
    verifier read the **first** `Rwp:` in the `.sum` (the review measured 75.7 vs 83.6 on one sample),
    so "verification" and "refinement" could not be compared. The `.out` value wins, `rwp_source` says
    where it came from, and the `.sum` value is kept separately as `rwp_sum`.
19. **Each verification destroyed the previous one's evidence** (grade (b): the rotated directory and
    its `.sum` are asserted). `rmSync(.verify-<stem>)` deleted the scratch, and with it the numbers
    behind an already-reported result. The directory is now rotated to `.verify-<stem>.prev`.
20. **`cif_to_pcr` deleted the `.pcr` before trying** (grade (c): the code path, asserted). A failed
    conversion destroyed the file the user already had; the existing `.pcr` is moved aside now and
    restored when no new one appears.
21. **`xrd.local.json.cif2pcr_exe` was documented but never read** (grade (c): docs vs code). The
    documented chain is implemented: `tool_path` → `$DSH_FULLPROF_CIF2PCR` → `cif2pcr_exe` in
    `xrd.local.json` → the suite's conventional path.
22. **The orphan sweep killed by NAME** (grade (b): a new control case). `sweep_orphans()` matched
    `fp2k.exe` + parentless + age, so it could kill another project's fp2k — or one the user started by
    hand (`fp2k_driver` runs the sweep automatically at start-up). It now requires the process's
    **image path** to be one of the binaries this plugin runs; anything unverifiable is reported under
    `skipped_unverified` and left alone. The control adds case **C2** (same name, other path ⇒ spared).
23. **A failed job-object assignment left the child unkillable and then raised** (grade (b): the guard
    controls). In the monitored branch `kill()` only called `TerminateJobObject` — a no-op when the job
    was never created or never assigned — and the following unbounded `p.wait(timeout=10)` raised
    `TimeoutExpired` out of `run_guarded`. The fallback `p.kill()` is now always attempted and the wait
    is guarded and reported (`alive_after_kill`).

### One 0.8.5 self-inflicted defect, recorded because it is instructive

A bounded wait needs a timer, and a plain `setTimeout` **holds the event loop open** for its whole
delay: after that change the audit suite printed `ALL PASS — pass=61 fail=0` and then sat there until
its per-script cap killed it (reported as `TIMEOUT`, not as a failure). The cap timers are now
`sleepUnref(...)`. The reverse mistake was measured immediately afterwards: unref'ing the **awaited**
grace/poll timers empties the loop and node dies with `unsettled top-level await` (exit 13) during a
graceful stop. Only timers that are raced against a promise that itself keeps the loop alive may be
unref'd.

### Known limitations (unchanged, stated so they cannot be read as fixed)

* **Third-party executables** (`fp2k.exe`, `CIFs_to_PCR.exe`) are still launched by Node outside any
  kill-on-close job object, so a hard death of the host can orphan one; the host `subprocess` service,
  when present, is preferred and does own tree-scoped teardown.
* With the host service, process death is **not confirmable** from the plugin (no pid is exposed), so
  `residual` is `null` ("not verified by us") rather than the `false` the 0.8.4 code claimed.
* Windows job-object acceptance under a **killed DSH parent** and the real host-service branch remain
  deployment-environment checks; here the service branch is exercised with a stub.
* The stated rule "background is fitted last" is **not** what the default manual-engine path does. The
  record now says so (item 4); the fitting itself was left alone on the user's instruction.

### Verification recorded for this release

* `audit/verify-proc-supervision.mjs` — **111/111** (was 103: the pre-abort crash, the zero fuse, the
  `stopped` declaration, the metric gate, the rotated scratch and the new sweep case are all asserted).
* `audit/verify-result-freshness.mjs` — **61/61**; `audit/verify-manual-timeout.mjs` — **37/37** (the
  fuse-vs-budget reason is now checked, not just the word "budget").
* `audit/py/ctrl-fp2k-orphan.py` — **ALL PASS** including the new case C2.
* `verify-all.mjs` — 12/12 (one SKIP: the real-project half of `verify-gsas-ops.mjs`, which needs a
  converged `.gpx`).

## 0.8.4 — 2026-09-28 — **the five release blockers a review of 0.8.3 found, and the claims that release should not have made**

0.8.3 shipped process supervision; an independent code review of that release (codex, 2026-09-28)
found **five blocking defects** and several unfinished mechanisms. Every item below was verified
against this tree before it was touched, and each now has a test that **fails on 0.8.3 and passes
here** (`audit/verify-proc-supervision.mjs`, sections 8–12). **Four of the five were regressions
introduced by 0.8.3 itself.** Still **11 tools** and **no output schema change**.

### Blockers that were regressions of 0.8.3

1. **`init_engine` produced a template that could not run** (grade (b): the copy is executed in the
   test). 0.8.3 gave `xrd_engine.py` two new sibling imports (`result_io`, `proc_guard`) but the
   template copy still copied that file alone ⇒ `ModuleNotFoundError: No module named 'result_io'`,
   which broke the *copy-the-template → edit → run* path the manual engine exists for. The copy now
   takes `result_io.py` and `proc_guard.py` with it, and the acceptance is that the **copy** imports
   successfully.
2. **`abort` did not terminate anything** (grade (b): real child, real abort). It only wrote the stop
   flag — and with no `outDir` not even that — so a cancelled tool call ran to its timeout (the review
   measured "aborted at 100 ms, the child ran the full 2116 ms and exited 0"). Abort now enters the
   same idempotent termination ladder as a stall, and the test asserts the run stops in ~1.9 s with
   **no process left behind**.
3. **The host-service fallback threw instead of falling back** (grade (b)). `const useService` was
   reassigned in the `catch` ⇒ `TypeError: Assignment to constant variable`, so a host whose
   `subprocess` service threw could not degrade to `child_process` at all. Now `let`, with a negative
   test whose stub throws **inside** `spawn()` — the case that previously never ran.
4. **The hard fuse sat ABOVE the tool timeout** (grade (c): source-read of the constants, asserted).
   `timeoutMs x 4` = 6000 s against a 1800 s tool timeout, so a healthy ticking run was cut by the
   **host** long before our fuse could fire — the opposite of "the hard timeout is the last resort".
   Both heartbeat backends now pass an explicit fuse derived from their own tool timeout
   (1800 s tool − 60 s cleanup = **1740 s**). Hierarchy: fuse 1740 s > nominal budget 1500 s, and
   both below the tool's 1800 s, so the stop is always OURS and always structured.

### Blocker that predates 0.8.3 (a claim 0.8.3 made and could not support)

5. **`xrd_verify_fp2k` could report an earlier verification's `.sum`** (grade (b): the review's fixture
   is reproduced). The scratch directory is reused per stem and the `.sum` was read by fixed name, so
   a pre-existing file was accepted when the new run wrote nothing — measured as "stale Rwp 3.21 came
   back although fp2k wrote nothing". The scratch is now cleared first and the `.sum` must be newer
   than the run. The same fixed-name hole in the **occupancy audit** JSON (`*.pcr_check.json`) is
   closed the same way. **0.8.3's CHANGELOG said "every engine result" is protected; that was
   over-claimed** — it was true of the JSON engines, not of these two paths.

### Mechanisms finished

* **Stop requests are now honoured by every engine, not just the manual one**: `guard.checkpoint()` at
  safe points in the GSAS refine steps, the post-ladder refine, the Uiso-floor rounds, each
  `xrd_gsas_ops` action, each `xrd_data_io` action and before every fp2k run; a stop writes a
  structured `stopped: true` result at the documented path and exits 97 (shared handler
  `run_guarded_main`), instead of surfacing as a traceback with no result.
* **The guard's self-report reaches the caller**: `guard`, `self_assigned`, `treeKilled` and
  `residual` are returned by the runner and appear in the failure text — "the job object failed and we
  ran unprotected" is no longer invisible. (Text only: no schema field was added.)
* **The tree kill is confirmed**: POSIX falls back from the process group to the pid (and the fallback
  spawn now creates a group), and the runner waits for the confirmation before returning, so
  `residual` means "measured", not "assumed".
* **Known limitation, stated rather than papered over**: `xrd_verify_fp2k` and `cif_to_pcr` start
  their external programs (`fp2k.exe`, `CIFs_to_PCR.exe`) directly from Node and have no in-process
  guard, so a hard death of DSH mid-call can still orphan them. Both are seconds-long conversions; this
  is recorded here instead of being claimed as fixed.

### Verification of this release

`verify-all.mjs` **12/12** scripts; `verify-proc-supervision.mjs` **103/103** (its sections 8–12 are the
new controls above); `verify-result-freshness.mjs` **61/61**; `verify-manual-timeout.mjs` **35/35**;
`verify-review-detector.mjs` **138/138**; `integrity-check.mjs` **0 FAIL**. Not run here: the real-host
`subprocess` branch (stub only), the Windows job-object acceptance under a killed DSH parent, and the
external-input suites listed in AUDIT.md — those remain deployment-environment work.

## 0.8.3 — 2026-09-28 — **an engine run is now observable, stoppable, and cannot outlive its launcher; a stale result can no longer be reported as this run's**

Raised by a code review of 0.8.1 (codex, 2026-09-27) that graded the process handling "mechanism
incomplete", and settled by measurement rather than by argument (2026-09-28). Still **11 tools** and
**no output schema change anywhere** — the run identity needed for the freshness gate travels inside
the plan JSON and is removed again before any payload reaches the model.

### 1. A result now belongs to an identified run (grade (a): real engine runs)

> **Correction (0.8.4):** this section originally claimed "every engine result". That was **too broad**:
> it covered the JSON results written through `result_io`. Two fixed-name paths (`xrd_verify_fp2k`'s
> `.sum`, the occupancy-audit JSON) stayed outside the gate until 0.8.4 closed them — see the 0.8.4
> entry below.

Seven tools read their engine's result from a **fixed name** inside the artifact directory
(`gsas_result.json`, `finalize_result.json`, …) and could not tell THIS run's file from one an earlier
run had left behind — the same bug class that was already fixed for the manual engine by a pre-run
mtime snapshot, measured there as "a stub that wrote nothing returned the earlier run's `rwp = 5.0`
with `ok = true`".

* `engine/result_io.py` — one shared writer: dump to `tmp`, `fsync`, `os.replace` (atomic on POSIX and
  Windows), and stamp the caller's `run_id` into the result.
* Every engine writes through it: `data_io`, `phase_screen`, `cif_edit`, `gsas_ops`, `gsas_finalize`,
  `gsas_export` (both paths), `gsas_engine`, `xrd_engine`, `fp2k_driver`.
* `lib/xrd-tools.js` gains `newRunId()` / `resultMtime()` / `planRunId()` / `readFreshResult()`: a
  result is accepted only when its **mtime advanced past a snapshot taken before the child started**
  and, when the engine echoed one, the **`run_id` matches**; otherwise the tool reports the refusal
  reason (`stale result file …` / `belongs to another run …`) instead of a number. All **8** plans
  carry a `run_id`; all **8** read sites go through the gate (`xrd_refine_auto` included).
* `verify-result-freshness.mjs` — **61/61**, including a negative control that reproduces the old bug
  on the same fixture (the leftover file is a *successful-looking* result carrying the older run's id).

### 2. An engine run can no longer be left behind (grade (b): real processes, real kills)

* `engine/proc_guard.py` — `start_guard()` puts the engine **itself** into a
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job object (Windows assigns descendants to the same job, so the
  whole subtree dies however this process dies — the only mechanism that survives the launcher's
  death, since `taskkill` needs a live killer), watches the parent (exits if it is gone), publishes
  `.xrd-progress.json`, and honours a `.xrd-stop` flag at checkpoints (`EngineStopped`, exit 97).
* One runner for every backend: `runEngine()` in `lib/xrd-tools.js`. Timeout/abort now write the stop
  flag, wait a grace period, and only then kill the **tree** (`taskkill /PID <pid> /T /F` on Windows,
  the process group on POSIX) — never a console event, which would reach the host console and make DSH
  exit. It prefers the host's `subprocess` service when the host offers one and falls back to
  `child_process` otherwise (recording which transport was used).
* The old launcher in `lib/index.js` is **deleted** rather than left unused: it still contained the
  bare `child.kill()` this release exists to remove.

### 3. Liveness is judged per backend (grade (b): real stall, real stop)

"Is it still alive?" is not one question. `LIVENESS = { manual: 'heartbeat', gsas: 'heartbeat' }`;
every other backend — `fp2k` above all — keeps the fixed policy.

* **Only a tick that claims `progress: true` counts.** A wrapper that is merely up says nothing about
  the work: GSAS-II can stall inside its own loop and fp2k was measured spinning at ~97% of one core
  for 600 s *after* writing its first output.
* **Manual engine** — progress comes from the residual loop (one evaluation = the fit moved). A slow but
  healthy fit therefore OUTLIVES its nominal timeout; it is judged stuck only after 90 s without
  progress, or at a 4× hard fuse.
* **GSAS-II** — progress is derived from **GSAS's own `.gpx`/`.lst` writes**, not from the wrapper.
* **fp2k** — unchanged authority (fixed timeout plus the driver's stall rule), plus a new
  `nan_result` verdict: `fp2k_guard` reads the R-factor line and kills immediately when its values are
  `NaN` (measured on two real pcrs: a NaN line within ~1 s, then an endless 100 %-CPU spin).
* **Timeouts raised for the heartbeat backends** (a bound on a *ticking* run, not an expected duration):
  `xrd_refine_manual` 620 s/660 s → **1500 s/1800 s**, `xrd_refine_gsas` 880 s/900 s → **1500 s/1800 s**.
  The internal budget still stays below the tool timeout, so a stuck run still comes back as OUR
  structured failure instead of the host's bare `tool call timed out`.
* `verify-proc-supervision.mjs` — **52/52**: a ticking run finishes at 4.9 s despite a 1.5 s nominal
  timeout; a run whose ticks stop is stopped as `heartbeat-lost`; the graceful stop lets the engine
  exit itself (code 0, result on disk); the host-service branch and its fallback; the NaN probe (and
  that a real R line is not flagged).

### 4. Test wiring

`verify-all.mjs` now also runs `verify-result-freshness.mjs`, `verify-proc-supervision.mjs` and the
python control `py/ctrl-fp2k-orphan.py` (it had never been in the default list — exactly how "the fp2k
layer is protected" went unchecked), and supports `.py` steps. Full suite: **12/12 scripts ok**;
`verify-manual-timeout.mjs` **35/35**; `verify-review-detector.mjs` **138/138**; `integrity-check.mjs`
**0 FAIL**.

### 5. What is deliberately NOT claimed

* The host-`subprocess` branch is exercised against a **stub service** (its shape, its collected
  output, the fallback). A **real host** was not run from the maintenance environment — the standing
  rule keeps end-to-end host runs for the deployment/clean environment.
* The fp2k **stall window stays at 60 s**. Every run longer than that which could be produced on this
  machine was pathological (NaN or a hung spin), and no healthy long run could be constructed —
  synthetic enlargements were rejected by fp2k and are reported as invalid fixtures, not as evidence.
* Engines still leave a `__pycache__` entry next to the module they newly import (same class as the
  pre-existing `fp2k_guard` behaviour; the packaging gate cleans it).

## 0.8.2 — 2026-09-27 — **the plugin's own words are now readable in both languages, and the recorded values are not**

A patch release with **no schema change at all**: the tool family stays at **11**, and no parameter or
return field moved. What changes is the **language of everything the plugin says back to the agent** —
and, just as deliberately, what does **not** change: any value a machine later matches on.

The reason is not symmetry for its own sake. The host translates nothing the model writes (see below), and
the messages the plugin injects are read by **both** the agent and the person watching — so an English-UI
user was reading Chinese status lines in the middle of their own refinement. The user's framing, which is
the rule this release follows: *the parts a person can see matter more than the parts only a subagent sees.*

### The injected lines are bilingual, with explicit language markers

Every one of the **14** texts the review detector injects into the conversation now arrives as a tag line
plus two marked blocks:

```
[状态表 · STATUS]
language-zh- 机械栏已由插件更新（不要手改）。请更新判断栏文件：…
language-en- The plugin has already updated the mechanical ledger; do not edit it. Update the judgement file instead: …
```

The markers exist so the agent can see the two blocks are **one instruction in two languages**, not two
instructions — the user asked for exactly this, to remove both the ambiguity and the internal translation
step. The tag appears **once**, so the grep anchors (`[状态表]`) survive. The English is written to carry
the meaning, not to mirror the Chinese word for word.

**`source.summary` is now a purpose-written label, not a slice of the payload.** Slicing produced a
truncated Chinese half-sentence once the payload was bilingual. Each of the 14 texts has its own short
bilingual label (all ≤ 52 characters) and the cap is relaxed to 200 — but every shipped label still fits
the host's documented bound of 120, so the relaxation is headroom, not a violation. Related correction,
taken from the host's own source rather than assumed: `CONTEXT_SUMMARY_MAX_CHARS = 120` bounds the
**summary**; `boundContextSummary()` **ellipsizes, it never rejects**, and it is called by other plugins,
not by the inbox path ours travels — and the payload itself (`content[0].text`) is **never** truncated.

### The one warning that reaches the agent through a tool result

`xrd_refine_manual`'s "engine produced no result" throw carried its caution — *a missing R value is not a
bad fit* — **in Chinese only**, while the same warning on the **timeout** path (`hint`) was English. Two
paths, one warning, two languages. The English is now appended with the same markers, and the Chinese
sentence is kept verbatim (the timeout suite asserts that exact string).

### `journal.md` speaks both languages; the records inside it do not

The refinement journal's title and column headers are bilingual. The **values** are untouched —
`未到洼地`, `物理`, `AI 判读` still appear exactly as the engine wrote them, because `journal.jsonl` is a
machine-read record and translating a recorded value breaks anything matching on it. This is the same rule
as the new state field below: **display follows the language, the machine value does not.**

### `limit_mode_choice`: the answer is recorded as a code, not as the label the user saw

`xrd-workflow` §6.5 has always required a four-option question at the limit-mode entrance, and always said
the answer goes into `workflow_state.json` and `journal.jsonl` — without saying *what to write*. So a
Chinese conversation recorded Chinese and an English one recorded English, and the same decision became two
different strings. The state file now carries

```jsonc
"limit_mode_choice": null,   // continue | retarget | informed_exception | handback
```

and §6.5's option table gained a **code column** beside the Chinese and English wording. The question is
displayed in **one** language — the conversation's — never side by side; the code is what gets stored.

### A language rule the skill did not have

`xrd-workflow` §0 now states it plainly: **everything the user will see is written in the language the user
is speaking** — question boxes, reports, answers, and any tool message the agent relays. The skill's own
Chinese is what the *agent* reads, not what the *user* reads. This is a rule because the host offers no
help here: `ask_user_question`'s schema is `id / question / header / options[].label / description /
multi_select` — **no i18n field of any kind** — so a label reaches the screen exactly as the model wrote it.

### Verification

* (b) `verify-review-detector.mjs` — **138/138**, up from 119. The 19 new assertions are the ones this
  release needs: each label is the purpose-written one, every label fits 120, the cap is 200, a 130-char
  summary survives, all 14 texts carry both language markers, and every label key has a matching text key.
  The existing "no bare A–E code letter" assertions now guard the English too — which is why no English
  line uses "A" as an article.
* (a) `verify-manual-timeout.mjs` — **34/34** on a real spawned python child, including three new
  assertions on the tool-result warning (both markers present, English present, Chinese first).
* (a) the journal header was checked by calling the **real** `_write_journal_md()` from
  `engine/xrd_engine.py` with a crafted `journal.jsonl` — 8/8, including the negative side: no English
  leaked into the record values.
* (b) `integrity-check.mjs` — **0 FAIL** after the skill edits (§6 / §6.9 field-name agreement with
  `defect_catalog.json` still holds).
* **Not done, deliberately:** the §6 checklist body and the review subagent's prompt stay Chinese — they
  are read by AI, not by people, and the user drew that line explicitly.
* **Still owed from 0.8.1:** the fixed injected message has not yet been spliced into a live host inbox;
  no refinement has run since, and `lib/` only loads at startup.

## 0.8.1 — 2026-09-27 — **the reviewer's own injection crashed the turn, and two defects the packaged audit could not catch**

A patch release with **no schema change at all**: the tool family stays at **11**, and no parameter
or return field moved. Four things are fixed. One of them was found by the plugin failing in a live
conversation; the other three were found by an outside reviewer reading the *packaged* tree, which
is exactly the tree the shipped test suite runs from and therefore the tree it is least able to
doubt.

### The injection was not a complete message, and it ended the turn

`lib/review.js` built the injected `[状态表]` / review line as `{role, content}` and handed it to
the host. The host reads `message.source.kind` when it splices the inbox, so every injection threw
`TypeError: Cannot read properties of undefined (reading 'kind')` and **the whole turn ended as an
error** — the refinement round was already recorded, so the damage was invisible in the plugin's
own state files. Measured in the session log: exactly **two** `turn/end` events with
`reason.kind='error'` in 111,000 lines, at `2026-09-26T17:48:23.927Z` and `17:49:58.256Z`, each
~0.4 s after the round it belonged to was written.

`injectedMessage()` now builds a complete `UserMessage` — `id` / `role` / `content` / `source` —
with `source = {kind:'plugin', plugin:'dsh-xrd', form:'notice', summary}`. The shape is not guessed:
it is read from the host's own declarations (`dsh-llm/lib/types/message.d.ts:120-133` makes all four
fields required, `:94-101` defines the plugin source, `:110` caps the summary at 120 characters).
A fresh message object is constructed per delivery, because the host freezes what it splices.

**A stub agent could never have caught this.** The stub receives whatever it is handed; only the
host's inbox does the read that throws. That is why `verify-review-detector.mjs` now asserts the
full shape (**119/119**, up from 108) and why its negative control
(`neg-control-injection.py`) deletes `source` and requires the shape assertions to fail.

### The shipped audit module carried a maintainer path

`audit/verify-manual-timeout.mjs` hard-coded a **maintainer's absolute interpreter path** — one
account's home directory, spelled out down to the Python installation — as its fallback. It
published a local user name and directory layout to anyone who opened the package, and on any other
machine that test could only fail. It now resolves the interpreter through the shared helper that
every other audit script uses (`audit/_lib.mjs` → `python()`) and exits 2 with a plain message when
none is found.

The rule it broke was already written down — **`audit/` is copied verbatim and is never scrubbed, so
every file in it must be free of maintainer paths and sample identifiers** — which is the same
reason the leak scan itself was widened in the same breath: a rule nothing checks is not a rule.

### The bond-window check matched elements by substring

`engine/gsas_export.py` asked whether each expected element string occurred *anywhere* in a
contact's key. An expected `P-O` window therefore matched a **Pb-O** contact, and `C-N` matched
**C-Na** — both real chemistries in this plugin's own use, both reported as compliant bonds. The
lookup is now an exact **element-pair** match (`_element_pair()` splits the window key, resolves
each side to an element, sorts the tuple), with a species-key exact match as the second chance; the
substring fallback is **deleted**, not demoted, so a miss can no longer silently become a pass.
Measured: a Pb/O structure tested against a `P-O` window now reports `no such contact found`
(`audit/verify-bond-window.py`, 15 assertions), and the negative control that restores the substring
rule fails exactly two of them (`audit/neg-control-bond-window.py`).

### The leak scan could be walked past

The user-name rule in `verify-package.mjs` matched backslash paths only, so an absolute
user-profile path written with forward slashes would have shipped inside `audit/` past the gate that
exists to catch exactly that. Every path rule
now covers **both separators**, and the JSON-escaped form of the tree is scanned as well. Adding the
rule immediately exposed three further offenders in the same folder; all were removed.

### Verification

* (a) **Real host evidence, read from the session log**: the two `turn/end` errors above, their
  timestamps, and the fact that **no `dsh-xrd` message ever appears as a successful
  `agent/inbox/spliced`** (the other 78 plugin splices in that log are from other plugins).
* (a) **A real refinement's logs replayed through the detector**: 10 real GSAS rounds recovered from
  their own `.lst` files (`wR` 34.21 → 35.11 → 18.46 → 14.41 → 25.48 → 13.71 …), replayed with the
  plugin's own `recordGsasRound` — reviews fall due at rounds 3/6/9 as designed.
* (a) **A real review subagent, three independent runs**, handed the status package the plugin
  assembles from those logs: each returned a single parseable code. Feeding those answers back
  through `runReviewInBackground` in the real round order gives level 1 → no injection, level 2 →
  one line injected, level 3 → soft freeze plus the full §6 checklist appended — and every injected
  message passes the host shape assertion. **Caveat, stated because it bounds the claim:** those
  three runs were made from inside the maintainer's own session, where a memory plugin injects
  long-term context into every agent — so this measures "a real subagent answers in one letter
  *under those conditions*", not a clean-room response.
* (b) `verify-review-detector.mjs` **119/119**; `verify-manual-timeout.mjs` **31/31**;
  `verify-bond-window.py` **15/15**; `integrity-check.mjs` **0 FAIL**; `three-copy-check.mjs` — the
  three copies agree; builder leak audit **clean**; `verify-package.mjs` **0 FAIL** on the rebuilt
  tree, including the maintainer-path scan over all 64 text files of `audit/`.
* **Not verified — the one thing still owed:** the fixed message has **not yet been spliced into a
  live host inbox**, because no refinement has run since the fix was synced and `lib/` only loads at
  startup. Everything up to the host is measured; the host's own read is inferred from its type
  declarations and from the two crashes it produced. **One live GSAS round after a restart settles
  it.** The result of that run is not claimed here.

## 0.8.0 — 2026-09-26 — **the checklist became machinery: a defect catalog, a per-sample table, and a reviewer that fires on its own**

The largest change since 0.5.0, and the first release where the *workflow* — not a tool — is the
deliverable. Three things happen here: the "complex case" checklist is now a real reference
document with a machine skeleton; a background reviewer is spawned from the plugin itself and can
soft-freeze a stalled route; and the manual engine finally fails **structurally** instead of
vanishing into a host timeout.

**One interface change** (everything else is behaviour, prose or fix): `xrd_refine_manual`'s output
gains `ok` / `timed_out` / `error` / `hint` / `out_dir` / `artifacts` — see INTERFACE.md.
The tool family is unchanged at 11; no other parameter or return field moved.

### The checklist is no longer prose

* **Appendix A — the defect catalog** (`skills/xrd-workflow/refs/defect-catalog.md`, skeleton
  `defect_catalog.json`): **14 defect classes** in 3 families, each with model form / XRD signature /
  how to judge / which backend may judge it / the usual misjudgement. The manual engine is **not**
  allowed to judge complex defects (split sites, antisite, occupancy coupling, extra species): that
  boundary is now written down, with the measured case where the two engines disagree in *opposite
  directions* on the same hypothesis.
* **Three footnotes** (interlayer cation mixing, oxygen-vacancy ordering, off-stoichiometry) are
  handled by reference to the classes they belong to, and all three route to GSAS-II.
* **`reachability`** on the two ordering/distortion classes: a high-symmetry CIF cannot refine its way
  to a low-symmetry ordered model, so "no ordering" may not be declared until the CIF screening step
  is confirmed — the start point, not the fit, is what has to change.
* **Per-sample table** (`defect_table.json`, 10 fields) — every class judged 适用 / 不适用 / 待定, with
  a reason for the exclusions and `R_before → R_after` for the executions. Excluding a dimension
  requires both schemes of a geometry-sensitive dimension and a citable geometry.
* **Coverage disclosure before any "cannot be improved"**: background order and whether the order
  escalation was ever run, which parameter families were released, which were never touched, and what
  the backend could not do at all. "Not tried" and "cannot be improved" are now different statements,
  and three consecutive no-improvement rounds **force** the checklist (a counter, not a memory).

### A reviewer that runs itself

* Every **3 GSAS rounds** the plugin spawns a disposable, read-only review subagent; it answers with
  **one code letter** (the plugin keeps the counters, the conversation only ever sees a level).
  Level ≥ 2 injects a single line; level 3 adds the full checklist and a **soft freeze** — it never
  changes what a tool does, it stops a stalled route from being optimised further. Repeated codes
  escalate; a clean round clears everything.
* All of it lands in the **sample's own subdirectory**: a plugin-maintained mechanical file plus an
  AI-written judgement file. **Human takeover closes the detector** (a new user message during
  refinement), and only an explicit "continue" re-arms it, with the counters reset.
* The review text is injected into the conversation, **never into a tool's return value** — the
  output schemas are closed, and a well-meant extra field there is a rejected tool call.

### Failures now leave the scene intact

* **`xrd_refine_manual`**: the engine's internal budget is **620 s**, deliberately below the tool's
  660 s, so a long run ends as *our* structured failure — verdict, artifact directory, the files
  already on disk, and the statement that **a missing R value is not a bad fit** — instead of the
  host's bare `tool call timed out`. (Found by the timeout test itself.)
* **Stale results can no longer be reported**: the result file is accepted only if *this* run wrote
  it (pre-run mtime snapshot). A crashed engine used to come back with the previous run's Rwp and
  `ok: true`; both were measured with a stub engine that writes nothing.
* **fp2k orphan protection** (three layers): the driver's process tree is owned by a job object, a
  sweep collects orphans left by an earlier session, and a stall fingerprint identifies the state.
  Two orphans left running for ~10 hours had starved the host's renderer; that is what this closes.
* **Whole-document vision**: `office_read_vision` named one page per call (the all-pages form could
  not finish inside its own budget and lost every finished page when it was stopped) — delivered
  separately in the office plugin; recorded here because the workflow's figure check depends on it.

### Verification

* (a) **Real runs**: the manual engine was driven with a real python child that hangs (budget expiry,
  kill, structured failure, artifact listing), that writes a valid result (success path), that writes
  nothing (engine fault + stale-result gate) — `verify-manual-timeout.mjs`, **31/31**.
* (b) **Stub-driven**: the review detector's full state machine — counters, escalation, injection
  levels, E reminders, release detection, human takeover and resume, one in-flight review per
  sample — `verify-review-detector.mjs`, **108/108**, on both the source tree and the runtime copy.
* (b) `integrity-check.mjs` — 11 tools, 10 engines, **0 FAIL**, now including the catalog section
  (14 classes, reachability, footnote homes, field-name agreement with the skill, per-sample ids).
* (b) `_neg-control-catalog.py` — the catalog check is verified to **fail** when a row is damaged, and
  restored byte-exact; a check that cannot fail is not a check.
* (c) `_sample-shape-scan.py` — enumeration of every sample-code-shaped token in the tree (the rule the
  package audit uses), 0 un-scrubbed tokens.
* **Not verified:** no new XRD data was refined for this release; the acceptance run (a real
  defect-system re-refinement through the new route) is the user's, and is expected **after** a dsh
  restart, because `lib/` changes only load at startup.

## 0.7.3 — 2026-09-23 — **a real sample code was still in two bundled skills; the scrub table had a gap**

Packaging-only: **no code change, no schema change, no behaviour change**. The version moves
because the package's bytes change, and this project's rule is one version per artifact (0.5.1
was cut for exactly that reason instead of re-cutting 0.5.0).

### What leaked — and why two independent checks both missed it

The release builder's SCRUB table held **two spellings of one `fe`-prefixed sample code** but none
of a **second** one, so two bundled skills shipped the literal code:

* `skills/xrd-workflow/SKILL.md` — §6.4's heading (the "… 只是一个示例，不是模板" line)
* `skills/xrd-refinement/SKILL.md` — its pointer to that section (the "… 只作示例" line)

It reached the 0.7.2 release, the Windows test bundle, and the portable package built from them.

**Both gaps were the same gap, and that is the point:**

1. the **scrub table was incomplete** — it is applied case-sensitively, so a missing key is silent;
2. the builder's **own leak audit carried a hand-written list of names to look for**, and that list
   was missing the same code — so a "clean" verdict could not mean *complete*.

This is the second instance of the 0.7.0 defect class; there, one spelling was listed while four
files used another, and the same reasoning applied. It was finally found by the **portable
package's own audit** (`dsh-diagnose/audit-portable-leaks.py`), which scans the assembled artifact
for sample-identifier **shapes** instead of checking a list. The lesson: **a completeness check
must not be derived from the same list it is checking.**

### Fix

* The missing code, in both capitalisations, was added to the SCRUB table (replaced by
  `sampleC` / `SampleC`, following the existing `sampleA` / `sampleB` convention).
* The audit's sample-name rule is now **generated from the SCRUB keys**, so table and audit can no
  longer diverge, **plus** an independent shape rule — `fe` followed by two digits — that catches a
  code nobody thought to list.
* The gap was closed by **enumeration, not by eye**: a case-insensitive sweep for that shape across
  the whole plugin tree returns exactly two codes, and both are in the table now. Standard
  reference materials (`Y2O3`, `PbSO4`, FullProf's own example) are deliberately not scrubbed, and
  the `KFeMn` label in the PBA template is a **chemical composition**, not a sample identity.
* **This changelog deliberately does not repeat the codes.** It ships inside the package, and the
  injection step runs *after* the scrub — so a code written here would travel unscrubbed. The new
  shape rule fails the build on exactly that, which is how this section came to be written twice.

### Evidence

* (a) the portable leak audit named both skill files and the exact lines;
* (b) the builder's audit now fails on any `fe`+digits token; the 0.7.3 build reports **0 findings**,
  and `three-copy-check.mjs` still reports package == sanitise(source);
* (b) `verify-package.mjs` stays green at 0.7.3.

## 0.7.2 — 2026-09-23 — **every render survives a value-less call, and the record no longer names the foreign variable**

Follow-up to 0.7.1, found by the packaged self-audit (`audit/verify-autofp-replaced.mjs`).

### A render could throw instead of reporting

`xrd_refine_auto`'s render dereferenced `value.rwp` outright, and that audit calls
`render({}, value)` after `try { value = await execute(…) } catch { … }` — so when execute threw,
`value` was `null` and the plugin died with

```
TypeError: Cannot read properties of null (reading 'rwp')
```

which surfaced as an unhandled stack trace instead of a named failure. **No host path was proven to
render after a throw**, so this is hardening rather than a reported bug — but a render is the one
place where a crash hides the real error, and all eleven renders had the same gap.

* all **11** renders now guard `value` first (`value = value ?? {}`), so a value-less call produces a
  readable card instead of an exception;
* new central control `verify-render-nullsafe.mjs` assembles the plugin and calls **every** tool's
  render with both `null` and `undefined`, asserting no throw and non-empty text. Central on purpose:
  the eleven renders have eleven value shapes, so a per-script assertion would only ever cover the
  tool that script happens to use;
* `verify-autofp-replaced.mjs` now wraps its own render call, so a render defect is reported as a
  named failure instead of killing the script with a stack trace.

### The old variable's name is out of the published record too

0.7.1 removed a foreign-named environment variable from the resolution chain but still named it in
this file and in `lib/config.js`. That name belongs to a private, never-published distribution, so it
is now gone from the published package entirely: the history says what happened ("an unrelated plugin
distribution's variable name") without carrying the name.

**Upgrading and seeing a different interpreter?** This plugin reads exactly two keys for it —
`DSH_XRD_PY`, and `"python"` in `xrd.local.json`. Any other interpreter-related environment variable
is ignored, by design.

### Verification

* (b) `verify-render-nullsafe.mjs` — 11 tools × 2 value-less calls, 0 throws, all cards non-empty.
* (b) `integrity-check.mjs`, `verify-package.mjs`, `three-copy-check.mjs` — green at 0.7.2.
* **Not verified:** nothing new to verify about the engines — 0.7.2 changes renders and prose only.

## 0.7.1 — 2026-09-23 — **the interpreter was borrowed from another plugin, and the failure said nothing**

Three defects, all found by asking one question — *"does this plugin depend on another plugin?"* — and
then measuring instead of reasoning. **No schema changed**: the tool family stays at 11, every
parameter and return field is as in 0.7.0, and only the resolution of the interpreter and the wording
of failures moved.

### The coupling was a name, not a dependency — and the name is now gone

The chain in `lib/config.js` ended with an environment variable whose name belonged to **an unrelated
plugin distribution** (private, and never published alongside this one). It was never a *code*
dependency — `package.json` declares no dependencies at all, `lib/index.js` injects only `['tools']`,
and nothing anywhere imports, spawns or calls that plugin (**verified by grep over the whole tree**:
the only hits for its name and for `modlens` were this variable's four occurrences). But it was wrong
twice over:

* a **published** plugin must not put another distribution's name in its interface — the README's own
  dependency table read as "this plugin depends on that other plugin";
* a value set for that other toolchain **silently redirected these engines**, so a machine that
  configured it could break xrd without touching it.

Measured on this machine before changing anything: that variable is **not set** in User, Machine or
Process scope, so both plugins were independently probing to the same
`%LOCALAPPDATA%\Programs\Python\Python313\python.exe`. The "sharing" was a coincidence in code, not a
configuration anyone relied on.

### The interpreter now uses the same chain as every other dependency

`pyExecutable()` had only two layers (env → probe) while `gsas_root`, `fp2k_root` and `cif2pcr_exe`
all used the documented four; in particular **`xrd.local.json`'s `"python"` key was documented in the
README and read by nothing**. It is now:

1. `DSH_XRD_PY` — this plugin's own name
2. `python` in `xrd.local.json` (or the file named by `$DSH_XRD_CONFIG`) — now actually honoured
3. probe `%LOCALAPPDATA%\Programs\Python\Python3*`, newest first
4. `python` on `PATH`

No per-call argument, deliberately: "which python" is a machine-level answer and does not belong in
nine tool schemas. `diagnosePython` / `PY_PROBE_MODULES` are exported so the contract is testable
(`dsh-diagnose/ctrl-python-resolve.mjs` asserts all of the above, including that **the old,
foreign-named variable now does nothing**).

### Why failures needed a diagnosis: measured, not assumed

A controlled A/B — same CPython 3.13.14, same engine, same plan, same cwd, **only the interpreter
differing** (a `python -m venv --without-pip` environment has the same version and `base_prefix` but
empty site-packages):

| interpreter | result |
|---|---|
| package-less venv | engine dies at `data_io.py:51  import numpy` (top level, outside `main()`'s try) → **no result file at all**, 209 B traceback, non-zero exit |
| real Python 3.13.14 | `DATA_IO_OK`, result written |

The engines therefore cannot report a structured error, and the tool's `produced no result (exit 1)`
was all the model got. Worse, it was *measured* that **the model receives the `render` text, not the
result JSON** — and `xrd_data_io` and `xrd_phase_screen` were the only two tools whose render did
**not** print `log_tail`. So the visible output was exactly two lines with no mention of numpy, of an
interpreter, or of a traceback.

* **every** python-engine failure branch (7 in `xrd-tools.js` + `xrd_refine_manual`'s throw +
  `xrd_refine_auto`) now appends `diagnosePython(PY)` to the message — one line naming the
  interpreter that was used and the imports that are missing. It goes in `error`, which **every**
  render prints, so all 11 tools benefit and no output schema changes;
* it runs **only on the failure path**, so the success path costs nothing, and it uses
  `importlib.util.find_spec` rather than importing, so no engine dependency is loaded;
* `xrd_data_io` and `xrd_phase_screen` renders now print `log_tail`, like the other nine;
* **GSAS-II is deliberately not probed**: the engines add the GSAS-II root to `sys.path` themselves,
  so a bare `import GSASII` fails even under a perfectly good interpreter (measured — it is the only
  import missing in the control run) and listing it would report a false fault.

### Verification

* (a) `dsh-diagnose/ctrl-python-resolve.mjs` — **8/8**: the resolution chain (probe answers when
  nothing is configured; the old foreign-named variable is set in the test and **must be ignored**;
  `DSH_XRD_PY`
  wins; `xrd.local.json`'s `"python"` is honoured; env beats the file) and all three
  `diagnosePython` shapes (good interpreter / package-less interpreter / missing binary).
* (a) `dsh-diagnose/ctrl-python-failure.mjs` — the tool-level A/B, **5/5 bad + 2/2 control**. It
  drives the real `xrd_data_io` and `xrd_phase_screen` through the real spawn path and prints the
  **render output** in both cases, i.e. what the model actually receives. Under the empty
  interpreter the model now reads, verbatim:

  ```
  xrd_data_io FAILED
  ERROR: data_io produced no result (exit 1)
  [python] …\venv\Scripts\python.exe (3.13.14) is MISSING numpy, pymatgen, gemmi, lmfit,
  matplotlib — the engines cannot run on it. Point the plugin at a full interpreter: set
  DSH_XRD_PY, or put "python" in xrd.local.json.
  --- engine ---
  Traceback (most recent call last):
    File "…\engine\data_io.py", line 51, in <module>
      import numpy as np
  ModuleNotFoundError: No module named 'numpy'
  ```

  Before this change the same run produced the first two lines only. With the real interpreter the
  same two tools report `ok` — the only variable in the comparison is the interpreter.
* (a) the A/B table above, driven from `workspace/_xrd-py-probe/` (fixture kept for re-runs).
* (b) `integrity-check.mjs` **FAIL 0 / WARN 0** (11 tools, 10 engines, 23 actions) and
  `verify-e2e.mjs` **FAIL 0 / WARN 0 / SKIP 0** — the full `build_orbit -> cif_to_pcr ->
  xrd_refine_auto` chain really executed, fp2k included.
* **Not verified:** a live host restart with a deliberately broken interpreter. The interpreter is
  resolved at module load, so that would need a dsh restart; the resolution and the failure text are
  both covered by the two controls above.

## 0.7.0 — 2026-09-22 — **the ADP mode was never switchable, and the review block was never visible**

Six items in one batch from the user (2026-09-22): one interface gap, one visibility defect, and four
workflow rules. **The tool family stays at 11** — the new capability is an action of `xrd_gsas_ops`.

- **NEW — `xrd_gsas_ops` action `set_adp_mode`** (`engine/gsas_ops.py`). GSAS-II decides WHICH thermal
  parameter to refine from the per-atom ADP **mode marker** (`atom[cia]` = `'I'`/`'A'`), **not** from the
  refine flags. Nothing in this tool used to touch that marker, so on an anisotropic project the refinement
  drove six Uij while the Uiso column sat at its initial value — and `uiso_clamp`, `fix_uiso`,
  `export_cif(uiso=physical)` and the Uiso review were all reading a quantity that was not being refined.
  `set_adp_mode {mode:"isotropic|anisotropic", atoms, open_uiso}` switches it. Two details that are
  deliberately not silent: **isotropic zeroes the other five ADP slots** so a stale negative beta cannot
  return if the marker goes back to `'A'`, and the starting Uiso is seeded from the **mean of the U11/U22/U33
  diagonal** (an explicit `uiso:{label:value}` wins) because the slot that becomes Uiso used to hold U11.
- **The gap was visible in our own code, which is how it was confirmed before anything was changed:**
  `atom_table()` read `Uiso` only `if str(at[cia]) == 'I'` — so an anisotropic atom produced **no** Uiso at
  all and silently escaped every Uiso check in `review_table()`. The review now reports each atom's `adp`
  mode, an `adp{isotropic,anisotropic}` count, and checks anisotropic atoms on the **U11/U22/U33 diagonal**
  instead (a negative diagonal is `fail`, exactly like a negative Uiso). The per-atom "this atom is
  anisotropic" notice is `info`, so it does not move the verdict on its own.
- **HIGH — the `review` block was returned but never rendered.** `xrd_gsas_ops`'s `render` printed the
  actions and the atom table and stopped there, so the sanity check the user mandated ("no particularly
  deranged U values") lived in the JSON and was **invisible to the model that is required to act on it**.
  Now rendered: verdict, the ADP counts, each flag as `[severity] what @ where = value`, and the note.
- **Skill rules (no code):**
  - `xrd-workflow` §3: **reply language follows the user's input language**; the language of skills /
    memory / tool descriptions is **never** the reason to answer in a given language. Skills are **not**
    translated here — they quote the user verbatim, so translation is a separate job for an English-facing
    release (and the external-facing README/INTERFACE/CHANGELOG were already English).
  - `xrd-workflow` §6.1: **discovering a second phase / multiphase / any complex situation is itself a
    trigger** for the checklist — no need to wait for a step to "fail".
  - `xrd-workflow` §6.3: the high-defect branch is now **two steps** — first **enumerate the crystal
    families reported in the literature for this material** (with sources), *then* screen 2–3 of them for
    comparative fitting. Picking one family up front silently rules out the others, which is the very
    situation this branch exists for.
  - `xrd-workflow` §6.5 (new sub-section): at the **entry point** of limit mode (counter = 3 failed
    checklist passes) the AI **must** put an explicit choice to the user through the host option box —
    ① continue (limit mode) ② close on the current **physical** result and re-scope the target
    ③ informed exception delivery (raw non-physical values + an explicit "physical limits not met", never
    claimed as compliant) ④ stop and hand back to a human — with the **evidence in the question**
    (best R, `Rexp`/`wRmin`, the gap, the three R values). **Option ② exists because "target not met" can
    be the wrong variable**: an unattainable target drives the counter to 3 no matter how good the fit is,
    so limit mode gets triggered by a *goal* problem and then emits numbers nobody asked for. Also: limit
    mode numbers may appear **only in the limit section** of the report, never as the delivered conclusion,
    and that section may not lead the report.
  - `xrd-workflow` §6.6 (new): **high-entropy / occupancy-modelled materials** — severe lattice distortion
    makes some physical constraints themselves unreliable, so a constraint violation must **not** be used
    to dismiss a GSAS-II result. The boundary is explicit: this lowers the **weight of a criterion**, it
    does **not** change what counts as a valid value (negative Uiso / negative Uij diagonal / out-of-range
    occupancy still go through the red line and §6.5).
  - `xrd-refinement` §4.4: `xrd_verify_fp2k` **runs fp2k once and does not refine**, so its Rwp is
    meaningful only when the pcr is already a converged fp2k pcr. For a converter-built pcr (the
    post-GSAS case) only the **density and cell** sentinels are usable (measured: `Rwp 61.1 / BraggR 94.7`
    on a pcr whose density and cell matched GSAS exactly), the Rwp must be labelled "single shot,
    unrefined", and **the step is not a delivery requirement**.
  - `xrd-refinement` §5: the ADP-mode check joined the mandatory GSAS-output review, with the
    high-entropy carve-out restated as a criterion-weight rule.
- **Also fixed — the sample-scrub table was case-sensitive, and a real sample identifier survived
  it.** The release builder applies its `SCRUB` table with `split/join`, i.e. **case-sensitively**.
  One key was listed only in its lower-case spelling while four files spell it capitalised —
  including a comment in `engine/gsas_export.py` that names the project a measurement was made on
  — so that identifier travelled into the package untouched (red line #2: the release must not
  carry real sample names). Found by scanning the freshly built Windows porting-test bundle, which
  takes its plugin from `dist/`; then **every case variant of every scrub key was enumerated with a
  script** (a case-insensitive sweep over all text files, not by eye — the other five keys have
  exactly one spelling each). The missing variant is now in the table and the package was rebuilt
  **in place**: nothing had been distributed, and the archives built earlier the same day, which
  carried it, were overwritten. **Related mechanism worth knowing:** `README.md`, `LICENSE` and
  `CHANGELOG.md` are injected *after* the scrub walk on purpose (they are release documents, not
  code), so nothing scrubs them — the leak audit is the only thing keeping sample names out of
  them, and it is exactly what rejected the first draft of this paragraph.
- **Also fixed (approved in the same message):** `skills/xrd-tools/SKILL.md` announced **"12 tools"** in its
  body heading while the plugin registers **11** (the same file's description already said 11). Leftover from
  the era when the legacy `autofp_refine` alias was still registered (removed 2026-09-20); nothing checked
  the count. Now 11, and the source tree carries no "12 工具" string any more.

**Verification.**

- **(a) on the real project where the trap was hit** — `gsas_proj.gpx`, a Pnma refinement whose six atoms
  were all left anisotropic. The same input was run twice, **before and after the engine reached the profile
  copy**, which makes it a controlled comparison rather than a demo:
  * **old engine** → `review.verdict = "pass"`, `flags: []`: six anisotropic atoms, **five negative diagonal
    components**, and the review said nothing at all.
  * **new engine** → `review.verdict = "fail"`, 6 × `[info] anisotropic ADP (Uij)` plus **5 × `[fail]`
    negative U22/U33**, `review.adp = {isotropic: 0, anisotropic: 6}`, and every atom row carries
    `adp: "A"` with its six `uij`.
  * **`set_adp_mode isotropic`** on the same project → `ADP mode -> isotropic on 6 atom(s)`, each atom
    `A → I` with `uiso_from_diagonal_mean` recorded, `u_flag_opened: 0` (flags were already `XU`), and the
    reopened review then reports **4 × `[fail] negative Uiso`** — after the switch the check sees atoms it
    was blind to before.
  * **Side finding**: the isotropic Uiso values on this desktop-resolution pattern are negative for four of
    six atoms (Li1 ≈ −0.032, and one Li1 Uij component is −0.2221) — an **independent reproduction** of the
    "desktop data does not constrain Uiso" note that had until now been recorded second-hand.
- **(b) discriminating control for the JS half** (`dsh-diagnose/ctrl-adp-render.mjs`). The render is read
  once at bundle start, so the running host still prints the old one; the control drives the **synced profile
  copy** directly (import → `apply()` with a mock ctx → `tool.output.render(...)` on the real result JSON):
  **8/8 assertions pass** (`REVIEW:`, `adp=` per atom, the counts, `[info]` and `[fail]` flag text). The same
  assertions are then re-run against a copy of `lib/` with **both edits reverted**
  (`if (false && value.review)` and no `adp=` field) and **must trip — it fails 6 assertions**, which is what
  makes the pass mean anything.
- **Regressions re-run, all green:** `integrity-check.mjs` FAIL 0 / WARN 0 (its two-way action-vocabulary
  check now lists `set_adp_mode` as **both advertised and implemented**); `verify-e2e.mjs`
  FAIL 0 / WARN 0 / SKIP 0; `verify-package.mjs` FAIL 0 / WARN 0 / SKIP 0; `three-copy-check.mjs` AGREE.
- **Not verified / boundaries:** the render change reaches the running host only after a dsh restart (the
  engine half is already live — same finding as 0.6.0). `set_adp_mode` was exercised on one real project in
  *reading + switching* mode, not inside an actual anisotropic **refinement**; multi-phase projects are not
  covered. `xrd_verify_fp2k` was **not** run in this batch — only its contract wording changed.



## 0.6.0 — 2026-09-21 — **Scherrer size becomes a tool action, and the amorphous-matrix recipe is written down**

Project-level update (user 2026-09-21): *"谢乐公式对非晶体xrd谱线分析与杂质尺寸判别很重要，要加入为工具。"*
and *"这是项目级别的更新，作为0.6.0版本，继续保留在本地…"* — so this version is **kept local and not
published**; the release point is tied to the review cycle of the project's paper. **No tool was added or
renamed: the tool family stays at 11**, and the new capability is an action of `xrd_data_io`, alongside the
`fwhm` action whose output it consumes (the user chose this over a 12th tool, to be revisited if it turns
out to be missed in real workflows).

- **NEW — `xrd_data_io` action `scherrer`** (`engine/data_io.py`). Per peak window:
  `D = K·λ/(β·cosθ)`, `β` = FWHM in radians, plus `d_spacing`, `beta_rad` and an **assumptions echo block**
  (`assumptions`) carrying λ, K, the subtraction mode, the Kα2 stance and the strain boundary.
  **(b)** discriminating control — `dsh-diagnose/ctrl-scherrer.py`, **27 assertions, 0 real failures**.
  On synthetic peaks of a *known* width (eta=0 pure Gaussian and eta=1 pure Lorentzian both carry
  FWHM == the width they were built with) the returned D equals the closed form `K·λ/(β·cosθ)` to the
  digit (**20.5608 nm** both ways). The gaussian branch (**23.7416**) and the lorentzian branch
  (**41.1216**) each match their own closed form and **differ from each other**, so the subtraction mode
  cannot be ignored silently; the Caglioti path is **bit-identical** to the explicit-width path; and
  over-subtraction comes back as `over_subtracted` with `D_nm` absent rather than as a silent 0.
  The whole battery is then re-run against a **copy of the engine with the quadrature subtraction
  reverted** (broadening silently ignored) and must trip — it does (**exactly 1** assertion), which is
  what makes the pass mean anything. A broad-hump case (FWHM 6° on a sloping background — i.e. the
  amorphous use case) is recovered as **5.73°**, 4.4% low: that is the half-maximum estimator's
  background choice showing through, reported here rather than smoothed over.
  **Three properties are deliberate, not omissions** — each is echoed back next to the number:
  1. **apparent vs corrected** — with no instrumental broadening supplied the value is flagged
     `corrected:false` and its note says APPARENT. Supply `inst_broadening` (degrees) or
     `caglioti:{U,V,W}` (which chains directly off the `fwhm` action's solved U/V/W) to subtract it;
     `inst_mode` selects `gaussian` (√(β_obs²−β_inst²)) or `lorentzian` (β_obs−β_inst). Over-subtraction
     (β_inst ≥ β_obs) is reported as `over_subtracted` **instead of silently returning 0**.
  2. **boundary — micro-strain separation is NOT implemented.** Williamson-Hall / size-strain is out of scope
     by decision; for strain, use GSAS-II's size/strain broadening. D is a coherence length, and a crystallite
     size only under strain-free, monodisperse, size-only broadening.
  3. **on an amorphous hump D is the apparent coherence length of that hump, not a crystallite size**, and
     **Kα2 is not stripped** (an unresolved doublet inflates the width and lowers D).
- **NEW — the amorphous-matrix recipe** (`skills/xrd-preflight` §阶段3, C class). A generic, six-step recipe
  for crystalline impurity peaks sitting on an amorphous matrix: build the matrix with the manual engine's
  `extras`, strip it and read the residual peaks, demand full-pattern consistency before naming a phase, use a
  **real CIF** rather than hand-written Bragg peak constraints, read matrix and impurity widths through the
  **same** Scherrer口径, and state that a peak-area ratio is **semi-quantitative** (not a weight fraction).
  Three measured pitfalls were absorbed into it: the data step must match exactly, XPS is surface-sensitive and
  cannot order XRD peak intensities, and a weak peak can be a tail of a neighbouring/同源 strong peak.
  **Wording stays generic by rule — no sample names, sample numbers or that sample's scientific numbers**
  (the maintenance-workspace memory rule); the originating work instance stays in its own project workspace.
- **Contract surface touched:** `lib/xrd-tools.js` (action help + tool description + render), `INTERFACE.md`
  §2 table, `README.md` §Tools, `skills/xrd-tools` §0/§1, `skills/xrd-workflow` §阶段② table, `package.json`
  (0.6.0). **Nothing else in the tool family changed.**
- **⚠ A process trap this version walked into:** `CHANGELOG.md` was edited *after* `sync.mjs` had run, and
  `three-copy-check.mjs` immediately reported `source vs profile : 27/28 DIFFERING: CHANGELOG.md`. Any edit —
  documentation included — must be followed by `sync.mjs` **and** a package rebuild, or the three-copy rule
  fails on a file that has nothing to do with the code.
- **Regressions re-run, all green.** `integrity-check.mjs` **FAIL 0 / WARN 0** — its two-way action
  vocabulary check now lists `scherrer` as *both advertised and implemented*
  (`xrd_data_io: 7 advertised action(s) all implemented (align, convert, cut, fwhm, head, read, scherrer)`).
  `verify-e2e.mjs` **FAIL 0 / WARN 0 / SKIP 0** (`build_orbit → cif_to_pcr → xrd_refine_auto` executed;
  it needed one sandbox escalation because the tool layer must spawn python through a pipe, which the
  confined mode denies — an environment limit, not a plugin defect). `verify-package.mjs`
  **FAIL 0 / WARN 0 / SKIP 0**; `three-copy-check.mjs` **AGREE** (source == profile byte for byte;
  package == sanitise(source); version 0.6.0 in both).
- **(a) on a real measured pattern.** The shipped fixture `audit/fixtures/Y2O3.dat` (7351 points, step
  0.02°, 3.00–150.00°): windows 28.36–29.96 / 47.72–49.32 / 56.82–58.42° give FWHM 0.1399 / 0.1068 /
  0.1017° and **D = 58.66 / 81.58 / 89.13 nm**, every one labelled APPARENT; each D is re-derived by an
  independent expression and agrees exactly; supplying a 0.05° instrumental width moves them to
  62.81 / 92.31 / 102.35 nm. The engine was also driven black-box through its CLI/plan-JSON interface
  (4 actions) → `ok: true`, 0 warnings, both broadening paths present and correctly labelled.
  ⚠ **These are fixture numbers, not the user's sample.**
- **(a) through the host tool layer — executed, and it did NOT need the restart everyone assumed.**
  `xrd_data_io` was called in the running host with a `scherrer` action on the fixture pattern and came back
  `ok: true`, 3/3 windows, **D = 62.81 / 92.30 / 102.35 nm** (`corrected: true`, `inst_source: caglioti U/V/W`)
  — identical to the black-box run. **This corrects a standing assumption:** a bundled python engine is spawned
  per call, so `sync.mjs` alone already makes its *behaviour* live; what a dsh restart is genuinely needed for
  is the **JS side** (tool description / parameter schema / `render`), which is read once at bundle start. In
  this very call the new engine answered while the old `render` printed no per-peak lines — the two sides are
  independent, and that is how the difference became visible instead of being assumed.
- **Remaining, and cosmetic only:** the running host still advertises the pre-0.6.0 `xrd_data_io` description
  until the next dsh restart. No capability depends on it. The recipe section is a method, not a measurement,
  and no number in it is re-derived here.



## 0.5.3 — 2026-09-21 — **three defects from a cloud end-to-end run, and two dependencies that were never declared**

Found by a clean-container run of the whole workflow — the point of that run was to have no
local state to hide behind. All three were confirmed by reading the source before anything was
changed, and one of them invalidates a claim this project has been making.

- **HIGH — the Uiso element override read the site LABEL instead of the element**
  (`engine/gsas_export.py`). `phase_atoms()` already records the real `element` straight out of
  the atom record, but `choose_uiso()` looked the element up in the label. So a mixed site
  labelled `M1` whose element is Fe missed `by_element['Fe']` and **silently took the default**
  Uiso, and a site labelled `Fe1` whose element is Mn **picked up Fe's value**. Both directions
  are wrong. The element field now wins; the label is only a fallback for records with no usable
  element. **(b)** discriminating control (`dsh-diagnose/ctrl-uiso-element.py`): the same cases
  run against the shipped module and against a copy with the fix reverted — the old code is wrong
  on exactly the two reported cases (`M1/Fe` 0.01 → 0.005, `Fe1/Mn` 0.005 → 0.02), the new code
  is right on all six.
- **MEDIUM — `r_plateau.basis` was a fixed sentence** (`engine/xrd_engine.py`). It stated *"fit
  converged, no parameter on a bound, and nvar/npts below 0.2"* **unconditionally**, so it
  contradicted `fit_status` exactly when the distinction matters: after `max_nfev` was hit, or
  with a parameter sitting on a bound. **`is_plateau` itself was always computed correctly** — the
  prose was the defect, and the skills tell the reader to trust `basis`, so it misled precisely
  where it was read. `basis` is now derived from the flags that produced the verdict and names
  the blockers; the structured `blockers` array was added **to the engine's `result.json`**
  (the packet keeps `basis`, and the tool's closed output schema is deliberately **not**
  extended — the house rule is to express things in logs/derived data rather than grow the
  interface). **(b)** discriminating control (`xrd-plugin-checks/ctrl-plateau-basis.mjs`): the
  manual engine is driven twice through the real tool layer, at `max_nfev=1` and `max_nfev=8000`.
  Measured — tight: `"NOT a plateau: the optimiser did not report success; it hit max_nfev (1) so
  this Rwp is a snapshot, not a minimum"`; loose: the same sentence plus `"; 1 parameter(s) sit on
  a bound"`. The two runs no longer share one sentence, which is exactly what the defect was.
- **HIGH — `verify-e2e.mjs` could not fail** (`workspace/xrd-plugin-checks/`). `cif_to_pcr` errors
  were only `console.log`ged, the final line merely **printed** a verdict ("CIF accepted / CIF
  rejected"), and **no assertion was attached to either** — so a failed conversion still exited
  **0**, and the audit this project leans on reported a pass. It also **never called
  `xrd_refine_auto`** (step 4 was a comment), which made the file name a lie. Rewritten: every
  stage ends in an assertion; the refinement now really runs (against a fixture pattern, and the
  output says plainly that the resulting Rwp is meaningless — what is asserted is that the chain
  executes); an environment limit (`spawn EPERM` in a restricted sandbox) is reported as **SKIP**,
  never as a pass. **(a)** on the build machine, whole chain executing: `FAIL 0 | WARN 0 | SKIP 0`.
  **⚠ Consequence — a claim is withdrawn:** any earlier "the end-to-end audit passes" result is
  **not evidence**, because the script producing it could not fail. It has to be re-run.
- **The dependency list was incomplete and the README never said so.** `pymatgen` and `gemmi` are
  **hard requirements** of `cif_edit.py` (space-group resolution, `build_orbit`, `validate`,
  `edit_structure`, `fetch_*`, `polymorphs`) and of `xrd_engine.py` (the manual engine loads its
  structure through pymatgen); `lmfit` drives the manual engine; `phase_screen.py` and the
  `sanity` block of `xrd_export_cif` need pymatgen too. Only `gsas_ops.py` / `gsas_engine.py`
  degrade gracefully without it. **A clean deployment therefore failed inside them with an import
  error that reads like a plugin defect.** The README now carries a Requirements table with the
  versions this release was tested against (DeepSeek Harness 0.1.0-rc.5; Python 3.13.14; FullProf
  2017 Windows / 2021 Linux with SHA256s; CIFs_to_PCR; GSAS-II 5.8.1; numpy 2.5.1; scipy 1.18.1;
  PyCifRW 5.0.1; pymatgen 2026.5.4; gemmi 0.7.5; lmfit 1.3.4), the one-line install command, and
  the two traps: stay on numpy 2.x, and `pymatgen` pulls ~500 MB of **unconditional** runtime
  dependencies (matplotlib / pandas / plotly / sympy are not extras).
- **The tool description still stated the refuted data-pairing rule** (`lib/index.js`). 0.5.2
  corrected the *code* and the *skills*, but `xrd_refine_auto`'s **description** — the text an
  agent reads *before* deciding to call the tool — still claimed fp2k "resolves the pattern from
  the data filename DECLARED INSIDE the pcr". It surfaced during the **final host-level
  verification**, by reading back the description the running host actually serves. It now states
  the measured rule (the pcr's own stem) and that the `! Files => DAT-file:` line is a comment.
  What the miss would have cost: a reader "fixing" a data-pairing failure by editing a line fp2k
  never reads. Description-only change — no behaviour is affected.
- **Host-level end-to-end finally executed** (the item that had been waiting on a dsh restart).
  `xrd_refine_auto` was called as a real tool through the running host on the Y2O3 fixture: the full
  ladder ran (23 steps, 11 accepted), the packet carried **both** R values
  (`Rwp = 16 (belongs to the delivered pcr)`, `decision mean Rwp = 16`), the delivery came from the
  cached start state (`Rwp_delivered = 16.0`, `cached_pcr = …_step17_start.pcr`, backup
  `…_step17_end.pcr` kept), the data pairing resolved with no `CODEFIL` error, and 57 artifacts were
  retained. **(a)** — through the host, which is the strongest form available here.

## 0.5.2 — 2026-09-21 — **the pattern file is found by the pcr's stem, not by a declared name**

### Release artifacts: two archives, two targets (user decision 2026-09-21)

**Windows deployment uses the `.zip`; Linux deployment uses the `.tar.gz`.** Both are now
produced for every release — before this, only the `.zip` existed, so a Linux install had
to fix permissions by hand. It is not cosmetic: the bundled Linux `fp2k` must be
executable, and a zip that has passed through Windows (unpacked and re-packed, or
extracted by a tool that ignores `external_attr`) lands as `0644` — the binary is present,
the plugin finds it, and the spawn fails, which reads like a missing dependency rather
than a permission problem. `tar` stores the mode in each entry header, so `tar xzf`
restores it. `make-release-tar.py` writes the archive with modes set explicitly and with a
top-level `dsh-xrd-<version>/` directory; `make-release-zip.py` did the same for the zip.
`verify-package.mjs` now **enforces** this: both archives must exist, the tar.gz must carry
one top-level directory, and `fp2k`/`fp2k.exe` inside it must be executable.


- **HIGH — the data-pairing rule was wrong, and the code was built on it** (`lib/index.js`).
  0.5.1's skill audit turned up the falsified premise: fp2k resolves the intensity file from the
  **pcr's own stem** (FullProf's "job code"), and the `! Files => DAT-file: …` line it was
  documented as reading is a **comment**. Re-measured in isolation, same pcr body throughout, with
  the declared file *present in the directory*:

  | pcr name | fp2k looked for | outcome |
  |---|---|---|
  | `Y2O3.pcr` | `Y2O3` | refines normally, Rwp **81.1** |
  | `probe.pcr` | `probe` | `Error opening file CODEFIL.DAT…with CODEFIL=probe`, **0 R-factor lines** |

  The old code resolved the *declared* name and copied the data there. That happened to work only
  because for converter-written pcrs no non-comment name line exists, so the resolution fell back to
  `<stem>.dat` — which is the stem rule. When a pcr really did carry a name line that differed from
  its stem, the data was copied to **a name fp2k never looks up**, and fp2k stopped after the banner
  (no R-factor line — which reads exactly like a wrong structure).
  **Fixed**: always place the data at `<pcr stem><ext>`; when the pcr body carries a non-comment name
  line, place a **second copy** under that name too (same bytes, so the two can never disagree).
  The pcr body is **no longer rewritten** — renaming a line fp2k ignores fixes nothing, and the old
  blanket replace could clobber the second pattern's name in a multi-pattern pcr (a latent bug the
  same edit removes). The "no data file" error now states the stem rule and names the file to create.
  **Evidence: (a) real fp2k, real tool code.** The discriminating layout is exactly the one the old
  rule could not survive — the pcr renamed to `zzz.pcr` while its body carries a **real
  (non-comment)** control line `Y2O3.dat` (line 11 of `skills/fullprof-pcr/templates/Y2O3_xray.pcr`),
  with the data present **only** under `Y2O3.dat`:
  * raw fp2k on that arrangement (measured separately, same bytes) → `CODEFIL=zzz`,
    **0 R-factor lines**;
  * `xrd_refine_auto` **after** the fix → `ok:true`, **rwp 61.2**, and the work directory holds
    **both** `zzz.dat` (the stem rule) and `Y2O3.dat` (the body's name, second copy).
  Driven by `dsh-diagnose/ctrl-datapairing-tool.mjs`, which imports the plugin from the **profile
  copy the host actually loads** and calls the real `execute()` — 4/4 assertions passed.
- The 0.5.1 section's A5 entries stay as they are: the skills were corrected there, this is the
  code catching up.

## 0.5.1 — 2026-09-21 — **the delivered pcr now carries its own Rwp**

A third audit pass (user-run, codex) reported four defects; **all four were confirmed by
reading the source before anything was changed**. Fixing them, and then running the engine
on **real data**, changed the picture twice over — once to refute the audit's own explanation
of the long-standing 16.0/17.5 gap, and once to replace the delivery contract outright.
**This is NOT interface-compatible with 0.5.0**: `xrd_refine_auto` gains one field
(`rwp_mean`) and `rwp` changes meaning (see "Not verified" — anything reading `rwp` as the
ladder headline must move to `rwp_mean`).

### Fixed

- **HIGH — the best-snapshot restore was overwritten by its own re-measurement**
  (`fp2k_driver.py`, restore path in `main()`). `measure_final()` writes its cached
  `restore_lines` into the pcr as the FIRST step of every run, and that cache was taken
  BEFORE `best_pcr` was copied in — so the "restore + re-measure" step actually measured
  the pre-restore state. **Evidence: (b)** isolated reproduction, and the control is
  *discriminating*: it seeds a snapshot that genuinely differs from the ladder-end state,
  executes the restore for real, and is run twice — against this engine and against a copy
  with the one-line refresh surgically removed. With the fix the post-restore
  re-measurement receives the snapshot (pass); without it, it receives the ladder-end
  state (fail), so the control reports opposite verdicts on either side of the change
  instead of passing trivially on both (`dsh-diagnose/ctrl-restore-best2.py`; verdict in
  `ctrl-work/restore2/control-verdict.json`). An earlier control for this item was
  non-discriminating and its result is retracted in place.
  **⚠ The follow-up real run RETRACTED the claim that this bug explained the long-standing
  "16.0 → 17.5 best snapshot is not reproducible" mystery**, and that claim was in this
  file's earlier drafts (and in the 0.5.0 section below). With the refresh fix in place and
  confirmed executing (`restore_verified.requested: true`), real data still re-measured the
  restored snapshot at 17.5 against a recorded 16.0. **The real cause was a different one**
  — see "the delivered pcr did not carry its own Rwp" below. The cache-refresh fix itself is
  still correct and still in the tree; it simply was not the explanation.
- **HIGH — the delivered pcr did not carry its own Rwp** (`fp2k_driver.py`; found by the
  real-data run that retracted the entry above). **Root cause: fp2k reports the Rwp of the
  parameter set it EVALUATED, but what it leaves in the pcr is the NEXT update, which it has
  not evaluated yet.** So the file written back is never the state the reported number
  belongs to, and the ladder's best value (measured from a step's *start* state) could never
  be reproduced from the write-back. Measured on real data, one fresh fp2k run each, same
  fixture:
  | file a fresh refinement starts from | ends at |
  |---|---|
  | the step's own **start** state | **16.0** — the value the driver recorded |
  | the **post-run write-back** | **17.5** |

  This also explains why the ladder's own end state measured 17.5 while an accepted step had
  recorded 16.0: those are two different models, not a reproducibility failure.
  **Fix (user decision): stop re-measuring anything.** `run_fp2k_mean` caches the state each
  step was *launched from* (and keeps the write-back beside it as a labelled backup), and a
  delivery now COPIES the cached start state of the best step instead of restoring a snapshot
  and re-refining it — so the reported number and the delivered file are one measurement by
  construction. **Evidence: (a) real data, end to end** — the delivered pcr is byte-identical
  to the cached start state it came from (hash compared), and three cached states were each
  refined independently with fp2k: `step17_start` → **16.0** (driver said 16.0),
  `baseline_start` → **81.1** (driver said 81.1), `step17_end` (the backup, deliberately not
  in the flow) → **17.5**. On the same data the old contract delivered 17.5 where the new one
  delivers 16.0.
  **Interface:** `xrd_refine_auto` gains ONE field, `rwp_mean` (the repeat mean = the
  decision value), and `rwp` is redefined as the value belonging to the delivered pcr.
  This is the only interface addition in this section and it exists because the user asked
  for both numbers to be reported with their roles separated. `INTERFACE.md` §3 updated.
- **HIGH — the delivered pcr belonged to a different run than the reported Rwp**
  (`run_fp2k_mean`). fp2k writes its result back, so after N repeats the file held the LAST
  run's state while the packet reported the run CLOSEST TO THE MEAN. The function now
  snapshots each run's state in memory and writes the representative run's state back, and
  records `representative_run_index` / `delivered_state` **in the driver's `result.json`
  artifact** — **not** in the tool packet. Stated precisely because the first draft of this
  entry read as if the interface had gained fields: `xrd_refine_auto`'s output schema is
  **closed** (`additionalProperties: false`), and the standing rule here is to fix with
  logs/derived data rather than grow the interface, so these two stay internal diagnostics
  next to the kept artifacts. **(b)** isolated reproduction: three runs 11/14/17 → mean 14
  reported, `representative_run_index=1`, delivered pcr = run #1's state (before the fix the
  file held
  run #2's state).
- **MEDIUM — `set_site_composition` copied the source atom's ranId onto the new rows**
  (`gsas_ops.py`). GSAS-II keeps the random atom id as the LAST element of the atom record
  and only repairs duplicates at index time, and constraints are keyed by ranId — so
  duplicates are ambiguous until then. `split_site` already drew fresh ids
  (`_fresh_atom_ids`); this action now does the same. **(b)** isolated reproduction:
  2 source rows + 2 mixed-site rows → 4 distinct ranIds, source row unchanged.
- **MEDIUM — two smaller defects** (`gsas_export.py`).
  (i) The GPX-export verdict path never consulted `charge_violations`, so a CIF whose only
  defect was a charge imbalance scored `pass` there while the CIF path downgraded it to
  `warn` — the same input judged differently depending on the route. Both paths now agree.
  **Evidence: (c)** source check (guard present on both paths).
  (ii) `_element_of` took every letter and capitalised it, so the composite labels this
  port itself writes (`Fe1_N` → `Fen`, `K2_1` → `K`) never matched `by_element` and those
  atoms silently fell back to the default Uiso. It now reads the leading element symbol.
  **(b)** isolated reproduction: `Fe1_N→Fe`, `K2_1→K`, `Na1→Na`, `Mn→Mn`, `123→''`.

### Not verified — do not describe these as finished

- The **cache-refresh fix** (`_fin_lines`) is **(b)** only: its control is discriminating, but
  the real-data run that exercised the restore path **re-measured 17.5 against a recorded 16.0
  even with the refresh in place**. The refresh line is kept as a correctness fix; it is
  **not** evidence of anything about the 16.0/17.5 gap.
- The **new delivery contract** is **(a)** on real data for the Y2O3 fixture: the delivered file
  is byte-identical to its cached start state, and three cached states independently re-refined
  to the values the driver had recorded for them. **Not yet exercised** on a second sample, on
  a multi-phase project, or through the plugin tool inside a running host (that last one needs
  a **dsh restart** — plugin code is loaded at bundle start).
- **`rwp` changed meaning** — from "the Rwp fp2k last printed" to "the Rwp belonging to the
  delivered pcr" — and `rwp_mean` is new. Any skill text or report template that reads `rwp` as
  the ladder's headline number must read `rwp_mean` for the decision instead. The four XRD
  skills were checked for this; **downstream user reports were not**.
- These fixes are **in no package**: `dist/dsh-xrd-0.5.0*` predates them. Rebuild with
  `workspace/build-xrd-release.mjs` and re-run `verify-package.mjs` on the result before
  handing a package to anyone.
- **This build must not be used for a publication or a formal delivery** until the package is
  rebuilt, `verify-package.mjs` passes on it, and the delivered-Rwp semantics have been
  exercised once end to end through the tool.
- Everything already listed as unverified for 0.5.0 stays unverified (fp2k's own
  non-determinism, external cross-checks not re-run) — **treat every R value as
  provisional**.
- **This build must not be used for a publication or a formal delivery** until the pending
  re-run is done and the package is rebuilt.

### Sync state (important)

`engine/fp2k_driver.py`, `engine/gsas_ops.py`, `engine/gsas_export.py`, `lib/index.js`,
`INTERFACE.md` and this file were edited in the SOURCE tree. The **profile runtime copy has
been synced** (whole-tree mirror, 0 mismatched), but the **packaged plugin has not** — rebuild
with `workspace/build-xrd-release.mjs` and re-run `verify-package.mjs`. Either way the host
only picks code changes up after a **restart**. Verify all three copies with
`three-copy-check.mjs`.

## 0.5.0 — 2026-09-21 — **corrected contents: independent-audit fixes**

Two independent audits of an earlier 0.4.0 build found logic bugs that affect what a
refinement REPORTS. **No tool name changed.** Several parameters and return fields were
added (`partial`, `calibration`, `phase_resolution`, `restored_best`, `codes_changed`,
`atom_codes`, `n_cycles_in_log`, `wR_source`, `phase_weights`, `phase_vanished`,
`all_phases_zero`, `bond_expect_defaults`), so a package built before this one is not
interface-compatible and **must not be used for a publication or a formal delivery**.

### How much of this is actually verified

Every item below carries its evidence level. Read it before quoting a fix:

- **(a) real data, end-to-end** — reproduced on real patterns/projects through the tool
  interface, with the numbers recorded.
- **(b) isolated reproduction** — a purpose-built minimal case pins the behaviour down;
  not yet re-run on the full sample.
- **(c) source-read only** — the defect and the fix are established from the code and
  the vendor file format. No behavioural measurement has been made.

**Not verified as a whole.** No single run has exercised every path in this file, and the
following are known to be incomplete:

- ~~the **`best`-state snapshot is not reproducible** (a best state recorded at 16.0
  re-measured at 17.5).~~ **ROOT CAUSE FOUND in 0.5.1 — and it is NOT the one round 3 first
  claimed.** The 0.5.0 build reported this gap through `restore_verified.within_tolerance=false`
  on every delivery where the ladder ended worse than its own best step. Round 3 attributed it
  to the restore being overwritten by its own re-measurement; that fix was applied and then
  **refuted by a real-data run**, which still re-measured 17.5 against a recorded 16.0. The
  actual cause is that **fp2k leaves the NEXT (unevaluated) parameter update in the pcr**, so
  the write-back is never the state the reported number belongs to — refining it returns 17.5
  where the recorded value was 16.0, while refining the step's own start state returns 16.0.
  The whole restore-and-re-measure contract was therefore replaced in 0.5.1 by delivery of the
  cached start state; `restore_verified.within_tolerance` **no longer exists** in 0.5.1, and
  the delivered pair is same-source by construction. See the 0.5.1 section above.
- ~~"the delivered pcr and the reported Rwp always describe the same state"~~ —
  **this claim was WRONG** (audit round 3): fp2k writes its result back into the pcr, so
  after the repeats the file held the LAST run's state while the packet reported the run
  closest to the mean. Fixed in the 0.5.1 section above (`representative_run_index`,
  `delivered_state`).
- **`rwp` in the 0.5.0 packet was the repeat mean**, not a value belonging to the delivered
  file. In 0.5.1 that job belongs to `rwp_mean` and `rwp` is redefined; read the 0.5.1
  section before comparing numbers across the two versions.
- **fp2k itself is not deterministic.** Re-running the same starting pcr gives a spread of
  up to 1.5 Rwp units, which is why trial acceptance now averages three runs and the
  difference above cannot be attributed with confidence.
- `xrd_google`-style external cross-checks were not re-run. Treat any R value in this
  release as **provisional** until the package is exercised on the sample in question.### Release-blocking (results could be wrong or silently absent)

- **`ladder:false` ran the whole ladder anyway.** `int(plan.get('outer_cycles') or 1)`
  treated the caller's `0` as falsy and turned it into `1`. Score-only mode now reads
  `1 if raw is None else max(0, int(raw))` and does not re-run at the end.
  **(a)** ~19 fp2k calls → 1 (18.6 s → 0.4 s, baseline Rwp only, empty ladder packet).
- **Every atomic refinement wrote its code into the WRONG column.** `set_atom_code`
  patched the last field of the atom main line — that field is `Spc` (the space-group
  code); the five refinement codes `X Y Z Biso Occ` live on the FOLLOWING line (layout
  read off AutoFP's own reader, `pyfullprof/pcrfilereader.py`). No atomic parameter was
  ever released, so the atom/Biso/coordinate ladder steps could not move Rwp. Now:
  correct column per quantity, main line untouched, unverified `N_t` layouts refused
  instead of guessed, and every step records `codes_changed` + `atom_codes` so "did not
  improve" can be told apart from "never reached the file".
  **(b)** isolated check: fixed-atom baseline 81.1 → Biso **78.9**, Occ **80.0**.
- **Three-run trials measured three successive ITERATIONS, not repeats.** fp2k writes
  its result back into the pcr, so 3 sequential runs were a chain. `run_fp2k_mean` now
  restores the start state before every repeat, and reports the run **closest to the
  mean** as representative. **(b)** measured spread dropped to 0 once restored.
  A2c was decided the same way: when candidate means are similar, all of them are
  carried forward rather than picking one on noise.
- **The manual engine's `coord_vary` did nothing.** `pattern_struct()` rebuilt the
  cell and occupancies but always passed the ORIGINAL coordinates, and the pattern
  cache key had no coordinates — so a refined coordinate changed the exported
  structure while the pattern (and χ²) stayed identical. Both fixed, and the cache key's
  occupancy precision went 4 → 10 decimals.
  **(c)** — the fix is a straight data-flow correction; no refinement was re-run to
  measure the resulting R change.
- **Linux could not use the bundled runtime.** `fp2kRoot()` demanded `fp2k.exe`
  while the package also ships `fp2k`; the new platform-aware `findFp2kBinary()`
  picks `fp2k.exe` on Windows and `fp2k` elsewhere. The bundled-runtime probe also
  gained a `nested` path so `third_party/fullprof` is found at all.
  **(b)** path resolution verified; **(c)** for non-Windows execution.
- **The ladder could end worse than its own best step.** In 0.5.0 a snapshot of the best
  accepted state was kept, restored when the final state was worse, and re-measured
  (`restored_best` + a warning reporting both values), with the snapshot written **before**
  the accepted switches were re-asserted and the restore verified afterwards
  (`restore_verified`).
  **(a)** mechanism observed working; **(c)** for the 16.0/17.5 discrepancy.
  **⚠ SUPERSEDED in 0.5.1**: the re-measurement was the wrong instrument (fp2k leaves an
  unevaluated update in the pcr, so the stored snapshot could not carry the recorded value).
  0.5.1 delivers the cached start state instead and drops `restore_verified` entirely.
- **Trial acceptance now uses the MEAN of 3 runs** (`trial_repeats`), because a single
  fp2k run is not reproducible: measured `Pref,Bov` = 16 / 17.5 / 17.5 (spread 1.5).
  Each step reports `Rwp_each` / `Rwp_spread` / `unstable`. The FINAL measurement uses
  the same repetition, so the reported number and the trial criterion agree.
  **(a)**.

### Structure and status

- **Multi-phase constraints were applied to whichever phase shared the labels**
  (`for ph in gpx.phases()`), i.e. the wrong phase on a label collision. `equiv_afrac`,
  `conserve_afrac` and `equiv_uiso` now resolve ONE phase and report it in the new
  per-action `phase_resolution` field (`applied-to-first-match` / `ambiguous` /
  `not-found`, with the candidate phases listed).
  **(a)** on a real two-phase project (F m -3 m + P 21/c, `AtomIdLookup =
  {'0': 6, '1': 20}`): `equiv_afrac K1+K2@mono(2)`, `conserve_afrac` held the sum at 2,
  `equiv_uiso` tied 2 atoms, empty warning, `ok=true`.
- **GSAS constraints silently failed on a fresh project.** `add_EquivConstr` bails out
  when the index tables are empty (`elif G2obj.TestIndexAll()`), and `index_ids()` is
  only populated for phases ATTACHED to a histogram — so every constraint was dropped
  with `KeyError: '1'`, `Constraints[Phase]` stayed empty, and the tool still said
  `ok:true`. The three constraint actions now call `gpx.index_ids()` explicitly.
  **(a)** real single-phase and two-phase constraint application.
  Two related correctness points: a stored constraint variable's `phase` field is a
  **ranId**, not the name-prefix index (the read-back had to accept both, normalised via
  `PhaseIdLookup`), and GSAS silently DROPS a constraint whose parameters are not being
  refined ("Ignoring this constraint; contains no refined parameters") — so the afrac
  actions now set the atom F flag by default and warn when a group is already frozen.
- **`split_site` could create duplicate site IDs** (running it twice, or a clashing
  existing name). Sub-site names are now made unique (`K2_1` → `K2_1_2`) and the
  adjustment is reported. **(b)**.
- **GSAS negative Uiso was checked after the verdict was frozen**, so a run whose only
  problem was a negative Uiso still returned `ok:true` with no warning. The verdict is
  now taken after every check, and `warning` is returned (it had been missing).
  **(a)**.
- **`ok` no longer means "the script finished".** `xrd_gsas_ops` returns `ok:false`
  when any action failed or the final save failed, plus a new `partial` flag;
  `xrd_finalize` returns `ok:false` (with `NOT DELIVERABLE (reason)` in the summary)
  when the packaged project cannot be reopened, lost a phase, failed its CIF export,
  or reports wR ≥ 50%. **(a)**.
- **wR was not necessarily the converged one.** The finalize report took the wR from
  the first cycle it found; it now takes the LAST cycle in the `.lst` and reports its
  provenance (`n_cycles_in_log`, `wR_source`). **(b)**.
- **A vanished phase was invisible.** `xrd_export_cif` now reports `phase_weights`,
  `phase_vanished` and `all_phases_zero`. The severity is **warn, not fail** — a phase
  going to zero can be a legitimate solution ("一相为 0 也是解").
  **(a)** three degenerate two-phase projects: cubic 1.0 / mono 0 → mono stays 0
  (wR 95.32); both 1.0 → GSAS drives cubic to 0 (wR 100.00); cubic 0 / mono 1.0 →
  cubic stays 0 (wR 91.82). GSAS prints `Error: 1-2 Parameter(s) dropped: 0:0:Scale`
  yet still declares `***** Refinement successful *****`, so the tool's own report
  cannot be the only signal.

### Output checks and calibration

- **Kα₂ is placed by Bragg's law**, `sin θ₂ = (λ₂/λ₁)·sin θ₁`, in both the main and
  the second-phase loops (it was a linear 2θ scaling: +0.0005° at 20°, +0.0043° at
  40°, +0.0154° at 60°, +0.197° at 120°). **(c)** — the correction is exact by
  construction; the R-value impact was not separately measured.
- **Charge neutrality is now checked** — conservatively: it only judges when every
  element has an unambiguous oxidation state (multivalent elements are deliberately
  left out, and the model's own declared states are preferred). A neutrality flag
  downgrades the verdict to `warn`; it never invents a chemistry verdict.
  **(b)** — note the check was dead on arrival (a missing `import re` made it raise,
  swallowed by the caller, and the field silently read `None`), which is why a
  passing-looking run now has to be distrusted: **(a)** for the import fix.
- **Default bond windows** ship with the exporter, applied ONLY to contacts that
  exist (an absent default pair is not a violation), caller windows still win, and
  `bond_expect_defaults:false` disables them. **(b)**.
- **A dead exporter verdict.** The verdict was computed before the anomalies were
  collected, so fatal findings (non-positive Uiso, out-of-range occupancy,
  coordinates outside the cell, unreadable cell) could not fail it. The verdict now
  incorporates them (and charge violations downgrade to `warn`), and the summary is
  emitted last. **(a)**.
- **`calibration` states what the figure and the wR belong to.** With
  `uiso.mode=physical` the Uiso is rewritten in the CIF ONLY — no re-refinement is
  performed (not practical) — so the figure/wR belong to the ORIGINAL gpx, which is
  the project to refine in GSAS-II for the final fit result; report the clamped CIF
  and the statistics separately. The four-way branch also covers `mode=both` (the
  official CIF is NOT clamped) and the case where no structured CIF was written at
  all (the clamp was never applied). **(a)**.
- **Manual-engine occupancy multipliers are bounded.** A multiplier above 1 (which
  cannot be interpreted against a CIF occupancy that is already ~1) used to be
  silently absorbed; it now raises a named error at the offending site. These
  multipliers remain **independent** — this engine has no total-conservation
  machinery, so coupled work (equal occupancies, conserved sums) belongs to
  `xrd_gsas_ops`. **(b)**.

### Workflow

- **Limit mode gained a MANDATORY trigger**: after **three complete complex-case
  checklist passes** without reaching the target, limit mode must be entered (no
  further permission needed), closing out on the best GSAS-II result — or the manual
  engine line if GSAS-II failed, stated explicitly — and reporting the final result
  explicitly. The counter lives in `workflow_state.json` as `limit_mode_attempts`.
  **(c)** — this is a procedure, not code; it takes effect through the skill text.
- **Two-phase construction is now documented and tested.** `add_phase`,
  `replace_phase`, `new_from` and `set_site_composition` were implemented in
  `engine/gsas_ops.py` but advertised nowhere, i.e. unreachable in practice. They are
  now in the `xrd_gsas_ops` action list with their meaning.
  **(a)** on a real converged project: `add_phase` produced 2 phases, BOTH linked to
  the single histogram; `set_site_composition` wrote a mixed K/Na site as extra rows
  at the same coordinates. Verification:
  `workspace/xrd-plugin-checks/verify-gsas-ops.mjs`.

### Legacy

- `engine/cif2pcr_helper.py` is **superseded and kept on purpose**: its two repairs
  (phase-name sanitising, Chebyshev → polynomial background) now live in
  `engine/fp2k_driver.py`, and its occupancy audit is `engine/pcr_check.py`. It is
  unreachable from the tool layer. It was not deleted because the engines that work
  are not to be disturbed.
- **Package integrity audit.** `workspace/xrd-plugin-checks/integrity-check.mjs`
  checks, from the runtime copy: tool assembly, every declared engine path existing and
  compiling, the advertised action vocabulary against what each engine dispatches (both
  directions), closed output schemas, the host JSON-schema keyword subset, and six
  named cross-tool chains. Current state: 11 tools, 10 engines, **0 FAIL / 0 WARN**.

### Self-audit module (`audit/`)

- **The package can now prove itself intact on the machine it lands on.** `audit/` ships
  with the release and is built from the workspace tooling wholesale, so the shipped
  module can never lag what is actually run here. Entry point:
  `node audit/verify-all.mjs` (or `audit/integrity-check.mjs` alone).
  **(a)** run against the packaged tree, which audits **itself**: 11 tools, 10 engines,
  six chains, 0 FAIL / 0 WARN. The release leak audit stays clean at 0 findings — that is
  the point of `audit/_lib.mjs`, which derives every path instead of naming one.
- **Nothing machine-specific is written into it.** The scripts resolve the plugin tree,
  the Python interpreter, GSAS-II, fp2k and the official CIF converter from their own
  location plus environment variables, and **skip** (rather than fail) when an input that
  is deliberately not bundled is absent — a converged `.gpx`, a reference Y2O3 project,
  AutoFP's source. `SKIP` is never reported as a pass, and the suite's summary says so.
  The two maintainer sync scripts (`sync.mjs`, `sync-skills.mjs`) are **excluded**: they
  edit a dsh profile/home, which a received package should not be able to do.
- **The audit is what found the two defects below that a source read would not have**:
  `ladder:false` silently running the full ladder, and four `xrd_gsas_ops` actions that
  were implemented but advertised nowhere. Its action-vocabulary check runs in **both**
  directions for exactly that reason.
- Honest limitation: `verify-all.mjs` exercises the self-contained checks. The manual
  engine's several-minute run and the reference-project comparisons are separate scripts
  on purpose, and are not part of the default suite.

## 0.4.0 — 2026-09-21 — **the release now carries the FullProf runtime**

### Why

0.3.0 reduced the FullProf dependency to a single file (`fp2k.exe`) but still
expected the user to obtain it. With the AutoFP *source* no longer part of the
picture there is nothing left to justify shipping without it, so the runtime is
now **inside the package** and the plugin works out of the box.

### Added

- **Bundled runtime** at `third_party/fullprof/`:
  `fp2k.exe` (FullProf 2017, Windows) and `fp2k` (FullProf 2021, Linux), plus the
  upstream `LICENSE` (GPL-3) and a `PROVENANCE.md` recording origin, byte size and
  SHA256 of each file.
  * **No code change was needed to use it.** `lib/config.js` probes the plugin
    directory first and already accepts a directory named `fullprof`, so
    `fp2kRoot()` resolves `<plugin>/third_party/fullprof` on its own. The bundled
    runtime is the *last* chain entry — an explicit argument, `DSH_FP2K_ROOT` or
    `xrd.local.json` still wins, so an existing install keeps using its own copy.
  * `fp2k.exe` was verified to run **as a single file**: it produced its
    R-factor line with `msvcr90.dll` / `msvcp90.dll` absent from the system, so
    the MSVC runtimes that sit next to it in AutoFP's tree are not needed and are
    not shipped.
  * The **Linux binary is shipped unverified** — it cannot be executed on the
    maintainer's Windows machine. It is included for portability, and this is
    stated rather than implied.

### Changed

- `package.json` → `0.4.0`; `third_party` added to `files` so the bundled runtime
  is declared rather than an unlisted extra.
- The release build injects `README.md` / `LICENSE` / `CHANGELOG.md` **from the
  plugin tree** instead of a separate asset copy, and injects `third_party/`
  **verbatim**: scrubbing a third-party licence text or a binary would corrupt
  exactly the thing a licence requires to stay intact.

### Licensing

The 0.4.0 release used separate licences for the plugin code and the bundled
FullProf files. During the 0.9.1 open-source preparation, the repository was
unified under **GNU GPL v3**. The FullProf files remain unchanged and retain
their attribution, hashes, licence text, and source pointer in
`third_party/fullprof/PROVENANCE.md`.

### Unchanged

The interface is untouched: 11 tools, `xrd_refine_auto` parameters and returns,
the ladder order, and every other tool behave exactly as in 0.3.0.

### Known gaps

- Ladder calibration still reaches **Rwp 17.5** on the reference Y2O3 sample where
  the project's own reference fit reports **7.04** (`Asym` / `S_L,D_L` are the
  prime suspects and are deliberately not ported).
- ToF / neutron profiles and multi-phase pcrs are not covered yet.
- No regression test suite ships with the plugin (the verification scripts live in
  the maintainer's workspace).

## 0.3.0 — 2026-09-20 — **breaking: AutoFP removed, interface merged**

### Why

The plugin drove FullProf through AutoFP (the `shautofp.py` wrapper plus its
`diffpy/pyfullprof` pcr reader). That reader **cannot read a `.pcr` written by the
official `CIFs_to_PCR.exe`**: it dies in the background block
(`pcrfilereader.py:749`, `IndexError`) and every call then reported *"refinement
did not report Rwp"* — while `fp2k.exe` itself had refined the file correctly all
along (verified: the same pcr runs in 0.3 s under fp2k and prints its R-factors).

Decision: stop repairing/reproducing AutoFP. FullProf is far more detailed than
GSAS-II, and in this pipeline AutoFP's only role was to *score* — it needs to run
from a script, nothing more.

### Changed

| 0.2.x | 0.3.0 |
|---|---|
| `autofp_refine` + `xrd_refine_auto` (same impl, two names) | **`xrd_refine_auto` only** — the alias is removed |
| ran `AUTOFP_ROOT/shaftofp.py`, read `par.txt` | runs the bundled **`engine/fp2k_driver.py`**, reads Rwp from fp2k's `.out` |
| AutoFP's strategy kept inside the wrapper | strategy **ported** into the driver (parameter order + enable/keep/rollback) |
| needed `shautofp.py` present | needs only **`fp2k.exe`** present |
| params `path`, `cycles`, `autoselect` | params **`pcr`**, `cycles`, **`ladder`**, `out_dir` |
| returns `rwp, refined_pcr, out_files, par_summary, log_tail` | returns **`ok, rwp, refined_pcr, out_files, ladder, cycles, error?, log_tail?`** |
| artifact dir `.autofp-work-*` | artifact dir **`.fp2k-work-*`** |
| `DSH_AUTOFP_ROOT` / `autofp_root` | **`DSH_FP2K_ROOT` / `fp2k_root`** (old names still honoured as a fallback) |
| tool count 12 | **11** |

### Added

- `engine/fp2k_driver.py` — headless fp2k driver (plan JSON in, result JSON out):
  the ported refinement ladder, Rwp parsed from `.out`, per-step accept/rollback,
  and the `ladder` judgement packet.
- `engine/cif2pcr_helper.py` — `preflight` (rejects a CIF the converter cannot use
  in ~1 s instead of after its ~110 s wait, and warns about an unquoted
  space-group name), `assemble`, `normalise`.
- The driver repairs two converter defects that otherwise make `fp2k.exe` refuse
  the file: the **phase-name line** (the converter copies the CIF filename, and the
  dot triggers `FindFMT: invalid character in a numeric field`) and the
  **background panel** (Chebyshev, whose coefficients are invisible to the refine
  codes → rewritten as a polynomial with the Job row corrected).
- `INTERFACE.md` — the explicit interface contract.
- Shared `fp2kRoot()` in `lib/config.js` (the probe was duplicated between
  `index.js` and `xrd-tools.js`; one definition, one error message now).

### Fixed along the way

- The compact `!Job` row must **not** be patched by column name: the two pcr
  generations label it differently, and a name-based edit silently moved `Nex`
  from 2 to 5, after which fp2k stopped right after its banner.
- `cif_to_pcr` timed out on a knife edge: the tool allowed 110 000 ms while the
  converter takes 110.5 s per call. With the AutoFP path gone the bottleneck is
  only the conversion step; the preflight above avoids the worst of it.
- **Two documented AutoFP "red lines" are void**: "disable the complex background
  group" and "the rewritten pcr may have glued columns". Both came from the
  diffpy layer, which is no longer in the path. Background is now released in
  graded steps instead of all at once.

### Unchanged

Every other tool keeps its name, parameters and outputs:
`xrd_data_io`, `xrd_cif_edit`, `cif_to_pcr`, `xrd_phase_screen`,
`xrd_refine_manual`, `xrd_refine_gsas`, `xrd_gsas_ops`, `xrd_export_cif`,
`xrd_finalize`, `xrd_verify_fp2k`.

### Known gaps

- The driver currently reaches **Rwp 17.5** on the reference Y2O3 sample where the
  project's own reference fit reports **7.04**. This is a *calibration* gap in the
  ladder, not an interface problem. The two AutoFP groups `Asym` and `S_L,D_L` are
  deliberately not ported (their fields are not refinable codes in the pcr panel
  this driver writes), and they are the prime suspects.
- ToF / neutron profiles and multi-phase pcrs are not covered yet.
- No regression test suite for the driver itself (verification scripts live in the
  maintenance workspace).
