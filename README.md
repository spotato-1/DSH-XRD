# DSH-XRD

[![Version](https://img.shields.io/badge/version-0.9.1-2563eb)](./CHANGELOG.md)
[![License](https://img.shields.io/badge/license-GPL--3.0-16a34a)](./LICENSE)
[![DSH plugin](https://img.shields.io/badge/DeepSeek%20Harness-plugin-111827)](./package.json)

**English** | [简体中文](./README.zh-CN.md)

DSH-XRD is an AI-native powder X-ray diffraction and Rietveld refinement plugin for **DeepSeek Harness (DSH)**. It combines 12 native tools, 6 workflow skills, FullProf, a bounded Python engine, and GSAS-II into a complete path from raw diffraction data to refined structures and packaged scientific outputs.

The plugin is designed for low-intervention, highly automated XRD work. An AI agent can inspect the input, identify phases, retrieve or reconstruct crystal structures, select a refinement route, run staged optimization, revisit competing hypotheses, continue unfinished human projects, and generate final CIFs, figures, reports, and project files.

- **Current release:** 0.9.1
- **Tool contract:** [`INTERFACE.md`](./INTERFACE.md)
- **Release history:** [`CHANGELOG.md`](./CHANGELOG.md)
- **License:** GNU GPL v3
- **Included runtime:** FullProf `fp2k` for Windows and Linux, with provenance and hashes


## Core Advantages

### Full XRD workflow coverage

DSH-XRD connects the complete working chain in one plugin: input-format inspection, vendor-header evidence, wavelength handling, data conversion, peak-width analysis, CIF retrieval and reconstruction, phase screening, staged Rietveld refinement, physical review, cross-validation, and publication-oriented delivery.

### Automated refinement

The bundled workflow skills let the AI organize and execute the refinement route automatically. The agent can select starting structures, compare candidates, release parameter groups in stages, keep improved states, roll back degraded steps, and proceed to final packaging with a traceable history.

### Three-engine collaboration

- **FullProf `fp2k`** provides rapid structure scoring, baseline fitting, and the first automatic refinement ladder.
- **Bounded Python/lmfit engine** provides an AI-editable numerical workspace for broad peaks, selected coordinates, soft bond targets, bounded parameters, and two-phase warm starts.
- **GSAS-II** provides final constrained refinement, phase-aware controls, crystallographic constraints, and standardized project delivery.

Explicit handoff records transfer selected cells, phase ratios, and profile terms between stages.

### Structure discovery and reconstruction

DSH-XRD can retrieve structures from Materials Project and COD, compare distinct polymorphs, edit site composition and occupancy, build new CIFs from space-group and Wyckoff information, generate full symmetry orbits, and validate a reconstructed model before refinement.

### Defect and complex-sample support

The workflow includes tools and methods for mixed sites, vacancy hypotheses, split sites, occupancy conservation, ADP modes, secondary phases, broad components, highly defective materials, and projects with three or more phases.

### Long-running autonomous work

Per-sample state files, append-only journals, progress heartbeats, run identifiers, checkpoints, result-freshness checks, rollback, and periodic review support long refinement tasks across multiple rounds and sessions.

### Human project continuation

The plugin can continue existing FullProf PCR files and GSAS-II GPX projects. A researcher can provide a partially refined project, intervene during the process, and return control to the AI with `continue`, `resume`, `继续`, `交回`, `接着`, or `恢复`.

### Complete deliverables

A converged project can be packaged with clean GPX files, refinement logs, phase CIFs, fit figures, residual panels, reflection ticks, hkl labels, Markdown reports, metrics, and manifests.

### Self-auditing release

The repository ships integrity checks, regression suites, real tool-entry tests, negative controls, fixtures, schema checks, engine syntax checks, and cross-tool chain verification.

## Workflow

The six bundled skills guide the AI through five connected stages:

```text
1. Material identification
        |
2. Data screening and wavelength evidence
        |
3. Phase identification and competing structures
        |
4. Refinement
   4.1 Build and validate CIF candidates
   4.2 Reach a numerical basin with fp2k or bounded LM
   4.3 Complete GSAS-II optimization and physical review
        |
5. Export, cross-check, and report
```

Stages can be revisited as new evidence appears. Projects with three or more phases use a dedicated multiphase orchestration route built on a stabilized two-phase handoff.

## 12 Native Tools

| Tool | Purpose |
|---|---|
| `xrd_data_io` | Read, inspect, convert, cut, align, measure FWHM, derive Caglioti terms, and estimate apparent Scherrer size |
| `xrd_cif_edit` | Read, edit, build, validate, symmetrize, fetch, and enumerate structural candidates |
| `cif_to_pcr` | Convert CIF with the official FullProf converter and audit normalized occupancies |
| `xrd_phase_screen` | Rank candidate phases against raw or residual peaks |
| `xrd_refine_auto` | Run the headless FullProf `fp2k` automatic refinement ladder |
| `xrd_refine_manual` | Run the bounded, AI-editable Python/lmfit engine |
| `xrd_refine_gsas` | Perform final one- or two-phase GSAS-II refinement with handoff and rollback |
| `xrd_refine_multiphase` | Orchestrate GSAS-II projects with three or more phases |
| `xrd_gsas_ops` | Control GSAS-II parameters, constraints, occupancies, ADPs, cells, scales, and phases |
| `xrd_export_cif` | Export and inspect structures and create fit figures from supported project or result sources |
| `xrd_finalize` | Package a converged GSAS-II project into a complete delivery set |
| `xrd_verify_fp2k` | Cross-check a PCR with FullProf and report R factors, density, and cell information |

Exact parameters, defaults, and return schemas are documented in [`INTERFACE.md`](./INTERFACE.md).

## Repository Layout

| Path | Contents |
|---|---|
| `lib/` | DSH registration, tool schemas, process orchestration, dependency resolution, and review logic |
| `engine/` | Headless Python engines and shared process guards |
| `skills/` | Six workflow and tool-usage skills |
| `audit/` | Integrity checks, regression suites, controls, and fixtures |
| `third_party/fullprof/` | Bundled FullProf binaries, GPL-3 text, hashes, and provenance |
| `INTERFACE.md` | Tool interface contract |
| `CHANGELOG.md` | Release history and verification notes |
| `cordis.patch.yml` | DSH bundle registration patch |

## Requirements

### Core environment

- DeepSeek Harness with the plugin APIs used by DSH desktop 0.1.0-rc.5
- Node.js supplied by the DSH host
- 64-bit CPython 3.13 recommended
- NumPy 2.x for the tested GSAS-II binary family

Python packages:

```bash
python -m pip install numpy scipy PyCifRW pymatgen gemmi lmfit
```

### Crystallographic programs

| Program | Main role | Distribution |
|---|---|---|
| FullProf `fp2k` | Automatic refinement and independent verification | Bundled for Windows and Linux |
| FullProfApp `CIFs_to_PCR.exe` | Official CIF-to-PCR conversion | Installed separately |
| GSAS-II | Final refinement, constraints, export, and packaging | Installed separately |
| Materials Project API | Structure retrieval | Optional credential |
| COD | Structure retrieval | Direct access without a key |

## Manual Installation

1. Put this repository in the active DSH profile, conventionally at `~/.dsh/profiles/web/node_modules/dsh-xrd`.
2. Add `"dsh-xrd": "file:/absolute/path/to/dsh-xrd"` to the profile dependencies.
3. Add `"dsh-xrd"` to `dsh.profile.bundles`.
4. Create `xrd.local.json` from [`xrd.local.example.json`](./xrd.local.example.json) and enter the available program paths.
5. On Linux, run `chmod +x third_party/fullprof/fp2k`.
6. Run `node audit/integrity-check.mjs`.
7. Restart DSH and confirm the tool and skill catalog.

## Configuration

`xrd.local.json` records the programs used by the plugin:

```json
{
  "python": "C:/Python313/python.exe",
  "gsas_root": "D:/GSAS-II",
  "fp2k_root": "D:/FullProf",
  "cif2pcr_exe": "C:/Program Files (x86)/FullProfAPP/resources/fpsuite/windows/CIFs_to_PCR.exe"
}
```

Explicit tool arguments have the highest priority, followed by environment variables, `xrd.local.json`, and conventional path discovery.

## Start an XRD Project

Create one workspace subdirectory per sample. Add the original diffraction data and any available composition, synthesis route, complementary characterization, literature, candidate CIF, PCR, or GPX project.

General request:

```text
The XRD data for <material> are in <path>. Use the complete DSH-XRD workflow to inspect the input, identify the phase model, perform automatic refinement, complete the final GSAS-II stage, and package all results.
```

Complex or defect-rich request:

```text
The XRD data for <material> are in <path>. The sample may contain <defects, disorder, hydration, or secondary phases>. Use the DSH-XRD complex-sample workflow, compare competing structural models, run the staged multi-engine refinement, preserve the refinement history, and deliver the final reviewed result set.
```

The plugin can use a user-supplied target R value as an optimization goal and combine it with its structure, parameter, residual, and delivery checks.

## Wavelength Handling

The data header can be read with `xrd_data_io` `header` or `evidence`. A known measurement wavelength can be supplied as:

- `wavelength`: a monochromatic wavelength in angstroms;
- `wavelength_doublet`: `[Kalpha1, Kalpha2, ratio]` for a laboratory source.

The value is passed explicitly into the selected PCR, FullProf, manual-engine, or GSAS-II write path.

## Verification

```bash
node audit/integrity-check.mjs
node audit/verify-all.mjs
```

The integrity gate verifies tool assembly, engine availability and syntax, action vocabulary, closed output schemas, host-schema compatibility, defect-catalog consistency, and cross-tool chains. Additional suites exercise conversion, refinement, supervision, review, wavelength handling, packaging, and negative controls.

## License

DSH-XRD 0.9.1 is distributed under the [GNU General Public License v3](./LICENSE). The license covers the repository as a whole.

The bundled FullProf binaries remain unchanged third-party files. Their origin, versions, file sizes, SHA-256 hashes, source pointer, and redistribution record are documented in [`third_party/fullprof/PROVENANCE.md`](./third_party/fullprof/PROVENANCE.md).

## If You Are an AI: Follow This Installation Route

Use the following route when the user asks you to install DSH-XRD. Complete the work end to end, report each discovered dependency clearly, and request user approval when the host requires elevated access or an external program must be installed.
Note: It is recommended to download the complete structured package directly from /releases, which includes a Windows build with all dependencies.

### 1. Inspect the package

1. Read `package.json`, this README, and `INTERFACE.md`.
2. Confirm that `package.json` reports version `0.9.1`.
3. Confirm that these directories exist: `lib/`, `engine/`, `skills/`, `audit/`, and `third_party/fullprof/`.
4. Locate the active DSH home and active profile. The conventional profile is `~/.dsh/profiles/web`, but use the path reported by the running DSH environment when available.

### 2. Inspect the host environment

Collect and report:

- operating system and architecture;
- active DSH version and profile path;
- Node.js availability through the DSH host;
- available 64-bit Python interpreter and version;
- discovered GSAS-II root;
- discovered FullProfApp `CIFs_to_PCR.exe` path;
- whether a Materials Project API key is configured.

Use existing installations first. Keep machine paths in the local configuration rather than inserting them into source files.

### 3. Prepare Python dependencies

Use one Python interpreter for the complete engine family. Check for:

```text
numpy
scipy
PyCifRW (import name: CifFile)
pymatgen
gemmi
lmfit
```

After receiving any required execution approval, install missing packages with:

```bash
python -m pip install numpy scipy PyCifRW pymatgen gemmi lmfit
```

Record the interpreter path selected for DSH-XRD. Python 3.13 64-bit with NumPy 2.x is the tested Windows configuration for this release.

### 4. Resolve crystallographic programs

- **FullProf refinement:** use the bundled runtime in `third_party/fullprof/`. No separate `fp2k` installation is needed for `xrd_refine_auto` or `xrd_verify_fp2k`.
- **CIF-to-PCR conversion:** locate FullProfApp's `CIFs_to_PCR.exe` for `cif_to_pcr`.
- **Final GSAS-II refinement:** locate a working GSAS-II installation whose root contains `GSASII/GSASIIpath.py`.
- **Crystal database access:** configure `MP_API_KEY` when Materials Project retrieval is wanted. COD access is available without a key.

When an external program is absent, tell the user exactly which component is missing and which capabilities use it, then continue after the user provides or installs it.

### 5. Install the plugin into DSH

Place or link this repository at the active profile's plugin location, conventionally:

```text
~/.dsh/profiles/web/node_modules/dsh-xrd
```

Merge the following entries into the profile's existing `package.json`:

```json
{
  "dependencies": {
    "dsh-xrd": "file:/absolute/path/to/dsh-xrd"
  },
  "dsh": {
    "profile": {
      "bundles": ["dsh-xrd"]
    }
  }
}
```

Preserve the profile's other dependencies, bundles, providers, and settings.

### 6. Create local configuration

Copy `xrd.local.example.json` to `xrd.local.json` and fill the paths discovered on the machine:

```json
{
  "python": "C:/Python313/python.exe",
  "gsas_root": "D:/GSAS-II",
  "fp2k_root": "D:/FullProf",
  "cif2pcr_exe": "C:/Program Files (x86)/FullProfAPP/resources/fpsuite/windows/CIFs_to_PCR.exe"
}
```

The bundled FullProf runtime is found automatically, so `fp2k_root` can be omitted when the bundled engine is used.

Equivalent environment variables are:

- `DSH_XRD_PY`
- `DSH_GSAS_ROOT`
- `DSH_FP2K_ROOT`
- `DSH_FULLPROF_CIF2PCR`
- `DSH_XRD_CONFIG`
- `MP_API_KEY`

### 7. Apply the Linux executable permission

On Linux, ensure that the bundled engine is executable:

```bash
chmod +x third_party/fullprof/fp2k
```

### 8. Run installation checks

Run the static integrity gate:

```bash
node audit/integrity-check.mjs
```

Confirm that it assembles 12 tools and reports `FAIL 0`. Then run additional audit suites appropriate to the installed external programs:

```bash
node audit/verify-all.mjs
```

The audit reports unavailable external inputs as `SKIP`, making the installed coverage visible in the final report.

### 9. Restart and verify DSH

Restart DSH so the JavaScript plugin is loaded. In the new session:

1. confirm that all 12 `xrd_*`/XRD tools are registered;
2. confirm that the six bundled skills are visible;
3. call a lightweight data or CIF inspection tool;
4. report the final Python, GSAS-II, FullProfApp, FullProf runtime, and Materials Project configuration.

### 10. Deliver the installation report

Report:

- installation directory;
- DSH profile modified;
- plugin version;
- Python interpreter and installed packages;
- resolved external-program paths;
- integrity/audit result;
- registered tool and skill counts;
- components awaiting user-provided software or credentials.

A user can start the autonomous route with this prompt:

```text
Install DSH-XRD 0.9.1 from this repository into my active DeepSeek Harness profile. If you are an AI, follow the “If You Are an AI” route in README.md from environment inspection through restart verification. Ask for approval when elevated execution or external installation is required, and finish with a complete installation report.
```

