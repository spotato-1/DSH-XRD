// dsh-xrd: XRD Rietveld refinement workflow as native dsh tools.
//
// Tool family (v0.3 interface, 2026-09-20):
//   xrd_refine_auto   — headless FullProf fp2k refinement driver (this file).
//   xrd_refine_manual — bounded-LM python engine (lib/xrd-tools.js).
//   xrd_verify_fp2k   — fp2k cross-check + chemistry/density.
//   plus the GSAS-II trio, CIF tools, data I/O and phase screening.
//
// Design notes for the fp2k driver:
// - Refinement is driven by the BUNDLED engine/fp2k_driver.py: plan JSON in,
//   result JSON out. It runs `fp2k.exe` directly and reads the R-factors from the
//   `.out` that fp2k writes.
// - The only thing taken from the FullProf installation is `fp2k.exe` itself.
//   AutoFP (the PyQt wrapper `shautofp.py` and its diffpy/pyfullprof pcr reader)
//   is NOT used anywhere in this plugin: that reader cannot read a pcr written by
//   the official CIFs_to_PCR.exe, so every call used to end in
//   "did not report Rwp" while fp2k itself had refined fine. AutoFP's auto-refine
//   strategy (parameter order + enable/keep/rollback loop) was PORTED into the
//   bundled engine, which is why no AutoFP structure survives in this interface.
// - fp2k is spawned by the python driver; under the dsh file sandbox the
//   subprocess/named-pipe path can be denied (EPERM), in which case the caller
//   must run this tool with the wider sandbox.
//
// Requires: python (see env) and fp2k.exe (located via config.js fp2kRoot()).
import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, rmSync, copyFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve, dirname, basename, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { createHash } from 'node:crypto'
import { homedir } from 'node:os'
import { registerXrdManual, registerXrdVerify, registerXrdGsas, registerXrdMultiphase, registerCifToPcr, registerXrdFinalize, registerXrdGsasOps, registerXrdExportCif, registerXrdCifEdit, registerXrdDataIo, registerXrdPhaseScreen, requireExecutionApproval, newRunId, resultMtime, readFreshResult, runEngine } from './xrd-tools.js'
import { resolveDir, missingMessage, fp2kRoot, findFp2kBinary, pyExecutable, diagnosePython } from './config.js'

export const name = 'dsh-xrd'
export const inject = ['tools']

// FullProf suite root. Nothing machine-specific is baked in: the chain is
// $DSH_FP2K_ROOT -> "fp2k_root" in xrd.local.json -> a conventional probe (see
// config.js). Resolution is deferred so the plugin still loads when FullProf is
// absent and only the other tools are used.
//
// The ONLY thing taken from that directory is fp2k.exe: the refinement is driven
// by the bundled engine/fp2k_driver.py, which needs no wrapper, no GUI and no pcr
// parser of its own.
// This file lives in <plugin>/lib/, so the plugin root is one level up.
const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BUNDLED_FP2K_DRIVER = join(PLUGIN_ROOT, 'engine', 'fp2k_driver.py')

const PY = pyExecutable()

// Mirror the bundled skills to the user skill root — WHOLE TREE, BY CONTENT (0.8.7).
//
// The 0.8.6 version copied only `skills/<name>/SKILL.md` and decided by MTIME, and both
// halves were wrong (fourth review): the workflow's `refs/` (the defect catalogue) and
// fullprof-pcr's `templates/*.pcr` were never deployed at all, and a copy the plugin had
// just written always had a LATER mtime than the next release's source file, so every
// later upgrade was skipped forever — "existing users never receive the new logic".
//
// Now: every file under skills/ is mirrored recursively, and the decision is by SHA-256
// with a record of what WE last wrote (`.dsh-xrd-skills.json` in the user skill root):
//   * content equals the shipped file            -> already current, do nothing;
//   * file missing                               -> write it (new skill, new refs);
//   * differs, but equals what we last wrote     -> upgrade it (ours, untouched by hand);
//   * differs and is NOT what we last wrote      -> the USER changed it: keep theirs and
//                                                   report it (never silently clobber).
function sha256File(p) {
  try { return createHash('sha256').update(readFileSync(p)).digest('hex') } catch { return null }
}

function walkFiles(root, rel = '') {
  const out = []
  let ents = []
  try { ents = readdirSync(join(root, rel), { withFileTypes: true }) } catch { return out }
  for (const e of ents) {
    const r = rel ? join(rel, e.name) : e.name
    if (e.isDirectory()) out.push(...walkFiles(root, r))
    else out.push(r)
  }
  return out
}

