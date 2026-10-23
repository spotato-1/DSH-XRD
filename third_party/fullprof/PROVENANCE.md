# Bundled FullProf runtime — provenance and attribution

This directory ships two FullProf binaries and the licence text that came with
them. They are **unchanged third-party files**; nothing here was rebuilt,
patched or renamed.

## What each file is

| File | What it is | Origin | Bytes | SHA256 |
|---|---|---|---|---|
| `fp2k.exe` | FullProf **2017** refinement engine, Windows x64 | taken verbatim from the **AutoFP** distribution (`autofp/fp2k.exe`), where it is the built-in FullProf | 3195904 | `DABFC8FBF35A4D728E2E94073FD546A3957D1F8EA6ADF63D7E931C0CBD06417F` |
| `fp2k` | FullProf **2021** refinement engine, Linux (Ubuntu 20.04+) | taken verbatim from the same **AutoFP** distribution (`autofp/fp2k`) | 4352240 | `78CBA56F9D41EFE048B143A32EF04F53150EAB60406F7B12E0A704834DB2C987` |
| `LICENSE` | GPL-3 licence text as shipped by AutoFP | copied unchanged from `autofp/LICENSE` | 35821 | `0B383D5A63DA644F628D99C33976EA6487ED89AAA59F0B3257992DEAC1171E6B` |

Verify any of them on your own machine:

```powershell
Get-FileHash .\third_party\fullprof\fp2k.exe -Algorithm SHA256
```

(or `sha256sum third_party/fullprof/fp2k` on Linux).

## How they got here

The FullProf binaries were obtained as part of **AutoFP** (an automatic
Rietveld-refinement front end for FullProf), whose own `Readme.txt` states:

> AutoFP is an auto tools for FullProf Rietveld refinement released under GPL v3 LICENSE.

and whose `README.md` documents the two binaries as its built-in engines:

> If you set it to "fp2k", the program will use the built-in fullprof 2017 version
> of fp2k. Windows: fp2k.exe (Fullprof 2017), Linux: fp2k (Fullprof 2021 -> Ubuntu 20.04+).

`dsh-xrd` re-packages those files **unchanged**, keeps the licence text beside
them, and records the hashes above. We did not modify FullProf; the AutoFP
*python* sources are deliberately **not** redistributed (see below).

## Licensing — read before redistributing

- The complete `dsh-xrd` repository is distributed under **GNU GPL v3**; see
  the root [`LICENSE`](../../LICENSE).
- The files **in this directory** are unchanged third-party FullProf binaries
  distributed with the same GPL-3 licence text in this directory. AutoFP's
  licence file states GPL-3, which is the version relied on here.
- GPL-3 (§4/§6) requires that recipients of the binaries receive the licence text
  and can obtain the corresponding source. The source is available from
  FullProf's author — **<https://www.ill.eu/sites/fullprof/>**
  (J. Rodríguez-Carvajal, ILL). AutoFP, which distributes these builds, is
  likewise distributed with source.
- Keep this directory, its licence, provenance record, and hashes together when
  redistributing a build that includes the bundled FullProf runtime.

## Version note

`fp2k.exe` and `fp2k` are **different FullProf generations** (2017 vs 2021), so
they are not expected to produce byte-identical results on the same `.pcr`. The
plugin drives whichever one runs on the host; a refinement is always reported
with the Rwp that the binary itself wrote into its `.out`.

## What is NOT bundled

- **AutoFP's python sources** (`shautofp.py`, `auto.py`, `paramgroup*.py`,
  the `diffpy/pyfullprof` pcr reader, …) — this plugin does not use them at all.
  Its own launcher is `engine/fp2k_driver.py`.
- **The rest of the FullProf suite** (WinPLOTR and friends) — only the
  refinement engine is used.
- **The MSVC 2008 runtimes** (`msvcr90.dll`, `msvcp90.dll`) that sit beside
  `fp2k.exe` in AutoFP's tree: measured unnecessary — `fp2k.exe` runs standalone
  (produced its R-factor line with both DLLs absent from the system).
- **GSAS-II** — a separate dependency, located at runtime through the plugin's
  own resolution chain, never redistributed here.
