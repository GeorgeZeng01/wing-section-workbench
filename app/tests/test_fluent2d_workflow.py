"""ANSYS 2D chain front half — everything that runs without ANSYS, a
license, Docker or a network: the walkthrough DXF writer (round-tripped
through the walkthrough DXF reader) and its domain proportions,
generated stage-script content (the spike's quirk pins), the chain
runner against a faked _popen seam (result-JSON verdicts, stage errors,
cancel kill), Fluent mesh parsing, installation discovery, and the
sizing modes.

Run directly:  python app/tests/test_fluent2d_workflow.py
"""
import json
import math
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scripts import fluent2d_workflow as w2  # noqa: E402
from scripts import fluent_workflow as wf    # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def oval(cx, cy, rx, ry, n=64):
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.column_stack([cx + rx * np.cos(t), cy + ry * np.sin(t)])


TMP = Path(tempfile.mkdtemp(prefix="wss_fl2d_"))

# ---- DXF writer: documented rectangle, ground row, round trip ----

# chord 0.35 m, lowest point 0.03 m above the ground plane
prof = oval(0.175, 0.05, 0.175, 0.02)
ret = w2.write_dxf_2d([prof], TMP / "one.dxf")
L = 0.35
H = 0.04
x0, y0, x1, y1 = ret["domain_m"]
# ceiling: three STACK heights above the ground (profile top 0.07 m
# above y = 0, ride height included) — the spike's walkthrough reading;
# the bbox-only reading tightened confinement by 3x the ride height
check("documented rectangle proportions: 3L front, 7L back, "
      "3-stack-H top",
      math.isclose(x0, 0.0 - 3 * L, rel_tol=1e-9)
      and math.isclose(x1, 0.35 + 7 * L, rel_tol=1e-9)
      and math.isclose(y1, 0.07 + 3 * 0.07, rel_tol=1e-9),
      f"(domain {tuple(round(v, 4) for v in ret['domain_m'])})")
check("the writer reports the proportions it used",
      ret["proportions"] == {"front_l": 3.0, "back_l": 7.0,
                             "top_h": 3.0})
# the defaults must reproduce the pre-parameterization rectangle
# BIT-EXACTLY: the literal 3.0/7.0/3.0 arithmetic, recomputed here
_xn, _xx = float(prof[:, 0].min()), float(prof[:, 0].max())
_yx = float(prof[:, 1].max())
_lx = _xx - _xn
check("default proportions reproduce the fixed rectangle bit-exactly",
      ret["domain_m"] == (_xn - 3.0 * _lx, 0.0, _xx + 7.0 * _lx,
                          _yx + 3.0 * _yx),
      f"({ret['domain_m']})")
ret_pin = w2.write_dxf_2d([prof], TMP / "pin.dxf", front_l=3.0,
                          back_l=7.0, top_h=3.0)
check("explicit 3/7/3 is identical to taking the defaults",
      ret_pin["domain_m"] == ret["domain_m"],
      f"({ret_pin['domain_m']})")
check("ground workflow pins the rectangle's bottom row at y = 0",
      y0 == 0.0)
check("writer reports its profile inventory",
      ret["n_profiles"] == 1 and ret["n_points"] == 64
      and ret["dxf_path"].endswith("one.dxf"))

geo = wf.read_dxf(ret["dxf_path"])
check("DXF round-trips through the walkthrough reader (profile + "
      "auto-detected rectangle)",
      len(geo["profiles"]) == 1 and geo["domain"] is not None,
      f"({len(geo['profiles'])} profiles)")
check("round trip preserves the mm units scale",
      abs(geo["scale"] - 1e-3) < 1e-12)
check("round-tripped domain matches the writer's report",
      geo["domain"] is not None
      and all(abs(a - b) < 1e-9
              for a, b in zip(geo["domain"], ret["domain_m"])))
rt = geo["profiles"][0]
check("round-tripped profile preserves the driven coordinates",
      len(rt) == 64
      and abs(rt[:, 0].min() - 0.0) < 1e-9
      and abs(rt[:, 0].max() - 0.35) < 1e-9
      and abs(rt[:, 1].max() - 0.07) < 1e-9)

