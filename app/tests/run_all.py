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
           "test_cfd_case.py", "test_cfd_run.py",
           "test_optimizer_candidates.py", "test_shaping.py",
           "test_rules_envelope.py"]


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
    for name in OFFLINE:
        print(f"\n=== {name} ===", flush=True)
        if subprocess.run([PY, str(HERE / name)], cwd=ROOT).returncode:
            failed.append(name)

    print("\n=== test_api_adversarial.py (scratch server) ===", flush=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    # isolate the scratch server's session store — the suite exercises
    # /api/session and must never clobber the real working state
    import tempfile
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
        import shutil
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
