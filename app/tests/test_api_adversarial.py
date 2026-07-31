"""Adversarial API regression: hostile payloads must 422 (never 500/hang),
sane payloads must keep working, and the stack-angle sign convention must
hold.

Run through run_all.py, which boots a throwaway scratch server. The suite
MUTATES server state (it uploads airfoils, warms caches and rewrites the
machine-level rule-preset library), so WSS_TEST_BASE is REQUIRED — there is
no default target, or a bare standalone run would mutate the live app on
port 8642. To run standalone, start a dedicated server and point at it:
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
BASE = os.environ.get("WSS_TEST_BASE")
if not BASE:
    print("WSS_TEST_BASE is not set — refusing to pick a default target: "
          "this suite mutates server state (uploads, caches, the "
          "machine-level rule-preset library).\n"
          "Run it through app/tests/run_all.py (isolated scratch server), "
          "or start a dedicated server and set WSS_TEST_BASE to it — see "
          "the module docstring.")
    sys.exit(2)


# proxy-free opener: an env/system HTTP proxy must not swallow the loopback
# test traffic (it cannot reach 127.0.0.1), or every call would error or route
# to the proxy instead of the scratch server
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as r:
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


def call_h(method, path, body, extra_headers):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **extra_headers})
    try:
        with _opener.open(req, timeout=30) as rr:
            return rr.status
    except urllib.error.HTTPError as e:
        return e.code


# SECURITY: cross-site / non-loopback callers are rejected (DNS-rebinding
# Host and cross-origin drive-by), same-origin loopback traffic is allowed
check("cross-site Origin -> 403",
      call_h("POST", "/api/analyze", {"config": GOOD},
             {"Origin": "https://evil.example"}) == 403)
check("non-loopback Host -> 403",
      call_h("POST", "/api/analyze", {"config": GOOD},
             {"Host": "evil.example"}) == 403)
check("loopback Origin -> 200 (same-origin app traffic)",
      call_h("POST", "/api/analyze", {"config": GOOD},
             {"Origin": "http://127.0.0.1:9"}) == 200)
check("absent Origin -> 200 (top-level navigation / CLI)",
      call_h("POST", "/api/analyze", {"config": GOOD}, {}) == 200)

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

# JSON 1e999 parses to float inf; int(inf) must be a validation error,
# not an unhandled OverflowError -> 500
s, _ = call("POST", "/api/geometry",
            {"config": {**GOOD, "n_panels_per_side": 1e999}}, timeout=10)
check("n_panels 1e999 (inf) -> 422", s == 422, f"(got {s})")

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
s_rv, _ = call("POST", "/api/export/reveal", {"path": "bad\x00path.dxf"})
check("reveal with an unusable path -> 422, not a 500",
      s_rv == 422, f"(got {s_rv})")

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

# "refine" is the UI's historical name for local mode — it must start, not 422
s_rf, r_rf = call("POST", "/api/optimize", {
    "config": GOOD, "options": {"target_downforce_n": 250, "budget": 150,
                                "mode": "refine"}})
check("optimizer mode 'refine' accepted as alias for 'local'",
      s_rf == 200 and "job_id" in (r_rf or {}), f"(got {s_rf})")
# one optimization at a time: a second start is refused while this one runs,
# so release the slot the way the UI does before the next start
s_busy, _ = call("POST", "/api/optimize",
                 {"config": GOOD, "options": {"target_downforce_n": 250}})
check("a second optimize while one runs -> 409", s_busy == 409,
      f"(got {s_busy})")
if r_rf and r_rf.get("job_id"):
    call("POST", f"/api/optimize/{r_rf['job_id']}/cancel")
    for _ in range(200):
        s_st, r_st = call("GET", f"/api/optimize/{r_rf['job_id']}")
        if (r_st or {}).get("state") in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)

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

# concurrent session saves must never 500 (the debounced autosave and the
# pagehide beacon race routinely; two windows race constantly)
import threading as _threading

_conc_codes = []
_conc_lock = _threading.Lock()


def _hammer(n):
    for k in range(n):
        # a transient loopback connection reset (loaded machine) is not the
        # race under test — retry once, then record a distinct code so the
        # thread never dies silently short of its quota
        for attempt in (1, 2):
            try:
                s_c, _ = call("POST", "/api/session",
                              {"state": {"marker": f"c{k}", "config": GOOD}})
                break
            except Exception:
                s_c = 599
        with _conc_lock:
            _conc_codes.append(s_c)


_threads = [_threading.Thread(target=_hammer, args=(12,)) for _ in range(3)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
check("36 concurrent session saves all succeed",
      len(_conc_codes) == 36 and all(c == 200 for c in _conc_codes),
      f"(codes {sorted(set(_conc_codes))})")

# NaN/Infinity numeric options must 422 up front, not finish 'done' with a
# garbage result (json.dumps emits the nonstandard NaN literal, which the
# server-side parser accepts into the options dict)
s_nan, _ = call("POST", "/api/optimize",
                {"config": GOOD,
                 "options": {"target_downforce_n": float("nan"),
                             "budget": 40}})
check("optimizer NaN target -> 422", s_nan == 422, f"(got {s_nan})")
s_inf, _ = call("POST", "/api/optimize",
                {"config": GOOD,
                 "options": {"drag_weight": float("inf"), "budget": 40}})
check("optimizer Infinity drag weight -> 422", s_inf == 422, f"(got {s_inf})")

# job rediscovery endpoint: shape must hold whether or not a job exists
s_oc, r_oc = call("GET", "/api/optimize/current")
check("optimize current reports a job id field",
      s_oc == 200 and "job_id" in r_oc and "state" in r_oc,
      f"(got {s_oc}: {r_oc})")

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
    with _opener.open(req, timeout=60) as r:   # proxy-free, like call()
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

# SECURITY: a single-line ".dat"-looking dat_text must be treated as literal
# content, never read as a filesystem path (would disclose arbitrary .dat
# files off the disk). Point it at a real file that exists and confirm the
# server did NOT read it.
import tempfile as _tf
import os as _os_sec
_secret = _os_sec.path.join(_tf.gettempdir(), "wss_adv_secret.dat")
with open(_secret, "w") as _fh:
    _fh.write("TOPSECRET\n" + "\n".join(
        f" {x:.4f} {y:.4f}" for x, y in
        [(1, 0), (.5, .05), (0, 0), (.5, -.05), (1, 0),
         (.6, .03), (.4, -.03), (.3, .02), (.2, -.02), (.1, .01), (.7, .0)]))
try:
    s_fr, r_fr = call("POST", "/api/airfoils/upload",
                      {"name": "attack", "dat_text": _secret})
    detail = (r_fr or {}).get("detail", "") if s_fr != 200 else ""
    disclosed = "TOPSECRET" in json.dumps(r_fr or {})
    check("path-like dat_text is not read from disk (no file disclosure)",
          not disclosed and (s_fr == 422),
          f"(status {s_fr}, disclosed {disclosed})")
finally:
    _os_sec.remove(_secret)

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
# 50-99 is fluent2d-only territory: the default engine must answer an
# actionable 422 naming its own floor, not a raw validation blob
s_ri2, r_ri2 = call("POST", "/api/rans/start",
                    {"config": GOOD, "max_iters": 60})
check("rans start with a fluent2d-only iteration count -> 422 naming "
      "the floor",
      s_ri2 == 422 and "at least 100 iterations"
      in str(r_ri2.get("detail", "")), f"(got {s_ri2}: {r_ri2})")
s_qi, _ = call("POST", "/api/rans-queue/start",
               {"items": [], "max_iters": 60})
check("queue start keeps the 100-iteration floor", s_qi == 422,
      f"(got {s_qi})")
s_rm, _ = call("POST", "/api/rans/start", {"config": GOOD,
                                           "mesh_size": "ultra"})
check("rans start with unknown mesh size -> 422", s_rm == 422,
      f"(got {s_rm})")
s_rf1, _ = call("GET", "/api/rans/no-such-job/flow")
check("flow view of unknown job -> 404", s_rf1 == 404, f"(got {s_rf1})")
s_rf2, _ = call("GET", "/api/rans/no-such-job/flow?field=vorticity")
check("flow view with unknown field -> 422", s_rf2 == 422, f"(got {s_rf2})")
s_rme, _ = call("POST", "/api/rans/start", {"config": GOOD,
                                            "mesher": "tetgen"})
check("rans start with unknown mesher -> 422", s_rme == 422,
      f"(got {s_rme})")
s_rf3, _ = call("GET", "/api/rans/no-such-job/flowfield?fields=vorticity")
check("flowfield with unknown fields -> 422", s_rf3 == 422,
      f"(got {s_rf3})")
s_rf4, _ = call("GET", "/api/rans/no-such-job/flowfield?fields=umag,cp")
check("flowfield fields=umag,cp validates, then unknown job -> 404",
      s_rf4 == 404, f"(got {s_rf4})")
s_rex, _ = call("POST", "/api/rans/no-such-job/export/fluent")
check("ANSYS export of unknown job -> 404", s_rex == 404,
      f"(got {s_rex})")
# config problems must answer BEFORE any license is touched
s_fme, _ = call("POST", "/api/export/fluent-mesh/save",
                {"config": {**GOOD, "elements": ["hello"]}})
check("fluent mesh export with broken config -> 422", s_fme == 422,
      f"(got {s_fme})")
s_fmm, _ = call("POST", "/api/export/fluent-mesh/save",
                {"config": GOOD, "mesh_size": "ultra"})
check("fluent mesh export with unknown mesh size -> 422", s_fmm == 422,
      f"(got {s_fmm})")

# Fluent 2D API surface — cross-field validation fires before any job (or
# ANSYS process) is spawned, so these stay offline like the rest
s_2dm, r_2dm = call("POST", "/api/rans/start",
                    {"config": GOOD, "engine": "fluent2d",
                     "mesh_size": "coarse"})
check("fluent2d start with a mesh preset -> 422 naming the sizing modes",
      s_2dm == 422
      and "studio-yplus1" in str((r_2dm or {}).get("detail", "")),
      f"(got {s_2dm})")
s_ofm, _ = call("POST", "/api/rans/start",
                {"config": GOOD, "mesh_size": "default"})
check("openfoam start with a fluent2d sizing -> 422", s_ofm == 422,
      f"(got {s_ofm})")
s_flm, _ = call("POST", "/api/rans/start",
                {"config": GOOD, "engine": "fluent",
                 "mesh_size": "studio-yplus1"})
check("fluent start with a fluent2d sizing -> 422", s_flm == 422,
      f"(got {s_flm})")
s_cv, _ = call("POST", "/api/rans/start",
               {"config": GOOD, "engine": "fluent2d", "mesh_size": "default",
                "conventions": "racing"})
check("bogus conventions -> 422", s_cv == 422, f"(got {s_cv})")
s_cvt, _ = call("POST", "/api/rans/start",
                {"config": GOOD, "engine": "fluent2d",
                 "mesh_size": "default", "conventions": "team"})
check("the retired conventions value is no longer accepted", s_cvt == 422,
      f"(got {s_cvt})")

# ---- ANSYS 2D settings overrides ----
# every range boundary, over HTTP: one step outside must 422 before any
# ANSYS process is touched. The in-range twin is NOT sent here — it would
# start a licensed run.
for _f, _bad in (("edge_size_mm", 0), ("edge_size_mm", -1),
                 ("edge_size_mm", float("inf")),
                 ("growth", float("nan")), ("first_layer_mm", 0),
                 ("first_layer_mm", -0.5), ("n_layers", -1),
                 ("n_layers", 101), ("growth", 1.0), ("growth", 3.0001),
                 ("front_l", 0.49), ("front_l", 20.01),
                 ("back_l", 0.49), ("back_l", 40.01),
                 ("top_h", 0.49), ("top_h", 20.01),
                 ("sc_budget_s", 29.9), ("sc_budget_s", 7200.1),
                 ("wb_budget_s", 59.9), ("wb_budget_s", 43200.1)):
    s_ov, _ = call("POST", "/api/rans/start",
                   {"config": GOOD, "engine": "fluent2d",
                    "mesh_size": "default", _f: _bad})
    check(f"rans start with {_f}={_bad} -> 422", s_ov == 422,
          f"(got {s_ov})")
# the mesh export shares the same override model — only out-of-range
# values are sent, so the request dies in validation and no ANSYS seat is
# ever touched (an in-range one would start a real multi-minute chain)
for _f, _bad in (("edge_size_mm", -0.05), ("n_layers", 200),
                 ("growth", 5.0), ("sc_budget_s", 1), ("top_h", 99)):
    s_ove, _ = call("POST", "/api/export/fluent2d-mesh/save",
                    {"config": GOOD, "sizing": "default", _f: _bad})
    check(f"fluent2d mesh export with {_f}={_bad} -> 422", s_ove == 422,
          f"(got {s_ove})")
# an override the chosen engine cannot honour must be refused by name
s_owe, r_owe = call("POST", "/api/rans/start",
                    {"config": GOOD, "engine": "openfoam",
                     "mesh_size": "coarse", "first_layer_mm": 0.5})
check("an ANSYS override on the OpenFOAM engine -> 422 naming the field",
      s_owe == 422
      and "first_layer_mm" in str((r_owe or {}).get("detail", "")),
      f"(got {s_owe}: {r_owe})")
s_owf, _ = call("POST", "/api/rans/start",
                {"config": GOOD, "engine": "fluent", "mesh_size": "medium",
                 "top_h": 6.0})
check("an ANSYS override on the Fluent slab engine -> 422", s_owf == 422,
      f"(got {s_owf})")
s_f2a, r_f2a = call("GET", "/api/fluent2d/availability")
check("fluent2d availability reports both halves either way",
      s_f2a == 200 and isinstance((r_f2a or {}).get("available"), bool)
      and "fluent" in (r_f2a or {}) and "workbench" in (r_f2a or {}),
      f"(got {s_f2a}: {r_f2a})")
s_f2e, _ = call("POST", "/api/export/fluent2d-mesh/save",
                {"config": {**GOOD, "elements": ["hello"]}})
check("fluent2d mesh export with broken config -> 422", s_f2e == 422,
      f"(got {s_f2e})")
s_f2s, _ = call("POST", "/api/export/fluent2d-mesh/save",
                {"config": GOOD, "sizing": "ultra"})
check("fluent2d mesh export with unknown sizing -> 422", s_f2s == 422,
      f"(got {s_f2s})")
s_f2t, _ = call("POST", "/api/export/fluent2d-mesh/save",
                {"config": GOOD, "sizing": "team"})
check("the retired sizing value is no longer accepted", s_f2t == 422,
      f"(got {s_f2t})")

# XFOIL engine without xfoil.exe must be a clean failed-dependency error
if not (Path(__file__).resolve().parents[2] / "xfoil" / "xfoil.exe").exists():
    s_x, r_x = call("POST", "/api/polar", {"spec": "naca0012", "re": 3e5,
                                           "engine": "xfoil"})
    check("xfoil engine absent -> 424 with guidance", s_x == 424,
          f"(got {s_x})")

# ---- rule envelope + rule presets ----
# the preset library is MACHINE-level state (app_data/rule_presets.json):
# on a scratch server it is isolated by WSS_DATA_DIR, but a standalone run
# against a live server must put the user's library back afterwards
_s_keep, _r_keep = call("GET", "/api/rule-presets")
s_g, r_g = call("POST", "/api/geometry",
                {"config": {**GOOD, "rule_envelope": {"max_height_mm": 60}}})
check("geometry with a violated envelope -> 200 + rules verdict",
      s_g == 200 and r_g["rules"] and not r_g["rules"]["ok"], f"(got {s_g})")
s_gb, _ = call("POST", "/api/geometry",
               {"config": {**GOOD, "rule_envelope": {"max_length_mm": -3}}})
check("geometry with a bogus envelope -> 422", s_gb == 422, f"(got {s_gb})")
s_ov, r_ov = call("POST", "/api/optimize",
                  {"config": {**GOOD, "rule_envelope": {"max_height_mm": 60}},
                   "options": {"target_downforce_n": 200}})
check("optimize from a rule-violating start -> 422 naming the rule",
      s_ov == 422 and "violates rule" in str(r_ov.get("detail", "")),
      f"(got {s_ov}: {str(r_ov)[:80]})")
s_p0, r_p0 = call("GET", "/api/rule-presets")
check("rule presets GET on a fresh server -> empty list",
      s_p0 == 200 and r_p0["presets"] == [], f"(got {s_p0}: {r_p0})")
# the library must be put back even if a check between the first PUT and
# the restore raises (connection drop, assertion crash) — finally, not
# fall-through
try:
    s_p1, _ = call("PUT", "/api/rule-presets", {"presets": [
        {"name": "FSAE test", "envelope": {"max_length_mm": 700,
                                           "max_height_mm": 250}}]})
    s_p2, r_p2 = call("GET", "/api/rule-presets")
    check("rule presets save + reload round-trip",
          s_p1 == 200 and s_p2 == 200 and len(r_p2["presets"]) == 1
          and r_p2["presets"][0]["name"] == "FSAE test"
          and r_p2["presets"][0]["envelope"]["max_length_mm"] == 700.0,
          f"(got {s_p1}/{s_p2}: {r_p2})")
    s_p3, _ = call("PUT", "/api/rule-presets", {"presets": [
        {"name": "bad", "envelope": {"max_height_mm": -1}}]})
    check("rule preset with a bogus envelope -> 422", s_p3 == 422,
          f"(got {s_p3})")
    s_p4, _ = call("PUT", "/api/rule-presets", {"presets": [
        {"name": "dup", "envelope": {}}, {"name": "DUP", "envelope": {}}]})
    check("rule presets with duplicate names -> 422", s_p4 == 422,
          f"(got {s_p4})")
    s_p5, _ = call("PUT", "/api/rule-presets",
                   {"presets": [{"name": "", "envelope": {}}]})
    check("rule preset without a name -> 422", s_p5 == 422, f"(got {s_p5})")
finally:
    if _s_keep == 200 and isinstance(_r_keep, dict):
        call("PUT", "/api/rule-presets",
             {"presets": _r_keep.get("presets", [])})   # restore the library

# ---- ANSYS settings presets ----
# machine-level state as well (app_data/ansys_presets.json): captured and
# restored the same way the rule library is

_s_akeep, _r_akeep = call("GET", "/api/ansys-presets")
s_a0, r_a0 = call("GET", "/api/ansys-presets")
check("ansys presets GET on a fresh server -> empty list",
      s_a0 == 200 and isinstance((r_a0 or {}).get("presets"), list),
      f"(got {s_a0}: {r_a0})")
_SETTINGS = {"sizing": "studio-yplus1", "conventions": "studio",
             "n_iters": 800, "n_ranks": 4, "edge_size_mm": 0.35,
             "first_layer_mm": 0.02, "n_layers": 25, "growth": 1.15,
             "front_l": 4.0, "back_l": 9.0, "top_h": 4.0,
             "sc_budget_s": 600, "wb_budget_s": 2400}
try:
    s_a1, _ = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": "Resolved wall", "settings": _SETTINGS}]})
    s_a2, r_a2 = call("GET", "/api/ansys-presets")
    _p = ((r_a2 or {}).get("presets") or [{}])[0]
    check("ansys presets save + reload round-trip",
          s_a1 == 200 and s_a2 == 200 and len(r_a2["presets"]) == 1
          and _p.get("name") == "Resolved wall"
          and _p.get("settings", {}).get("edge_size_mm") == 0.35
          and _p.get("settings", {}).get("n_iters") == 800,
          f"(got {s_a1}/{s_a2}: {r_a2})")
    check("ansys preset: the name lives beside the settings, never inside",
          "name" not in (_p.get("settings") or {}), f"({_p})")
    s_a3, r_a3 = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": "sneaky", "settings": {**_SETTINGS, "name": "inside"}}]})
    s_a3g, r_a3g = call("GET", "/api/ansys-presets")
    check("ansys preset: a name smuggled into the settings is dropped",
          s_a3 == 200
          and "name" not in (r_a3g["presets"][0].get("settings") or {}),
          f"(got {s_a3}: {r_a3g})")
    s_a4, _ = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": "sparse", "settings": {}}]})
    s_a4g, r_a4g = call("GET", "/api/ansys-presets")
    _sp = r_a4g["presets"][0]["settings"]
    check("ansys preset: an empty settings object stores the documented "
          "defaults with null overrides",
          s_a4 == 200 and _sp["sizing"] == "default"
          and _sp["conventions"] == "default" and _sp["n_iters"] == 500
          and _sp["n_ranks"] == 1 and _sp["edge_size_mm"] is None,
          f"(got {s_a4}: {_sp})")
    # a falsy WRONG value is a wrong value, never coerced into the
    # default: the retired vocabulary must stay refused in every form
    for _falsy in ({"sizing": ""}, {"sizing": 0}, {"conventions": ""},
                   {"conventions": False}, {"n_iters": 0},
                   {"n_ranks": 0}):
        s_af, _ = call("PUT", "/api/ansys-presets", {"presets": [
            {"name": "falsy", "settings": {**_SETTINGS, **_falsy}}]})
        check(f"ansys preset with {_falsy} -> 422 (never coerced to the "
              f"default)", s_af == 422, f"(got {s_af})")
    for _bad_name in (["x"], 123, {"n": 1}):
        s_anm, _ = call("PUT", "/api/ansys-presets", {"presets": [
            {"name": _bad_name, "settings": {}}]})
        check(f"ansys preset name {_bad_name!r} -> 422", s_anm == 422,
              f"(got {s_anm})")
    s_a5, _ = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": "dup", "settings": {}}, {"name": "DUP", "settings": {}}]})
    check("ansys presets with duplicate names -> 422", s_a5 == 422,
          f"(got {s_a5})")
    s_a6, _ = call("PUT", "/api/ansys-presets",
                   {"presets": [{"name": "", "settings": {}}]})
    check("ansys preset without a name -> 422", s_a6 == 422, f"(got {s_a6})")
    s_a7, _ = call("PUT", "/api/ansys-presets",
                   {"presets": [{"name": "x" * 61, "settings": {}}]})
    check("ansys preset with a 61-char name -> 422", s_a7 == 422,
          f"(got {s_a7})")
    s_a8, _ = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": f"p{i}", "settings": {}} for i in range(51)]})
    check("51 ansys presets -> 422", s_a8 == 422, f"(got {s_a8})")
    s_a9, _ = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": "n", "settings": ["not", "a", "map"]}]})
    check("ansys preset with a non-object settings -> 422", s_a9 == 422,
          f"(got {s_a9})")
    for _bad in ({"sizing": "team"}, {"conventions": "team"},
                 {"sizing": "coarse"}, {"n_iters": 49},
                 {"n_iters": 20001}, {"n_ranks": 0}, {"n_ranks": 33},
                 {"edge_size_mm": 0}, {"first_layer_mm": -1},
                 {"n_layers": 101}, {"n_layers": 2.5}, {"growth": 1.0},
                 {"growth": 3.01}, {"front_l": 0.4}, {"back_l": 41},
                 {"top_h": 21}, {"sc_budget_s": 29},
                 {"wb_budget_s": 43201}):
        s_ab, _ = call("PUT", "/api/ansys-presets", {"presets": [
            {"name": "bad", "settings": {**_SETTINGS, **_bad}}]})
        check(f"ansys preset with {_bad} -> 422", s_ab == 422,
              f"(got {s_ab})")
    s_ak, r_ak = call("GET", "/api/ansys-presets")
    check("a refused ansys PUT never replaced the stored library",
          s_ak == 200 and len(r_ak["presets"]) == 1
          and r_ak["presets"][0]["name"] == "sparse", f"({r_ak})")

    # null means "the documented default" for EVERY settings key, not
    # just the nine overrides — a client that spells out all thirteen
    # keys, null for the ones the user did not touch, is the shape the
    # contract describes
    s_an, _ = call("PUT", "/api/ansys-presets", {"presets": [
        {"name": "nulls", "settings": {"sizing": None, "conventions": None,
                                       "n_iters": None, "n_ranks": None,
                                       "edge_size_mm": None}}]})
    s_ang, r_ang = call("GET", "/api/ansys-presets")
    _sn = ((r_ang or {}).get("presets") or [{}])[0].get("settings") or {}
    check("ansys preset: an explicit null is the documented default for "
          "every key",
          s_an == 200 and _sn.get("n_iters") == 500
          and _sn.get("n_ranks") == 1 and _sn.get("sizing") == "default"
          and _sn.get("conventions") == "default"
          and _sn.get("edge_size_mm") is None, f"(got {s_an}: {_sn})")

    # revision control: a library saved by another window since this one
    # loaded it must answer 409, not be silently replaced
    _rev = (r_ang or {}).get("rev")
    s_ar1, r_ar1 = call("PUT", "/api/ansys-presets", {
        "presets": [{"name": "rev a", "settings": {}}], "rev": _rev})
    s_ar2, r_ar2 = call("PUT", "/api/ansys-presets", {
        "presets": [{"name": "rev b", "settings": {}}], "rev": _rev})
    s_arg, r_arg = call("GET", "/api/ansys-presets")
    check("ansys presets: matching rev accepted, stale rev -> 409 and "
          "never lands",
          isinstance(_rev, int) and s_ar1 == 200
          and (r_ar1 or {}).get("rev") == _rev + 1 and s_ar2 == 409
          and r_arg["presets"][0]["name"] == "rev a",
          f"(rev {_rev}, got {s_ar1}/{s_ar2}: {r_arg})")
finally:
    if _s_akeep == 200 and isinstance(_r_akeep, dict):
        call("PUT", "/api/ansys-presets",
             {"presets": _r_akeep.get("presets", [])})

print(f"\n{sum(results)}/{len(results)} adversarial checks passed")
sys.exit(0 if all(results) else 1)
