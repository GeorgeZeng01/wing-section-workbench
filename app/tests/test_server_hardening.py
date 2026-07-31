"""Server/launcher hardening regression: session revision control, streamed
body-size cap, RANS start mutex, CFD-case cleanup, the Fluent mesh export's
exclusive claim, ANSYS-export state gating, atomic flat-file save names,
.dat name sanitization and the desktop fallback chain.

Self-contained: boots its own scratch uvicorn server on a free port with
WSS_DATA_DIR / WSS_EXPORTS_DIR pointed at throwaway temp dirs, so it never
touches the real app_data/ or exports/.

    .venv\\Scripts\\python.exe app\\tests\\test_server_hardening.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# isolation must be in place BEFORE app.server is imported: SESSION_FILE and
# EXPORTS_DIR are resolved at module import
SCRATCH_DATA = tempfile.mkdtemp(prefix="wss_hard_data_")
SCRATCH_EXPORTS = tempfile.mkdtemp(prefix="wss_hard_exports_")
os.environ["WSS_DATA_DIR"] = SCRATCH_DATA
os.environ["WSS_EXPORTS_DIR"] = SCRATCH_EXPORTS

PY = sys.executable

results = []


def check(label, ok, extra=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


GOOD = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
         "dx": -0.03, "dy": -0.03}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}


# ---------- .dat name sanitization (pure function) ----------

import numpy as np

from app.core import export as export_mod

coords = np.array([[1.0, 0.0], [0.5, 0.1], [0.0, 0.0]])

out = export_mod.dat_text(coords, "evil\n 0.500000 9.000000")
lines = out.splitlines()
check("dat_text: newline in name cannot inject coordinate lines",
      len(lines) == 1 + len(coords)
      and lines[0] == "evil 0.500000 9.000000")
check("dat_text: coordinate payload unchanged",
      lines[1] == " 1.000000 0.000000" and lines[-1] == " 0.000000 0.000000")

out2 = export_mod.dat_text(coords, "a\r\nb\tc\x00d")
check("dat_text: CR/LF/control chars collapse to single spaces",
      out2.splitlines()[0] == "a b c d")

out3 = export_mod.dat_text(coords, "x" * 500)
check("dat_text: name is length-capped", len(out3.splitlines()[0]) <= 80)

out4 = export_mod.dat_text(coords, "\r\n\x01\x1f  ")
check("dat_text: name that sanitizes to nothing gets a placeholder",
      out4.splitlines()[0] == "airfoil")

out5 = export_mod.dat_text(coords, "S1223 (uploaded)")
check("dat_text: ordinary names pass through verbatim",
      out5.splitlines()[0] == "S1223 (uploaded)")


# ---------- desktop launcher: fallback truthfulness ----------
# a stub pywebview whose start() rejects every signature is exactly the
# environment the signature ladder exists for — it must report failure so
# main() reaches the browser fallbacks

_stub = types.ModuleType("webview")
_stub.settings = {}


class _Hook:
    def __iadd__(self, fn):
        return self


def _create_window(*a, **k):
    return types.SimpleNamespace(events=types.SimpleNamespace(loaded=_Hook()))


_stub.create_window = _create_window


def _start_rejects(*a, **k):
    raise TypeError("start() got an unexpected keyword argument")


_stub.start = _start_rejects
sys.modules["webview"] = _stub
import app.desktop as desktop  # noqa: E402  (needs the stub in place)

# main() assigns the js_api global before opening the window; calling the
# opener directly needs the same precondition
desktop._api = None

check("desktop: start() rejecting every signature reports False",
      desktop._open_native_with_api("http://127.0.0.1:1", 1) is False)

_started = []
_stub.start = lambda *a, **k: _started.append(k)
check("desktop: successful start still reports True",
      desktop._open_native_with_api("http://127.0.0.1:1", 1) is True
      and len(_started) == 1)
del sys.modules["webview"]


# ---------- body-size middleware: streamed (chunked) counting ----------

import asyncio

import app.core as core_pkg
import app.server as server


def _asgi_probe(body_chunks):
    """Drive the middleware with a Content-Length-less stream; returns
    (response status, whether the inner app ran to completion)."""
    sent = []
    state = {"inner_done": False}

    async def inner(scope, receive, send):
        while True:
            msg = await receive()
            if msg["type"] != "http.request" or not msg.get("more_body"):
                break
        state["inner_done"] = True
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = server._BodySizeLimitMiddleware(inner)
    chunks = list(body_chunks)

    async def receive():
        body = chunks.pop(0)
        return {"type": "http.request", "body": body,
                "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    asyncio.run(mw(scope, receive, send))
    status = next((m["status"] for m in sent
                   if m["type"] == "http.response.start"), None)
    return status, state["inner_done"]


status, inner_done = _asgi_probe([b"x" * 1_000_000] * 17)
check("middleware: 17 MB chunked stream -> 413, endpoint never runs",
      status == 413 and not inner_done)

status, inner_done = _asgi_probe([b"y" * 1000] * 3)
check("middleware: small chunked stream passes through",
      status == 200 and inner_done)


# ---------- RANS start mutex (endpoint logic, stubbed solver) ----------

from fastapi import HTTPException  # noqa: E402

_lock_held = {"single": None, "queue": None}


def _probe_lock():
    got = server._rans_start_lock.acquire(blocking=False)
    if got:
        server._rans_start_lock.release()
    return not got


_cfd_run_stub = types.ModuleType("cfd_run")
_seen_start = {}
_seen_iters = {}


def _cfd_start(config, mesh_size, max_iters, n_ranks=1,
               engine="openfoam", mesher="fluent", conventions="default",
               settings=None):
    _lock_held["single"] = _probe_lock()
    _seen_start.update(engine=engine, mesh=mesh_size,
                       conventions=conventions, settings=settings)
    _seen_iters["max_iters"] = max_iters
    return "job-stub"


_cfd_run_stub.start = _cfd_start

_rq_stub = types.ModuleType("rans_queue")
_rq_stub.get_current = lambda: None


def _rq_start(items, mesh_size, max_iters, n_ranks=1, max_concurrent=1):
    _lock_held["queue"] = _probe_lock()
    return "queue-stub"


_rq_stub.start = _rq_start

_saved_cfd_run = getattr(core_pkg, "cfd_run", None)
_saved_rans_queue = getattr(core_pkg, "rans_queue", None)
core_pkg.cfd_run = _cfd_run_stub
core_pkg.rans_queue = _rq_stub
try:
    r = server.rans_start(server.RansStartBody(config=GOOD))
    check("rans_start: solver slot is claimed while the start mutex is held",
          r["job_id"] == "job-stub" and _lock_held["single"] is True)

    _rq_stub.get_current = lambda: types.SimpleNamespace(state="running")
    try:
        server.rans_start(server.RansStartBody(config=GOOD))
        code = None
    except HTTPException as e:
        code = e.status_code
    check("rans_start: active queue -> 409", code == 409, f"(got {code})")

    r2 = server.rans_queue_start(server.RansQueueBody(items=[{}]))
    check("rans_queue_start: queue registers under the same start mutex",
          r2["queue_id"] == "queue-stub" and _lock_held["queue"] is True)

    # cross-field engine/mesh validation: the Literal admits every
    # engine's values, so the endpoint must police the pairing itself
    _rq_stub.get_current = lambda: None   # reset from the 409 case
    for eng, mesh, want in (("fluent2d", "default", 200),
                            ("fluent2d", "studio-yplus1", 200),
                            ("fluent2d", "coarse", 422),
                            ("openfoam", "default", 422),
                            ("fluent", "studio-yplus1", 422),
                            ("openfoam", "coarse", 200)):
        try:
            server.rans_start(server.RansStartBody(
                config=GOOD, engine=eng, mesh_size=mesh))
            got = 200
        except HTTPException as e:
            got = e.status_code
        check(f"rans_start: {eng} + {mesh} -> {want}", got == want,
              f"(got {got})")

    # engine-aware iterations floor: fluent2d admits the 50-iteration
    # runs that replicate the manual walkthrough, the other engines keep
    # the 100 floor with an actionable message; below 50 nothing passes
    # the model
    r50 = server.rans_start(server.RansStartBody(
        config=GOOD, engine="fluent2d", mesh_size="default", max_iters=50))
    check("rans_start: fluent2d + 50 iterations is accepted",
          r50["job_id"] == "job-stub")

    # an omitted cap resolves per ENGINE: the 2D engine's documented
    # walkthrough length, the drift-stopped engines' generous ceiling
    for eng, mesh, want in (
            ("fluent2d", "default", server.FLUENT2D_ITERS),
            ("openfoam", "coarse", server.RANS_ITERS_DEFAULT)):
        _seen_iters.clear()
        server.rans_start(server.RansStartBody(config=GOOD, engine=eng,
                                               mesh_size=mesh))
        check(f"rans_start: an omitted cap resolves to {want} on {eng}",
              _seen_iters.get("max_iters") == want, f"({_seen_iters})")
    _seen_iters.clear()
    server.rans_start(server.RansStartBody(
        config=GOOD, engine="fluent2d", mesh_size="default",
        max_iters=1200))
    check("rans_start: an explicit cap still wins over the engine default",
          _seen_iters.get("max_iters") == 1200, f"({_seen_iters})")
    for eng in ("openfoam", "fluent"):
        try:
            server.rans_start(server.RansStartBody(
                config=GOOD, engine=eng, max_iters=99))
            got, detail = 200, ""
        except HTTPException as e:
            got, detail = e.status_code, str(e.detail)
        check(f"rans_start: {eng} + 99 iterations -> 422 naming the floor",
              got == 422 and "at least 100 iterations" in detail
              and "Fluent 2D" in detail, f"(got {got}: {detail})")
    try:
        server.RansStartBody(config=GOOD, engine="fluent2d", max_iters=49)
        got = 200
    except Exception:
        got = 422
    check("rans_start body: 49 iterations rejected for every engine",
          got == 422)

    _seen_start.clear()
    server.rans_start(server.RansStartBody(
        config=GOOD, engine="fluent2d", mesh_size="studio-yplus1",
        conventions="studio"))
    check("rans_start: conventions reach cfd_run.start verbatim",
          _seen_start == {"engine": "fluent2d", "mesh": "studio-yplus1",
                          "conventions": "studio", "settings": {}},
          f"({_seen_start})")

    # ---- ANSYS 2D settings overrides ----
    # the request model owns the ranges; the endpoint owns the pairing.
    # Boundaries: the first value inside the range must construct, the
    # first value outside must not.
    for field, ok_vals, bad_vals in (
            ("edge_size_mm", (1e-9, 5.0), (0, -1)),
            ("first_layer_mm", (1e-9, 2.0), (0, -0.5)),
            ("n_layers", (0, 100), (-1, 101)),
            ("growth", (1.0001, 3.0), (1.0, 3.0001)),
            ("front_l", (0.5, 20.0), (0.49, 20.01)),
            ("back_l", (0.5, 40.0), (0.49, 40.01)),
            ("top_h", (0.5, 20.0), (0.49, 20.01)),
            ("sc_budget_s", (30.0, 7200.0), (29.9, 7200.1)),
            ("wb_budget_s", (60.0, 43200.0), (59.9, 43200.1))):
        made = []
        for v in ok_vals:
            try:
                server.RansStartBody(config=GOOD, engine="fluent2d",
                                     mesh_size="default", **{field: v})
                made.append(True)
            except Exception:
                made.append(False)
        refused = []
        for v in bad_vals:
            try:
                server.RansStartBody(config=GOOD, engine="fluent2d",
                                     mesh_size="default", **{field: v})
                refused.append(False)
            except Exception:
                refused.append(True)
        check(f"override {field}: in-range accepted, out-of-range refused",
              all(made) and all(refused), f"({made}, {refused})")

    for field, v in (("edge_size_mm", float("inf")),
                     ("growth", float("nan")),
                     ("front_l", float("inf"))):
        try:
            server.RansStartBody(config=GOOD, engine="fluent2d",
                                 mesh_size="default", **{field: v})
            got = 200
        except Exception:
            got = 422
        check(f"override {field}: non-finite refused", got == 422)

    # an override on an engine that cannot honour it must be refused, not
    # silently dropped — a reported sizing the solve never used is a lie
    for eng, mesh in (("openfoam", "coarse"), ("fluent", "medium")):
        try:
            server.rans_start(server.RansStartBody(
                config=GOOD, engine=eng, mesh_size=mesh, edge_size_mm=0.2))
            got, detail = 200, ""
        except HTTPException as e:
            got, detail = e.status_code, str(e.detail)
        check(f"rans_start: override on the {eng} engine -> 422 naming it",
              got == 422 and "edge_size_mm" in detail
              and "fluent2d" in detail, f"(got {got}: {detail})")

    _seen_start.clear()
    server.rans_start(server.RansStartBody(
        config=GOOD, engine="fluent2d", mesh_size="default",
        edge_size_mm=0.25, n_layers=0, growth=1.15, front_l=4.0,
        wb_budget_s=1800))
    check("rans_start: set overrides reach cfd_run.start, unset ones "
          "stay absent",
          _seen_start.get("settings") == {"edge_size_mm": 0.25,
                                          "n_layers": 0, "growth": 1.15,
                                          "front_l": 4.0,
                                          "wb_budget_s": 1800.0},
          f"({_seen_start.get('settings')})")
finally:
    for name, saved in (("cfd_run", _saved_cfd_run),
                        ("rans_queue", _saved_rans_queue)):
        if saved is None:
            delattr(core_pkg, name)
        else:
            setattr(core_pkg, name, saved)


# ---------- CFD case export: cleanup + atomic name claim ----------

_cfd_stub = types.ModuleType("cfd")


class _MeshError(Exception):
    pass


_cfd_stub.MeshError = _MeshError
_seen = {}


def _build_fail_runtime(cfg, case, mesh_size):
    _seen["pre_created"] = case.is_dir()
    (case / "mesh.msh").write_text("partial", encoding="utf-8")
    raise RuntimeError("disk full")


_saved_cfd = getattr(core_pkg, "cfd", None)
core_pkg.cfd = _cfd_stub
try:
    body = server.CfdExportBody(config=GOOD)

    _cfd_stub.build_case = _build_fail_runtime
    try:
        server.export_cfd_save(body)
        err = None
    except RuntimeError as e:
        err = e
    leftovers = list(Path(SCRATCH_EXPORTS).glob("cfd_case_*"))
    check("cfd save: unexpected failure surfaces and leaves no half case",
          isinstance(err, RuntimeError) and _seen.get("pre_created") is True
          and not leftovers, f"(leftovers: {[p.name for p in leftovers]})")

    def _build_fail_mesh(cfg, case, mesh_size):
        (case / "mesh.msh").write_text("partial", encoding="utf-8")
        raise _MeshError("gmsh exploded")

    _cfd_stub.build_case = _build_fail_mesh
    try:
        server.export_cfd_save(body)
        code = None
    except HTTPException as e:
        code = e.status_code
    leftovers = list(Path(SCRATCH_EXPORTS).glob("cfd_case_*"))
    check("cfd save: mesh failure keeps its 422 and cleans up",
          code == 422 and not leftovers)

    def _build_ok(cfg, case, mesh_size):
        (case / "mesh.msh").write_text("ok", encoding="utf-8")
        return {"cells": 1}

    _cfd_stub.build_case = _build_ok
    r1 = server.export_cfd_save(body)
    r2 = server.export_cfd_save(body)
    check("cfd save: back-to-back saves claim distinct case dirs",
          r1["path"] != r2["path"] and Path(r1["path"]).is_dir()
          and Path(r2["path"]).is_dir(),
          f"({Path(r1['path']).name} vs {Path(r2['path']).name})")
finally:
    if _saved_cfd is None:
        delattr(core_pkg, "cfd")
    else:
        core_pkg.cfd = _saved_cfd


# ---------- Fluent mesh export: exclusive-claim wiring ----------
# the guard check alone is one-directional check-then-act; the export must
# register itself in cfd_run (claim_exclusive) for its whole duration and
# release in a finally, with the start mutex held only around guard+claim

_events = []

_cfdr_stub = types.ModuleType("cfd_run")
_cfdr_stub.claim_exclusive = lambda tag: _events.append(f"claim:{tag}")
_cfdr_stub.release_exclusive = lambda: _events.append("release")

_flu_stub = types.ModuleType("fluent_run")
_flu_stub.availability = lambda: {"available": True, "detail": ""}


def _export_ok(config, mesh_size, exports_dir):
    _events.append("export")
    _events.append("mutex-held" if _probe_lock() else "mutex-free")
    return {"filename": "fluent_mesh_stub", "path": "stub",
            "dir": str(exports_dir), "files": []}


_flu_stub.export_native_case = _export_ok

_rqs_stub = types.ModuleType("rans_queue")
_rqs_stub.get_current = lambda: None

_saved_mods = {n: getattr(core_pkg, n, None)
               for n in ("cfd_run", "fluent_run", "rans_queue")}
core_pkg.cfd_run = _cfdr_stub
core_pkg.fluent_run = _flu_stub
core_pkg.rans_queue = _rqs_stub
try:
    body = server.CfdExportBody(config=GOOD)
    r = server.export_fluent_mesh_save(body)
    check("fluent-mesh export: claim held across the export (mutex free), "
          "released after",
          r["filename"] == "fluent_mesh_stub"
          and _events == ["claim:a Fluent mesh export", "export",
                          "mutex-free", "release"], f"({_events})")

    _events.clear()

    def _claim_refused(tag):
        raise RuntimeError("a RANS verification is already running")

    _cfdr_stub.claim_exclusive = _claim_refused
    try:
        server.export_fluent_mesh_save(body)
        code = None
    except HTTPException as e:
        code = e.status_code
    check("fluent-mesh export: refused claim -> 409, no export, no release",
          code == 409 and _events == [], f"({code}, {_events})")

    _events.clear()
    _cfdr_stub.claim_exclusive = lambda tag: _events.append(f"claim:{tag}")

    def _export_boom(config, mesh_size, exports_dir):
        _events.append("export")
        raise RuntimeError("license lost mid-mesh")

    _flu_stub.export_native_case = _export_boom
    try:
        server.export_fluent_mesh_save(body)
        code = None
    except HTTPException as e:
        code = e.status_code
    check("fluent-mesh export: failed meshing -> 422 and the claim is "
          "still released",
          code == 422 and _events == ["claim:a Fluent mesh export", "export",
                                      "release"], f"({code}, {_events})")
finally:
    for _n, _saved in _saved_mods.items():
        if _saved is None:
            if hasattr(core_pkg, _n):
                delattr(core_pkg, _n)
        else:
            setattr(core_pkg, _n, _saved)


# ---------- fluent2d availability: both halves must be present ----------
# the endpoint composes fluent_run (solver) and scripts.fluent2d_workflow
# (Workbench chain); either missing must read unavailable with the reason

_flu_av = types.ModuleType("fluent_run")
_wb_av = types.ModuleType("fluent2d_workflow")
_saved_flu_av = getattr(core_pkg, "fluent_run", None)
_saved_wb_av = sys.modules.get("scripts.fluent2d_workflow")
core_pkg.fluent_run = _flu_av
sys.modules["scripts.fluent2d_workflow"] = _wb_av
try:
    _flu_av.availability = lambda: {"available": True, "detail": "",
                                    "awp_roots": {"AWP_ROOT261": "x"},
                                    "pyfluent": True}
    _wb_av.availability = lambda: {"available": True, "detail": "",
                                   "awp_root": "x", "version": "261"}
    r = server.fluent2d_availability()
    check("fluent2d availability: both halves present -> available",
          r["available"] is True and r["detail"] == ""
          and r["fluent"]["available"] is True
          and r["workbench"]["available"] is True, f"({r})")

    _wb_av.availability = lambda: {
        "available": False, "awp_root": "", "version": "",
        "detail": "SpaceClaim was not found in the ANSYS installation"}
    r = server.fluent2d_availability()
    check("fluent2d availability: missing Workbench half -> unavailable "
          "with the reason",
          r["available"] is False and "SpaceClaim" in r["detail"]
          and r["fluent"]["available"] is True, f"({r['detail']})")

    _wb_av.availability = lambda: (_ for _ in ()).throw(
        RuntimeError("broken module"))
    r = server.fluent2d_availability()
    check("fluent2d availability: broken workflow module -> unavailable, "
          "never a 500",
          r["available"] is False and r["detail"] != "", f"({r['detail']})")
finally:
    if _saved_flu_av is None:
        if hasattr(core_pkg, "fluent_run"):
            delattr(core_pkg, "fluent_run")
    else:
        core_pkg.fluent_run = _saved_flu_av
    if _saved_wb_av is None:
        sys.modules.pop("scripts.fluent2d_workflow", None)
    else:
        sys.modules["scripts.fluent2d_workflow"] = _saved_wb_av


# ---------- ANSYS 2D mesh export: exclusive-claim wiring ----------
# mirrors the Fluent mesh export contract: guard + claim under the start
# mutex, the multi-minute chain outside it, release in a finally — and a
# failed chain must leave no half-built bundle behind

_e2 = []

_cfdr2 = types.ModuleType("cfd_run")
_cfdr2.claim_exclusive = lambda tag: _e2.append(f"claim:{tag}")
_cfdr2.release_exclusive = lambda: _e2.append("release")

_rq2 = types.ModuleType("rans_queue")
_rq2.get_current = lambda: None

_wb2 = types.ModuleType("fluent2d_workflow")
_wb2.availability = lambda: {"available": True, "detail": "",
                             "awp_root": "x", "version": "261"}
_wb2.mesh_sizing = lambda mode, cfg: {"edge_size_mm": 0.1,
                                      "first_layer_mm": 1.0,
                                      "n_layers": 10, "growth": 1.2}


def _wb2_dxf(profiles, out_path, **kw):
    Path(out_path).write_text("dxf", encoding="utf-8")
    return {"dxf_path": str(out_path), "domain_m": (0.0, 0.0, 1.0, 1.0),
            "n_profiles": len(profiles), "n_points": 9}


_wb2.write_dxf_2d = _wb2_dxf


def _wb2_chain(dxf_path, work_dir, **kw):
    _e2.append("chain")
    _e2.append("mutex-held" if _probe_lock() else "mutex-free")
    w = Path(work_dir)
    w.mkdir(parents=True, exist_ok=True)
    (w / "FFF.msh").write_text("(2 2)", encoding="utf-8")
    return {"msh_path": str(w / "FFF.msh"), "n_cells": 43210,
            "zones": ["fluid", "inlet", "outlet", "ground",
                      "upper_bound", "profile"],
            "stage_s": {"spaceclaim": 1.0, "workbench": 2.0},
            "project_dir": str(w)}


_wb2.run_chain = _wb2_chain

_saved_mods2 = {n: getattr(core_pkg, n, None)
                for n in ("cfd_run", "rans_queue")}
_saved_wb2 = sys.modules.get("scripts.fluent2d_workflow")
core_pkg.cfd_run = _cfdr2
core_pkg.rans_queue = _rq2
sys.modules["scripts.fluent2d_workflow"] = _wb2
try:
    body2 = server.Fluent2DMeshBody(config=GOOD)
    r = server.export_fluent2d_mesh_save(body2)
    dest2 = Path(r["path"])
    readme2 = (dest2 / "README.txt").read_text(encoding="utf-8")
    check("2D mesh export: claim held across the chain (mutex free), "
          "released after",
          _e2 == ["claim:an ANSYS 2D mesh export", "chain", "mutex-free",
                  "release"], f"({_e2})")
    check("2D mesh export: bundle carries DXF + FFF.msh + README, no "
          "work dir",
          (dest2 / "FFF.msh").is_file()
          and "section_2d.dxf" in r["files"]
          and "README.txt" in r["files"]
          and "_mesh_work" not in r["files"]
          and r["n_cells"] == 43210, f"({r['files']})")
    check("2D mesh export: README carries the documented solve recipe, "
          "no repo paths",
          "upper_bound" in readme2 and "moving wall" in readme2
          and "500 iterations" in readme2
          and "app\\" not in readme2 and "app/" not in readme2)
    check("2D mesh export: README and payload report the recipe values "
          "when nothing is overridden",
          "Mesh sizing (default):" in readme2
          and "0.1 mm" in readme2 and r["overrides"] == {}
          and r["mesh_sizing"]["first_layer_mm"] == 1.0,
          f"({r.get('mesh_sizing')})")

    # overrides ride on top of the recipe: the chain gets the overridden
    # numbers, the DXF writer and the stage budgets get theirs, and the
    # label stays the recipe's
    _e2.clear()
    _seen_kw = {}

    def _wb2_dxf_kw(profiles, out_path, **kw):
        _seen_kw["dxf"] = kw
        return _wb2_dxf(profiles, out_path)

    def _wb2_chain_kw(dxf_path, work_dir, **kw):
        _seen_kw["chain"] = kw
        return _wb2_chain(dxf_path, work_dir)

    _wb2.write_dxf_2d = _wb2_dxf_kw
    _wb2.run_chain = _wb2_chain_kw
    try:
        r_ov = server.export_fluent2d_mesh_save(server.Fluent2DMeshBody(
            config=GOOD, sizing="default", edge_size_mm=0.4, n_layers=6,
            front_l=5.0, top_h=4.0, sc_budget_s=600))
        readme_ov = (Path(r_ov["path"]) / "README.txt").read_text(
            encoding="utf-8")
    finally:
        _wb2.write_dxf_2d = _wb2_dxf
        _wb2.run_chain = _wb2_chain
    check("2D mesh export: mesh overrides reach the chain, domain "
          "overrides reach the DXF, budgets ride along",
          _seen_kw["chain"]["edge_size_mm"] == 0.4
          and _seen_kw["chain"]["n_layers"] == 6
          and _seen_kw["chain"]["first_layer_mm"] == 1.0
          and _seen_kw["chain"]["sc_budget_s"] == 600.0
          and "wb_budget_s" not in _seen_kw["chain"]
          and _seen_kw["dxf"] == {"front_l": 5.0, "top_h": 4.0},
          f"({_seen_kw})")
    check("2D mesh export: the label stays the recipe's, the numbers are "
          "the ones used",
          "Mesh sizing (default + request overrides):" in readme_ov
          and "0.4 mm" in readme_ov and "up to 6 layers" in readme_ov
          and r_ov["sizing"] == "default"
          and r_ov["mesh_sizing"]["edge_size_mm"] == 0.4,
          f"({r_ov.get('mesh_sizing')})")

    # the export must hand the chain the clearances the inflation stack
    # faces (without them the cap never engages and Mechanical's whole
    # generation can collapse), and must document the stack the mesh GOT
    _e2.clear()
    _cap_kw = {}
    DEG = ("the inflation layers failed on this geometry — meshed "
           "WITHOUT boundary layers")

    def _wb2_chain_capped(dxf_path, work_dir, **kw):
        _cap_kw.update(kw)
        out = _wb2_chain(dxf_path, work_dir)
        out["inflation"] = {"first_layer_mm": 1.0, "n_layers": 0,
                            "capped": True, "degraded": True, "note": DEG}
        return out

    _wb2.run_chain = _wb2_chain_capped
    try:
        r_cap = server.export_fluent2d_mesh_save(
            server.Fluent2DMeshBody(config=GOOD))
        readme_cap = (Path(r_cap["path"]) / "README.txt").read_text(
            encoding="utf-8")
    finally:
        _wb2.run_chain = _wb2_chain
    check("2D mesh export: the chain is told the clearances the inflation "
          "stack faces",
          "slot_gap_mm" in _cap_kw
          and (_cap_kw.get("ground_clear_mm") or 0) > 0, f"({_cap_kw})")
    check("2D mesh export: README and payload state the inflation the "
          "mesh GOT, not the one requested",
          r_cap["mesh_sizing"]["n_layers"] == 0
          and r_cap["requested_sizing"]["n_layers"] == 10
          and (r_cap["inflation"] or {}).get("degraded") is True
          and "no inflation layers" in readme_cap
          and "up to 10 layers" not in readme_cap
          and DEG in readme_cap,
          f"({r_cap['mesh_sizing']}, {readme_cap[-300:]})")

    _e2.clear()

    def _claim_refused2(tag):
        raise RuntimeError("a RANS verification is already running")

    _cfdr2.claim_exclusive = _claim_refused2
    try:
        server.export_fluent2d_mesh_save(body2)
        code = None
    except HTTPException as e:
        code = e.status_code
    check("2D mesh export: refused claim -> 409, no chain, no release",
          code == 409 and _e2 == [], f"({code}, {_e2})")

    _e2.clear()
    _cfdr2.claim_exclusive = lambda tag: _e2.append(f"claim:{tag}")
    _before2 = {p.name for p in Path(SCRATCH_EXPORTS).glob("fluent2d_mesh_*")}

    def _chain_boom(dxf_path, work_dir, **kw):
        _e2.append("chain")
        raise RuntimeError("SpaceClaim stage failed")

    _wb2.run_chain = _chain_boom
    try:
        server.export_fluent2d_mesh_save(body2)
        code = None
    except HTTPException as e:
        code = e.status_code
    _after2 = {p.name for p in Path(SCRATCH_EXPORTS).glob("fluent2d_mesh_*")}
    check("2D mesh export: failed chain -> 422, claim released, no "
          "half-built bundle",
          code == 422
          and _e2 == ["claim:an ANSYS 2D mesh export", "chain", "release"]
          and _after2 == _before2, f"({code}, {_e2})")
finally:
    for _n, _saved in _saved_mods2.items():
        if _saved is None:
            if hasattr(core_pkg, _n):
                delattr(core_pkg, _n)
        else:
            setattr(core_pkg, _n, _saved)
    if _saved_wb2 is None:
        sys.modules.pop("scripts.fluent2d_workflow", None)
    else:
        sys.modules["scripts.fluent2d_workflow"] = _saved_wb2


# ---------- ANSYS solved-case export: state gate precedes the snapshot ----


class _FakeFluentJob:
    def __init__(self, case_dir, state="done"):
        self.id = "flu-fake"
        self.state = state
        self.case_dir = Path(case_dir)

    def snapshot(self):
        return {"engine": "fluent", "mesh_size": "coarse",
                "mesh": {"n_cells": 1234},
                "result": ({"n_iters_run": 800, "cl_rans": 2.91,
                            "cd_rans": 0.21, "converged": True}
                           if self.state == "done" else None)}


class _FlipOnSnapshot:
    """Emulates the running->done flip landing between the endpoint's two
    reads: state reads 'running' until snapshot() has been taken."""

    def __init__(self):
        self.id = "flu-race"
        self.case_dir = Path(SCRATCH_EXPORTS)   # never reached
        self._snapped = False

    @property
    def state(self):
        return "done" if self._snapped else "running"

    def snapshot(self):
        self._snapped = True
        return {"engine": "fluent", "mesh_size": "coarse", "mesh": None,
                "result": None}


_export_jobs = {}
_cfdr_get = types.ModuleType("cfd_run")
_cfdr_get.get = lambda job_id: _export_jobs.get(job_id)

_saved_cfdr = getattr(core_pkg, "cfd_run", None)
core_pkg.cfd_run = _cfdr_get
try:
    _case = Path(tempfile.mkdtemp(prefix="wss_hard_flucase_"))
    (_case / "case.cas.h5").write_bytes(b"cas")
    (_case / "case.dat.h5").write_bytes(b"dat")
    (_case / "config.json").write_text("{}", encoding="utf-8")

    _export_jobs["ok"] = _FakeFluentJob(_case)
    r = server.rans_export_fluent("ok")
    readme = (Path(r["path"]) / "README.txt").read_text(encoding="utf-8")
    check("ANSYS export: done run exports the real summary, not "
          "placeholders",
          "Cl 2.91" in readme and "README.txt" in r["files"])

    for st in ("failed", "cancelled"):
        _export_jobs["t"] = _FakeFluentJob(_case, state=st)
        try:
            server.rans_export_fluent("t")
            got = (None, "")
        except HTTPException as e:
            got = (e.status_code, str(e.detail))
        check(f"ANSYS export: {st} run -> honest 409",
              got[0] == 409 and st in got[1]
              and "has not finished" not in got[1], f"({got})")

    # the 2D engine's runs export through the same endpoint, with the
    # run's own conventions in the README instead of the slab recipe
    class _Fake2DJob(_FakeFluentJob):
        def snapshot(self):
            return {"engine": "fluent2d", "mesh_size": "default",
                    "mesh": {"n_cells": 55},
                    "result": {"n_iters_run": 500, "cl_rans": -2.1,
                               "cd_rans": 0.11, "converged": True,
                               "conventions": "default",
                               "ref_note": "Fluent's own references: "
                                           "area 1 m2 per meter depth."}}

    _export_jobs["f2d"] = _Fake2DJob(_case)
    r = server.rans_export_fluent("f2d")
    readme_2d = (Path(r["path"]) / "README.txt").read_text(encoding="utf-8")
    check("ANSYS export: fluent2d run accepted, README keeps its own "
          "conventions",
          "1 m2 per meter depth" in readme_2d
          and "slab" not in readme_2d.lower(), f"({r['files']})")

    class _FakeOFJob(_FakeFluentJob):
        def snapshot(self):
            return {"engine": "openfoam", "mesh_size": "coarse",
                    "mesh": None, "result": None}

    _export_jobs["of"] = _FakeOFJob(_case)
    try:
        server.rans_export_fluent("of")
        got = (None, "")
    except HTTPException as e:
        got = (e.status_code, str(e.detail))
    check("ANSYS export: OpenFOAM run still refused with a 422",
          got[0] == 422 and "OpenFOAM" in got[1], f"({got})")

    _export_jobs["race"] = _FlipOnSnapshot()
    try:
        server.rans_export_fluent("race")
        got = (None, "")
    except HTTPException as e:
        got = (e.status_code, str(e.detail))
    check("ANSYS export: state is judged before any snapshot is taken",
          got[0] == 409 and "has not finished" in got[1]
          and _export_jobs["race"]._snapped is False, f"({got})")


    # ---- rans stop: refusal text is engine-aware ----

    class _StubStopJob:
        def __init__(self, engine):
            self._engine = engine

        def stop_graceful(self):
            return False

        def snapshot(self):
            return {"engine": self._engine, "state": "running"}

    _export_jobs["flu-stop"] = _StubStopJob("fluent")
    _export_jobs["of-stop"] = _StubStopJob("openfoam")
    try:
        server.rans_stop("flu-stop")
        got = (None, "")
    except HTTPException as e:
        got = (e.status_code, str(e.detail))
    check("rans stop: Fluent refusal names the real reason, not "
          "'not running yet'",
          got[0] == 409 and "Cancel" in got[1] and "Fluent" in got[1]
          and "not running yet" not in got[1], f"({got})")
    try:
        server.rans_stop("of-stop")
        got = (None, "")
    except HTTPException as e:
        got = (e.status_code, str(e.detail))
    check("rans stop: OpenFOAM pre-solve refusal keeps its message",
          got[0] == 409 and "not running yet" in got[1], f"({got})")
    _export_jobs["f2d-stop"] = _StubStopJob("fluent2d")
    try:
        server.rans_stop("f2d-stop")
        got = (None, "")
    except HTTPException as e:
        got = (e.status_code, str(e.detail))
    check("rans stop: fluent2d refusal points at Cancel, not "
          "'not running yet'",
          got[0] == 409 and "Cancel" in got[1]
          and "not running yet" not in got[1], f"({got})")
finally:
    if _saved_cfdr is None:
        delattr(core_pkg, "cfd_run")
    else:
        core_pkg.cfd_run = _saved_cfdr


# ---------- ANSYS settings normalizer (what the preset library stores) ---
# the stored object must be exactly what a run would accept: unknown keys
# dropped, unset overrides left null so they resolve against the recipe at
# run time, every number inside the run's own range

_norm = server._validate_ansys_settings(
    {"name": "smuggled", "sizing": "studio-yplus1", "conventions": "studio",
     "n_iters": 750, "n_ranks": 4, "growth": 1.25, "junk": [1, 2]})
check("ansys settings: recipe kept, unknown keys and a smuggled name "
      "dropped",
      _norm["sizing"] == "studio-yplus1" and _norm["conventions"] == "studio"
      and _norm["n_iters"] == 750 and _norm["n_ranks"] == 4
      and "name" not in _norm and "junk" not in _norm, f"({_norm})")
check("ansys settings: unset overrides stay null, not frozen numbers",
      _norm["edge_size_mm"] is None and _norm["first_layer_mm"] is None
      and _norm["growth"] == 1.25)

_norm2 = server._validate_ansys_settings({})
check("ansys settings: an empty object normalizes to the documented "
      "defaults",
      _norm2["sizing"] == "default" and _norm2["conventions"] == "default"
      and _norm2["n_iters"] == 500 and _norm2["n_ranks"] == 1,
      f"({_norm2})")

for _bad in ({"sizing": "team"}, {"sizing": "coarse"},
             {"conventions": "team"}, {"n_iters": 49},
             {"n_iters": 20001}, {"n_ranks": 0}, {"n_ranks": 33},
             {"edge_size_mm": 0}, {"n_layers": 101},
             {"n_layers": 2.5}, {"growth": 1.0}, {"growth": 3.1},
             {"front_l": 0.4}, {"back_l": 41}, {"top_h": 21},
             {"sc_budget_s": 29}, {"wb_budget_s": 43201},
             {"edge_size_mm": "0.2"}, {"n_layers": True},
             {"growth": float("nan")}):
    try:
        server._validate_ansys_settings(_bad)
        _ok = False
    except ValueError:
        _ok = True
    check(f"ansys settings: {_bad} refused", _ok)

try:
    server._validate_ansys_settings([1, 2, 3])
    _ok = False
except ValueError:
    _ok = True
check("ansys settings: a non-object is refused", _ok)


# ---------- flat-file export save: same-second saves cannot collide ------
# freeze the timestamp so every save computes the same base name; the
# O_EXCL claim must hand each concurrent save its own file (an exists()
# probe lets the racers pick one name and silently overwrite)

_real_strftime = time.strftime
time.strftime = lambda fmt, *a: ("19990101-000000"
                                 if fmt == "%Y%m%d-%H%M%S"
                                 else _real_strftime(fmt, *a))
try:
    body = server.ExportBody(config=GOOD)
    _paths, _errs = [], []
    _plock = threading.Lock()
    _barrier = threading.Barrier(6)

    def _save():
        try:
            _barrier.wait(timeout=10)
            r = server.export_save("csv", body)
            with _plock:
                _paths.append(r["path"])
        except Exception as e:
            with _plock:
                _errs.append(repr(e))

    _threads = [threading.Thread(target=_save) for _ in range(6)]
    for _t in _threads:
        _t.start()
    for _t in _threads:
        _t.join(30)
    _on_disk = [p for p in _paths
                if Path(p).is_file() and Path(p).stat().st_size > 0]
    check("export save: six same-second saves claim six distinct files",
          not _errs and len(set(_paths)) == 6 and len(_on_disk) == 6,
          f"(errs {_errs}, paths {sorted(Path(p).name for p in _paths)})")
finally:
    time.strftime = _real_strftime


# ---------- scratch HTTP server for end-to-end checks ----------

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


def _read_response_head(s):
    s.settimeout(15)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except OSError:
        pass
    return buf


_server_proc = subprocess.Popen(
    [PY, "-m", "uvicorn", "app.server:app", "--port", str(PORT),
     "--log-level", "warning"],
    cwd=ROOT, env={**os.environ})

try:
    if not wait_healthy(BASE):
        check("scratch server became healthy", False)
    else:
        check("scratch server became healthy", True)

        # --- session revision control ---
        s, r = call("GET", "/api/session")
        check("session GET: empty store -> state None, rev 0",
              s == 200 and r == {"state": None, "rev": 0})

        s, r = call("POST", "/api/session", {"state": {"marker": "a", "t": 1}})
        check("session PUT: rev-less (shipped UI) write accepted",
              s == 200 and r.get("ok") is True and r.get("rev") == 1)

        s, r = call("GET", "/api/session")
        check("session GET: returns stored state with its rev",
              s == 200 and (r.get("state") or {}).get("marker") == "a"
              and r.get("rev") == 1)

        s, r = call("POST", "/api/session",
                    {"state": {"marker": "b", "t": 2}, "rev": 1})
        check("session PUT: matching rev accepted, rev increments",
              s == 200 and r.get("rev") == 2)

        s, r = call("POST", "/api/session",
                    {"state": {"marker": "stale", "t": 0}, "rev": 1})
        det = (r or {}).get("detail") or {}
        check("session PUT: stale rev -> 409 carrying current rev + state",
              s == 409 and det.get("rev") == 2
              and (det.get("state") or {}).get("marker") == "b")

        s, r = call("GET", "/api/session")
        check("session: the stale write never landed",
              s == 200 and (r.get("state") or {}).get("marker") == "b"
              and r.get("rev") == 2)

        s, r = call("POST", "/api/session", {"state": {"marker": "c", "t": 3}})
        check("session PUT: rev-less write keeps last-write-wins",
              s == 200 and r.get("rev") == 3)

        # --- pre-rev session.json (bare state) stays readable ---
        (Path(SCRATCH_DATA) / "session.json").write_text(
            json.dumps({"marker": "legacy", "t": 9}), encoding="utf-8")
        s, r = call("GET", "/api/session")
        check("session GET: pre-rev bare file reads as rev 0",
              s == 200 and (r.get("state") or {}).get("marker") == "legacy"
              and r.get("rev") == 0)
        s, r = call("POST", "/api/session", {"state": {"marker": "up"},
                                             "rev": 0})
        check("session PUT: rev 0 write upgrades a bare file",
              s == 200 and r.get("rev") == 1)

        # --- preset libraries: the same revision control ---
        # a preset PUT carries the client's WHOLE library, so a second
        # window's save would otherwise vanish on the next write here
        for path, what in (("/api/ansys-presets", "ANSYS"),
                           ("/api/rule-presets", "rule")):
            body = ({"name": "one", "settings": {}} if "ansys" in path
                    else {"name": "one", "envelope": {}})
            body2 = dict(body, name="two")
            s, r = call("GET", path)
            check(f"{what} presets GET: empty library -> rev 0",
                  s == 200 and r == {"presets": [], "rev": 0}, f"({r})")
            s, r = call("PUT", path, {"presets": [body]})
            check(f"{what} presets PUT: rev-less write accepted",
                  s == 200 and r.get("rev") == 1, f"({r})")
            s, r = call("PUT", path, {"presets": [body, body2], "rev": 1})
            check(f"{what} presets PUT: matching rev accepted, rev "
                  f"increments", s == 200 and r.get("rev") == 2, f"({r})")
            s, r = call("PUT", path, {"presets": [body], "rev": 1})
            det = (r or {}).get("detail") or {}
            check(f"{what} presets PUT: stale rev -> 409 carrying the "
                  f"newer library",
                  s == 409 and det.get("rev") == 2
                  and len(det.get("presets") or []) == 2, f"({s}: {det})")
            s, r = call("GET", path)
            check(f"{what} presets: the stale write never landed",
                  s == 200 and len(r.get("presets") or []) == 2
                  and r.get("rev") == 2, f"({r})")
            # a pre-rev library on disk is a bare list — still readable
            fname = ("ansys_presets.json" if "ansys" in path
                     else "rule_presets.json")
            (Path(SCRATCH_DATA) / fname).write_text(
                json.dumps([dict(body, name="legacy")]), encoding="utf-8")
            s, r = call("GET", path)
            check(f"{what} presets GET: pre-rev bare list reads as rev 0",
                  s == 200 and r.get("rev") == 0
                  and (r.get("presets") or [{}])[0].get("name") == "legacy",
                  f"({r})")
            for bad_name in (["x"], 123, {"n": 1}, None):
                s, _ = call("PUT", path,
                            {"presets": [dict(body, name=bad_name)]})
                check(f"{what} preset name {bad_name!r} -> 422 (never a "
                      f"stored repr)", s == 422, f"(got {s})")

        # --- body cap: oversized Content-Length rejected before upload ---
        with socket.create_connection(("127.0.0.1", PORT), timeout=30) as sk:
            sk.sendall((f"POST /api/session HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{PORT}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {server.MAX_BODY_BYTES + 1}\r\n"
                        f"\r\n").encode())
            head = _read_response_head(sk)
        check("oversized Content-Length -> 413 before any body upload",
              head.startswith(b"HTTP/1.1 413"))

        # --- body cap: chunked transfer (no Content-Length) rejected ---
        # a reader thread captures the 413 the instant it arrives: reading
        # after the server has closed would lose the response to the RST
        # that a continued send provokes
        sk = socket.create_connection(("127.0.0.1", PORT), timeout=30)
        head_box = {"head": b""}
        sent = 0
        try:
            sk.sendall((f"POST /api/session HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{PORT}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Transfer-Encoding: chunked\r\n\r\n").encode())
            reader = threading.Thread(
                target=lambda: head_box.__setitem__(
                    "head", _read_response_head(sk)), daemon=True)
            reader.start()
            chunk = b"x" * 65536
            frame = b"10000\r\n" + chunk + b"\r\n"
            try:
                while sent < server.MAX_BODY_BYTES + 131_072:
                    sk.sendall(frame)
                    sent += len(chunk)
            except OSError:
                pass               # server already rejected and closed
            reader.join(timeout=20)
        finally:
            sk.close()
        check("chunked body over cap -> 413 mid-stream",
              head_box["head"].startswith(b"HTTP/1.1 413"),
              f"(after {sent} bytes sent)")

        # --- normal-size requests still pass through the new middleware ---
        s, r = call("POST", "/api/analyze", {"config": GOOD})
        check("analyze happy path unaffected by the streamed body cap",
              s == 200 and "forces" in (r or {}))
finally:
    _server_proc.terminate()
    try:
        _server_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _server_proc.kill()
    import shutil
    shutil.rmtree(SCRATCH_DATA, ignore_errors=True)
    shutil.rmtree(SCRATCH_EXPORTS, ignore_errors=True)

# ---- error details must not carry machine paths ----
# an OSError from deep in the run tree stringifies with the whole home
# directory, and these strings end up in screenshots and pasted issue
# reports — the publisher's username must not ride out with them
_B = chr(92)
for _raw, _must_go, _must_stay in (
        ("cannot open C:" + _B + "Users" + _B + "George" + _B + "wss" + _B
         + "run" + _B + "p", "George", "cannot open"),
        ("[Errno 2] No such file: '/home/george/wss/case/200/U'",
         "george", "No such file"),
        ("failed reading " + _B + _B + "SERVER" + _B + "share" + _B
         + "secret.dat", "SERVER", "failed reading")):
    _out = server._scrub_paths(_raw)
    check("error detail drops the machine path, keeps the message",
          _must_go not in _out and _must_stay in _out, f"({_out})")
check("scrubbing keeps the filename that identifies the problem",
      server._scrub_paths(
          "cannot open C:" + _B + "wss" + _B + "run" + _B + "p").endswith("p"))
check("messages without paths are untouched",
      server._scrub_paths("iterations must be between 50 and 20000")
      == "iterations must be between 50 and 20000")
check("KeyError details are still unquoted after scrubbing",
      server._err_detail(KeyError("airfoil 'x' not found"))
      == "airfoil 'x' not found")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
