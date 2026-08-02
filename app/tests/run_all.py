"""Run every validation suite with one command.

    .venv\\Scripts\\python.exe app\\tests\\run_all.py

The offline suites run in-process order; the API suite gets its own scratch
server on a free port, started and torn down here.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = sys.executable

OFFLINE = ["test_manufacturing.py", "test_panel_validation.py",
           "test_model_and_data.py", "test_export_content.py",
           "test_cfd_case.py", "test_cfd_run.py", "test_delta_cd.py",
           "test_recirculation.py", "test_harvest.py",
           "test_optimizer_candidates.py", "test_shaping.py",
           "test_rules_envelope.py", "test_optimizer_target_modes.py",
           "test_slot_signature.py", "test_optimizer_objectives.py",
           "test_pareto_front.py", "test_rans_queue.py",
           "test_wake_shadow.py", "test_fluent_workflow.py",
           "test_fluent_run.py",
           "test_fluent2d_workflow.py", "test_fluent2d_run.py",
           "test_frontend_fl2d_state.py", "test_frontend_rules.py",
           "test_geometry_hardening.py", "test_server_hardening.py"]


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def wait_healthy(base: str, timeout: float = 30.0) -> bool:
    # proxy-free: a system/env proxy must not swallow the loopback probe
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


def main() -> int:
    failed = []
    # defense in depth: every offline suite gets scratch data and exports
    # dirs even if it forgets to isolate itself — a test bug must never
    # write into (or prune!) the user's real app_data or exports again.
    # EXPORTS_DIR is resolved independently of WSS_DATA_DIR, so both are
    # needed
    import shutil
    import tempfile
    offline_data = tempfile.mkdtemp(prefix="wss_test_offline_data_")
    offline_exports = tempfile.mkdtemp(prefix="wss_test_offline_exports_")
    offline_env = {**os.environ, "WSS_DATA_DIR": offline_data,
                   "WSS_EXPORTS_DIR": offline_exports}
    try:
        for name in OFFLINE:
            print(f"\n=== {name} ===", flush=True)
            if subprocess.run([PY, str(HERE / name)], cwd=ROOT,
                              env=offline_env).returncode:
                failed.append(name)
    finally:
        shutil.rmtree(offline_data, ignore_errors=True)
        shutil.rmtree(offline_exports, ignore_errors=True)

    print("\n=== test_api_adversarial.py (scratch server) ===", flush=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    # isolate the scratch server's session store — the suite exercises
    # /api/session and must never clobber the real working state
    scratch_data = tempfile.mkdtemp(prefix="wss_test_data_")
    scratch_exports = tempfile.mkdtemp(prefix="wss_test_exports_")
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.server:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env={**os.environ, "WSS_DATA_DIR": scratch_data,
                       "WSS_EXPORTS_DIR": scratch_exports})
    try:
        if not wait_healthy(base):
            print("FAIL  scratch server did not become healthy")
            failed.append("test_api_adversarial.py (server)")
        else:
            env = {**os.environ, "WSS_TEST_BASE": base}
            if subprocess.run([PY, str(HERE / "test_api_adversarial.py")],
                              cwd=ROOT, env=env).returncode:
                failed.append("test_api_adversarial.py")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(scratch_data, ignore_errors=True)
        shutil.rmtree(scratch_exports, ignore_errors=True)

    print("\n" + "=" * 50)
    if failed:
        print("FAILED suites:", ", ".join(failed))
        return 1
    print("All suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
