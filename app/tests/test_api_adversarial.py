"""Adversarial API regression: hostile payloads must 422 (never 500/hang),
sane payloads must keep working, and the stack-angle sign convention must
hold.

Run through run_all.py, which boots a throwaway scratch server. The suite
MUTATES server state (it uploads airfoils and warms caches), so do not point
it at a live working session; if you must run it standalone, start a
dedicated server and set WSS_TEST_BASE to it:
    .venv\\Scripts\\python.exe -m uvicorn app.server:app --port 8652
    set WSS_TEST_BASE=http://127.0.0.1:8652
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.environ.get("WSS_TEST_BASE", "http://127.0.0.1:8642")


def call(method, path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None


GOOD = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
         "dx": -0.03, "dy": -0.03}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}

results = []


def check(label, ok, extra=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


# happy path still works
t0 = time.time()
s, r = call("POST", "/api/analyze", {"config": GOOD})
check("analyze happy path", s == 200, f"({time.time()-t0:.1f}s)")

# sign fix: +4 deg must beat -1 deg downforce
_, r_hi = call("POST", "/api/analyze", {"config": {**GOOD, "stack_aoa_deg": 4}})
_, r_lo = call("POST", "/api/analyze", {"config": {**GOOD, "stack_aoa_deg": -1}})
check("stack angle sign (+4 > -1 downforce)",
      r_hi["forces"]["downforce_n"] > r_lo["forces"]["downforce_n"],
      f"({r_hi['forces']['downforce_n']} vs {r_lo['forces']['downforce_n']} N)")

# element as string -> 422 not 500
s, _ = call("POST", "/api/geometry", {"config": {**GOOD, "elements": ["hello"]}})
check("string element -> 422", s == 422, f"(got {s})")

# NaN chord -> 422
s, _ = call("POST", "/api/geometry",
            {"config": {**GOOD, "chord_mm": float("nan")}})
check("NaN chord -> 422", s == 422, f"(got {s})")

# giant panel count -> 422 fast
t0 = time.time()
s, _ = call("POST", "/api/geometry",
            {"config": {**GOOD, "n_panels_per_side": 100000}}, timeout=10)
check("n_panels 100000 -> 422 fast", s == 422 and time.time() - t0 < 5,
      f"(got {s} in {time.time()-t0:.1f}s)")

# absurd alpha span -> 422 fast
t0 = time.time()
s, _ = call("POST", "/api/polar",
            {"spec": "s1223", "re": 3e5, "alpha_start": -1e7,
             "alpha_stop": 1e7, "alpha_step": 0.06}, timeout=10)
check("huge alpha range -> 422 fast", s == 422 and time.time() - t0 < 5,
      f"(got {s} in {time.time()-t0:.1f}s)")

# inverted alpha range -> 422
s, _ = call("POST", "/api/polar",
            {"spec": "s1223", "re": 3e5, "alpha_start": 10, "alpha_stop": -10})
check("inverted alpha range -> 422", s == 422, f"(got {s})")

# ncrit out of band -> 422
s, _ = call("POST", "/api/analyze", {"config": {**GOOD, "ncrit": 1000}})
check("ncrit 1000 -> 422", s == 422, f"(got {s})")

# bogus export frame -> 422
s, _ = call("POST", "/api/export/csv", {"config": GOOD, "frame": "bogus"})
check("bogus export frame -> 422", s == 422, f"(got {s})")

# manufacturing prep: happy path analyzes, treated TE lands in the geometry
s, r = call("POST", "/api/analyze", {"config": {
    **GOOD, "manufacturing": {"te_gap_mm": 1.5, "te_mode": "thicken",
                              "min_thickness_mm": 4}}})
mfg_ok = (s == 200
          and all(e["airfoil_eff"].startswith("mfg:") for e in r["elements"])
          and all(el["mfg"]["te_gap_mm"] >= 1.45
                  for el in r["geometry"]["design"]))
check("manufacturing prep analyze -> 200, as-built TE >= 1.5 mm", mfg_ok,
      f"(got {s}, {r['forces']['downforce_n'] if s == 200 else '-'} N)")

# hostile manufacturing payloads -> 422 not 500
s, _ = call("POST", "/api/analyze", {"config": {
    **GOOD, "manufacturing": {"te_mode": "vshape"}}})
check("bogus te_mode -> 422", s == 422, f"(got {s})")
s, _ = call("POST", "/api/analyze", {"config": {
    **GOOD, "manufacturing": {"te_gap_mm": float("nan")}}})
check("NaN te_gap_mm -> 422", s == 422, f"(got {s})")

# stack .dat round-trip: large-x first coordinate must NOT be re-stitched
import numpy as np  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core import airfoils, export, geometry  # noqa: E402
cfg3 = geometry.StackConfig.from_dict({
    "elements": [{"airfoil": "s1223"},
                 {"airfoil": "s1223", "chord_ratio": 0.40, "deflection_deg": 20},
                 {"airfoil": "s1223", "chord_ratio": 0.30, "deflection_deg": 35,
                  "dx": -0.02, "dy": -0.02}],
    "chord_mm": 350, "ride_height_mm": 30,
})
inst = geometry.install_stack(geometry.build_stack(cfg3), cfg3.ride_height_c)
flap2 = inst[2]["coords"]
dat = export.dat_text(flap2, "flap2 installed")
_, back = airfoils.read_dat(dat, "roundtrip")
check("stack .dat round-trip exact",
      len(back) == len(flap2) and np.allclose(back, flap2, atol=1e-6),
      f"(first x = {flap2[0][0]:.3f})")

# real Lednicer file still parses (synthetic count-pair header)
xs = [0.0, 0.1, 0.3, 0.5, 0.8, 1.0]
up = "\n".join(f"{x} {0.1*(1-x)*x*4+0.001:.4f}" for x in xs)
lo = "\n".join(f"{x} {-0.08*(1-x)*x*4-0.001:.4f}" for x in xs)
led = f"test foil\n 6. 6.\n{up}\n{lo}\n"
_, lc = airfoils.read_dat(led, "led")
check("Lednicer re-stitch still works",
      len(lc) == 11 and lc[0][0] == 1.0 and lc[-1][0] == 1.0)

# screener cache includes custom registry state
scr_body = {"re": 2e5, "ncrit": 7, "cl_ref": 1.5, "thickness_pct_max": 8,
            "include_low_confidence": True}
s, r1 = call("POST", "/api/screen", scr_body, timeout=300)
n1 = r1["count"]
# unique header name AND geometry each run: uploads deduplicate by
# name+coords server-side, and normalization cancels uniform shifts
uniq = f"cachecheck-{uuid.uuid4().hex[:8]}"
base_coords = airfoils.repaneled("naca0006", 40)[1].copy()
k = len(base_coords) // 3
base_coords[k, 1] *= 1.0 + (uuid.uuid4().int % 50 + 1) * 1e-4
dat_up = uniq + "\n" + "\n".join(
    f" {x:.6f} {y:.6f}" for x, y in base_coords) + "\n"
s2, up = call("POST", "/api/airfoils/upload", {"name": uniq,
                                               "dat_text": dat_up})
s3, r2 = call("POST", "/api/screen", scr_body, timeout=300)
new_spec = up["spec"] if s2 == 200 else None
check("screener sees new upload",
      s2 == 200 and r2["count"] == n1 + 1
      and any(row["spec"] == new_spec for row in r2["rows"]),
      f"({n1} -> {r2['count']}, spec {new_spec})")

# optimizer with airfoil selection: from a mediocre baseline it must pick
# stronger sections from the shortlist and land on the target
af_cfg = {
    "elements": [
        {"airfoil": "naca4412", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "naca4412", "chord_ratio": 0.35, "deflection_deg": 24,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9, "span_efficiency": 0.9,
}
# 300 N sits inside the model's validity envelope (realized ground loading
# under the 2x allowance); the optimizer now honestly refuses targets that
# need the penalized past-stall regime, so an unreachable-honestly target
# would report a miss rather than a fake hit
s_start, job = call("POST", "/api/optimize", {"config": af_cfg, "options": {
    "target_downforce_n": 300, "mode": "global", "budget": 900,
    "opt_stack_aoa": True, "opt_deflections": True, "opt_positions": True,
    "opt_airfoils": True}})
jid = job["job_id"]
t0 = time.time()
while True:
    time.sleep(1.5)
    _, s = call("GET", f"/api/optimize/{jid}")
    if s["state"] in ("done", "failed", "cancelled") or time.time() - t0 > 240:
        break
# a failed/hung job must report FAIL with its error, not crash the harness
best = s.get("best") or {}
af_ok = (s["state"] == "done"
         and abs(best.get("downforce_n", 1e9) - 300) < 6
         and best.get("penalty", 1) < 0.5
         and isinstance(best.get("airfoils"), list)
         and all(isinstance(a, str) and a for a in best["airfoils"])
         and [e["airfoil"] for e in s["best_config"]["elements"]]
             == best["airfoils"])
check("optimizer airfoil selection", af_ok,
      f"({s['state']} {best.get('downforce_n')} N in {s['elapsed_s']}s, "
      f"picked {best.get('airfoils')}, error={s.get('error')})")

# the finished job must expose a candidate set alongside the best design
cands = s.get("candidates") or []
check("optimizer returns candidate designs",
      s["state"] == "done" and 1 <= len(cands) <= 6
      and all(c.get("config", {}).get("elements") for c in cands),
      f"({len(cands)} candidates)")

# export save-to-disk: reports a real file inside the exports folder
s_sv, sv = call("POST", "/api/export/dxf/save",
                {"config": GOOD, "frame": "installed", "entity": "spline"})
from pathlib import Path as _P  # noqa: E402
save_ok = (s_sv == 200 and sv and _P(sv["path"]).exists()
           and _P(sv["path"]).stat().st_size == sv["size_bytes"]
           and _P(sv["dir"]) in _P(sv["path"]).parents)
check("export save writes a real file and reports its location", save_ok,
      f"({sv.get('filename') if sv else s_sv})")
if save_ok:
    _P(sv["path"]).unlink()   # keep the test run tidy

# reveal is locked to the exports folder
s_rv, _ = call("POST", "/api/export/reveal",
               {"path": "C:/Windows/System32/drivers/etc/hosts"})
check("reveal outside exports folder -> 403", s_rv == 403, f"(got {s_rv})")
s_rv, _ = call("POST", "/api/export/reveal",
               {"path": (sv["dir"] + "/does-not-exist.dxf") if sv else "x"})
check("reveal missing file -> 404", s_rv == 404, f"(got {s_rv})")

# ---- error-path consistency: unresolvable specs are client errors ----

bad_cfg = {**GOOD, "elements": [{"airfoil": "no-such-foil-xyz",
                                 "chord_ratio": 1.0}]}
s_an, r_an = call("POST", "/api/analyze", {"config": bad_cfg})
detail = (r_an or {}).get("detail", "")
check("unknown airfoil analyze -> 422 (not 500)", s_an == 422, f"(got {s_an})")
check("error detail is clean text (no KeyError quotes)",
      isinstance(detail, str) and not detail.startswith(("'", '"')),
      f"({detail[:60]!r})")
s_ex, _ = call("POST", "/api/export/csv", {"config": bad_cfg})
check("unknown airfoil export -> 422 (not 500)", s_ex == 422, f"(got {s_ex})")
s_op, _ = call("POST", "/api/optimize", {"config": bad_cfg, "options": {}})
check("unknown airfoil optimize -> 422 up front", s_op == 422, f"(got {s_op})")

# malformed optimizer options are client errors, not background-job failures
for label, opts in (("budget string", {"budget": "lots"}),
                    ("bogus mode", {"mode": "psychic"}),
                    ("malformed bounds", {"bounds": {"stack_aoa_deg": "x"}}),
                    ("unknown bounds key", {"bounds": {"warp_factor": [0, 9]}})):
    s_o, _ = call("POST", "/api/optimize", {"config": GOOD, "options": opts})
    check(f"optimizer options: {label} -> 422", s_o == 422, f"(got {s_o})")

# single-element optimization is legal (stack angle alone is a variable)
s_1e, job1 = call("POST", "/api/optimize", {
    "config": {**GOOD, "elements": [GOOD["elements"][0]]},
    "options": {"target_downforce_n": 120, "mode": "local", "budget": 120,
                "opt_stack_aoa": True, "opt_deflections": False,
                "opt_positions": False}})
check("single-element optimize accepted", s_1e == 200, f"(got {s_1e})")
if s_1e == 200:
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(1.0)
        _, s1 = call("GET", f"/api/optimize/{job1['job_id']}")
        if s1["state"] in ("done", "failed", "cancelled"):
            break
    check("single-element optimize completes",
          bool(s1["state"] == "done" and s1.get("result")),
          f"({s1['state']}, {(s1.get('best') or {}).get('downforce_n')} N)")

# a custom spec the server has never seen (a project whose upload was lost)
# must be rejected at job creation, not finish 'done' with nothing to show
s_gone, _ = call("POST", "/api/optimize", {"config": {
    **GOOD, "elements": [{"airfoil": "custom:never-registered",
                          "chord_ratio": 1.0}]}, "options": {}})
check("lost custom airfoil optimize -> 422 up front", s_gone == 422,
      f"(got {s_gone})")

# session persistence round-trip
marker = uuid.uuid4().hex
s_sp, _ = call("POST", "/api/session", {"state": {"marker": marker,
                                                  "config": GOOD}})
s_sg, r_sg = call("GET", "/api/session")
check("session save/load round-trip",
      s_sp == 200 and s_sg == 200
      and (r_sg.get("state") or {}).get("marker") == marker)

# CSV export must quote airfoil names containing commas
comma_name = "acme, mk2"
cc = airfoils.repaneled("naca0009", 40)[1].copy()
cc[len(cc) // 3, 1] *= 1.002
s_cu, cu = call("POST", "/api/airfoils/upload", {
    "name": comma_name, "dat_text": comma_name + "\n" + "\n".join(
        f" {x:.6f} {y:.6f}" for x, y in cc)})
if s_cu == 200:
    csv_cfg = {**GOOD, "elements": [{"airfoil": cu["spec"],
                                     "chord_ratio": 1.0}]}
    import csv as _csv
    import io as _io
    req = urllib.request.Request(
        BASE + "/api/export/csv",
        data=json.dumps({"config": csv_cfg}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
    rows = list(_csv.reader(_io.StringIO(text)))
    body_ok = (rows[0][:3] == ["element", "role", "airfoil"]
               and all(len(row) == 6 for row in rows[1:] if row)
               and rows[1][2].startswith(comma_name))
    check("CSV quotes comma-containing airfoil names", body_ok,
          f"(name cell: {rows[1][2]!r})")
else:
    check("CSV quotes comma-containing airfoil names (upload failed)", False,
          f"(upload got {s_cu})")

# polar reports the Reynolds clamp instead of silently substituting
s_p, r_p = call("POST", "/api/polar", {"spec": "naca0012", "re": 5000,
                                       "alpha_start": -2, "alpha_stop": 6,
                                       "alpha_step": 1})
check("low-Re polar reports clamp",
      s_p == 200 and r_p.get("re_clamped") is True
      and r_p.get("re_used") == 10000.0,
      f"(re_used={r_p.get('re_used') if r_p else s_p})")

# RANS verification API surface — no run is ever started here (bad payloads
# only), so the suite stays docker-free
s_ra, r_ra = call("GET", "/api/rans/availability")
check("rans availability reports a status either way",
      s_ra == 200 and isinstance(r_ra.get("available"), bool)
      and "image" in r_ra, f"(got {s_ra}: {r_ra})")
s_rc0, r_rc0 = call("GET", "/api/rans/current")
check("rans current reports no job on a fresh server",
      s_rc0 == 200 and r_rc0.get("job_id") is None, f"(got {s_rc0}: {r_rc0})")
s_rs, _ = call("GET", "/api/rans/no-such-job")
check("unknown rans job -> 404", s_rs == 404, f"(got {s_rs})")
s_rc, _ = call("POST", "/api/rans/no-such-job/cancel")
check("cancel of unknown rans job -> 404", s_rc == 404, f"(got {s_rc})")
s_rb, _ = call("POST", "/api/rans/start",
               {"config": {**GOOD, "elements": ["hello"]}})
check("rans start with broken config -> 422", s_rb == 422, f"(got {s_rb})")
s_ri, _ = call("POST", "/api/rans/start", {"config": GOOD, "max_iters": 7})
check("rans start with out-of-range iterations -> 422", s_ri == 422,
      f"(got {s_ri})")
s_rm, _ = call("POST", "/api/rans/start", {"config": GOOD,
                                           "mesh_size": "ultra"})
check("rans start with unknown mesh size -> 422", s_rm == 422,
      f"(got {s_rm})")

# XFOIL engine without xfoil.exe must be a clean failed-dependency error
if not (Path(__file__).resolve().parents[2] / "xfoil" / "xfoil.exe").exists():
    s_x, r_x = call("POST", "/api/polar", {"spec": "naca0012", "re": 3e5,
                                           "engine": "xfoil"})
    check("xfoil engine absent -> 424 with guidance", s_x == 424,
          f"(got {s_x})")

print(f"\n{sum(results)}/{len(results)} adversarial checks passed")
sys.exit(0 if all(results) else 1)
