"""Adjoint polish — everything except ANSYS itself.

AdjointPolishJob's state machine through the faked _mcp seam: seed
validation, option resolution, the envelope-clipped design region, the
morphed-node extraction (attribution, ordering, guards), the polished
rule/buildability re-checks, the loop's stop doctrine (budget, rule
boundary, converged, walked-past-the-peak), best-compliant delivery,
artifact honesty (case/flow written only when the session ends on the
delivered shape), license release on every path including cancel and
raise, and the no-harvest / no-panel guarantees for free-form shapes.

Run directly:  python app/tests/test_adjoint_run.py
"""
import json
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="wss_adjoint_run_")
os.environ["WSS_DATA_DIR"] = _TMP
os.environ["WSS_EXPORTS_DIR"] = str(Path(_TMP) / "exports")

import numpy as np  # noqa: E402

from app.core import adjoint_run, cfd, cfd_run  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "ride_height_mm": 30, "chord_mm": 350, "span_mm": 1400,
    "speed_ms": 15, "ncrit": 7, "n_panels_per_side": 50,
    "manufacturing": {"te_gap_mm": 2, "te_mode": "thicken",
                      "min_thickness_mm": 2},
    "rule_envelope": {"max_length_mm": 900, "max_height_mm": 500,
                      "min_ground_clearance_mm": None},
}
CFG_OBJ = StackConfig.from_dict(CFG)
GEO = cfd.section_geometry(CFG_OBJ, "coarse")
BASE_POLYS = [np.asarray(p, float) for p, _b in GEO["polys"]]


# ---------- resolve_options ----------

o = adjoint_run.resolve_options(None)
check("options: empty request takes every default",
      o == dict(adjoint_run.DEFAULTS))
o = adjoint_run.resolve_options({"drag_exchange_k": 1.5,
                                 "design_iters": 3})
check("options: override wins, absent falls back",
      o["drag_exchange_k"] == 1.5 and o["design_iters"] == 3
      and o["flow_iters"] == adjoint_run.DEFAULTS["flow_iters"])
check("options: None falls back like absent",
      adjoint_run.resolve_options({"margin_pct": None})["margin_pct"]
      == adjoint_run.DEFAULTS["margin_pct"])
for bad, why in [({"drag_exchange_k": -0.1}, "below range"),
                 ({"design_iters": 61}, "above range"),
                 ({"design_iters": 2.5}, "fractional int"),
                 ({"flow_iters": float("inf")}, "non-finite"),
                 ({"settle_iters": True}, "bool"),
                 ({"step_pct": 0.05}, "step below range"),
                 ({"step_pct": 10.1}, "step above range"),
                 ({"auto_reverify": "yes"}, "non-bool auto")]:
    try:
        adjoint_run.resolve_options(bad)
        check(f"options: {why} refused", False)
    except ValueError as e:
        check(f"options: {why} refused", list(bad)[0] in str(e))
check("options: the auto-reverify contract defaults ON",
      adjoint_run.resolve_options(None)["auto_reverify"] is True
      and adjoint_run.resolve_options(
          {"auto_reverify": False})["auto_reverify"] is False)


# ---------- region_bounds ----------

rb = adjoint_run.region_bounds(BASE_POLYS, CFG_OBJ, 6.0)
allp = np.vstack(BASE_POLYS)
m = 0.06 * CFG_OBJ.chord_m
check("region: margin extends the bbox on free sides",
      abs(rb["x"][0] - (allp[:, 0].min() - m)) < 1e-6
      and abs(rb["x"][1] - (allp[:, 0].max() + m)) < 1e-6)
check("region: with no clearance rule the floor keeps half the current "
      "clearance",
      abs(rb["y"][0] - adjoint_run.GROUND_KEEP_FRAC * allp[:, 1].min())
      < 1e-6 and any("half the current" in c for c in rb["clips"]))
check("region: the envelope's height cap is honored",
      rb["y"][1] <= 500 / 1000.0 + 1e-9)