export function syncBundledSkills() {
  const summary = { version: null, added: [], updated: [], kept: [], current: 0, errors: [] }
  try {
    const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
    const userSkillRoot = join(dshHome, 'skills')
    const bundledRoot = join(dirname(fileURLToPath(import.meta.url)), '..', 'skills')
    if (!existsSync(bundledRoot)) return summary
    let version = null
    try {
      version = JSON.parse(readFileSync(join(PLUGIN_ROOT, 'package.json'), 'utf8')).version ?? null
    } catch { /* version is only for the record */ }
    summary.version = version
    mkdirSync(userSkillRoot, { recursive: true })
    const stampPath = join(userSkillRoot, '.dsh-xrd-skills.json')
    let stamp = { files: {} }
    let haveStamp = false
    try {
      stamp = JSON.parse(readFileSync(stampPath, 'utf8')) || { files: {} }
      haveStamp = true
    } catch { /* first run under this scheme */ }
    const recorded = stamp && typeof stamp.files === 'object' && stamp.files ? stamp.files : {}
    const next = { version, updated_at: new Date().toISOString(), files: { ...recorded } }
    // A LEGACY INSTALL (skills copied before this record existed) has no stamp, so every
    // file would look "user-modified" and stay stale forever — exactly the complaint this
    // release is fixing. There, upgrade, but keep the previous content beside it as
    // `<name>.user-backup-<stamp>` so nothing a user wrote is lost.
    const legacyBackup = haveStamp ? null : String(Date.now())
    for (const rel of walkFiles(bundledRoot)) {
      const src = join(bundledRoot, rel)
      const dst = join(userSkillRoot, rel)
      const srcHash = sha256File(src)
      if (!srcHash) continue
      try {
        if (!existsSync(dst)) {
          mkdirSync(dirname(dst), { recursive: true })
          copyFileSync(src, dst)
          next.files[rel] = srcHash
          summary.added.push(rel)
          continue
        }
        const dstHash = sha256File(dst)
        if (dstHash === srcHash) { next.files[rel] = srcHash; summary.current += 1; continue }
        if (recorded[rel] && recorded[rel] === dstHash) {
          copyFileSync(src, dst)
          next.files[rel] = srcHash
          summary.updated.push(rel)
          continue
        }
        if (legacyBackup) {
          try { copyFileSync(dst, `${dst}.user-backup-${legacyBackup}`) } catch { /* best effort */ }
          copyFileSync(src, dst)
          next.files[rel] = srcHash
          summary.updated.push(rel)
          summary.errors.push(`${rel}: upgraded from a pre-0.8.7 install; the previous copy is kept as `
            + `${basename(dst)}.user-backup-${legacyBackup}`)
          continue
        }
        // known record says this is NOT what we wrote: the user edited it — keep it
        summary.kept.push(rel)
      } catch (e) {
        summary.errors.push(`${rel}: ${String(e?.message ?? e).slice(0, 120)}`)
      }
    }
    try { writeFileSync(stampPath, JSON.stringify(next, null, 1), 'utf8') } catch { /* read-only home */ }
  } catch (e) {
    summary.errors.push(String(e?.message ?? e).slice(0, 200))
  }
  return summary
}

// NOTE (P1, 0.8.3): the local runCli() that lived here is GONE. It spawned with
// `child.kill()` on timeout — direct child only, no grace, no tree, no
// verification — which is exactly the defect the 2026-09-27 audit reported. All
// engine starts now go through runEngine() (lib/xrd-tools.js), which owns the
// per-backend liveness policy, the graceful stop and the tree kill. Keeping a
// second launcher around is how the old behaviour would creep back in.

export function apply(ctx) {
  // ---- bundled skills → user skill root (~/.dsh/skills) ----
  // The host skill-filesystem provider only scans <dshHome>/skills (and project roots),
  // NOT plugin node_modules. So ship the whole skills/ tree inside this bundle and mirror
  // it here. WHAT is copied and HOW the decision is made changed in 0.8.7 — see
  // syncBundledSkills() above: whole tree, by content hash, and a hand-edited file is
  // KEPT (and reported) rather than silently overwritten or permanently stale.
  const skillSync = syncBundledSkills()
  if (skillSync && (skillSync.updated.length || skillSync.kept.length || skillSync.added.length)) {
    try {
      console.error('[xrd] skills: ' + JSON.stringify({
        version: skillSync.version, added: skillSync.added.length,
        updated: skillSync.updated, kept: skillSync.kept,
      }))
    } catch { /* diagnostics only */ }
  }
  // ---- xrd engine tool family (manual refine + fp2k verify) ----
  registerXrdManual(ctx)
  registerXrdVerify(ctx)
  registerXrdGsas(ctx)
// 0.8.10: the >=3-phase entry with an AI-editable engine template (hard gate:
// 1-2 phases stay on registerXrdGsas; the template refuses a lower count at run
// time as well, so an edited copy cannot bypass it).
registerXrdMultiphase(ctx)
  registerCifToPcr(ctx)
  registerXrdFinalize(ctx)
  registerXrdGsasOps(ctx)
  registerXrdExportCif(ctx)
  registerXrdCifEdit(ctx)
  registerXrdDataIo(ctx)
  registerXrdPhaseScreen(ctx)
  ctx.tools.register(makeFp2kRefineTool(ctx))
}