# free air: bottom drops 3H below the profile instead of the ground row
ret_fa = w2.write_dxf_2d([prof], TMP / "free.dxf", ground=False)
check("ground=False drops the floor 3H below the profile",
      math.isclose(ret_fa["domain_m"][1], 0.03 - 3 * H, rel_tol=1e-9),
      f"(y0 {ret_fa['domain_m'][1]:.4f})")

# custom proportions move the rectangle, and only the rectangle
ret_c = w2.write_dxf_2d([prof], TMP / "custom.dxf", front_l=5.0,
                        back_l=12.0, top_h=4.0)
cx0, cy0, cx1, cy1 = ret_c["domain_m"]
check("custom proportions move the ground-mode rectangle as specified",
      math.isclose(cx0, 0.0 - 5 * L, rel_tol=1e-9)
      and math.isclose(cx1, 0.35 + 12 * L, rel_tol=1e-9)
      and math.isclose(cy1, 0.07 + 4 * 0.07, rel_tol=1e-9)
      and cy0 == 0.0
      and ret_c["proportions"] == {"front_l": 5.0, "back_l": 12.0,
                                   "top_h": 4.0},
      f"(domain {tuple(round(v, 4) for v in ret_c['domain_m'])})")
ret_cf = w2.write_dxf_2d([prof], TMP / "custom_free.dxf", ground=False,
                         top_h=4.0)
check("custom top_h mirrors free air above and below the bbox",
      math.isclose(ret_cf["domain_m"][1], 0.03 - 4 * H, rel_tol=1e-9)
      and math.isclose(ret_cf["domain_m"][3], 0.07 + 4 * H,
                       rel_tol=1e-9),
      f"(y {ret_cf['domain_m'][1]:.4f}..{ret_cf['domain_m'][3]:.4f})")

# range guards per the settings contract (0.5..20 / 0.5..40 / 0.5..20)
for kw, label in ((("front_l", 0.4), "front_l below 0.5"),
                  (("front_l", 20.5), "front_l above 20"),
                  (("back_l", 0.2), "back_l below 0.5"),
                  (("back_l", 41.0), "back_l above 40"),
                  (("top_h", 0.1), "top_h below 0.5"),
                  (("top_h", 25.0), "top_h above 20"),
                  (("top_h", float("nan")), "a non-finite top_h")):
    try:
        w2.write_dxf_2d([prof], TMP / "range.dxf", **{kw[0]: kw[1]})
        check(f"{label} is rejected", False)
    except ValueError:
        check(f"{label} is rejected", True)

# a profile touching or crossing the ground cannot fill an annulus
try:
    w2.write_dxf_2d([oval(0.1, 0.0, 0.1, 0.02)], TMP / "bad.dxf")
    check("profile at/below the ground plane is rejected", False)
except ValueError:
    check("profile at/below the ground plane is rejected", True)
try:
    w2.write_dxf_2d([], TMP / "none.dxf")
    check("empty profile list is rejected", False)
except ValueError:
    check("empty profile list is rejected", True)

# ---- multi-element: 3 profiles -> 4 closed LWPOLYLINEs ----

three = [oval(0.15, 0.06, 0.15, 0.02),
         oval(0.34, 0.09, 0.05, 0.010),
         oval(0.42, 0.12, 0.03, 0.006)]
ret3 = w2.write_dxf_2d(three, TMP / "three.dxf")
import ezdxf  # noqa: E402
doc3 = ezdxf.readfile(TMP / "three.dxf")
ents = [e for e in doc3.modelspace() if e.dxftype() == "LWPOLYLINE"]
check("three-element DXF writes 4 closed LWPOLYLINEs",
      len(ents) == 4 and all(e.closed for e in ents)
      and ret3["n_profiles"] == 3,
      f"({len(ents)} polylines)")
geo3 = wf.read_dxf(TMP / "three.dxf")
check("three-element DXF round-trips (3 profiles + domain)",
      len(geo3["profiles"]) == 3 and geo3["domain"] is not None)

# ---- sizing modes ----

check("mesh_sizing('default') is the walkthrough's exact numbers",
      w2.mesh_sizing("default", None) == {"edge_size_mm": 0.1,
                                          "first_layer_mm": 1.0,
                                          "n_layers": 10, "growth": 1.2})