tight = dict(CFG, rule_envelope={"max_length_mm": 900,
                                 "max_height_mm": 130,
                                 "min_ground_clearance_mm": 20})
tcfg = StackConfig.from_dict(tight)
rb2 = adjoint_run.region_bounds(BASE_POLYS, tcfg, 6.0)
check("region: tight height cap clips the top with a note",
      abs(rb2["y"][1] - 0.130) < 1e-9
      and any("height cap" in c for c in rb2["clips"]))
check("region: ground-clearance floor clips the bottom",
      abs(rb2["y"][0] - 0.020) < 1e-9
      and any("clearance floor" in c for c in rb2["clips"]))

long_env = dict(CFG, rule_envelope={"max_length_mm": 460,
                                    "max_height_mm": 500,
                                    "min_ground_clearance_mm": None})
lcfg = StackConfig.from_dict(long_env)
rb3 = adjoint_run.region_bounds(BASE_POLYS, lcfg, 15.0)
span = rb3["x"][1] - rb3["x"][0]
check("region: length cap bounds the span and still contains the stack",
      span <= 0.460 + 1e-9
      and rb3["x"][0] <= allp[:, 0].min() + 1e-9
      and rb3["x"][1] >= allp[:, 0].max() - 1e-9
      and any("length cap" in c for c in rb3["clips"]))

flat_env = dict(CFG, rule_envelope={"max_length_mm": 900,
                                    "max_height_mm": 1,
                                    "min_ground_clearance_mm": None})
try:
    adjoint_run.region_bounds(BASE_POLYS, StackConfig.from_dict(flat_env),
                              6.0)
    check("region: an envelope with no room raises", False)
except ValueError as e:
    check("region: an envelope with no room raises", "no room" in str(e))


# ---------- split_wall_nodes ----------

def displaced(polys, dy):
    return [p + np.array([0.0, dy]) for p in polys]


rng = np.random.default_rng(7)
morph = displaced(BASE_POLYS, 0.0004)
nodes = np.vstack(morph)
perm = rng.permutation(len(nodes))
out, stats = adjoint_run.split_wall_nodes(
    nodes[perm], BASE_POLYS, 0.10 * CFG_OBJ.chord_m)
check("extract: every node lands with its own element",
      len(out) == len(BASE_POLYS)
      and all(len(o) == len(p) for o, p in zip(out, morph)))
ordered_ok = all(
    np.allclose(np.sort(o, axis=0), np.sort(p, axis=0), atol=1e-12)
    for o, p in zip(out, morph))
check("extract: shuffled nodes recover the per-element point sets",
      ordered_ok)
# ordering: consecutive points must stay near-neighbors (a shuffled set
# re-ordered by baseline index cannot jump across the contour)
gaps = [float(np.hypot(*(o[1:] - o[:-1]).T).max()) for o in out]
seg = [float(np.hypot(*(p[1:] - p[:-1]).T).max()) for p in BASE_POLYS]
check("extract: recovered order walks the contour (no cross-jumps)",
      all(g <= 3.0 * s + 1e-9 for g, s in zip(gaps, seg)),
      f"max gap {max(gaps):.4f} m")
check("extract: displacement stats match the imposed morph",
      abs(stats["max_disp_mm"] - 0.4) < 0.05
      and abs(stats["mean_disp_mm"] - 0.4) < 0.05)

far = np.vstack([nodes, [[9.0, 9.0]]])
try:
    adjoint_run.split_wall_nodes(far, BASE_POLYS, 0.10 * CFG_OBJ.chord_m)
    check("extract: an unattributable node raises", False)
except ValueError as e:
    check("extract: an unattributable node raises",
          "cannot attribute" in str(e))

try:
    adjoint_run.split_wall_nodes(nodes[:15], BASE_POLYS,
                                 0.10 * CFG_OBJ.chord_m)
    check("extract: an element starved of nodes raises", False)
except ValueError:
    check("extract: an element starved of nodes raises", True)


