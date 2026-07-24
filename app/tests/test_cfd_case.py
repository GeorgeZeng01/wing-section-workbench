"""CFD case export validation: a generated OpenFOAM case must be complete
and internally consistent — the mesh declares every patch the 0/* files
reference, the force coefficients are downforce-positive, the ground
carries the freestream, and run.sh survives a Linux shell.

Runs entirely offline (gmsh only — no WSL, no OpenFOAM).

Run directly:  python app/tests/test_cfd_case.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import cfd, geometry  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG = geometry.StackConfig.from_dict({
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "ride_height_mm": 30, "chord_mm": 350, "span_mm": 1400,
    "speed_ms": 15, "n_panels_per_side": 60,
})

REQUIRED_FILES = [
    "mesh.msh", "run.sh", "README.txt", "case.foam",
    "system/controlDict", "system/fvSchemes", "system/fvSolution",
    "constant/turbulenceProperties", "constant/transportProperties",
    "0/U", "0/p", "0/k", "0/omega", "0/nut",
]
REQUIRED_PATCHES = {"inlet", "outlet", "top", "ground",
                    "wing_e1", "wing_e2", "frontAndBack"}


def msh_stats(text):
    """(physical names, 3D cell count) from a MSH2 file."""
    names = set(re.findall(r'^\d+ \d+ "([^"]+)"$',
                           text[text.index("$PhysicalNames"):
                                text.index("$EndPhysicalNames")], re.M))
    body = text[text.index("$Elements"):text.index("$EndElements")]
    lines = body.splitlines()[2:]
    # MSH2 element line: number type n-tags tags... nodes...; 3D types:
    # 4 tet, 5 hex, 6 prism, 7 pyramid
    n3 = sum(1 for ln in lines if ln.split(None, 2)[1] in ("4", "5", "6", "7"))
    return names, n3


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wss_cfd_case_"))
    try:
        case = tmp / "case"
        path_before = cfd._real_env_path()
        summary = cfd.build_case(CFG, case, "coarse")

        # gmsh clobbers the real Win32 process PATH; build_case must restore
        # it or every later subprocess (docker, explorer) fails to resolve
        check("real process PATH survives meshing",
              cfd._real_env_path() == path_before,
              f"(len {len(path_before or '')} -> "
              f"{len(cfd._real_env_path() or '')})")

        missing = [f for f in REQUIRED_FILES if not (case / f).is_file()]
        check("all case files present", not missing, f"missing: {missing}")

        msh = (case / "mesh.msh").read_text()
        check("mesh is MSH2 (gmshToFoam-compatible)",
              msh.splitlines()[1].startswith("2.2"))
        names, n3 = msh_stats(msh)
        check("mesh declares every required physical group",
              REQUIRED_PATCHES <= names, f"({sorted(names)})")
        check("summary cell count matches the mesh file",
              n3 == summary["n_cells"], f"(msh {n3}, summary {summary})")
        check("coarse 2D cell count in the sane range (6k..40k)",
              6_000 <= summary["n_cells"] <= 40_000, f"({summary['n_cells']})")
        check("first layer targets y+ ~ 1",
              summary["boundary_layer"] and 0.5 <= summary["y_plus_est"] <= 2.0,
              f"(y+ {summary['y_plus_est']}, h1 {summary['first_layer_mm']} mm)")

        cd = (case / "system/controlDict").read_text()
        # the RANS runner's graceful stop rewrites this exact token — the
        # two must never drift apart
        check("controlDict carries the stopAt token the runner rewrites",
              "stopAt          endTime;" in cd
              and "runTimeModifiable true;" in cd)
        fc = cd[cd.index("forceCoeffs"):]
        check("controlDict forceCoeffs is downforce-positive on the wing",
              "liftDir         (0 -1 0);" in fc
              and '("wing_.*")' in fc
              and f"lRef            {CFG.chord_m:g};" in fc)

        u0 = (case / "0/U").read_text()
        m = re.search(r"ground\s*\{[^}]*\}", u0, re.S)
        check("0/U ground BC moves with the freestream",
              m and "fixedValue" in m.group(0)
              and "uniform (15 0 0)" in m.group(0),
              f"({' '.join((m.group(0) if m else 'no ground block').split())})")
        check("0/U wing BC is noSlip and z-planes empty",
              re.search(r'"wing_\.\*"\s*\{[^}]*noSlip', u0, re.S)
              and re.search(r"frontAndBack\s*\{[^}]*empty", u0, re.S))

        sh = (case / "run.sh").read_bytes()
        check("run.sh has LF line endings", b"\r" not in sh)
        text = sh.decode()
        check("run.sh sources OpenFOAM before strict mode",
              "/usr/lib/openfoam/openfoam*/etc/bashrc" in text
              and text.index('source "$BASHRC"') < text.index("\nset -e\n"))
        check("run.sh converts, fixes patch types, solves, extracts",
              all(s in text for s in (
                  "gmshToFoam mesh.msh",
                  "entry0/frontAndBack/type -set empty",
                  "entry0/ground/type -set wall",
                  "entry0/wing_e1/type -set wall",
                  "entry0/wing_e2/type -set wall",
                  "simpleFoam", "writeCellCentres", "coefficient.dat",
                  "results.txt")))

        check("run.sh resets a previously-run case to a clean start",
              "resetting the case to a clean start" in text
              and text.index("resetting the case") < text.index("gmshToFoam"))

        check("bad mesh_size is rejected",
              _raises(lambda: cfd.build_case(CFG, tmp / "x", "ultra")))

        # a validated config can still be too tall for the fixed CFD box —
        # refuse with a clear error instead of meshing a broken domain
        # (the box is Y_TOP_C chords tall; the section must clear 75% of it)
        import dataclasses as _dc
        too_tall = _dc.replace(CFG, chord_mm=30.0, ride_height_mm=400.0)
        check("section outside the CFD domain is rejected before meshing",
              _raises(lambda: cfd.build_case(too_tall, tmp / "y", "coarse")))

        cd_text = (case / "system/controlDict").read_text()
        check("controlDict writes wall diagnostics beside the fields",
              "yPlus1" in cd_text and "wallShearStress1" in cd_text
              and "type            yPlus;" in cd_text
              and "type            wallShearStress;" in cd_text)
        check("run.sh reports tail drift and flags a trending run",
              "NOT CONVERGED" in text and "Cl drift over the trailing" in text)
        check("run.sh reset clears stale result artifacts",
              "flow_umag.png" in text and "results.txt" in text.split(
                  "resetting the case")[1].split("gmshToFoam")[0])

        # ---- foam_post field parsing: every legal ASCII list encoding ----
        from app.core import foam_post
        import numpy as np
        multi = ("internalField   nonuniform List<scalar> \n3\n(\n1.0\n2.0"
                 "\n3.0\n)\n;\n")
        check("parser: multi-line scalar list",
              list(foam_post.parse_internal_field(multi)) == [1.0, 2.0, 3.0])
        single = "internalField nonuniform List<scalar> 3(1.0 2.0 3.0);"
        check("parser: single-line scalar list",
              list(foam_post.parse_internal_field(single)) == [1.0, 2.0, 3.0])
        compact = "internalField nonuniform List<scalar> 4{2.5};"
        check("parser: compact repeat form",
              list(foam_post.parse_internal_field(compact)) == [2.5] * 4)
        vec = ("internalField nonuniform List<vector> 2((1 2 3) (4 5 6));")
        check("parser: single-line vector list",
              foam_post.parse_internal_field(vec).tolist()
              == [[1, 2, 3], [4, 5, 6]])
        # wall_report on a synthetic stopped case: reversed tau_x > 0
        wcase = tmp / "wallcase"
        (wcase / "100").mkdir(parents=True)
        (wcase / "100" / "wallShearStress").write_text(
            "internalField nonuniform List<vector> 1((0 0 0));\n"
            "boundaryField\n{\n    wing_e1\n    {\n"
            "        type calculated;\n"
            "        value nonuniform List<vector> 4((-1 0 0) (-1 0 0) "
            "(1 0 0) (-2 0 0));\n    }\n}\n")
        pp = wcase / "postProcessing" / "yPlus1" / "0"
        pp.mkdir(parents=True)
        (pp / "yPlus.dat").write_text(
            "# Time patch min max average\n"
            "100\twing_e1\t0.5\t4.0\t1.2\n")
        wr = foam_post.wall_report(wcase)
        check("wall_report measures reversed fraction and y+",
              wr and wr["separation"]["wing_e1"]["reversed_frac"] == 0.25
              and wr["yplus"]["wing_e1"]["avg"] == 1.2, f"({wr})")
        check("wall_report is None for a case without diagnostics",
              foam_post.wall_report(tmp / "case") is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} cfd-case checks passed")
    return all(results)


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