for bad in ("resolved", "team"):
    try:
        w2.mesh_sizing(bad, None)
        check(f"sizing mode {bad!r} is rejected", False)
    except ValueError:
        check(f"sizing mode {bad!r} is rejected", True)

from app.core import cfd  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402
cfg_d = {"chord_mm": 350, "speed_ms": 15}
h1_m, _ = cfd.first_layer(StackConfig.from_dict(cfg_d))
sz = w2.mesh_sizing("studio-yplus1", cfg_d)
check("studio-yplus1 first layer follows the app's y+ ~ 1 correlation",
      abs(sz["first_layer_mm"] - h1_m * 1e3) < 1e-12
      and sz["first_layer_mm"] < 0.5,
      f"(h1 {sz['first_layer_mm']:.4f} mm)")
check("studio-yplus1 edge size is the fine preset's wall fraction",
      abs(sz["edge_size_mm"] - 0.7) < 1e-12
      and sz["n_layers"] == 30 and sz["growth"] == 1.2)

# ---- installation discovery (hermetic: env is faked) ----

_saved_env = {k: os.environ[k] for k in list(os.environ)
              if k.startswith("AWP_ROOT")}
for k in _saved_env:
    del os.environ[k]

av0 = w2.availability()
check("no AWP_ROOT: unavailable with a user-facing detail",
      av0["available"] is False and "ANSYS" in av0["detail"]
      and av0["awp_root"] == "" and av0["version"] == "")
check("availability detail carries no repo paths",
      str(ROOT) not in av0["detail"]
      and "fluent2d_workflow" not in av0["detail"]
      and "scripts/" not in av0["detail"])


