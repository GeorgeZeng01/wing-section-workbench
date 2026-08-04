"""Pinned-designs library: the durable, machine-level pin store.

Contracts pinned here:
  * GET on an empty store -> {pins: [], rev: 0}; rev-less PUT accepted;
    matching rev increments; stale rev -> 409 CARRYING the newer library
    (rev + pins) and the stale write never lands; a pre-rev bare-list file
    reads as rev 0.
  * Round-trip fidelity: the config dict and embedded custom-airfoil .dat
    text come back byte-identical (verbatim storage, no dataclass
    round-trip materializing defaults).
  * A config referencing an UNREGISTERED custom: spec PASSES — restart
    survival is the library's whole point (uploads die with the process).
  * Hostile pins 422 by name: bad/duplicate/malformed ids, non-text or
    over-long labels, non-dict/unparseable configs, bad custom_airfoils
    keys/values, malformed outlines, runs with unknown channels, no
    status object, unknown image keys, non-data-URI or oversized images.
  * A run's terminal status travels VERBATIM inside runs.{rans,fl2d} — the
    same restore path project files flow through renders it.
  * Caps: 49 pins -> 422; an over-bytes library -> 422; the previous file
    survives every rejected write; no tmp stragglers remain.
  * The key= generalization of the preset helpers did not disturb the
    rule/ANSYS preset libraries (regression round-trip on both).

Self-contained: boots its own scratch uvicorn with WSS_DATA_DIR /
WSS_EXPORTS_DIR pointed at throwaway temp dirs.

    .venv\\Scripts\\python.exe app\\tests\\test_pinned_store.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRATCH_DATA = tempfile.mkdtemp(prefix="wss_pins_data_")
SCRATCH_EXPORTS = tempfile.mkdtemp(prefix="wss_pins_exports_")
os.environ["WSS_DATA_DIR"] = SCRATCH_DATA
os.environ["WSS_EXPORTS_DIR"] = SCRATCH_EXPORTS

PY = sys.executable
results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def wait_healthy(base: str, timeout: float = 30.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(f"{base}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, timeout=60):
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


CFG = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15,
}
DAT = "pin test airfoil\n 1.000000 0.000000\n 0.500000 0.080000\n" \
      " 0.000000 0.000000\n 0.500000 -0.020000\n 1.000000 0.000000\n"
THUMB = "data:image/jpeg;base64," + ("A" * 400)
RUN = {"rans": {"status": {"id": "1c2d3e4f5a6b", "state": "done",
                           "engine": "openfoam", "mesh_size": "fine",
                           "result": {"cl_rans": 7.59, "cl_rans_std": 0.02,
                                      "cd_rans": 0.31,
                                      "delta_cl_pct": -7.9,
                                      "downforce_n_at_rans_cl": 238.0,
                                      "converged": True}},
                "images": {"umag": THUMB, "cp": THUMB}}}


def pin(pid="a1b2c3d4e5f6", label="#1", **kw):
    p = {"id": pid, "v": 1, "t": "2026-08-03T12:00:00Z", "label": label,
         "config": dict(CFG), "target": 250.0,
         "headline": {"downforce_n": 259.1, "drag_n": 30.8,
                      "drag_is_lower_bound": True, "ld": 8.4,
                      "warnings": 2, "elements": 2, "frac_max": 0.889,
                      "confidence_min": 0.91},
         "outline": [[[0.0, 0.0], [0.5, 0.08], [1.0, 0.0]],
                     [[1.1, -0.05], [1.3, 0.02]]],
         "runs": {"rans": {"status": dict(RUN["rans"]["status"]),
                           "images": dict(RUN["rans"]["images"])}}}
    p.update(kw)
    return p


_server = subprocess.Popen(
    [PY, "-m", "uvicorn", "app.server:app", "--port", str(PORT),
     "--log-level", "warning"],
    cwd=ROOT, env={**os.environ})

try:
    check("scratch server became healthy", wait_healthy(BASE))

    # ---- empty store + basic revision flow -----------------------------
    s, r = call("GET", "/api/pinned-designs")
    check("GET empty -> pins [], rev 0",
          s == 200 and r == {"pins": [], "rev": 0})

    s, r = call("PUT", "/api/pinned-designs", {"pins": [pin()]})
    check("rev-less PUT accepted -> rev 1",
          s == 200 and r.get("ok") is True and r.get("rev") == 1)

    s, r = call("GET", "/api/pinned-designs")
    got = (r.get("pins") or [{}])[0]
    check("round-trip: config comes back byte-identical (verbatim, no "
          "dataclass round-trip)",
          s == 200 and got.get("config") == CFG)
    check("round-trip: headline, outline and target survive",
          got.get("headline", {}).get("drag_is_lower_bound") is True
          and got.get("outline") and got.get("target") == 250.0)
    check("round-trip: the run's terminal status travels verbatim with "
          "its images",
          got.get("runs", {}).get("rans", {}).get("status")
          == RUN["rans"]["status"]
          and got.get("runs", {}).get("rans", {}).get("images", {})
          .get("umag") == THUMB)

    s, r = call("PUT", "/api/pinned-designs",
                {"pins": [pin(), pin("b2" * 6, "#2")], "rev": 1})
    check("matching rev accepted -> rev 2", s == 200 and r.get("rev") == 2)

    s, r = call("PUT", "/api/pinned-designs",
                {"pins": [pin("c3" * 6, "stale")], "rev": 1})
    det = (r or {}).get("detail") or {}
    check("stale rev -> 409 carrying the newer library under 'pins'",
          s == 409 and det.get("rev") == 2
          and isinstance(det.get("pins"), list) and len(det["pins"]) == 2)

    s, r = call("GET", "/api/pinned-designs")
    check("the stale write never landed",
          s == 200 and r.get("rev") == 2 and len(r.get("pins")) == 2
          and all(p["label"] != "stale" for p in r["pins"]))

    # ---- restart survival is load-bearing ------------------------------
    lost = dict(CFG)
    lost["elements"] = [dict(CFG["elements"][0], airfoil="custom:gone-xyz"),
                        CFG["elements"][1]]
    s, r = call("PUT", "/api/pinned-designs",
                {"pins": [pin(), pin("d4" * 6, "lost-upload", config=lost,
                                     custom_airfoils={"custom:gone-xyz": DAT})],
                 "rev": 2})
    check("a config referencing an UNREGISTERED custom: spec passes "
          "(restart survival is the point)", s == 200 and r.get("rev") == 3)
    s, r = call("GET", "/api/pinned-designs")
    got = [p for p in r.get("pins", []) if p["label"] == "lost-upload"]
    check("embedded .dat text survives byte-identical",
          got and got[0].get("custom_airfoils", {}).get("custom:gone-xyz")
          == DAT)

    # ---- hostile pins 422 by name --------------------------------------
    bad = [
        ("label is a list", pin(label=["x"])),
        ("label is a number", pin(label=123)),
        ("label empty", pin(label="")),
        ("label over-long", pin(label="a" * 61)),
        ("id malformed (path)", pin(pid="../x")),
        ("id malformed (upper hex)", pin(pid="ABCDEF12")),
        ("id malformed (short)", pin(pid="abc")),
        ("config is text", pin(config="x")),
        ("config with 5 elements",
         pin(config={**CFG, "elements": CFG["elements"] * 3})),
        ("config with absurd chord", pin(config={**CFG, "chord_mm": 1e9})),
        ("target is NaN-ish text", pin(target="NaN")),
        ("custom_airfoils not a dict", pin(custom_airfoils="x")),
        ("custom_airfoils bad key (shape:)",
         pin(custom_airfoils={"shape:x": DAT})),
        ("custom_airfoils bad key (spaces/upper)",
         pin(custom_airfoils={"custom:UP PER": DAT})),
        ("custom_airfoils oversized value",
         pin(custom_airfoils={"custom:big": "x" * 120_001})),
        ("outline with 5 polylines",
         pin(outline=[[[0, 0], [1, 0]]] * 5)),
        ("outline with 121 points",
         pin(outline=[[[0, 0]] * 121])),
        ("outline with NaN", pin(outline=[[[0, float("nan")]]])),
        ("outline with text point", pin(outline=[[[0, "x"]]])),
        ("runs unknown channel", pin(runs={"xfoil": RUN["rans"]})),
        ("runs channel not an object", pin(runs={"rans": "x"})),
        ("runs without a status object",
         pin(runs={"rans": {"images": {"umag": THUMB}}})),
        ("runs status is text",
         pin(runs={"rans": {"status": "done"}})),
        ("runs images unknown key",
         pin(runs={"rans": {"status": {"state": "done"},
                            "images": {"selfie": THUMB}}})),
        ("runs image not an image data URI",
         pin(runs={"rans": {"status": {"state": "done"},
                            "images": {"umag":
                                       "data:text/html;base64,PGI+"}}})),
        ("runs image missing base64 marker",
         pin(runs={"rans": {"status": {"state": "done"},
                            "images": {"umag": "data:image/png,raw"}}})),
        ("runs image oversized",
         pin(runs={"rans": {"status": {"state": "done"},
                            "images": {"umag": "data:image/jpeg;base64,"
                                       + "A" * 250_001}}})),
        ("duplicate ids", None),   # handled below (needs two pins)
    ]
    for label, p in bad:
        if p is None:
            s, r = call("PUT", "/api/pinned-designs",
                        {"pins": [pin(), pin(label="#dup")]})
        else:
            s, r = call("PUT", "/api/pinned-designs", {"pins": [p]})
        check(f"hostile pin 422: {label}", s == 422, f"(status {s})")
    s, r = call("PUT", "/api/pinned-designs", {"pins": 123})
    check("hostile body 422: pins not a list", s == 422)

    s, r = call("GET", "/api/pinned-designs")
    check("the library survived every hostile write intact",
          s == 200 and r.get("rev") == 3 and len(r.get("pins")) == 2)

    # ---- caps ----------------------------------------------------------
    s, r = call("PUT", "/api/pinned-designs",
                {"pins": [pin(f"{i:012x}", f"p{i}") for i in range(49)]})
    check("49 pins -> 422 naming the cap", s == 422
          and "48" in str((r or {}).get("detail")))
    # 24 pins x (~119 KB dat + 2 x ~240 KB run images) ~ 14.4 MB: over the
    # 12 MB store cap while comfortably under the 16 MB streamed body cap,
    # so the refusal exercised is the store validator's
    big = [pin(f"{i:012x}", f"big{i}",
               custom_airfoils={f"custom:big{i}": "x" * 119_000},
               runs={"rans": {"status": {"state": "done"},
                              "images": {k: "data:image/jpeg;base64,"
                                         + "A" * 240_000
                                         for k in ("umag", "cp")}}})
           for i in range(24)]
    s, r = call("PUT", "/api/pinned-designs", {"pins": big})
    check("an over-bytes library -> 422 telling the user to delete pins",
          s == 422 and "too large" in str((r or {}).get("detail")))
    s, r = call("GET", "/api/pinned-designs")
    check("caps: previous library still intact",
          s == 200 and r.get("rev") == 3 and len(r.get("pins")) == 2)

    # ---- pre-rev bare-list file reads as rev 0 -------------------------
    (Path(SCRATCH_DATA) / "pinned_designs.json").write_text(
        json.dumps([pin("e5" * 6, "legacy")]), encoding="utf-8")
    s, r = call("GET", "/api/pinned-designs")
    check("pre-rev bare-list file reads as rev 0",
          s == 200 and r.get("rev") == 0
          and (r.get("pins") or [{}])[0].get("label") == "legacy")
    s, r = call("PUT", "/api/pinned-designs",
                {"pins": [pin("e5" * 6, "legacy")], "rev": 0})
    check("rev 0 write upgrades a bare file", s == 200 and r.get("rev") == 1)

    # ---- no tmp stragglers ---------------------------------------------
    strays = list(Path(SCRATCH_DATA).glob(".pinned_designs.*.tmp"))
    check("no tmp stragglers in the data dir", not strays, f"({strays})")

    # ---- the key= generalization left the preset libraries alone -------
    s, r = call("PUT", "/api/rule-presets",
                {"presets": [{"name": "regression",
                              "envelope": {"max_length_mm": 600}}]})
    ok_put = s == 200
    s, r = call("GET", "/api/rule-presets")
    check("rule presets still round-trip after the key= generalization",
          ok_put and s == 200
          and (r.get("presets") or [{}])[0].get("name") == "regression")
    s, r = call("PUT", "/api/ansys-presets",
                {"presets": [{"name": "regression", "settings": {}}]})
    ok_put = s == 200
    s, r = call("GET", "/api/ansys-presets")
    check("ANSYS presets still round-trip after the key= generalization",
          ok_put and s == 200
          and (r.get("presets") or [{}])[0].get("name") == "regression")

finally:
    _server.terminate()
    try:
        _server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _server.kill()

print(f"\n{sum(results)}/{len(results)} pinned-store checks passed")
sys.exit(0 if all(results) else 1)