# ---------- check_polished ----------

rules = adjoint_run.check_polished(BASE_POLYS, CFG_OBJ)
check("rules: the unmorphed as-built section is compliant",
      rules["ok"] and not rules["violations"], str(rules["violations"]))
check("rules: aft-thickness floor measured per element",
      all("aft_min_thickness_mm" in r for r in rules["rows"])
      and all(r["aft_floor_ok"] for r in rules["rows"]))

high = [BASE_POLYS[0] + np.array([0.0, 0.5]), BASE_POLYS[1]]
r2 = adjoint_run.check_polished(high, CFG_OBJ)
check("rules: a morph past the height cap violates the box",
      not r2["ok"]
      and any(v["rule"] == "max_height_mm" for v in r2["violations"]))


def squashed(poly, keep=0.35):
    c = poly.copy()
    mid = 0.5 * (c[:, 1].max() + c[:, 1].min())
    c[:, 1] = mid + (c[:, 1] - mid) * keep
    return c


thin = [BASE_POLYS[0], squashed(BASE_POLYS[1])]
r3 = adjoint_run.check_polished(thin, CFG_OBJ)
check("rules: thinning an element below the mfg floor is a violation",
      not r3["ok"]
      and any(v["rule"] == "min_thickness_mm" for v in r3["violations"]))

le_env = dict(CFG, rule_envelope={"max_length_mm": 900,
                                  "max_height_mm": 500,
                                  "min_le_radius_mm": 40.0,
                                  "le_radius_scope": "frontmost"})
r4 = adjoint_run.check_polished(BASE_POLYS,
                                StackConfig.from_dict(le_env))
le_row = next((r for r in r4["rows"] if "le_radius_mm" in r), None)
check("rules: an impossible LE-radius floor is caught on the contour",
      le_row is not None
      and (le_row["le_radius_ok"] is False or le_row["le_radius_ok"]
           is None),
      str(le_row))

no_env = {k: v for k, v in CFG.items() if k != "rule_envelope"}
r5 = adjoint_run.check_polished(BASE_POLYS, StackConfig.from_dict(no_env))
check("rules: with no envelope the mfg floor still guards",
      r5["ok"] and all("aft_min_thickness_mm" in r for r in r5["rows"]))


# ---------- the job through fakes ----------