// Headless FullProf fp2k refinement driver. ONE tool, ONE name.
//
// The old `autofp_refine` alias is intentionally gone (2026-09-20): the plugin no
// longer uses any AutoFP structure, so keeping a second name for the same driver
// would only preserve a dependency that no longer exists. Callers use
// `xrd_refine_auto`, which is also the name the skills reference.
function makeFp2kRefineTool(ctx) {
  return {
    name: 'xrd_refine_auto',
    description:
      'Refine a FullProf .pcr with the fullprof engine fp2k, driven headlessly by this plugin. The bundled engine/fp2k_driver.py runs fp2k.exe directly and reads the R-factors from the .out fp2k writes; the only file needed from the FullProf installation is fp2k.exe (AutoFP and its pcr reader are not used, and neither is FullProfApp). It applies a ported auto-refinement ladder — scale, zero point, background (a few terms, then the rest), cell, W, then V/U and Y/X, then atom coordinates and Biso — enabling ONE group at a time and keeping it only when Rwp improves (otherwise it rolls back). TWO R VALUES ARE REPORTED, WITH DIFFERENT ROLES: `rwp` belongs to the DELIVERED pcr (the value that run measured, so it travels into the next stage), while `rwp_mean` is the repeat mean and is the DECISION value — use it to pick between candidate CIFs and to judge whether the result is good enough to go straight into GSAS-II. A delivery never re-measures a restored snapshot (fp2k is not reproducible enough for that): each run caches the state it was LAUNCHED from, and when the ladder ends worse than its best step that cached start state is delivered instead, copied rather than re-refined. The state fp2k wrote back is kept alongside as a labelled backup (it holds the refined parameters) but does not enter the flow. `pcr` is the .pcr to refine; fp2k resolves the pattern from the pcr file stem (the FullProf job code) — the `! Files => DAT-file:` line is a COMMENT and has no effect — so the data file must sit next to it as `<pcr stem><ext>`; a non-comment name line in the body is honoured with a second copy, so either spelling works. (Corrected 2026-09-21: this description used to state the opposite, and a controlled experiment refuted it.) The driver repairs two defects the official CIFs_to_PCR.exe leaves behind, without which fp2k cannot open the file: the phase-name line (it copies the CIF filename, and the dot makes fp2k abort with "FindFMT: invalid character in a numeric field") and the background panel (its Chebyshev layout hides the coefficients from the refine codes, so it is rewritten as a polynomial). Artifacts (.out/.sum/.prf, the delivered pcr, the per-step start/backup pcrs, the step-by-step Rwp trace) are kept in a .fp2k-work-* directory next to the pcr. Returns { ok, rwp, rwp_mean?, refined_pcr, out_files, ladder, cycles, error?, log_tail? }; on failure rwp is null and error explains, but artifacts are preserved — never a bare throw. Needs fp2k.exe located via $DSH_FP2K_ROOT or "fp2k_root" in xrd.local.json. May need a wider sandbox (subprocess) on first use.',
    parameters: {
      type: 'object',
      properties: {
        pcr: { type: 'string', description: 'Absolute path to the .pcr file. Its pattern data (the filename declared inside the pcr) must be in the same directory.' },
        ladder: { type: 'boolean', description: 'Run the full refinement ladder (default true). false = a single baseline refinement, useful for scoring a structure quickly.' },
        cycles: { type: 'integer', description: 'fp2k refinement cycles per call, 5-200 (default 20). Raise it only when the ladder stalls early.' },
        wavelength: { type: 'number', description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): a single monochromatic wavelength in A. The driver sets it on its WORKING COPY of the pcr (the file fp2k actually refines, inside the .fp2k-work-* directory) BEFORE anything runs, and reports 改前 -> 改后 back in `wavelength`; the pcr you passed in is left exactly as it was. OMIT it and the pcr keeps the value the converter wrote (default-off, byte-identical). Take the value from the data file the user supplied (xrd_data_io `header` dumps its header for you to read). Mutually exclusive with `wavelength_doublet`.' },
        wavelength_doublet: { type: 'array', items: { type: 'number' }, description: 'OPTIONAL and EXPLICIT-ONLY (0.9.0): [K-alpha1, K-alpha2, ratio] for a laboratory tube. Omit to leave the pcr untouched. Mutually exclusive with `wavelength`.' },
        out_dir: { type: 'string', description: 'Where the .fp2k-work-* artifact directory is created (default: the session workspace).' },
      },
      required: ['pcr'],
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          rwp: { type: 'number', description: 'Rwp that BELONGS TO THE DELIVERED pcr — the value that run itself measured. This is the flow value: it enters the next stage together with the pcr. Absent when the refinement reported none.' },
          rwp_mean: { type: 'number', description: 'Mean of the repeat runs — the DECISION value: use it to choose between candidate CIFs and to judge whether the result is good enough to go straight into GSAS-II. It belongs to the set of runs, not to any single file, so it is reported separately from `rwp`. Omitted when only one run was made (then it equals `rwp`).' },
          refined_pcr: { type: 'string', description: 'The .pcr the reported Rwp belongs to, in the .fp2k-work-* directory. NAMING NOTE (0.8.6): this is the best state the ladder reached — the seed a later run (or GSAS-II) should start from — and NOT necessarily a file that already carries the final write-back parameters, because fp2k\'s own write-back measures differently from the state the reported Rwp was taken from. Treat it as "the best seed", not as "the converged output".' },
          out_files: { type: 'array', items: { type: 'string' }, description: 'Kept artifacts: the refined pcr, fp2k .out/.sum/.prf, the plan and result JSON.' },
          ladder: { type: 'string', description: 'Judgement packet: which refinement groups were accepted and the Rwp each reached, e.g. "scale=80.1, zero=74.9, simple background/L1=52.1, W=35.3, Pref,Bov=16.0". Empty when ladder was false.' },
          cycles: { type: 'integer', description: 'fp2k cycles actually used.' },
          warning: { type: 'array', items: { type: 'string' }, description: 'The driver\'s own warnings (0.8.6): failed repeat runs, unstable spreads between repeats, skipped background levels, a baseline measured over fewer runs than the candidates. READ THEM before trusting a small Rwp improvement — fp2k is not reproducible run to run.' },
          repeat_runs: {
            type: 'object',
            additionalProperties: true,
            description: 'How each accepted step was measured (0.8.6): {repeats, baseline_n_ok, baseline_spread, unstable_steps, repeats_failed, background_levels_skipped}. A step is only accepted when EVERY repeat succeeded; `repeats_failed` counts the steps that were rejected for that reason.',
          },
          error: { type: 'string' },
          wavelength: { type: 'object', additionalProperties: true, description: 'Present ONLY when `wavelength`/`wavelength_doublet` was passed (0.9.0): {ok, changed, before, after, kind, value_line_no, warnings}. before/after are the pcr\'s Lambda1 VALUE lines, so the change is auditable; when the pcr already carried the requested value, changed=false and `note` says so.' },
          stopped: { type: 'boolean', description: 'true when the driver stopped itself at a safe point because it was asked to (0.8.7) — the numbers, if any, describe a partial run.' },
          aborted: { type: 'boolean', description: 'true when the CALL was cancelled (0.8.7). A cancel is not a timeout and not a fit.' },
          log_tail: { type: 'string' },
        },
        required: ['ok', 'out_files'],
        additionalProperties: false,
      },
      render: (_args, value) => {
        value = value ?? {}   // a failed call has no value; render must not throw
        const head = value.rwp === undefined || value.rwp === null
          ? 'fp2k refinement: no Rwp reported'
          : `Rwp = ${value.rwp} (belongs to the delivered pcr)${value.cycles ? ` [${value.cycles} cycles]` : ''}`
        const lines = [head]
        if (typeof value.rwp_mean === 'number') {
          lines.push(`decision mean Rwp = ${value.rwp_mean} (repeat mean — use THIS to pick between candidate CIFs and to judge the GSAS-II gate)`)
        }
        if (value.ok === false) lines.push('RESULT: failed — check artifacts, do not treat as a fit')
        if (value.error) lines.push('ERROR: ' + value.error)
        if (value.ladder) lines.push('ladder: ' + value.ladder)
        if (value.wavelength) {
          const w = value.wavelength
          if (w.changed) {
            lines.push(`wavelength: pcr Lambda1 value line ${w.before} -> ${w.after} (${w.kind})`)
          } else {
            lines.push(`wavelength: pcr Lambda1 line NOT changed — ${w.note || (w.warnings || []).join('; ')}`)
          }
        }
        if (value.refined_pcr) lines.push('refined pcr: ' + value.refined_pcr)
        if (value.out_files?.length) lines.push(`artifacts (${value.out_files.length}): ${value.out_files.join(', ')}`)
        if (value.log_tail) lines.push('--- log tail ---\n' + value.log_tail)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 900_000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (typeof args?.pcr !== 'string' || !args.pcr.trim()) {
        throw new Error('xrd_refine_auto needs a non-empty "pcr" (absolute path to a .pcr file)')
      }
      const pcr = resolve(args.pcr)
      if (!existsSync(pcr)) throw new Error(`xrd_refine_auto: pcr file not found: ${pcr}`)
      if (!basename(pcr).toLowerCase().endsWith('.pcr')) {
        throw new Error(`xrd_refine_auto: expected a .pcr file, got ${basename(pcr)}`)
      }
      const cycles = Number.isInteger(args.cycles) ? Math.min(Math.max(args.cycles, 5), 200) : 20
      const ladder = args.ladder !== false
      // Things the caller must be told about but which do not stop the run (0.8.7):
      // currently a data file name carried by the pcr body being REFUSED because it is
      // not a bare file name.
      const pathWarnings = []

      // 产物/工作目录:默认会话工作区(exec.agent.session.header.cwd),可 out_dir 覆盖;
      // 不在 pcr 旁建目录,避免污染数据目录、结果归入工作区便于追溯。
      const sessionCwd = exec?.agent?.session?.header?.cwd
      const baseDir = args.out_dir ? resolve(args.out_dir)
        : (sessionCwd ? resolve(sessionCwd) : dirname(pcr))
      mkdirSync(baseDir, { recursive: true })
      const pcrDir = dirname(pcr)
      const workDir = join(baseDir, `.fp2k-work-${basename(pcr).replace(/\.pcr$/i, '')}-${randomUUID().slice(0, 6)}`)
      mkdirSync(workDir, { recursive: true })
      const workPcr = join(workDir, basename(pcr))
      copyFileSync(pcr, workPcr)
      const stem = basename(pcr).replace(/\.pcr$/i, '')
      // DATA PAIRING — CORRECTED 2026-09-21 by direct experiment.
      //   * fp2k resolves the intensity file from the pcr's own STEM (FullProf's "job
      //     code"), NOT from a name written in the pcr body. Measured on the same pcr
      //     body with the declared file PRESENT in the directory: renaming the pcr to
      //     `probe.pcr` produced `Error opening file CODEFIL.DAT…with CODEFIL=probe`
      //     and ZERO R-factor lines, while under its own name the same bytes refined
      //     normally to Rwp 81.1.
      //   * the `! Files => DAT-file: …` / `!File names of data(patterns) files` line is
      //     a COMMENT and has no effect. The previous rule here ("fp2k reads the declared
      //     name") came from an experiment that a stray same-directory data file had
      //     contaminated — see MAINTENANCE.md.
      //   * when nothing matches, fp2k does NOT fail loudly: it stops after the banner,
      //     the .out carries no R-factor line, and that reads exactly like "the
      //     structure/occupancies are wrong".
      // So the data MUST exist as `<pcr stem><ext>` in the work dir. A non-comment name
      // line is ALSO honoured with a second copy — a pcr that genuinely lists its
      // patterns may be read that way, and one extra small copy cannot hurt. The pcr body
      // is deliberately NOT rewritten any more: renaming a line fp2k ignores fixes
      // nothing, and the old blanket replace could clobber the second pattern's name in a
      // multi-pattern pcr.
      const DATA_EXTS = ['.dat', '.xra', '.fxye', '.txt', '.xy', '.raw']
      const pcrDirFiles = readdirSync(pcrDir)
      const byLower = new Map(pcrDirFiles.map((f) => [f.toLowerCase(), f]))
      const declared = []
      try {
        const text = readFileSync(workPcr, 'utf8')
        for (const line of text.split(/\r?\n/)) {
          const m = line.match(/^\s*([^\s!][^\s]*\.(?:dat|xra|fxye|txt|xy|raw))\s*$/i)
          if (m) declared.push(m[1])
        }
      } catch { /* unreadable pcr ⇒ the stem rule below still applies */ }
      // 1) the name fp2k actually looks for: <pcr stem> + an extension fp2k accepts
      let dataSrc = null
      let dataExt = null
      for (const ext of DATA_EXTS) {
        const hit = byLower.get((stem + ext).toLowerCase())
        if (hit) { dataSrc = hit; dataExt = ext; break }
      }
      // 2) else a non-comment name carried by the pcr body (other writers / older pcrs)
      if (!dataSrc && declared.length) {
        const hit = byLower.get(declared[0].toLowerCase())
        if (hit) { dataSrc = hit; dataExt = extname(hit) }
      }
      if (!dataSrc) {
        const loose = pcrDirFiles.filter((f) => DATA_EXTS.includes(extname(f).toLowerCase()))
        throw new Error(
          `xrd_refine_auto: no data file for ${basename(pcr)} — fp2k looks the pattern up by ` +
          `the pcr's OWN name (its stem), so it needs "${stem}<ext>" where <ext> is one of ` +
          `${DATA_EXTS.join('/')}, in the same directory. A missing data file makes fp2k stop ` +
          `after the banner with no R-factor line, which is NOT a failed fit.` +
          (declared.length ? ` The pcr also names "${declared[0]}" on a control line.` : '') +
          ` Found next to the pcr: ${loose.length ? loose.join(', ') : '(no data-like file at all)'}. ` +
          `Rename the data to "${stem}<ext>" (or pass a pcr whose stem matches the data) and retry.`)
      }
      const primary = `${stem}${dataExt}`
      copyFileSync(join(pcrDir, dataSrc), join(workDir, primary))
      if (declared.length && declared[0].toLowerCase() !== primary.toLowerCase()) {
        // second copy under the name the pcr body carries, so BOTH lookup rules are
        // satisfied; the same bytes, so the two can never disagree.
        //
        // THE NAME IS UNTRUSTED INPUT (0.8.7): it is read out of a file, and the fourth
        // review showed that copying it verbatim let a pcr carry `../escaped-copy.dat`
        // and write a full copy of the pattern OUTSIDE the work directory. Only a bare
        // file name is accepted, and the resolved target must still be inside workDir.
        const declaredSafe = (() => {
          const raw = String(declared[0])
          if (!raw || raw.includes('..')) return null
          if (/^[A-Za-z]:/.test(raw) || raw.startsWith('/') || raw.startsWith('\\')) return null
          if (raw.includes('/') || raw.includes('\\')) return null
          const target = resolve(workDir, raw)
          if (dirname(target) !== resolve(workDir)) return null
          if (basename(target) !== raw) return null
          return raw
        })()
        if (declaredSafe) {
          copyFileSync(join(pcrDir, dataSrc), join(workDir, declaredSafe))
        } else {
          pathWarnings.push(
            `the pcr names "${declared[0]}" on a control line, which is NOT a bare file name — `
            + 'the second copy was refused (a name from a file must not be able to write outside '
            + `the work directory). The pattern was still placed as "${primary}".`)
        }
      }
      // auxiliary files the driver/profile steps may want (.cif / .prf); these have
      // no name constraint, so copy any that were not already carried over. NOTE: no
      // fuzzy "first .dat wins" copying — an unrelated sample's data must never be
      // silently substituted for the missing one (that is what the check above is for).
      for (const f of pcrDirFiles) {
        const lower = f.toLowerCase()
        if (lower.endsWith('.cif') || lower.endsWith('.prf')) {
          const dst = join(workDir, f)
          if (!existsSync(dst)) {
            try { copyFileSync(join(pcrDir, f), dst) } catch { /* best effort */ }
          }
        }
      }
      // ── the refinement itself: bundled headless fp2k driver ──
      const fp2kPath = findFp2kBinary(fp2kRoot())
      const planPath = join(workDir, 'fp2k_plan.json')
      const resultPath = join(workDir, 'fp2k_result.json')
      const runId = newRunId()   // P0 (0.8.3): this run's identity, echoed by the driver
      // 0.9.0 wavelength (EXPLICIT-ONLY): omitted => the pcr's own Lambda1 value line
      // stays exactly as the converter wrote it; given => the driver rewrites it
      // before anything runs and hands 改前 -> 改后 back in result.wavelength.
      let wavelengthPlan = null
      if (args.wavelength != null && args.wavelength_doublet != null) {
        throw new Error('xrd_refine_auto: pass either `wavelength` (a single value) or '
          + '`wavelength_doublet` ([kalpha1, kalpha2, ratio]) — not both')
      }
      if (args.wavelength != null) {
        const v = Number(args.wavelength)
        if (!Number.isFinite(v) || v <= 0) throw new Error('xrd_refine_auto: `wavelength` must be a positive number (A)')
        wavelengthPlan = { kalpha1: v, kalpha2: 0, ratio: 0 }
      } else if (args.wavelength_doublet != null) {
        const d = args.wavelength_doublet
        if (!Array.isArray(d) || d.length < 2) {
          throw new Error('xrd_refine_auto: `wavelength_doublet` must be [kalpha1, kalpha2, ratio]')
        }
        const [k1, k2, r] = d.map(Number)
        if (!Number.isFinite(k1) || !Number.isFinite(k2) || k1 <= 0 || k2 <= 0) {
          throw new Error('xrd_refine_auto: `wavelength_doublet` needs two positive wavelengths (A)')
        }
        wavelengthPlan = { kalpha1: k1, kalpha2: k2, ratio: Number.isFinite(r) ? r : 0.5 }
      }
      writeFileSync(planPath, JSON.stringify({
        run_id: runId,
        pcr: workPcr,
        fp2k: fp2kPath,
        cycles,
        outer_cycles: ladder ? 1 : 0,
        timeout: 300,
        working_copy: false,
        ...(wavelengthPlan ? { wavelength: wavelengthPlan } : {}),
        // P1 (0.8.5): the driver's guard looks for .xrd-stop / writes .xrd-progress.json
        // in plan["out_dir"] — WITHOUT this the fp2k backend had no stop file to watch
        // at all, so a cancel could only be answered by a tree kill. Same directory the
        // JS layer writes the stop request into (workDir).
        out_dir: workDir,
      }, null, 1))
      // bundled engine ⇒ plugin-owned, but still routed through the approval hub
      await requireExecutionApproval(ctx, exec, 'script', BUNDLED_FP2K_DRIVER,
        `run headless fp2k refinement driver: ${BUNDLED_FP2K_DRIVER}`)
      const preMtime = resultMtime(resultPath)   // P0 (0.8.3): before the child starts
      // backend 'fp2k' ⇒ the FIXED policy: the driver only waits for a black box
      // that touched its outputs 2-3 times per run in every measurement we have,
      // so fp2k's own stall rule (engine/fp2k_guard.py) stays the authority.
      // The run still gains the graceful stop, the grace period and the tree kill.
      const { code, stdout, stderr } = await runEngine(PY, [BUNDLED_FP2K_DRIVER, planPath, '--out', resultPath], workDir, {
        timeoutMs: 880_000,
        signal: exec?.signal,
        backend: 'fp2k',
        runId,
        outDir: workDir,
        ctx,
      })
      const logTail = (stdout + '\n' + stderr).slice(-3000)
      let rwp = null
      // P0 (0.8.3): only a result THIS run wrote may be reported. The work dir is
      // per-call today, but the same bug class was reachable here too — a killed
      // ladder left fp2k_result.json behind, and reading that path back accepted
      // the previous run's numbers as this run's own.
      const fresh = readFreshResult(resultPath, { preMtime, runId })
      const driver = fresh.out
      if (driver && typeof driver.Rwp === 'number') rwp = driver.Rwp
      // judgement packet: the ladder groups that were accepted, with their Rwp
      const steps = Array.isArray(driver?.steps) ? driver.steps : []
      const accepted = steps.filter((s) => s.accepted && s.group !== '(baseline)')
      const ladderText = accepted.length
        ? accepted.map((s) => `${s.group}${s.level !== undefined ? '/L' + s.level : ''}=${s.Rwp}`).join(', ')
        : ''
      // TWO R VALUES, TWO ROLES (user decision 2026-09-21):
      //   rwp      — the value that BELONGS TO THE DELIVERED pcr (that run's own R). It
      //              is what travels into the next stage; up to GSAS-II this workflow is
      //              R-driven, so the number entering a step must belong to the file
      //              entering it.
      //   rwp_mean — the MEAN of the repeats, i.e. the DECISION value: it is what picks
      //              between candidate CIFs and what "good enough to go straight into
      //              GSAS-II" is judged on.
      // The driver no longer re-measures a restored snapshot to produce the first one
      // (fp2k cannot be relied on to converge back to the same solution); it delivers the
      // cached pcr a run actually produced and reports that same run's R.
      const rwpMean = driver && typeof driver.Rwp_mean === 'number' ? driver.Rwp_mean : null
      // 收集产物(全保留:精修后 pcr、out/sum/prf/fst、plan 与 result)
      const outFiles = readdirSync(workDir)
        .filter((f) => /\.(out|prf|sum|fst|pcr|new|json)$/i.test(f) || /^fp2k/.test(f))
        .map((f) => join(workDir, f))
      if (rwp === null && driver && driver.error) {
        return {
          ok: false,
          error: `xrd_refine_auto: ${driver.error}`,
          refined_pcr: driver.refined_pcr || workPcr,
          out_files: outFiles,
          ladder: ladderText,
          cycles,
          stopped: driver.stopped === true ? true : undefined,
          log_tail: String(driver.log_tail || logTail).slice(-1000),
        }
      }
      // 错误也返回结果(可追溯):rwp 为空时省略 rwp 字段并携带 error,不吞现场
      if (rwp === null) {
        return {
          ok: false,
          error: `xrd_refine_auto: the refinement did not report Rwp (driver exit ${code}`
            + `${fresh.reason ? `; ${fresh.reason}` : ''}); `
            + 'artifacts kept — a missing R-factor line means fp2k did not complete, '
            + 'which is NOT the same as a bad fit.'
            // no driver result at all ⇒ the driver script itself never ran to completion:
            // the usual cause is the interpreter, so say which one was used (2026-09-23)
            + (driver ? '' : `\n${diagnosePython(PY)}`),
          out_files: outFiles,
          refined_pcr: (driver && driver.refined_pcr) || workPcr,
          ladder: ladderText,
          cycles,
          log_tail: logTail.slice(-1000),
        }
      }
      // THE DRIVER'S VERDICT IS AUTHORITATIVE (0.8.7). A numeric Rwp is NOT proof of
      // success: a run can report `ok:false, Rwp:9.9` (a late write-back or delivery step
      // failed) and this wrapper used to overwrite that with `ok: true`, so a failed
      // refinement was reported as a usable one — the fourth review's finding. The
      // Rwp is still reported (it is a real measurement) but the VERDICT comes from the
      // driver, together with whatever it said went wrong.
      if (driver && driver.ok === false) {
        const bad = {
          ok: false,
          error: `xrd_refine_auto: the driver reported FAILURE`
            + `${driver.error ? ` — ${driver.error}` : ' (no error text)'}`
            + `. The Rwp below (${rwp}) was measured, but a step of the run failed, so it must `
            + `not be treated as a finished refinement.`,
          rwp,
          out_files: outFiles,
          refined_pcr: (driver && driver.refined_pcr) || workPcr,
          ladder: ladderText,
          cycles,
          stopped: driver.stopped === true ? true : undefined,
          aborted: driver.aborted === true ? true : undefined,
          log_tail: String(driver.log_tail || logTail).slice(-1000),
        }
        if (rwpMean !== null) bad.rwp_mean = rwpMean
        const w0 = Array.isArray(driver.warning) ? driver.warning.map((w) => String(w)) : []
        if (w0.length) bad.warning = [...pathWarnings, ...w0].slice(0, 40)
        else if (pathWarnings.length) bad.warning = pathWarnings.slice(0, 40)
        return bad
      }
      const out = {
        ok: true,
        rwp,
        out_files: outFiles,
        refined_pcr: (driver && driver.refined_pcr) || workPcr,
        ladder: ladderText,
        cycles,
        log_tail: logTail.slice(-1000),
      }
      if (driver && driver.stopped === true) out.stopped = true
      if (driver && driver.aborted === true) out.aborted = true
      // omitted rather than sent as undefined: the host rejects undefined values
      if (rwpMean !== null) out.rwp_mean = rwpMean
      // The driver's OWN warnings are part of the result, not background noise (0.8.6):
      // the third review counted 34 warnings (failed repeat runs, unstable spreads,
      // skipped background levels, a baseline that was a mean over fewer runs than the
      // candidates) that never reached the caller, so the model could not see that a
      // result was shaky. `repeat_runs` states how each accepted step was measured.
      const dwarn = Array.isArray(driver?.warning) ? driver.warning : []
      const allWarn = [...pathWarnings, ...dwarn.map((w) => String(w))]
      if (allWarn.length) out.warning = allWarn.slice(0, 40)
      const runsInfo = {
        repeats: driver?.baseline?.repeats ?? null,
        baseline_n_ok: driver?.baseline?.n_ok_runs ?? null,
        baseline_spread: driver?.baseline?.spread ?? null,
        unstable_steps: dwarn.filter((w) => /spread|noisy/.test(String(w))).length,
        repeats_failed: dwarn.filter((w) => /repeat run\(s\) succeeded/.test(String(w))).length,
        background_levels_skipped: driver?.background_levels_skipped ?? 0,
      }
      if (Object.values(runsInfo).some((v) => v !== null && v !== 0)) out.repeat_runs = runsInfo
      return out
    },
  }
}
