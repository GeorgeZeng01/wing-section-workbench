"""Server/launcher hardening regression: session revision control, streamed
body-size cap, RANS start mutex, CFD-case cleanup, .dat name sanitization
and the desktop fallback chain.

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


def _cfd_start(config, mesh_size, max_iters):
    _lock_held["single"] = _probe_lock()
    return "job-stub"


_cfd_run_stub.start = _cfd_start

_rq_stub = types.ModuleType("rans_queue")
_rq_stub.get_current = lambda: None


def _rq_start(items, mesh_size, max_iters):
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

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