def plausible_field_csv(path):
    lines = ["cellnumber, x-coordinate, y-coordinate,"
             " x-velocity, y-velocity, pressure"]
    n = 400
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 0.3 + 0.25 * (i % 7) / 7
        x, y = 0.2 + r * math.cos(a), 0.25 + abs(r * math.sin(a))
        p_pa = -61.25 if i < n // 2 else -250.0
        lines.append(f"{i+1}, {x:.6e}, {y:.6e}, "
                     f"{15.0 + math.sin(a):.6e}, {0.5:.6e}, {p_pa:.6e}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class SeedStub:
    """A finished Fluent2DJob, as far as the polish is concerned."""

    def __init__(self, config=CFG, state="done", engine="fluent2d",
                 files=True, result=None, conventions="studio"):
        self.id = "seed" + os.urandom(3).hex()
        self.state = state
        self._engine = engine
        self.config = json.loads(json.dumps(config))
        self.case_dir = Path(_TMP) / f"runs-{self.id}"
        self.case_dir.mkdir(parents=True, exist_ok=True)
        if files:
            (self.case_dir / "case.cas.h5").write_bytes(b"CAS")
            (self.case_dir / "case.dat.h5").write_bytes(b"DAT")
        self.result = result if result is not None else {
            "cl_rans": 2.10, "cd_rans": 0.085, "converged": True,
            "sizing": "default", "conventions": conventions,
            "profiles_override": False}
        self.conventions = conventions
        self.settings = {"sizing": "default", "n_iters": 800,
                         "edge_size_mm": 0.1}
        self.mesh = {"n_cells": 10360}
        self.n_ranks = 1

    def snapshot(self):
        return {"engine": self._engine, "id": self.id}


class FakeFM:
    """The fluent_mcp module, polish flavor. cl/cd per design iteration
    come from cl_seq/cd_seq (index 0 = the settle baseline); the morph
    per iteration from morph_fn(it) -> list of polylines."""

    def __init__(self, cl_seq, cd_seq, morph_fn,
                 fail_setup=False, no_profile_zone=False,
                 launch_raises=False, hold=None):
        self.cl_seq, self.cd_seq = cl_seq, cd_seq
        self.morph_fn = morph_fn
        self.fail_setup = fail_setup
        self.no_profile_zone = no_profile_zone
        self.launch_raises = launch_raises
        self.hold = hold
        self.it = 0
        self.calls = []
        self.shutdowns = 0
        self.interrupts = 0
        self.setup_kw = None

    def launch(self, **kw):
        self.calls.append("launch")
        if self.launch_raises:
            raise RuntimeError("no license seat")
        return {"launched_in_s": 0.1, **kw}

    def read_case_data(self, path):
        self.calls.append("read_case_data")
        self.read_path = path
        walls = ["ground"] if self.no_profile_zone \
            else ["profile", "ground"]
        return {"zones": {"wall": walls, "velocity_inlet": ["inlet"]}}

    def solve(self, iterations, initialize=True):
        self.calls.append(f"solve:{iterations}:{initialize}")
        return {}

    def _compute_reports(self, names):
        return {"lift_coef": self.cl_seq[self.it],
                "drag_coef": self.cd_seq[self.it]}

    def adjoint_setup(self, **kw):
        self.calls.append("adjoint_setup")
        self.setup_kw = kw
        if self.fail_setup:
            return {"applied": [], "failed": [
                {"step": "observable: downforce", "error": "Boom"}]}
        return {"applied": ["all"], "failed": [],
                "objective": {"name": "polish-objective",
                              "k": kw.get("drag_exchange_k")}}

    def adjoint_step(self):
        if self.hold is not None:
            self.hold.wait(timeout=10)
        self.it += 1
        self.calls.append(f"step:{self.it}")
        return {"reports": self._compute_reports([]), "wall_s": 0.5}

    def export_ascii(self, filename, quantities=None, location=None,
                     surfaces=None):
        self.calls.append(f"export:{Path(filename).name}")
        if location == "node" and surfaces:
            polys = self.morph_fn(self.it)
            nodes = np.vstack(polys)
            rng = np.random.default_rng(self.it)
            nodes = nodes[rng.permutation(len(nodes))]
            lines = ["nodenumber, x-coordinate, y-coordinate, "
                     "x-wall-shear, y-wall-shear"]
            for i, (x, y) in enumerate(nodes):
                lines.append(f"{i+1}, {x:.8e}, {y:.8e}, 1.5e0, 0.0e0")
            Path(filename).write_text("\n".join(lines) + "\n",
                                      encoding="utf-8")
            return {"written": str(filename), "rows": len(nodes)}
        plausible_field_csv(filename)
        return {"written": str(filename), "rows": 400}

    def write_case_data(self, stem):
        self.calls.append("write_case_data")
        p = Path(f"{stem}.cas.h5")
        p.write_bytes(b"CASH5")
        p.with_name("case.dat.h5").write_bytes(b"DATH5")
        return {"written": str(p)}

    def adjoint_interrupt(self):
        self.interrupts += 1
        return {"interrupted": True}

    def shutdown(self):
        self.shutdowns += 1
        return {"status": "exited"}


def run_job(fake, seed=None, options=None, wait=True):
    seed = seed or SeedStub()
    job = adjoint_run.AdjointPolishJob(seed, options or {})
    real = adjoint_run._mcp
    adjoint_run._mcp = lambda: fake
    try:
        if wait:
            job.run()
        return job
    finally:
        adjoint_run._mcp = real


# --- seed validation ---

for stub, why, frag in [
        (SeedStub(engine="openfoam"), "wrong engine", "ANSYS 2D"),
        (SeedStub(state="running"), "unfinished seed", "not finished"),
        (SeedStub(files=False), "missing case files", "case+data"),
        (SeedStub(result={"cl_rans": 2.0, "profiles_override": True}),
         "re-verify seed", "already-polished")]:
    try:
        adjoint_run.AdjointPolishJob(stub, {})
        check(f"seed: {why} refused", False)
    except ValueError as e:
        check(f"seed: {why} refused", frag in str(e), str(e))

try:
    adjoint_run.AdjointPolishJob(SeedStub(), {"design_iters": 0})
    check("seed: bad option refused at construction", False)
except ValueError:
    check("seed: bad option refused at construction", True)


# --- happy path: three improving iterations ---

CL = [2.10, 2.16, 2.20, 2.22, 2.225, 2.226]   # settle + steps
CD = [0.085, 0.0855, 0.086, 0.0862, 0.0863, 0.0863]
fake = FakeFM(CL, CD, lambda it: displaced(BASE_POLYS, 0.0003 * it))
job = run_job(fake, options={"design_iters": 3, "drag_exchange_k": 0.5})
r = job.result
check("happy: job is done with a result", job.state == "done"
      and r is not None, job.error or "")
check("happy: license released exactly once on the clean path",
      fake.shutdowns == 1)
check("happy: pipeline order — launch, read, settle, setup, steps",
      fake.calls[0] == "launch" and fake.calls[1] == "read_case_data"
      and fake.calls[2].startswith("solve:")
      and "adjoint_setup" in fake.calls
      and fake.calls.index("adjoint_setup") < fake.calls.index("step:1"))
check("happy: three design iterations ran and are in the history",
      r["iterations_run"] == 3 and len(job.history) == 3
      and job.history[-1]["iter"] == 3)
check("happy: improved, delivering the last (best) iterate",
      r["improved"] and r["delivered_iteration"] == 3)
b, p = r["baseline"], r["polished"]
check("happy: baseline is the settle solve's reports",
      abs(b["cl"] - 2.10) < 1e-9 and abs(b["cd"] - 0.085) < 1e-9)
q = CFG_OBJ.q_pa
area = CFG_OBJ.chord_m * (CFG_OBJ.span_mm / 1000.0)
df_want = q * area * 2.22 * CFG_OBJ.efficiency_3d
drag_want = abs(df_want) * 0.0862 / 2.22
check("happy: newtons follow the studio conversions",
      abs(p["downforce_n"] - round(df_want, 1)) < 0.11
      and abs(p["drag_n"] - round(drag_want, 1)) < 0.11)
check("happy: J = downforce - k*drag at the run's k",
      abs(p["j_n"] - round(df_want - 0.5 * drag_want, 1)) < 0.15)
check("happy: deltas are polished minus baseline",
      abs(r["delta"]["downforce_n"]
          - round(p["downforce_n"] - b["downforce_n"], 1)) < 0.11)
check("happy: rules re-checked and compliant on the delivered shape",
      r["rules"] is not None and r["rules"]["ok"])
check("happy: displacement stats ride the result",
      r["displacement"] is not None
      and abs(r["displacement"]["max_disp_mm"] - 0.9) < 0.1)
check("happy: session ends on the delivered shape — case + flow written",
      r["artifacts"]["session_is_delivered"]
      and r["artifacts"]["case_written"]
      and r["artifacts"]["flow_exported"]
      and "write_case_data" in fake.calls)
check("happy: polished profiles json written",
      (job.case_dir / "polished_profiles.json").is_file())
saved = json.loads((job.case_dir / "polished_profiles.json").read_text())
check("happy: saved profiles carry every element at the delivered morph",
      len(saved["elements"]) == len(BASE_POLYS)
      and saved["iteration"] == 3)
check("happy: wall verdict graded from the polished flow",
      isinstance(r["wall_verdict"], str) and "attached" in r["wall_verdict"])
check("happy: morphed-mesh caution always present",
      any("provisional" in c for c in r["cautions"]))
check("happy: region in result matches region_bounds and reached the "
      "session layer",
      r["region"]["x"] == fake.setup_kw["region_x"]
      and r["region"]["y"] == fake.setup_kw["region_y"])
check("happy: exchange rate handed to the observable builder",
      fake.setup_kw["drag_exchange_k"] == 0.5)
check("happy: step request handed to the session layer",
      fake.setup_kw["step_pct"] == 2.0)
check("happy: the baseline rides the snapshot as iteration 0",
      job.snapshot()["baseline"] is not None
      and job.snapshot()["baseline"]["iter"] == 0
      and abs(job.snapshot()["baseline"]["cl"] - 2.10) < 1e-9)
check("happy: no harvest row for a polish",
      not (Path(_TMP) / "harvest.jsonl").exists())
check("happy: engine and seed threading in the snapshot",
      job.snapshot()["engine"] == "polish"
      and job.snapshot()["seed_id"] == job.seed_id)

# --- rule boundary: second step thins the flap below the mfg floor
# (a small morph, well inside the attribution tolerance — a rule can be
# broken without the geometry going anywhere far) ---


def escaping(it):
    if it >= 2:
        return [BASE_POLYS[0], squashed(BASE_POLYS[1])]
    return displaced(BASE_POLYS, 0.0003 * it)


fake = FakeFM(CL, CD, escaping)
job = run_job(fake, options={"design_iters": 5})
r = job.result
check("rule-stop: loop ends at the violating iterate",
      job.state == "done" and r["iterations_run"] == 2
      and "violated min_thickness_mm" in r["stop_reason"],
      (job.error or "")[:200] + str(r and r.get("stop_reason")))
check("rule-stop: delivers the last compliant iterate",
      r["improved"] and r["delivered_iteration"] == 1)
check("rule-stop: history records the violation",
      job.history[-1]["compliant"] is False
      and job.history[0]["compliant"] is True)
check("rule-stop: session one step past delivery — no case/flow claims",
      not r["artifacts"]["session_is_delivered"]
      and not r["artifacts"]["case_written"]
      and not r["artifacts"]["flow_exported"]
      and "write_case_data" not in fake.calls)
check("rule-stop: the mismatch is stated as a caution",
      any("past the delivered shape" in c for c in r["cautions"]))
check("rule-stop: delivered profiles are the compliant morph",
      (job.case_dir / "polished_profiles.json").is_file()
      and json.loads((job.case_dir / "polished_profiles.json")
                     .read_text())["iteration"] == 1)

# --- the floor-tight seed: the FIRST morph violates, nothing gained
# (the measured live behavior on a seed whose flaps sit at the floor) ---

fake = FakeFM([2.10, 2.11, 2.12], [0.085] * 3,
              lambda it: [BASE_POLYS[0], squashed(BASE_POLYS[1])])
job = run_job(fake, options={"design_iters": 12})
r = job.result
check("floor-tight: no-gain verdict carries the first-morph diagnosis",
      job.state == "done" and not r["improved"]
      and r["stop_reason"].startswith("no compliant improvement")
      and "very first morph" in r["stop_reason"]
      and "smaller step request" in r["stop_reason"], r["stop_reason"])
check("floor-tight: no contradictory delivery clause on a no-gain run",
      "delivering" not in r["stop_reason"])
check("floor-tight: the baseline still rides the snapshot for the chart",
      job.snapshot()["baseline"] is not None
      and len(job.snapshot()["history"]) == 1)

# --- no compliant improvement: J only degrades ---

fake = FakeFM([2.10, 2.05, 2.00, 1.98],
              [0.085, 0.086, 0.087, 0.088],
              lambda it: displaced(BASE_POLYS, 0.0002 * it))
job = run_job(fake, options={"design_iters": 3})
r = job.result
check("no-gain: run completes with improved=False",
      job.state == "done" and not r["improved"])
check("no-gain: stop reason says so",
      r["stop_reason"].startswith("no compliant improvement"))
check("no-gain: overshoot guard ends the loop early",
      r["iterations_run"] == 2, str(r["iterations_run"]))
check("no-gain: no polished artifacts claimed",
      r["polished"] is None and r["delta"] is None
      and not (job.case_dir / "polished_profiles.json").exists())

# --- early stop: J flattens ---

fake = FakeFM([2.10, 2.20, 2.2001, 2.2001, 2.2001, 2.2001],
              [0.085] * 6,
              lambda it: displaced(BASE_POLYS, 0.0002 * it))
job = run_job(fake, options={"design_iters": 5})
r = job.result
check("early-stop: two flat iterations end the loop",
      r["iterations_run"] == 3 and "converged" in r["stop_reason"],
      f"{r['iterations_run']}: {r['stop_reason']}")

# --- setup failure ---

fake = FakeFM(CL, CD, lambda it: BASE_POLYS, fail_setup=True)
job = run_job(fake)
check("setup-fail: job fails with the fenced step named",
      job.state == "failed" and "observable: downforce" in job.error)
check("setup-fail: license released", fake.shutdowns == 1)

# --- reloaded case without the profile zone ---

fake = FakeFM(CL, CD, lambda it: BASE_POLYS, no_profile_zone=True)
job = run_job(fake)
check("zone-guard: a case without 'profile' fails with a clear message",
      job.state == "failed" and "profile" in job.error)
check("zone-guard: license released", fake.shutdowns == 1)

# --- launch raise ---

fake = FakeFM(CL, CD, lambda it: BASE_POLYS, launch_raises=True)
job = run_job(fake)
check("launch-raise: failure recorded, no license held",
      job.state == "failed" and "license seat" in job.error
      and fake.shutdowns == 0)

# --- settle mismatch caution + separated-seed caution ---

fake = FakeFM([1.80, 1.85, 1.9], [0.085] * 3,
              lambda it: displaced(BASE_POLYS, 0.0002 * it))
sep_seed = SeedStub(result={
    "cl_rans": 2.10, "cd_rans": 0.085, "converged": True,
    "sizing": "default", "conventions": "studio",
    "profiles_override": False,
    "field_verdict": {"separated": True, "verdict": "field: separated"}})
job = run_job(fake, seed=sep_seed, options={"design_iters": 2})
r = job.result
check("cautions: re-settled baseline far from the seed is stated",
      any("re-settled baseline" in c for c in r["cautions"]))
check("cautions: separated seed flow is stated",
      any("separation" in c for c in r["cautions"]))

# --- cancel mid-step ---

hold = threading.Event()
fake = FakeFM(CL, CD, lambda it: displaced(BASE_POLYS, 0.0003 * it),
              hold=hold)
seed = SeedStub()
job = adjoint_run.AdjointPolishJob(seed, {"design_iters": 4})
real = adjoint_run._mcp
adjoint_run._mcp = lambda: fake
try:
    t = threading.Thread(target=job.run)
    t.start()
    for _ in range(200):
        if any(c.startswith("step:") or c == "adjoint_setup"
               for c in fake.calls):
            break
        time.sleep(0.02)
    job.cancel()
    hold.set()
    t.join(timeout=10)
finally:
    adjoint_run._mcp = real
check("cancel: lands as cancelled, never failed",
      job.state == "cancelled", job.state)
check("cancel: optimizer interrupted and session shot",
      fake.interrupts >= 1 and fake.shutdowns >= 1)

# --- protected dirs: seed and polish dirs are provided while referenced ---

prot = adjoint_run._polish_dirs()
check("protected: live jobs pin their seed and case dirs",
      str(job.seed_dir) in prot and str(job.case_dir) in prot)

print(f"\n{sum(results)}/{len(results)} adjoint-run checks passed")
sys.exit(0 if all(results) else 1)
