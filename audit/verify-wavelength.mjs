// verify-wavelength.mjs — the λ (wavelength) contract of the FOUR write entry points.
//
// WHY (2026-10-03, 0.9.0): the wavelength feature added two parameters to four tools and
// one new writer (`cif2pcr_helper.set_wavelength`). Its ONE red line is *default-off*: an
// omitted parameter must not touch the input at all. That is a property of the guard, not of
// a lucky run — so the guard is asserted on the plugin SOURCE, while the writer is exercised
// on a real pcr. What is checked here:
//
//   1) all four tools declare `wavelength` + `wavelength_doublet`, and BOTH AT ONCE is refused;
//   2) every writer is reached only through a presence test (`!= null`) — an omitted parameter
//      cannot reach it (asserted per file/section, so a rename that drops the guard fails);
//   3) `set_wavelength` on a REAL shipped pcr: value line rewritten, every OTHER line byte-identical,
//      `.prewavelength` backup written, and a second identical call reports changed=false;
//   4) the target table agrees with GSAS-II's own values, and a synchrotron/unknown target has none;
//   5) `xrd_data_io header` stays POSITIONAL on hostile headers (duplicate keys, a value containing
//      '=', a line without '=', multi-values) — it must echo every line verbatim and find the data run.
//   6) [2026-10-05 defect] the GSAS-II writer must NOT reach for a keyword the loader does not have,
//      and a NEW GSAS-II project must really build with the requested λ in its slots: the 0.9.0
//      writer passed `wavelength=` to add_powder_histogram, which has no such argument and no
//      **kwargs (GSASIIscriptable.py:1044), so EVERY new-project GSAS refinement raised TypeError
//      and surfaced as "could not import powder data". 2) is a source assertion and verify-gsas-ops
//      only ever CONTINUED an existing .gpx, so nothing caught it — 6) runs the engine for real.
//
// No network, no fp2k, no refinement. Needs python for 3) and 4). Section 6 additionally needs a
// discoverable GSAS-II install ($DSH_GSAS_ROOT or xrd.local.json) and SKIPS cleanly without one.
import { readFileSync, writeFileSync, copyFileSync, existsSync, mkdirSync, openSync, closeSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import {
  importRoot, pluginRoot, pluginEntry, mockContext, execIn, freshDir, WORK_ROOT, report, python,
  tryGsasRoot, FIXTURES,
} from './_lib.mjs'

// The tree under audit: an explicit --root / $DSH_XRD_ROOT, or this package when the script
// is running from inside one. Exit 2 = "refused to start" (verify-all reports it as such).
let ROOT
try {
  ROOT = pluginRoot()
} catch (e) {
  console.error(String(e.message ?? e))
  process.exit(2)
}
const WORK = freshDir(join(WORK_ROOT, 'wavelength'))
const exec = execIn(WORK)
console.log('plugin tree under test:', ROOT)

let PY = null
try { PY = python() } catch { PY = null }

// ── 1) registration + schema ────────────────────────────────────────────────
const tools = []
try {
  const mod = await import(pathToFileURL(pluginEntry(importRoot())).href)
  await (mod.apply ?? mod.default)(mockContext(tools))
} catch (e) {
  report.fail(`the plugin could not be assembled: ${String(e.message).slice(0, 300)}`)
  process.exit(report.finish('WAVELENGTH CONTRACT NOT CHECKED'))
}

const FOUR = ['xrd_refine_manual', 'cif_to_pcr', 'xrd_refine_auto', 'xrd_refine_gsas']
const byName = Object.fromEntries(FOUR.map(n => [n, tools.find(t => t.name === n)]))
report.check(FOUR.every(n => !!byName[n]),
  'all four λ write entry points are registered',
  `missing: ${FOUR.filter(n => !byName[n]).join(', ')}`)

for (const n of FOUR) {
  const t = byName[n]
  if (!t) continue
  const props = t.parameters?.properties ?? {}
  report.check(!!props.wavelength && !!props.wavelength_doublet,
    `${n}: declares wavelength + wavelength_doublet`,
    `${n}: missing ${['wavelength', 'wavelength_doublet'].filter(k => !props[k]).join(', ')}`)
  const d = String(props.wavelength?.description ?? '')
  // the refusal of a default is what makes the feature safe to ship; a description that does
  // not say so would let a caller assume a value was applied when none was passed.
  report.check(/OMIT/i.test(d),
    `${n}: the schema states the parameter is explicit-only (OMIT => untouched)`,
    `${n}: the wavelength description does not state the omit/default-off rule`)
}

// ── 1b) the RETURN schema must declare what the handler returns ─────────────
// The host validates a tool's returned value against `output.schema`, and those schemas are
// closed (`additionalProperties: false`). An undeclared key therefore fails the WHOLE call with
// `value.wavelength is not a declared property` — which is exactly what the host end-to-end run
// hit on `cif_to_pcr` (2026-10-03) while every isolated check in this file passed. Two tools
// return a λ echo, so both output schemas must declare it.
for (const n of ['cif_to_pcr', 'xrd_refine_auto']) {
  const t = byName[n]
  if (!t) continue
  const props = t.output?.schema?.properties ?? t.outputSchema?.properties ?? {}
  report.check(!!props.wavelength,
    `${n}: output schema declares \`wavelength\` (host-validated; an undeclared key fails the call)`,
    `${n}: the handler returns a \`wavelength\` echo but the output schema does not declare it`)
}

// BOTH AT ONCE must be refused, not silently resolved. The data file has to EXIST, because the
// handler checks the input before it looks at λ — so a synthetic 40-point pattern is written
// first; the refusal must still happen before anything is executed.
const SYN = join(WORK, 'conflict.dat')
writeFileSync(SYN, Array.from({ length: 40 }, (_, i) => `${(10 + i * 0.02).toFixed(2)} ${100 + (i % 7) * 3}`).join('\n') + '\n')
try {
  await byName['xrd_refine_manual'].execute({ data: SYN, wavelength: 1.5, wavelength_doublet: [1.5, 1.6, 0.5] }, exec)
  report.fail('xrd_refine_manual accepted BOTH wavelength and wavelength_doublet')
} catch (e) {
  const m = String(e.message ?? e)
  report.check(/either|not both/i.test(m),
    'xrd_refine_manual refuses both parameters at once (before touching any file)',
    `xrd_refine_manual threw, but not the mutual-exclusion error: ${m.slice(0, 200)}`)
}

// ── 2) the default-off guard, asserted per writer ───────────────────────────
// Each entry: file + the regex that must match its "only when the caller passed something" test.
const GUARDS = [
  ['lib/xrd-tools.js', /if \(args\.wavelength != null \|\| args\.wavelength_doublet != null\)/, 'cif_to_pcr writer call'],
  ['lib/index.js', /wavelengthPlan = null[\s\S]{0,400}?if \(args\.wavelength != null && args\.wavelength_doublet != null\)[\s\S]{0,2500}?\.\.\.\(wavelengthPlan \? \{ wavelength: wavelengthPlan \} : \{\}\)/, 'xrd_refine_auto plan'],
  ['engine/fp2k_driver.py', /_wl = plan\.get\(['"]wavelength['"]\)[\s\S]{0,120}?if _wl:/, 'fp2k driver pre-run rewrite'],
  ['engine/gsas_engine.py', /wl\s*=\s*plan\.get\(["']wavelength["']\)/, 'gsas_engine slot write'],
  ['engine/xrd_engine.py', /plan\.get\(["']lambda["']/, 'manual engine λ read'],
]
for (const [rel, re, what] of GUARDS) {
  const p = join(ROOT, rel)
  if (!existsSync(p)) { report.fail(`${rel} not found under ${ROOT}`); continue }
  const src = readFileSync(p, 'utf8')
  report.check(re.test(src), `${rel}: presence test present (${what}) — omitted ⇒ writer not reached`,
    `${rel}: the presence test for ${what} is GONE — an omitted parameter could reach the writer`)
}

// ── 2b) the GSAS-II writer must use the loader's REAL interface ───────────────
// A NEGATIVE assertion, so it needs the prose that explains it stripped first: the
// comment next to the call quotes the forbidden keyword, and a rule whose explanatory
// text trips the rule is a rule that will be deleted rather than obeyed.
{
  const rel = 'engine/gsas_engine.py'
  const p = join(ROOT, rel)
  const raw = existsSync(p) ? readFileSync(p, 'utf8') : ''
  const code = raw.split('\n').filter(l => !/^\s*#/.test(l)).join('\n')   // drop comment-only lines
  const kwarg = /add_powder_histogram[\s\S]{0,600}?wavelength\s*=/.exec(code)
  report.check(raw !== '', `${rel} found`, `${rel} not found under ${ROOT}`)
  report.check(!kwarg,
    `${rel}: no \`wavelength=\` reaches add_powder_histogram (the loader has no such argument)`,
    `${rel}: a \`wavelength=\` keyword is passed to add_powder_histogram — that raises TypeError on EVERY new project (2026-10-05 regression)`)
  const writes = (code.match(/_wl_write\(/g) || []).length
  report.check(writes >= 2,
    `${rel}: the new-project AND the reopened path both write the λ slots through _wl_write`,
    `${rel}: _wl_write is called ${writes}x — the two paths have diverged again (that divergence IS how the defect survived)`)
}

// ── 3) set_wavelength on a REAL pcr (shipped template: no external dependency) ──
const TEMPLATE = join(ROOT, 'skills', 'fullprof-pcr', 'templates', 'Y2O3_xray.pcr')
const HELPER = join(ROOT, 'engine', 'cif2pcr_helper.py')

/** run the helper CLI, returning the JSON it wrote via --out (no piped stdio: the sandbox denies it) */
function setWavelength(pcr, vals) {
  const out = join(WORK, `wl-${vals.join('_')}.json`)
  const r = spawnSync(PY, ['-B', HELPER, 'setwavelength', pcr, ...vals.map(String), '--out', out],
    { cwd: WORK, stdio: 'ignore' })
  if (!existsSync(out)) return { ok: false, error: `helper wrote no --out file (exit ${r.status})` }
  return JSON.parse(readFileSync(out, 'utf8'))
}

if (!existsSync(TEMPLATE)) {
  report.skip(`shipped pcr template not found: ${TEMPLATE}`)
} else if (!PY) {
  report.skip('no python interpreter — set_wavelength not exercised (set DSH_XRD_PYTHON)')
} else {
  const before = readFileSync(TEMPLATE, 'utf8')
  const lines0 = before.split('\n')

  // 3a) monochromatic: Lam2/Ratio must be ZEROED, not left at the old doublet
  const mono = join(WORK, 'mono.pcr')
  copyFileSync(TEMPLATE, mono)
  const r1 = setWavelength(mono, [0.41419])
  report.check(r1.ok === true && r1.changed === true,
    'monochromatic set reports ok + changed',
    `mono set: ${JSON.stringify(r1).slice(0, 240)}`)
  const L1 = readFileSync(mono, 'utf8').split('\n')
  const idx = L1.findIndex((l, i) => i > 0 && /^\s*[-+0-9.]/.test(l) && /Lambda1/.test(L1[i - 1]))
  report.check(idx > 0 && /^0\.414190\s+0\.000000\s+0\.00000\b/.test(L1[idx].trimStart()),
    'monochromatic value line = Kα1 + zeroed Kα2/ratio (FullProf reads that as no Kα2)',
    `value line #${idx + 1}: ${JSON.stringify(L1[idx])}`)
  const diff = L1.map((l, i) => (l === lines0[i] ? null : i)).filter(i => i !== null)
  report.check(diff.length === 1,
    'exactly ONE line differs from the template (every other byte preserved)',
    `${diff.length} lines differ: ${diff.map(i => i + 1).join(', ')}`)
  // the trailing fields of that same line (Bkpos / Wdt / Cthm / muR / AsyLim / Rpolarz) must
  // survive: a λ writer that reflows the profile/geometry columns would silently change the model.
  const rest0 = lines0.find((l, i) => i > 0 && /Lambda1/.test(lines0[i - 1]) && /^\s*[-+0-9.]/.test(l))
    ?.trim().split(/\s+/).slice(3).join(' ')
  const rest1 = idx > 0 ? L1[idx].trim().split(/\s+/).slice(3).join(' ') : null
  report.check(!!rest0 && rest0 === rest1,
    'the trailing fields of the value line (Bkpos/Wdt/Cthm/muR/AsyLim/Rpolarz) are preserved verbatim',
    `trailing fields changed: ${JSON.stringify(rest0)} -> ${JSON.stringify(rest1)}`)
  report.check(existsSync(mono + '.prewavelength'),
    'the pre-change copy is kept next to the pcr (.prewavelength)',
    'no .prewavelength backup was written')
  report.check(!!r1.before && !!r1.after && r1.before !== r1.after,
    'the change is echoed 改前 -> 改后',
    `echo missing: before=${JSON.stringify(r1.before)} after=${JSON.stringify(r1.after)}`)

  // 3b) idempotence: the same request must NOT report a change
  const r2 = setWavelength(mono, [0.41419])
  report.check(r2.ok === true && r2.changed === false,
    'setting the same value again reports changed=false (idempotent, no fake edit)',
    `second identical call: ${JSON.stringify(r2).slice(0, 240)}`)
  report.check(readFileSync(mono, 'utf8') === L1.join('\n'),
    'the idempotent call left the file byte-identical',
    'the second call altered the pcr')

  // 3c) laboratory doublet
  const dbl = join(WORK, 'doublet.pcr')
  copyFileSync(TEMPLATE, dbl)
  const r3 = setWavelength(dbl, [1.5406, 1.54439, 0.5])
  const L3 = readFileSync(dbl, 'utf8').split('\n')
  const i3 = L3.findIndex((l, i) => i > 0 && /^\s*[-+0-9.]/.test(l) && /Lambda1/.test(L3[i - 1]))
  report.check(r3.ok === true && i3 > 0 && /^1\.540600\s+1\.544390\s+0\.50000\b/.test(L3[i3].trimStart()),
    'laboratory doublet writes Kα1 / Kα2 / ratio together',
    `doublet value line #${i3 + 1}: ${JSON.stringify(L3[i3])}`)

  // 3d) the writer refuses to invent: no numeric value line => warn, not a silent "ok"
  const bad = join(WORK, 'no-banner.pcr')
  writeFileSync(bad, 'COMMENT a pcr with no Lambda1 banner at all\n   1.0 2.0 3.0\n')
  const r4 = setWavelength(bad, [1.5406])
  report.check(r4.changed === false && r4.ok === false && (r4.warnings || []).length > 0,
    'a pcr without the Lambda1 banner warns and reports ok=false instead of claiming it wrote',
    `no-banner case: ${JSON.stringify(r4).slice(0, 240)}`)
}

// ── 4) the target table ────────────────────────────────────────────────────
if (!PY) {
  report.skip('no python interpreter — target table not checked')
} else {
  const out = join(WORK, 'table.json')
  const code = [
    'import json,sys',
    `sys.path.insert(0, ${JSON.stringify(join(ROOT, 'engine'))})`,
    'import wavelength as w',
    `open(${JSON.stringify(out)}, 'w', encoding='utf-8').write(json.dumps({`,
    "  'cu': w.defaults_for('Cu'), 'mo': w.defaults_for('Mo'), 'co': w.defaults_for('Co'),",
    "  'syn': w.defaults_for('synchrotron'), 'sloppy': w.defaults_for(' cu '),",
    "  'classic': list(w.CLASSIC_CU), 'n': len(w.TARGET_TABLE)}, ensure_ascii=False))",
  ].join('\n')
  spawnSync(PY, ['-B', '-c', code], { cwd: WORK, stdio: 'ignore' })
  if (!existsSync(out)) {
    report.fail('the target table could not be read (python -c produced no output file)')
  } else {
    const t = JSON.parse(readFileSync(out, 'utf8'))
    // GSAS-II's GSASIIElem.waves (Intl. Tables C, Table 4.2.2.1) — the numbers the GSAS engine uses.
    const EXPECT = { cu: [1.54051, 1.54433], mo: [0.70926, 0.713543], co: [1.78892, 1.79278] }
    for (const [k, [k1, k2]] of Object.entries(EXPECT)) {
      report.check(t[k]?.kalpha1 === k1 && t[k]?.kalpha2 === k2,
        `table ${k.toUpperCase()}: Kα1=${k1} Kα2=${k2} (agrees with GSAS-II)`,
        `table ${k.toUpperCase()}: got ${JSON.stringify([t[k]?.kalpha1, t[k]?.kalpha2])}, expected ${k1}/${k2}`)
    }
    report.check(t.syn === null,
      'a synchrotron source has NO table entry (the value must come from the user)',
      `synchrotron returned ${JSON.stringify(t.syn)} instead of null`)
    report.check(t.sloppy?.target === 'Cu',
      'a sloppy target string (" cu ") still resolves to Cu',
      `' cu ' resolved to ${JSON.stringify(t.sloppy)}`)
    report.check(t.classic?.[0] === 1.54056 && t.classic?.[1] === 1.54439,
      'the classic Cu tabulation is kept SEPARATE from the table (5e-5 apart — the convention must be reported)',
      `classic Cu = ${JSON.stringify(t.classic)}`)
  }
}

// ── 5) `header` stays positional on hostile headers ─────────────────────────
// The tool's whole reason to exist is "read the vendor header as-is"; a smart parser that
// dedups keys, splits on the first '=' or needs one would quietly lose exactly the line the
// wavelength is on. These are the shapes real Rigaku/GSAS/bruker headers actually contain.
const dataIo = tools.find(t => t.name === 'xrd_data_io')
if (!dataIo) {
  report.fail('xrd_data_io is not registered — the header contract cannot be checked')
} else {
  const HOSTILE = [
    ';RAW4.00',
    '[RawHeader]',
    'Anode=Cu',
    'Alpha1=1.5406',
    'Alpha1=1.540600',              // duplicate key: BOTH must survive
    'Signature=YWJjZA==',           // a value containing '=' (base64)
    'NoEqualsHere',                 // a line with no '=' at all
    'Range=10,20,30',               // multi-value
    'ActuallyUsedLambda=1.5418',
    'Monochromator=0',
    '[Data]',
    'Angle, PSD,',
    '10.0, 169,',
    '10.02, 171,',
    '10.04, 168,',
    '10.06, 170,',
  ]
  const f = join(WORK, 'hostile_header.txt')
  writeFileSync(f, HOSTILE.join('\n') + '\n')
  let r = null
  try {
    r = await dataIo.execute({ actions: [{ type: 'header', file: f }] }, exec)
  } catch (e) {
    const m = String(e.message ?? e)
    // A restricted sandbox denies piped stdio, so the TOOL cannot spawn its engine here
    // ("spawn EPERM"). That is an environment boundary, not a plugin defect — but the header
    // logic still has to be checked, so the same engine is driven directly through a plan file
    // with no pipe at all. The fallback is announced: it does not silently count as a tool check.
    if (/EPERM/i.test(m) && PY) {
      report.warn(`the tool could not spawn its engine here (${m.slice(0, 60)}) — falling back to the engine directly; the TOOL layer stays unexercised in this sandbox`)
      const planPath = join(WORK, 'di_plan.json')
      const resPath = join(WORK, 'di_result.json')
      writeFileSync(planPath, JSON.stringify({
        run_id: 'wavelength-check', out_dir: WORK, sample: null,
        actions: [{ type: 'header', file: f }],
      }))
      const rr = spawnSync(PY, ['-B', join(ROOT, 'engine', 'data_io.py'), planPath, '--out', resPath],
        { cwd: WORK, stdio: 'ignore' })
      r = existsSync(resPath) ? JSON.parse(readFileSync(resPath, 'utf8'))
        : { ok: false, error: `the data_io engine wrote no result (exit ${rr.status})` }
    } else {
      report.fail(`xrd_data_io header threw on a hostile header: ${m.slice(0, 240)}`)
    }
  }
  if (r) {
    // the engine returns one entry per action: actions[0].header carries the meta,
    // actions[0].lines carries the line-numbered verbatim text (see the tool's render()).
    const a = (r.actions ?? [])[0] ?? {}
    const h = a.header ?? {}
    const text = (a.lines ?? []).join('\n')
    const flat = JSON.stringify(r)
    report.check(r.ok === true && h.data_found === true,
      'xrd_data_io header ran and found the data run',
      `ok=${r.ok} data_found=${h.data_found} error=${JSON.stringify(r.error ?? a.error)}`)
    const has = (s) => text.includes(s) || flat.includes(s)
    report.check(has('Signature=YWJjZA=='),
      'a value containing "=" (base64) survives verbatim',
      'the base64-valued line was dropped or split')
    report.check(has('NoEqualsHere'),
      'a line with no "=" survives (no key/value requirement)',
      'the "=" -less line was dropped')
    report.check((text.match(/Alpha1=/g) || []).length >= 2 || (flat.match(/Alpha1=/g) || []).length >= 2,
      'duplicate keys are both kept (no dedup — the reader decides which one to trust)',
      'a duplicate key was deduplicated away')
    report.check(has('Range=10,20,30'),
      'a multi-value line survives whole',
      'the multi-value line was truncated or split')
    const start = Number(h.data_start_line)
    report.check(Number.isFinite(start) && start === 13,
      'the data run is found by POSITION (data starts at line 13, the "Angle, PSD," row excluded)',
      `data_start_line = ${JSON.stringify(h.data_start_line)} (expected 13)`)
  }
}

// ── 6) REAL GSAS-II: a NEW project builds and the requested λ lands in its slots ──
// This is the regression control for the 2026-10-05 defect (see the header). It runs
// whenever a GSAS-II install is discoverable, because an opt-in environment gate is
// exactly what let the defect ship.
console.log('== 6. real GSAS-II: new project + explicit λ ==')
{
  const gsas = tryGsasRoot()
  const dat = join(FIXTURES, 'Y2O3.dat')
  const cif = join(FIXTURES, 'Y2O3.cif')
  const instprm = gsas ? join(gsas, 'tests', 'testinp', 'INST_XRY.PRM') : null
  if (!PY) {
    report.skip('no python interpreter — the real new-project λ control cannot run (set DSH_XRD_PYTHON)')
  } else if (!gsas || !existsSync(join(gsas, 'GSASII', 'GSASIIscriptable.py'))) {
    report.skip('no GSAS-II install discoverable ($DSH_GSAS_ROOT or xrd.local.json) — new-project λ control skipped')
  } else if (!existsSync(dat) || !existsSync(cif) || !instprm || !existsSync(instprm)) {
    report.skip(`fixtures or instrument parameters missing (${dat} / ${cif} / ${instprm})`)
  } else {
    const outDir = join(WORK, 'gsas-new')
    mkdirSync(outDir, { recursive: true })
    const plan = join(WORK, 'gsas-new-plan.json')
    const resPath = join(outDir, 'gsas_result.json')
    const logPath = join(WORK, 'gsas-new.stdout.txt')
    const GPX = join(outDir, 'gsas_proj.gpx')
    const K1 = 1.5406
    const K2 = 1.54439
    writeFileSync(plan, JSON.stringify({
      out_dir: outDir, run_id: 'wl-new-project', sample: 'wlNew', gsas_root: gsas,
      data: { file: dat, fmthint: null }, instprm, cif, refine: [], max_cycles: 1,
      wavelength: { kalpha1: K1, kalpha2: K2, ratio: 0.5 },
    }, null, 1))
    // no pipes anywhere: the sandbox denies piped stdio, so stdout goes straight to a file
    let spawned = true
    try {
      const fd = openSync(logPath, 'w')
      spawnSync(PY, ['-B', join(ROOT, 'engine', 'gsas_engine.py'), plan, '--out', resPath],
        { cwd: WORK, stdio: ['ignore', fd, fd], windowsHide: true })
      closeSync(fd)
    } catch (err) {
      spawned = false
      report.skip(`the GSAS-II control could not spawn a child here (environment, not a defect): ${err?.message ?? err}`)
    }
    const log = existsSync(logPath) ? readFileSync(logPath, 'utf8') : ''
    let res = null
    try { res = JSON.parse(readFileSync(resPath, 'utf8')) } catch { /* none */ }

    if (spawned) {
      report.check(!!res && res.ok === true && (res.histograms || []).length === 1,
        'a NEW GSAS-II project builds (no TypeError at histogram creation)',
        `result=${res ? `ok=${res.ok} hist=${(res.histograms || []).length} error=${JSON.stringify(res.error ?? null)}` : 'none written'}`)
      report.check(/(^|\n)wavelength: .*Lam1=.*->.*Lam1=/.test(log) || /Lam=.*->.*Lam=/.test(log),
        'the engine reports the λ change 改前 -> 改后 on the freshly created histogram',
        `no wavelength line in the engine log: ${JSON.stringify(log.slice(-300))}`)

      // ...and it must be in the SAVED FILE, not only in memory (the write would otherwise
      // be a silent no-op: gpx.save() is what the refinement later reads back).
      const probe = join(WORK, 'probe_slots.py')
      const slots = join(WORK, 'slots.json')
      writeFileSync(probe, [
        'import json, sys',
        'root, gpx, out = sys.argv[1], sys.argv[2], sys.argv[3]',
        'sys.path.insert(0, root)',
        'from GSASII import GSASIIscriptable as G2sc',
        'h = G2sc.G2Project(gpxfile=gpx).histograms()[0]',
        'ip = h.data["Instrument Parameters"][0]',
        'json.dump({"Lam1": ip.get("Lam1", [None, None])[1],',
        '           "Lam2": ip.get("Lam2", [None, None])[1]}, open(out, "w"))',
      ].join('\n'))
      if (existsSync(GPX)) {
        try {
          spawnSync(PY, ['-B', probe, gsas, GPX, slots], { cwd: WORK, stdio: 'ignore', windowsHide: true })
        } catch { /* reported below */ }
      }
      let got = null
      try { got = JSON.parse(readFileSync(slots, 'utf8')) } catch { /* none */ }
      report.check(!!got && Math.abs(Number(got.Lam1) - K1) < 1e-9 && Math.abs(Number(got.Lam2) - K2) < 1e-9,
        `the saved .gpx carries Lam1=${K1} / Lam2=${K2} (read back from disk, index 1 = the value GSAS refines)`,
        `slot readback: ${JSON.stringify(got)} (expected Lam1=${K1} Lam2=${K2})`)
    }
  }
}

process.exit(report.finish('λ CONTRACT CHECKED — four entries default-off, writer reverts nothing, header stays positional'))
