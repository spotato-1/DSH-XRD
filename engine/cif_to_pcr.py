#!/usr/bin/env python3
"""Standalone CIF -> FullProf PCR writer (Python >=3.9; gemmi >=0.7.5,<0.8).

Install dependency: python -m pip install "gemmi>=0.7.5,<0.8"

Python API::

    phase = load_cif("structure.cif")
    pattern = Pattern("pattern.dat", 10.0, 0.02, 80.0, wavelength1=1.5406,
                      wavelength2=1.54439, ratio=0.5)
    result = write_pcr("job.pcr", [phase], pattern)
    print(result.audit)

Command line::

    python cif_to_pcr.py --cif main.cif --cif impurity.cif \
        --data-file pattern.dat --range 10 0.02 80 \
        --lambda1 1.5406 --lambda2 1.54439 --ratio 0.5 --out job.pcr

Only writes PCR (and an audit JSON if explicitly requested). Does not invoke
FullProf, AutoFP, a GUI, or a bundled CIF converter. Does not rewrite diffraction
data, introduce defect sites, change composition, or choose a refinement route.
By default only phase scales have nonzero refinement codes; atomic parameters
remain fixed in the generated input. A later caller may change these codes.

Scope: one constant-step 2theta X-ray pattern, one or more tabulated space-group
settings, isotropic ADPs, six polynomial background coefficients, TCH profile.
Rejects anisotropic ADPs by default; pass anisotropic="ueq" (CLI:
--anisotropic-ueq) to explicitly reduce them using the unit-cell metric.
Rejects rhombohedral axes; supply a hexagonal-axis CIF instead. Missing Biso is filled
with an explicit configurable default and recorded. Supplied zero and large
nonnegative Biso values are preserved. Atomic scattering types use neutral
element symbols; CIF oxidation state is recorded, not used as an ionic form
factor. Magnetic, TOF, modulated and molecular rigid-body models are out of scope.

Ins=10 requires SIX header lines in the referenced DAT, then 2theta/I[/sigma].
Use --validate-dat to check an existing file. Without it, only the PCR and its
configuration are validated; no claim is made about external DAT contents.
Input/output filesystem paths can be Unicode; the embedded DAT filename must
be a portable ASCII basename in the same directory as the PCR.

Format references (consulted 2026-10-21):
https://www2017.ill.eu/sites/fullprof/downloads/Docs/FullProf_Manual.pdf
  Appendix, space-group line (HALL notation); atomic occupation; Ins=10.
https://gemmi.readthedocs.io/en/stable/chemistry.html
  SmallStructure / chemical occupancy / symmetry settings.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import gemmi

__version__ = "0.1.0"
PathLike = Union[str, Path]


class PcrError(ValueError):
    """Invalid or unsupported input; no model repair is performed implicitly."""


@dataclass(frozen=True)
class Atom:
    label: str
    element: str
    xyz: Tuple[float, float, float]
    occupancy: float = 1.0  # chemical site occupancy, NOT the PCR occupation
    biso: float = 0.5
    source_label: str = ""
    source_type: str = ""
    declared_multiplicity: Optional[int] = None


@dataclass(frozen=True)
class Phase:
    name: str
    hall: str  # exact setting; avoids dropping an origin/axis suffix from H-M
    cell: Tuple[float, float, float, float, float, float]
    atoms: Tuple[Atom, ...]
    scale: float = 0.001
    uvwxy: Tuple[float, float, float, float, float] = (0.01, -0.01, 0.01, 0.0, 0.0)
    notes: Tuple[str, ...] = ()
    source: str = ""
    source_sha256: str = ""


@dataclass(frozen=True)
class Pattern:
    data_file: str
    two_theta_min: float
    step: float
    two_theta_max: float
    wavelength1: float
    wavelength2: Optional[float] = None
    ratio: float = 0.0
    cthm: float = 0.91
    geometry: int = 0  # FullProf Ilo; set explicitly for the actual instrument
    rpolarz: float = 0.0
    background_origin: float = 40.0
    peak_cutoff: float = 8.0
    absorption_mu_r: float = 0.0
    asymmetry_limit: float = 60.0


@dataclass(frozen=True)
class PcrOptions:
    title: str = "DSH-XRD CIF to PCR"
    cycles: int = 10
    background: Tuple[float, ...] = (0.0,) * 6
    refine_scales: bool = True
    refine_background: bool = False
    refine_zero: bool = False
    position_tolerance: float = 1e-4  # fractional; CIF rounding near special sites


@dataclass(frozen=True)
class BuildResult:
    text: str
    audit: Dict[str, Any]


def _finite(values: Sequence[float], context: str) -> None:
    if not all(math.isfinite(x) for x in values):
        raise PcrError(context + ": non-finite number")


def _number(raw: str) -> Optional[float]:
    if not raw or gemmi.cif.is_null(raw):
        return None
    try:
        value = gemmi.cif.as_number(raw)
    except (ValueError, RuntimeError) as exc:
        raise PcrError("Invalid CIF number: " + raw) from exc
    if not math.isfinite(value):
        raise PcrError("Invalid CIF number: " + raw)
    return value


def _symbol(group: "gemmi.SpaceGroup") -> str:
    text = "HALL " + group.hall
    if len(text) > 20 or not text.isascii():
        raise PcrError("Hall symbol exceeds FullProf's 20-column space-group field")
    return text


def _spacegroup(hall: str) -> "gemmi.SpaceGroup":
    try:
        group = gemmi.find_spacegroup_by_ops(gemmi.symops_from_hall(hall))
    except (ValueError, RuntimeError) as exc:
        raise PcrError("Invalid Hall symbol: " + hall) from exc
    if group is None:
        raise PcrError("Space-group setting is not tabulated: " + hall)
    if group.ext == "R":
        raise PcrError("Rhombohedral axes are unsupported; supply a hexagonal-axis CIF")
    _symbol(group)
    return group


def _orbit(group: "gemmi.SpaceGroup", xyz: Sequence[float], tol: float) -> List[Tuple[float, ...]]:
    points = []
    for op in group.operations():
        p = tuple(v % 1.0 for v in op.apply_to_xyz(xyz))
        if not any(_same_position(p, q, tol) for q in points):
            points.append(p)
    return points


def _same_position(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
    return max(abs((x - y + 0.5) % 1.0 - 0.5) for x, y in zip(a, b)) < tol


def _label(original: str, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "", original) or "Atom"
    if not base[0].isalpha():
        base = "A" + base
    candidate = base[:8]
    count = 1
    while candidate in used:
        suffix = str(count)
        candidate = base[:8 - len(suffix)] + suffix
        count += 1
    used.add(candidate)
    return candidate


def load_cif(path: PathLike, *, block: Optional[str] = None,
             missing_biso: Optional[float] = 0.5, scale: float = 0.001,
             anisotropic: str = "reject", negative_biso: Optional[float] = None) -> Phase:
    """Read a CIF without changing its chemistry; return an immutable Phase.

    Select a block explicitly for a multi-block file. Set missing_biso=None to
    reject missing isotropic ADPs instead of using the documented fallback.
    """
    path = Path(path)
    doc = gemmi.cif.read(str(path))
    if block is None:
        if len(doc) != 1:
            raise PcrError("Multiple CIF blocks; choose block= explicitly")
        cif = doc[0]
    else:
        cif = doc.find_block(block)
        if cif is None:
            raise PcrError("CIF block not found: " + block)
    if anisotropic not in ("reject", "ueq"):
        raise PcrError("anisotropic must be 'reject' or 'ueq'")
    if negative_biso is not None:
        _finite([negative_biso], "negative_biso replacement")
        if negative_biso < 0:
            raise PcrError("negative_biso replacement must be nonnegative")
    if any(cif.find_value(tag) for tag in ("_space_group_ssg_name", "_space_group_magn.name_BNS")):
        raise PcrError("Superspace/magnetic CIF models are unsupported")
    st = gemmi.make_small_structure_from_block(cif)
    group = st.spacegroup
    if group is None:
        raise PcrError("Cannot resolve CIF space group; no P1 fallback is permitted")
    _spacegroup(group.hall)
    if st.symops:
        ops = gemmi.GroupOps([gemmi.Op(op) for op in st.symops])
        if {op.wrap().triplet() for op in ops} != {op.wrap().triplet() for op in group.operations()}:
            raise PcrError("CIF symmetry operations disagree with the resolved setting")
    if st.spacegroup_hall:
        declared_group = _spacegroup(st.spacegroup_hall)
        if declared_group.hall != group.hall:
            raise PcrError("CIF Hall symbol and symmetry operations disagree")
    hm = st.spacegroup_hm
    if hm:
        named = gemmi.find_spacegroup_by_name(hm, st.cell.alpha, st.cell.gamma)
        if named and named.number != group.number:
            raise PcrError("CIF space-group name and symmetry operations have different IT numbers")
    if st.spacegroup_number and st.spacegroup_number != group.number:
        raise PcrError("CIF IT number disagrees with resolved space group")
    if not st.sites:
        raise PcrError("CIF contains no fractional atomic sites")
    n = len(st.sites)
    def column(tag: str) -> List[str]:
        values = list(cif.find_values(tag))
        if values and len(values) != n:
            raise PcrError("CIF atom column length mismatch: " + tag)
        return values or [""] * n
    bs = column("_atom_site_B_iso_or_equiv")
    us = column("_atom_site_U_iso_or_equiv")
    occs = column("_atom_site_occupancy")
    mults = column("_atom_site_symmetry_multiplicity")
    atoms, notes, used = [], [], set()
    if missing_biso is not None:
        _finite([missing_biso], "missing_biso")
        if missing_biso < 0:
            raise PcrError("missing_biso must be nonnegative")
    for index, site in enumerate(st.sites):
        has_aniso = site.aniso.nonzero()
        if has_aniso and anisotropic == "reject":
            raise PcrError(site.label + ": anisotropic ADPs require an explicitly prepared isotropic model")
        label = _label(site.label, used)
        if label != site.label:
            notes.append("Atom label mapped: %r -> %s" % (site.label, label))
        b, u = _number(bs[index]), _number(us[index])
        if b is not None and u is not None and not math.isclose(b, 8 * math.pi ** 2 * u, abs_tol=0.02, rel_tol=0.02):
            raise PcrError(site.label + ": supplied Biso and Uiso disagree")
        if has_aniso and b is None and u is None:
            b = 8 * math.pi ** 2 * st.cell.calculate_u_eq(site.aniso)
            notes.append("%s: anisotropic tensor explicitly reduced to metric-correct B_eq=%g" % (site.label, b))
        elif has_aniso:
            notes.append(site.label + ": anisotropic tensor omitted by explicit ueq policy; retained supplied isotropic B/U equivalent")
        if b is None:
            b = 8 * math.pi ** 2 * u if u is not None else missing_biso
        if b is None:
            raise PcrError(site.label + ": missing isotropic ADP")
        if _number(bs[index]) is None and u is None and not has_aniso:
            notes.append("%s: missing Biso/Uiso; used explicit default Biso=%g" % (site.label, b))
        if b < 0:
            if negative_biso is None:
                raise PcrError(site.label + ": negative supplied Biso; choose an explicit replacement if required")
            notes.append("%s: negative Biso=%g replaced with explicit value %g" % (site.label, b, negative_biso))
            b = negative_biso
        if b > 5:
            notes.append("%s: large supplied Biso=%g preserved" % (site.label, b))
        occupancy = _number(occs[index])
        if occupancy is None:
            occupancy = 1.0
            notes.append(site.label + ": missing chemical occupancy; assumed 1")
        declared = _number(mults[index])
        if declared is not None and (declared <= 0 or not declared.is_integer()):
            raise PcrError(site.label + ": invalid declared multiplicity")
        element = site.element.name
        if element == "X":
            raise PcrError(site.label + ": unknown element; supply a valid atom type")
        atoms.append(Atom(label, element, tuple(site.fract), occupancy, b,
                          site.label, site.type_symbol,
                          int(declared) if declared is not None else None))
        if site.charge:
            notes.append("%s: CIF type %s written using neutral element %s" % (site.label, site.type_symbol, element))
    name = re.sub(r"[^A-Za-z0-9_. -]", "_", cif.name).strip()[:70] or "Phase"
    return Phase(name, group.hall, tuple(st.cell.parameters), tuple(atoms),
                 scale=scale, notes=tuple(notes), source=str(path),
                 source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _validate_pattern(pattern: Pattern, options: PcrOptions) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", pattern.data_file):
        raise PcrError("data_file must be a portable ASCII basename, e.g. pattern.dat")
    _finite([pattern.two_theta_min, pattern.step, pattern.two_theta_max,
             pattern.wavelength1, pattern.ratio, pattern.cthm, pattern.rpolarz,
             pattern.background_origin, pattern.peak_cutoff, pattern.absorption_mu_r,
             pattern.asymmetry_limit], "Pattern")
    if not 0 <= pattern.two_theta_min < pattern.two_theta_max < 180 or pattern.step <= 0:
        raise PcrError("Require 0 <= 2theta_min < 2theta_max < 180 and step > 0")
    steps = (pattern.two_theta_max - pattern.two_theta_min) / pattern.step
    if not math.isclose(steps, round(steps), abs_tol=1e-5, rel_tol=0):
        raise PcrError("Range and step do not describe an integer number of intervals")
    if pattern.wavelength1 <= 0 or pattern.ratio < 0:
        raise PcrError("Wavelength must be positive; intensity ratio must be nonnegative")
    if pattern.wavelength2 is None:
        if pattern.ratio != 0:
            raise PcrError("A nonzero intensity ratio requires wavelength2")
    else:
        _finite([pattern.wavelength2], "wavelength2")
        if pattern.wavelength2 <= 0:
            raise PcrError("wavelength2 must be positive")
    if pattern.geometry not in (0, 1, -1, 2, 3):
        raise PcrError("Unsupported Ilo geometry")
    if not 0 <= pattern.cthm <= 1 or not 0 <= pattern.rpolarz <= 1:
        raise PcrError("Polarization parameters must lie in [0,1]")
    if pattern.peak_cutoff <= 0 or pattern.absorption_mu_r < 0:
        raise PcrError("Invalid cutoff/absorption parameter")
    if not options.title.isascii() or any(c in options.title for c in "\r\n") or len(options.title) > 70:
        raise PcrError("Title must be one ASCII line, at most 70 characters")
    if type(options.cycles) is not int or options.cycles < 1:
        raise PcrError("cycles must be a positive integer")
    if len(options.background) != 6:
        raise PcrError("Exactly six background coefficients are required")
    _finite(options.background, "Background")
    if not 0 < options.position_tolerance <= 1e-3:
        raise PcrError("Fractional position tolerance must lie in (0,1e-3]")


def _phase_audit(phase: Phase, pattern: Pattern, tol: float) -> Dict[str, Any]:
    group = _spacegroup(phase.hall)
    if len(phase.cell) != 6:
        raise PcrError("Cell requires six parameters")
    _finite(phase.cell, "Cell")
    if min(phase.cell[:3]) <= 0 or not all(0 < v < 180 for v in phase.cell[3:]):
        raise PcrError("Invalid cell lengths/angles")
    cell = gemmi.UnitCell(*phase.cell)
    if not math.isfinite(cell.volume) or cell.volume <= 0 or not cell.is_compatible_with_spacegroup(group):
        raise PcrError("Cell metric is incompatible with " + group.xhm())
    if not phase.atoms or not phase.name.isascii() or any(c in phase.name for c in "\r\n"):
        raise PcrError("Phase needs atoms and a single-line ASCII name")
    _finite([phase.scale] + list(phase.uvwxy), "Scale/profile")
    if phase.scale <= 0 or len(phase.uvwxy) != 5:
        raise PcrError("Require positive scale and five U/V/W/X/Y coefficients")
    u, v, w, x, y = phase.uvwxy
    if x < 0 or y < 0:
        raise PcrError("Lorentzian X/Y coefficients must be nonnegative")
    ts = [math.tan(math.radians(a / 2)) for a in (pattern.two_theta_min, pattern.two_theta_max)]
    if u > 0 and ts[0] <= -v / (2 * u) <= ts[-1]:
        ts.append(-v / (2 * u))
    if min(u * t * t + v * t + w for t in ts) <= 0:
        raise PcrError("Initial Gaussian width squared is nonpositive in the scan range")
    audit_atoms, sites, labels = [], [], set()
    mass = 0.0
    general = len(group.operations())
    notes = list(phase.notes)
    for atom in phase.atoms:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,7}", atom.label) or atom.label in labels:
            raise PcrError("Atom labels must be unique portable names of 1..8 characters")
        labels.add(atom.label)
        if len(atom.xyz) != 3:
            raise PcrError(atom.label + ": three fractional coordinates required")
        _finite(list(atom.xyz) + [atom.occupancy, atom.biso], atom.label)
        if not 0 <= atom.occupancy <= 1 or atom.biso < 0:
            raise PcrError(atom.label + ": occupancy outside [0,1] or negative Biso")
        element = gemmi.Element(atom.element)
        if element.name == "X" or atom.element != element.name:
            raise PcrError(atom.label + ": use a canonical neutral element symbol")
        orbit = _orbit(group, atom.xyz, tol)
        mult = len(orbit)
        if general % mult:
            raise PcrError(atom.label + ": unstable special-position multiplicity; inspect coordinates/tolerance")
        if atom.declared_multiplicity is not None and atom.declared_multiplicity != mult:
            raise PcrError(atom.label + ": declared and calculated multiplicity disagree")
        for site in sites:
            if any(_same_position(atom.xyz, p, tol) for p in site["orbit"]):
                site["occupancy"] += atom.occupancy
                site["labels"].append(atom.label)
                break
        else:
            sites.append(dict(orbit=orbit, occupancy=atom.occupancy, labels=[atom.label]))
        occ_pcr = atom.occupancy * mult / general
        mass += atom.occupancy * mult * element.weight
        audit_atoms.append(dict(label=atom.label, source_label=atom.source_label,
                                element=atom.element, source_type=atom.source_type,
                                xyz=list(atom.xyz), biso=atom.biso,
                                chemical_occupancy=atom.occupancy, multiplicity=mult,
                                pcr_occupation=occ_pcr))
    for site in sites:
        if site["occupancy"] > 1.000001:
            raise PcrError("Overfilled symmetry-equivalent mixed site: " + ",".join(site["labels"]))
    if mass <= 0:
        raise PcrError("Phase contains no occupied atomic mass")
    return dict(name=phase.name, source=phase.source, source_sha256=phase.source_sha256,
                spacegroup=group.xhm(), hall=group.hall, cell=list(phase.cell),
                general_multiplicity=general, cell_mass_atz=mass,
                scale=phase.scale, uvwxy=list(phase.uvwxy), notes=notes, atoms=audit_atoms)


def build_pcr(phases: Sequence[Phase], pattern: Pattern,
              options: Optional[PcrOptions] = None) -> BuildResult:
    """Validate and render PCR in memory, with provenance and occupation audit."""
    options = options or PcrOptions()
    _validate_pattern(pattern, options)
    if not phases:
        raise PcrError("At least one phase is required")
    audits = [_phase_audit(p, pattern, options.position_tolerance) for p in phases]
    count = 0
    def code(enabled: bool) -> int:
        nonlocal count
        if not enabled:
            return 0
        count += 1
        return 10 * count + 1
    zero_code = code(options.refine_zero)
    background_codes = [code(options.refine_background) for _ in range(6)]
    scale_codes = [code(options.refine_scales) for _ in phases]
    fmt = lambda values: " ".join(format(v, ".12g") for v in values)
    wavelength2 = pattern.wavelength1 if pattern.wavelength2 is None else pattern.wavelength2
    lines = [
        "COMM " + options.title, "NPATT 1 1", "W_PAT 1",
        "! Nph Dum Ias Nre Cry Opt Aut", "%d 0 0 0 0 0 1" % len(phases),
        "! Job Npr Nba Nex Nsc Nor Iwg Ilo Res Ste Uni Cor Anm Int",
        "0 7 0 0 0 1 0 %d 0 0 0 0 0 0" % pattern.geometry,
        "! Data file; Ins=10 requires six header lines", pattern.data_file,
        "! Mat Pcr NLI Rpa Sym Sho", "0 1 0 0 0 0",
        "! Ipr Ppl Ioc Ls1 Ls2 Ls3 Prf Ins Hkl Fou Ana", "0 0 1 0 4 0 3 10 0 0 0",
        "! Lambda1 Lambda2 Ratio Bkpos Wdt Cthm muR AsyLim Rpolarz 2ndmuR",
        fmt([pattern.wavelength1, wavelength2, pattern.ratio, pattern.background_origin,
             pattern.peak_cutoff, pattern.cthm, pattern.absorption_mu_r,
             pattern.asymmetry_limit, pattern.rpolarz, 0]),
        "! NCY Eps R_at R_an R_pr R_gl", "%d 0.1 1 1 1 1" % options.cycles,
        "! Thmin Step Thmax PSD Sent0",
        fmt([pattern.two_theta_min, pattern.step, pattern.two_theta_max, 0, 0]),
        "%d ! Number of independent refined parameters" % count,
        "! Zero Code SyCos Code SySin Code Lambda Code MORE",
        fmt([0, zero_code, 0, 0, 0, 0, 0, 0, 0]),
        "! Six background coefficients, then codes", fmt(options.background), fmt(background_codes)]
    for index, (phase, audit, scale_code) in enumerate(zip(phases, audits, scale_codes), 1):
        group = _spacegroup(phase.hall)
        lines += ["! Phase %d" % index, phase.name,
                  "! Nat Dis Ang Jbt Isy Str Furth ATZ Nvk More",
                  "%d 0 0 0 0 0 0 %.12g 0 0" % (len(phase.atoms), audit["cell_mass_atz"]),
                  "! Contributions", "1", "! Irf Npr Jtyp Nsp_Ref Ph_Shift", "0 7 0 0 0",
                  "! Pr1 Pr2 Pr3 Brind Rmua Rmub Rmuc", "0 0 1 1 1 1 1",
                  "%-20s <--Space group symbol" % _symbol(group),
                  "! Atom Typ X Y Z Biso Occ In Fin N_t Spc"]
        for atom, atom_audit in zip(phase.atoms, audit["atoms"]):
            lines += ["%-8s %-4s %s 0 0 0 0" % (
                atom.label, atom.element,
                fmt(list(atom.xyz) + [atom.biso, atom_audit["pcr_occupation"]])), "0 0 0 0 0"]
        lines += ["! Scale Shape1 Bov Str1 Str2 Str3 Strain-Model",
                  fmt([phase.scale, 0, 0, 0, 0, 0, 0]), fmt([scale_code, 0, 0, 0, 0, 0]),
                  "! U V W X Y GauSiz LorSiz Size-Model",
                  fmt(list(phase.uvwxy) + [0, 0, 0]), "0 0 0 0 0 0 0",
                  "! a b c alpha beta gamma", fmt(phase.cell), "0 0 0 0 0 0",
                  "! Pref1 Pref2 Asy1 Asy2 Asy3 Asy4 S_L D_L",
                  "1 0 0 0 0 0 0 0", "0 0 0 0 0 0 0 0"]
    lines += ["! Plot interval", fmt([pattern.two_theta_min, pattern.two_theta_max, 1])]
    text = "\n".join(lines) + "\n"
    text.encode("ascii")
    audit = dict(generator="cif_to_pcr", version=__version__, gemmi_version=gemmi.__version__,
                 scope="constant-step CW X-ray; isotropic atoms; no engine invocation",
                 pattern=asdict(pattern), options=asdict(options), refined_parameters=count,
                 phases=audits, pcr_sha256=hashlib.sha256(text.encode("ascii")).hexdigest(),
                 dat_validated=False)
    return BuildResult(text, audit)


def write_pcr(path: PathLike, phases: Sequence[Phase], pattern: Pattern,
              options: Optional[PcrOptions] = None, *, overwrite: bool = False) -> BuildResult:
    """Write only PCR; refuse accidental overwrites, return the full build audit."""
    path = Path(path)
    if path.suffix.lower() != ".pcr":
        raise PcrError("Output filename must end in .pcr")
    if path.name == pattern.data_file:
        raise PcrError("PCR output and referenced DAT filenames must differ")
    if any(p.source and Path(p.source).resolve() == path.resolve() for p in phases):
        raise PcrError("Refusing to overwrite a source CIF with PCR output")
    result = build_pcr(phases, pattern, options)
    with path.open("w" if overwrite else "x", encoding="ascii", newline="\n") as stream:
        stream.write(result.text)
    return result


def validate_dat(path: PathLike, pattern: Pattern) -> int:
    """Read-only check of an Ins=10 DAT against the declared constant-step grid."""
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 8:
        raise PcrError("Ins=10 DAT requires six header lines and at least two points")
    def numeric_row(line: str) -> Optional[List[float]]:
        if not line.strip() or line.lstrip().startswith(("!", "#")):
            return None
        try:
            return [float(v) for v in line.split()]
        except ValueError:
            return None
    if any(numeric_row(line) for line in lines[:6]):
        raise PcrError("Ins=10 would discard numeric data in the first six lines; fix the DAT header first")
    rows = []
    for index, line in enumerate(lines[6:], 7):
        if not line.strip() or line.lstrip().startswith(("!", "#")):
            continue
        row = numeric_row(line)
        if row is None or len(row) not in (2, 3):
            raise PcrError("Invalid numeric DAT row at line %d" % index)
        _finite(row, "DAT line %d" % index)
        if len(row) == 3 and row[2] <= 0:
            raise PcrError("DAT sigma must be positive at line %d" % index)
        expected = pattern.two_theta_min + len(rows) * pattern.step
        if not math.isclose(row[0], expected, abs_tol=max(1e-7, pattern.step * 1e-3), rel_tol=0):
            raise PcrError("DAT angle/grid mismatch at line %d" % index)
        rows.append(row)
    expected_points = round((pattern.two_theta_max - pattern.two_theta_min) / pattern.step) + 1
    if len(rows) != expected_points:
        raise PcrError("DAT point count differs from the PCR range/step")
    return len(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cif", action="append", required=True, help="One CIF per phase; repeat for multiphase")
    parser.add_argument("--block", help="Block name, for a single multi-block CIF")
    parser.add_argument("--data-file", default="pattern.dat", help="Embedded ASCII DAT basename")
    parser.add_argument("--range", type=float, nargs=3, required=True, metavar=("MIN", "STEP", "MAX"))
    parser.add_argument("--lambda1", type=float, required=True)
    parser.add_argument("--lambda2", type=float)
    parser.add_argument("--ratio", type=float, default=0.0)
    parser.add_argument("--cthm", type=float, default=0.91)
    parser.add_argument("--scale", type=float, default=0.001, help="Common initial phase scale; per-phase values are available via Python API")
    parser.add_argument("--missing-biso", type=float, default=0.5)
    parser.add_argument("--require-biso", action="store_true")
    parser.add_argument("--anisotropic-ueq", action="store_true", help="Explicitly use isotropic equivalents for anisotropic ADPs")
    parser.add_argument("--negative-biso", type=float, help="Explicit replacement for negative source Biso; recorded in audit")
    parser.add_argument("--background", type=float, nargs=6, default=(0.0,) * 6)
    parser.add_argument("--refine-background", action="store_true")
    parser.add_argument("--refine-zero", action="store_true")
    parser.add_argument("--fixed-scales", action="store_true")
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--audit", type=Path, help="Optional JSON audit output")
    parser.add_argument("--validate-dat", action="store_true", help="Check DAT beside --out, read-only")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.block and len(args.cif) != 1:
            raise PcrError("--block is for one CIF; use Python API for per-phase block selection")
        if args.audit is not None:
            protected = [args.out, args.out.parent / args.data_file] + [Path(p) for p in args.cif]
            if args.audit.resolve() in {p.resolve() for p in protected}:
                raise PcrError("Audit output must differ from PCR, DAT and source CIF paths")
            if args.audit.exists() and not args.overwrite:
                raise FileExistsError(str(args.audit))
        phases = [load_cif(p, block=args.block,
                           missing_biso=None if args.require_biso else args.missing_biso,
                           scale=args.scale, anisotropic="ueq" if args.anisotropic_ueq else "reject",
                           negative_biso=args.negative_biso) for p in args.cif]
        pattern = Pattern(args.data_file, *args.range, args.lambda1, args.lambda2, args.ratio, args.cthm)
        options = PcrOptions(cycles=args.cycles, background=tuple(args.background),
                             refine_scales=not args.fixed_scales,
                             refine_background=args.refine_background, refine_zero=args.refine_zero)
        _validate_pattern(pattern, options)
        points = validate_dat(args.out.parent / pattern.data_file, pattern) if args.validate_dat else None
        result = write_pcr(args.out, phases, pattern, options, overwrite=args.overwrite)
        audit = dict(result.audit, dat_validated=points is not None, dat_points=points)
        if args.audit:
            with args.audit.open("w" if args.overwrite else "x", encoding="utf-8", newline="\n") as stream:
                json.dump(audit, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        for phase in phases:
            for note in phase.notes:
                print("NOTE: " + note, file=sys.stderr)
        print(json.dumps(dict(pcr=str(args.out), phases=len(phases),
                              parameters=result.audit["refined_parameters"], dat_validated=points is not None)))
        return 0
    except (PcrError, OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, "cif_to_pcr: %s\n" % exc)


if __name__ == "__main__":
    raise SystemExit(main())
