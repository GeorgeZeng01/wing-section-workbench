"""The calibration harvest sink: one row per finished solve.

Pins cfd_run.harvest_row / append_harvest / worst_reversed.

Why this exists: the shipped separation screen is calibrated on five cases,
and it is five because run-dir housekeeping deleted everything else — the
previous record had to be hand-extracted from case directories that were
already disappearing. Every solve now leaves a row that outlives its case,
carrying the CHEAP screen's prediction and the EXPENSIVE measurement side
by side, which is the pairing a regression needs and the thing nothing on
disk currently holds.

What this suite refuses to let regress:
  * losing a calibration row must never fail a solve that already succeeded
    (append_harvest returns False, never raises);
  * the sink honours the same data-dir override the rest of the app does,
    so a test run can never write into the real record;
  * "how separated was this run" has ONE definition, shared by the queue's
    ranking and the sink;
  * the config travels verbatim, because a row has to stay reproducible
    after its case directory is gone;
  * nothing in a row is graded.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_harvest.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import cfd_run  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG = {"chord_mm": 350, "elements": [{"airfoil": "s1223"}]}

RESULT = {
    "cl_rans": 7.65, "cd_rans": 0.2615,
    "delta_cl_pct": 27.1, "delta_cd_pct": 413.7,
    "delta_cd_is_upper_bound": True,
    "converged": True, "user_stopped": False,
    "stop_reason": "force history converged", "n_iters_run": 8204,
    "case_dir": str(Path("app_data") / "rans" / "abc123def456"),
    "panel": {"c_est": 6.0185, "cd_profile": 0.0509,
              "shadow_mins": [None, 0.509, 0.44]},
    "wall_report": {"separation": {
        "wing_e1": {"reversed_frac": 0.057, "n_faces": 400},
        "wing_e2": {"reversed_frac": 0.217, "n_faces": 300},
        "wing_e3": {"reversed_frac": 0.027, "n_faces": 200}}},
    "recirc_report": {
        "status": "measured", "bound": False,
        "elements": [
            {"near_wall_reversed_frac": 0.0202, "sides": {
                "lower": {"runs": [{"closes": None}]},
                "upper": {"runs": []}}},
            {"near_wall_reversed_frac": 0.3895, "sides": {
                "upper": {"runs": [{"closes": True}]},
                "lower": {"runs": []}}},
            {"near_wall_reversed_frac": 0.0, "sides": {}},
        ]},
}


def main() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="wss-harvest-"))
    saved = {k: os.environ.get(k) for k in ("WSS_HARVEST_PATH",
                                            "WSS_DATA_DIR")}
    try:
        # ---- the path honours the same overrides as the rest of the app --
        os.environ.pop("WSS_HARVEST_PATH", None)
        os.environ["WSS_DATA_DIR"] = str(tmp / "data")
        check("the sink lands under the data dir override, never the real "
              "record", cfd_run._harvest_path() == tmp / "data" / "harvest.jsonl",
              f"({cfd_run._harvest_path()})")
        os.environ["WSS_HARVEST_PATH"] = str(tmp / "explicit.jsonl")
        check("an explicit path override wins",
              cfd_run._harvest_path() == tmp / "explicit.jsonl")

        # ---- one definition of 'how separated was this run' --------------
        check("worst_reversed takes the worst element",
              cfd_run.worst_reversed(RESULT) == 0.217)
        check("worst_reversed is None when nothing was measured",
              cfd_run.worst_reversed({}) is None
              and cfd_run.worst_reversed({"wall_report": None}) is None
              and cfd_run.worst_reversed(
                  {"wall_report": {"separation": {}}}) is None)
        qsrc = (ROOT / "app" / "core" / "rans_queue.py").read_text(
            encoding="utf-8")
        check("the queue uses that shared definition rather than its own",
              "cfd_run.worst_reversed(r)" in qsrc
              and "reversed_frac" not in qsrc.split("_ranked_rows")[0]
              .split("def _harvest")[-1])

        # ---- the row pairs prediction with measurement -------------------
        row = cfd_run.harvest_row(RESULT, CFG, "openfoam", "fine")
        check("the row carries the CHEAP screen's prediction",
              row["shadow_mins"] == [None, 0.509, 0.44]
              and row["c_est"] == 6.0185 and row["cd_profile_est"] == 0.0509)
        check("the row carries the EXPENSIVE wall-shear measurement",
              row["wall_reversed"] == {"wing_e1": 0.057, "wing_e2": 0.217,
                                       "wing_e3": 0.027}
              and row["worst_reversed"] == 0.217)
        check("the row carries the field probe beside it",
              row["recirc_near_wall_frac"] == [0.0202, 0.3895, 0.0]
              and row["recirc_status"] == "measured"
              and row["recirc_bound"] is False)
        check("closure travels per element, and is not flattened into a "
              "severity",
              sorted(str(x) for x in row["recirc_closes"][0]) == ["None"]
              and row["recirc_closes"][1] == [True]
              and row["recirc_closes"][2] == [])
        check("both comparison columns ride along, bound flag included",
              row["delta_cl_pct"] == 27.1 and row["delta_cd_pct"] == 413.7
              and row["delta_cd_is_upper_bound"] is True)
        check("run trustworthiness travels with the numbers",
              row["converged"] is True and row["user_stopped"] is False
              and row["n_iters_run"] == 8204 and row["stop_reason"])
        check("the config travels verbatim so the row outlives its case",
              row["config"] == CFG and row["case_id"] == "abc123def456"
              and row["engine"] == "openfoam" and row["mesh_size"] == "fine")
        check("no grading in a row beyond the ADOPTED field verdict "
              "(2026-08-03 adoption; ad-hoc grades stay forbidden)",
              not any(("verdict" in k or "grade" in k)
                      and k != "field_verdict" for k in row))

        # ---- a sparse result must not raise ------------------------------
        thin = cfd_run.harvest_row({}, CFG, "fluent2d", None)
        check("a result missing every optional key still builds a row",
              thin["cl_rans"] is None and thin["shadow_mins"] is None
              and thin["wall_reversed"] is None
              and thin["recirc_near_wall_frac"] is None
              and thin["case_id"] is None)

        # ---- appending -----------------------------------------------------
        p = tmp / "explicit.jsonl"
        check("append creates the file and writes exactly one JSON line",
              cfd_run.append_harvest(row) is True and p.is_file()
              and len(p.read_text(encoding="utf-8").strip().splitlines()) == 1)
        cfd_run.append_harvest(thin)
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        check("appending is append, not overwrite", len(lines) == 2)
        check("every line round-trips as JSON",
              all(json.loads(ln)["engine"] in ("openfoam", "fluent2d")
                  for ln in lines))
        os.environ["WSS_HARVEST_PATH"] = str(tmp / "deep" / "a" / "b.jsonl")
        check("missing parent directories are created",
              cfd_run.append_harvest(row) is True
              and (tmp / "deep" / "a" / "b.jsonl").is_file())

        # ---- failure must never take a solve down ------------------------
        nan_row = dict(row)
        nan_row["cl_rans"] = float("nan")
        os.environ["WSS_HARVEST_PATH"] = str(tmp / "nan.jsonl")
        check("a NaN refuses to be written rather than producing invalid "
              "JSON, and reports the refusal",
              cfd_run.append_harvest(nan_row) is False)
        os.environ["WSS_HARVEST_PATH"] = str(tmp / "explicit.jsonl" / "x")
        check("an unwritable path returns False and does not raise",
              cfd_run.append_harvest(row) is False)

        # ---- every engine feeds the sink ---------------------------------
        for name, eng in (("cfd_run", "openfoam"),
                          ("fluent2d_run", "fluent2d"),
                          ("fluent_run", "fluent")):
            src = (ROOT / "app" / "core" / f"{name}.py").read_text(
                encoding="utf-8")
            check(f"{name} appends a harvest row at result assembly",
                  "append_harvest(" in src and f'"{eng}"' in src)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} harvest checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