def _fake_install(name):
    root = TMP / name
    for rel in (("scdm", "SpaceClaim.exe"),
                ("Framework", "bin", "Win64", "RunWB2.exe")):
        p = root.joinpath(*rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    return root


root251 = _fake_install("awp251")
root261 = _fake_install("awp261")
os.environ["AWP_ROOT251"] = str(root251)
av1 = w2.availability()
check("a complete AWP_ROOT251 install is discovered",
      av1["available"] is True and av1["version"] == "251"
      and av1["awp_root"] == str(root251) and av1["detail"] == "")
os.environ["AWP_ROOT261"] = str(root261)
check("AWP_ROOT261 is preferred when present",
      w2.availability()["version"] == "261")
# a 261 root missing its executables falls back to the next complete one
os.environ["AWP_ROOT261"] = str(TMP / "awp261_empty")
(TMP / "awp261_empty").mkdir(exist_ok=True)
av2 = w2.availability()
check("incomplete preferred root falls back to a complete install",
      av2["available"] is True and av2["version"] == "251")
os.environ["AWP_ROOT261"] = str(root261)

# ---- parse_msh_zones on a synthetic 2D mesh ----

ALL_ZONES = ("interior-fluid", "fluid", "inlet", "outlet", "ground",
             "upper_bound", "profile")
_ZTYPE = {"interior-fluid": "interior", "fluid": "fluid",
          "inlet": "velocity-inlet", "outlet": "pressure-outlet"}


def write_msh(path, n_cells=8000, zones=ALL_ZONES):
    lines = ['(0 "synthetic 2D mesh for the offline suite")',
             "(2 2)",
             "(10 (0 1 %x 0))" % (n_cells + 200),
             "(12 (0 1 %x 0))" % n_cells,
             "(13 (0 1 %x 0))" % (2 * n_cells),
             "(12 (4 1 %x 1 0))" % n_cells]
    for i, z in enumerate(zones):
        lines.append("(45 (%d %s %s)())" % (i + 1,
                                            _ZTYPE.get(z, "wall"), z))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


msh_direct = TMP / "direct.msh"
write_msh(msh_direct, n_cells=0x1F40)
pz = w2.parse_msh_zones(msh_direct)
check("parse_msh_zones: 2D header, hex cell count, all zone names",
      pz["dimension"] == 2 and pz["n_cells"] == 8000
      and set(w2.REQUIRED_ZONES) <= set(pz["zones"])
      and "interior-fluid" in pz["zones"],
      f"({pz['n_cells']} cells, {len(pz['zones'])} zones)")

# ---- run_chain against the faked _popen seam ----


class FakeProc:
    def __init__(self, rc=0, on_poll=None):
        self.pid = 424242
        self.killed = False
        self._rc = rc
        self._on_poll = on_poll
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self._on_poll is not None:
            self._on_poll(self)
        return self._rc

    def kill(self):
        self.killed = True
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


class ChainFake:
    """Stands in for _popen: plays SpaceClaim/RunWB2 by writing the
    stage results the chain validates. Scenario picks the failure."""

    def __init__(self, work, scenario="ok", on_poll=None):
        self.work = Path(work)
        self.scenario = scenario
        self.on_poll = on_poll
        self.calls = []
        self.procs = []

    def _sc_ok(self):
        (self.work / "fluid.scdocx").write_bytes(b"scdocx")
        (self.work / "sc_result.json").write_text(json.dumps(
            {"status": "ok",
             "groups": {"inlet": 1, "outlet": 1, "ground": 1,
                        "upper_bound": 1, "profile": 64}}))

    def _wb_ok(self):
        msh = self.work / "wb" / "proj_files" / "dp0" / "FFF" / "MECH" \
            / "FFF.msh"
        zones = ALL_ZONES if self.scenario != "bad-zones" \
            else ("fluid", "inlet", "outlet")
        n = 100 if self.scenario == "count-mismatch" else 8000
        write_msh(msh, n_cells=n, zones=zones)
        (self.work / "mech_result.json").write_text(json.dumps(
            {"status": "ok", "ns": {"profile": 64},
             "mesh": {"nodes": 8123, "elements": 8000}}))
        (self.work / "wb_result.json").write_text(json.dumps(
            {"status": "ok", "msh": str(msh),
             "msh_bytes": msh.stat().st_size}))

    def __call__(self, cmd, **kw):
        self.calls.append([str(c) for c in cmd])
        exe = str(cmd[0]).lower()
        if "taskkill" in exe:
            p = FakeProc(rc=0)
        elif "spaceclaim" in exe:
            if self.scenario == "hang":
                p = FakeProc(rc=None, on_poll=self.on_poll)
            elif self.scenario == "sc-error":
                (self.work / "sc_result.json").write_text(json.dumps(
                    {"status": "error: the DXF import yielded no "
                               "curves"}))
                p = FakeProc(rc=0)
            elif self.scenario == "sc-silent":
                p = FakeProc(rc=0)
            else:
                self._sc_ok()
                p = FakeProc(rc=0)
        elif "runwb2" in exe:
            if self.scenario == "wb-hang":
                p = FakeProc(rc=None, on_poll=self.on_poll)
            elif self.scenario == "wb-empty-once" and not any(
                    "runwb2" in e for e, _ in self.procs):
                # first WB pass: Mechanical reports an empty mesh (the
                # live inflation-collapse signature); retry must follow
                (self.work / "mech_result.json").write_text(json.dumps(
                    {"status": "error: mesh generated 0 elements (the "
                     "sizing or inflation does not fit this geometry)",
                     "mesh": {"nodes": 0, "elements": 0}}))
                (self.work / "wb_result.json").write_text(json.dumps(
                    {"status": "ok", "msh": ""}))
                p = FakeProc(rc=0)
            else:
                self._wb_ok()
                p = FakeProc(rc=0)
        else:
            p = FakeProc(rc=0)
        self.procs.append((exe, p))
        return p


_real_popen = w2._popen
_real_poll_s = w2._POLL_S
dxf_run = ret["dxf_path"]
try:
    # success path
    work_ok = TMP / "run_ok"
    fake = ChainFake(work_ok)
    w2._popen = fake
    res = w2.run_chain(dxf_run, work_ok)
    check("run_chain succeeds against the seam and returns the mesh "
          "contract",
          res["msh_path"].endswith("FFF.msh") and res["n_cells"] == 8000
          and set(w2.REQUIRED_ZONES) <= set(res["zones"])
          and set(res["stage_s"]) == {"spaceclaim", "workbench"}
          and res["project_dir"] == str(work_ok / "wb"),
          f"({res['n_cells']} cells)")
    check("both stages launched through the seam (SpaceClaim then "
          "RunWB2, batch flags)",
          any("spaceclaim" in c[0].lower() and "/Headless=True" in c
              for c in fake.calls)
          and any("runwb2" in c[0].lower() and "-B" in c and "-R" in c
                  for c in fake.calls))

    # generated stage scripts carry the default recipe and the quirk
    # pins
    mech_txt = (work_ok / "mech_mesh.py").read_text(encoding="utf-8")
    check("mech script pins the default recipe: 0.1 mm edges, 1 mm "
          "first layer, 10 layers",
          'Quantity("0.1 [mm]")' in mech_txt
          and '"1 [mm]")' in mech_txt
          and "MaximumLayers = 10" in mech_txt)
    check("mech script sets InflationOption = 2 (first layer "
          "thickness, quirk 10)",
          "InflationOption = 2" in mech_txt)
    # studio sizing through run_chain itself, not a bare _fill re-test
    work_st = TMP / "run_studio"
    w2._popen = ChainFake(work_st)
    w2.run_chain(dxf_run, work_st, edge_size_mm=0.7,
                 first_layer_mm=0.0421, n_layers=30, growth=1.2)
    stud_txt = (work_st / "mech_mesh.py").read_text(encoding="utf-8")
    check("studio sizing lands in the generated script via run_chain",
          '"0.7 [mm]"' in stud_txt and '"0.0421 [mm]"' in stud_txt
          and "MaximumLayers = 30" in stud_txt)
    w2._popen = fake
    wbjn_txt = (work_ok / "mesh.wbjn").read_text(encoding="utf-8")
    check("wbjn pins 2D before the geometry attach (quirk 2)",
          "AnalysisType_2D" in wbjn_txt and "SetFile" in wbjn_txt
          and wbjn_txt.index("AnalysisType_2D")
          < wbjn_txt.index("SetFile"))
    check("wbjn uses the plain Fluid Flow template (quirk 1)",
          'TemplateName="Fluid Flow"' in wbjn_txt
          and "Solver=" not in wbjn_txt)
    check("wbjn imports named selections (quirk 3)",
          "GeometryImportNamedSelections = True" in wbjn_txt)
    sc_txt = (work_ok / "sc_build.py").read_text(encoding="utf-8")
    check("sc script fills, sweeps datum-plane curves, saves .scdocx "
          "(quirks 4/5/7)",
          "Fill.Execute" in sc_txt and "DatumPlanes" in sc_txt
          and "fluid.scdocx" in sc_txt.replace("\\\\", "/").replace(
              "\\", "/"))

    # stage failure: the result JSON is the verdict, exit code 0 is not
    work_err = TMP / "run_err"
    w2._popen = ChainFake(work_err, scenario="sc-error")
    try:
        w2.run_chain(dxf_run, work_err)
        check("stage error raises Fluent2DError naming the stage", False)
    except w2.Fluent2DError as e:
        check("stage error raises Fluent2DError naming the stage",
              e.stage == "spaceclaim" and "spaceclaim" in str(e)
              and "DXF" in e.detail)

    work_silent = TMP / "run_silent"
    w2._popen = ChainFake(work_silent, scenario="sc-silent")
    try:
        w2.run_chain(dxf_run, work_silent)
        check("a stage that exits 0 without a result JSON fails "
              "(quirk 13)", False)
    except w2.Fluent2DError as e:
        check("a stage that exits 0 without a result JSON fails "
              "(quirk 13)",
              e.stage == "spaceclaim" and "result" in e.detail)

    # mesh validation: zones and cell count, never file existence
    work_bz = TMP / "run_badzones"
    w2._popen = ChainFake(work_bz, scenario="bad-zones")
    try:
        w2.run_chain(dxf_run, work_bz)
        check("missing zone names fail the workbench stage", False)
    except w2.Fluent2DError as e:
        check("missing zone names fail the workbench stage",
              e.stage == "workbench" and "ground" in e.detail
              and "profile" in e.detail)

    work_cm = TMP / "run_mismatch"
    w2._popen = ChainFake(work_cm, scenario="count-mismatch")
    try:
        w2.run_chain(dxf_run, work_cm)
        check("cell count mismatched to Mechanical's fails (default-"
              "mesh guard)", False)
    except w2.Fluent2DError as e:
        check("cell count mismatched to Mechanical's fails (default-"
              "mesh guard)",
              e.stage == "workbench" and "cell count" in e.detail)

    # cancel mid-stage: process tree killed, cancel signalled
    work_cn = TMP / "run_cancel"
    evt = threading.Event()

    def _arm(proc):
        if proc.polls >= 2:
            evt.set()

    fake_cn = ChainFake(work_cn, scenario="hang", on_poll=_arm)
    w2._popen = fake_cn
    w2._POLL_S = 0.01
    try:
        w2.run_chain(dxf_run, work_cn, cancel_evt=evt)
        check("cancel mid-stage raises the cancel signal", False)
    except w2.Fluent2DError as e:
        check("cancel mid-stage raises the cancel signal",
              e.cancelled and e.stage == "spaceclaim"
              and e.detail.startswith("cancelled"))
    hung = next(p for exe, p in fake_cn.procs if "spaceclaim" in exe)
    check("cancel kills the stage process tree (taskkill /T + kill)",
          hung.killed
          and any(c[0].lower() == "taskkill" and "/T" in c
                  and str(hung.pid) in c for c in fake_cn.calls))

    # cancel during the WORKBENCH stage: same contract as SpaceClaim
    work_wc = TMP / "run_wb_cancel"
    evt_w = threading.Event()

    def _arm_w(proc):
        if proc.polls >= 2:
            evt_w.set()

    fake_wc = ChainFake(work_wc, scenario="wb-hang", on_poll=_arm_w)
    w2._popen = fake_wc
    try:
        w2.run_chain(dxf_run, work_wc, cancel_evt=evt_w)
        check("cancel mid-Workbench raises the cancel signal", False)
    except w2.Fluent2DError as e:
        check("cancel mid-Workbench raises the cancel signal",
              e.cancelled and e.stage == "workbench")
    hung_w = next(p for exe, p in fake_wc.procs if "runwb2" in exe)
    check("Workbench cancel kills the stage process tree",
          hung_w.killed
          and any(c[0].lower() == "taskkill" and str(hung_w.pid) in c
                  for c in fake_wc.calls))

    # Workbench budget timeout: honest error, tree killed
    work_wt = TMP / "run_wb_timeout"
    fake_wt = ChainFake(work_wt, scenario="wb-hang")
    w2._popen = fake_wt
    try:
        w2.run_chain(dxf_run, work_wt, wb_budget_s=0.05)
        check("Workbench budget timeout raises with the stage named",
              False)
    except w2.Fluent2DError as e:
        check("Workbench budget timeout raises with the stage named",
              e.stage == "workbench" and "timed out" in e.detail)
    hung_t = next(p for exe, p in fake_wt.procs if "runwb2" in exe)
    check("Workbench timeout kills the stage process tree",
          hung_t.killed)

    # the SpaceClaim budget is honored on its own stage
    work_sct = TMP / "run_sc_timeout"
    fake_sct = ChainFake(work_sct, scenario="hang")
    w2._popen = fake_sct
    try:
        w2.run_chain(dxf_run, work_sct, sc_budget_s=0.05)
        check("SpaceClaim budget timeout raises with the stage named",
              False)
    except w2.Fluent2DError as e:
        check("SpaceClaim budget timeout raises with the stage named",
              e.stage == "spaceclaim" and "timed out" in e.detail)

    # empty-mesh inflation collapse: ONE retry without inflation,
    # recorded as a degradation, never a silent success
    work_dg = TMP / "run_degrade"
    fake_dg = ChainFake(work_dg, scenario="wb-empty-once")
    w2._popen = fake_dg
    res_dg = w2.run_chain(dxf_run, work_dg)
    wb_runs = [c for c in fake_dg.calls if "runwb2" in c[0].lower()]
    inf_dg = res_dg["inflation"]
    check("empty-mesh collapse retries exactly once without inflation",
          len(wb_runs) == 2 and inf_dg["degraded"] is True
          and inf_dg["n_layers"] == 0,
          f"({len(wb_runs)} runs, {inf_dg})")
    check("the degradation is recorded honestly in the note",
          "WITHOUT boundary layers" in (inf_dg["note"] or ""))
    mech_retry = (work_dg / "mech_mesh.py").read_text(encoding="utf-8")
    check("the retry script really disables inflation",
          "if 0 > 0:" in mech_retry)

    # input validation stays in front of any launch
    try:
        w2.run_chain(dxf_run, TMP / "run_v", edge_size_mm=0.0)
        check("non-positive edge size is rejected", False)
    except ValueError:
        check("non-positive edge size is rejected", True)
    try:
        w2.run_chain(TMP / "nothing.dxf", TMP / "run_v2")
        check("missing DXF is rejected before any stage launches", False)
    except ValueError:
        check("missing DXF is rejected before any stage launches", True)
finally:
    w2._popen = _real_popen
    w2._POLL_S = _real_poll_s
    for k in list(os.environ):
        if k.startswith("AWP_ROOT"):
            del os.environ[k]
    os.environ.update(_saved_env)

# ---- inflation clearance cap ----
# the default sizing (1 mm x 10 at growth 1.2 = ~26 mm of layers)
# collapsed Mechanical's whole generation to 0 elements inside a
# 5.25 mm slot gap (two opposing fronts). Slot budget 0.4 x gap per
# front mirrors the studio's cfd.BL_CLEAR_FRAC share; ground clearance
# carries one front and only guards outright overlap (0.9x) — the
# proven single-element walkthrough case (26 mm in a 30 mm ride height)
# must stay uncapped.

fl, n, note = w2._cap_inflation(1.0, 10, 1.2, None, 30.0)
check("single-element walkthrough case stays uncapped at 30 mm ride "
      "height",
      (fl, n, note) == (1.0, 10, None), f"({fl}, {n}, {note})")
fl, n, note = w2._cap_inflation(1.0, 10, 1.2, 7.0, 30.0)
check("7 mm slot gap caps 10 layers to 2 (parity with the slab route)",
      fl == 1.0 and n == 2 and note, f"({fl}, {n})")
fl, n, note = w2._cap_inflation(1.0, 10, 1.2, 5.25, 30.0)
check("5.25 mm slot gap (the live failure) caps to 1 layer",
      fl == 1.0 and n == 1 and note, f"({fl}, {n})")
fl, n, note = w2._cap_inflation(1.0, 10, 1.2, 0.5, 30.0)
check("sub-layer slot gap shrinks the first layer itself",
      fl < 1.0 and n == 1 and "first layer" in (note or ""),
      f"({fl}, {n})")
fl, n, note = w2._cap_inflation(1.0, 10, 1.2, None, None)
check("no clearances given -> no cap", (fl, n, note) == (1.0, 10, None))

# the zero-elements guard must live in the generated Mechanical script
check("mech template fails a 0-element mesh honestly",
      "mesh generated 0 elements" in w2._MECH_TEMPLATE)

# ---- IronPython ASCII contract ----
# Both ANSYS script hosts run IronPython 2.7, where a non-ASCII byte in
# a script without a coding line is a COMPILE error that SendCommand
# swallows silently (found live: an em dash in a template comment cost
# a full meshing chain — the run failed minutes later on the
# default-mesh guard with no visible cause).

for name, tpl in (("SpaceClaim", w2._SC_TEMPLATE),
                  ("Mechanical", w2._MECH_TEMPLATE),
                  ("Workbench", w2._WBJN_TEMPLATE)):
    ok = True
    try:
        tpl.encode("ascii")
    except UnicodeEncodeError as e:
        ok = False
        extra = f"(offset {e.start}: {tpl[e.start:e.start + 20]!r})"
    check(f"{name} template is pure ASCII", ok,
          "" if ok else extra)

try:
    w2._fill("x = 1  # em dash — poisons IronPython", DUMMY="y")
    check("_fill refuses a non-ASCII generated script", False)
except w2.Fluent2DError as e:
    check("_fill refuses a non-ASCII generated script",
          e.stage == "generate")

print(f"\n{sum(results)}/{len(results)} fluent2d-workflow checks passed")
sys.exit(0 if all(results) else 1)
