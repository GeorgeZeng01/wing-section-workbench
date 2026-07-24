"""Wing Section Studio — native desktop launcher.

Runs the FastAPI server in-process on a private local port and presents the
UI in a native application window (Windows WebView2 via pywebview) — no
browser, no address bar, no tabs. Closing the window shuts the server down.

Launch (no console window):
    .venv\\Scripts\\pythonw.exe app\\desktop.py
or double-click the "Wing Section Studio" shortcut.

The window is the primary experience; if the native runtime is unavailable
the launcher falls back to an Edge/Chrome app window, then to the default
browser, so it always opens something.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_TITLE = "Wing Section Studio"
ICON_PATH = ROOT / "app" / "static" / "favicon.ico"
PORT_FILE = Path.home() / ".wing_section_studio_port"

_server = None  # uvicorn.Server, so fallbacks can stop it on exit


def _log(msg: str) -> None:
    """Write diagnostics only if a stream exists (pythonw has none)."""
    try:
        if sys.stderr is not None:
            print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _start_server(port: int):
    """Start uvicorn in a daemon thread; return the Server instance."""
    import uvicorn
    from app.server import app

    global _server
    # A minimal log config that never touches sys.stderr — under pythonw the
    # standard streams are None and uvicorn's default StreamHandler would fail.
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"null": {"class": "logging.NullHandler"}},
        "root": {"handlers": ["null"], "level": "WARNING"},
    }
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False,
                            log_config=log_config)
    _server = uvicorn.Server(config)
    # Server.run installs signal handlers only on the main thread, so running
    # it in a worker thread is safe and leaves this thread free for the GUI.
    threading.Thread(target=_server.run, name="uvicorn", daemon=True).start()
    return _server


def _wait_healthy(port: int, timeout: float = 25.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    # proxy-free opener: the default one honors http_proxy/system proxies,
    # which routes the loopback probe to a corporate proxy that cannot reach
    # this machine's 127.0.0.1 — the launcher would then report "server did
    # not start" forever although the server is up
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _stop_server():
    if _server is not None:
        _server.should_exit = True


_SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Wing Section Studio</title><style>
  html,body{height:100%;margin:0;background:#0b101b;color:#a8b2c7;
    font:14px "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
    display:flex;align-items:center;justify-content:center}
  .box{text-align:center}
  .mark{font:600 18px "Segoe UI Variable Display","Segoe UI",sans-serif;
    letter-spacing:.14em;color:#e9edf6;text-transform:uppercase;margin-bottom:6px}
  .sub{font-size:12px;color:#66718a;margin-bottom:26px}
  .ring{width:34px;height:34px;margin:0 auto;border-radius:50%;
    border:3px solid #243050;border-top-color:#8fa0c0;
    animation:s .8s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
  .note{margin-top:22px;font-size:12px}
</style></head><body><div class="box">
  <div class="mark">Wing Section Studio</div>
  <div class="sub">multi-element design &amp; optimization</div>
  <div class="ring"></div>
  <div class="note">Starting aerodynamic models&hellip;</div>
</div></body></html>"""

_ERROR_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{height:100%;margin:0;background:#0b101b;color:#a8b2c7;
    font:14px "Segoe UI",system-ui,sans-serif;
    display:flex;align-items:center;justify-content:center}
  .box{max-width:420px;text-align:center;line-height:1.6}
  b{color:#e9edf6}
</style></head><body><div class="box">
  <b>The analysis server did not start.</b><br>
  Close this window and launch again. If it persists, run
  <code>start "" .venv\\Scripts\\python.exe -m uvicorn app.server:app</code>
  from the project folder to see the error.
</div></body></html>"""


def _open_app_browser(url: str) -> bool:
    """Chromeless Edge/Chrome '--app' window (blocks until it closes)."""
    import shutil
    import subprocess
    import tempfile

    candidates = []
    import os
    for base in (os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramFiles"),
                 os.environ.get("LocalAppData")):
        if not base:
            continue
        candidates += [
            Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    for name in ("msedge", "chrome"):
        w = shutil.which(name)
        if w:
            candidates.append(Path(w))

    exe = next((c for c in candidates if Path(c).exists()), None)
    if exe is None:
        return False
    # one profile per port: with a shared profile a second instance's
    # browser process delegates to the first and returns immediately, and
    # this launcher would then shut down its own (still displayed) server
    profile = (Path(tempfile.gettempdir())
               / f"wing_section_studio_profile_{url.rsplit(':', 1)[-1]}")
    try:
        subprocess.run([str(exe), f"--app={url}",
                        "--window-size=1480,920",
                        f"--user-data-dir={profile}", "--no-first-run"],
                       check=False)
        return True
    except Exception as exc:
        _log(f"app-window browser unavailable ({exc})")
        return False


class _Api:
    def open_external(self, href):
        import webbrowser
        webbrowser.open(href)


def main():
    port = _free_port()
    _start_server(port)
    url = f"http://127.0.0.1:{port}"

    try:
        PORT_FILE.write_text(url, encoding="utf-8")
    except Exception:
        pass

    # self-test hook: verify the embedded server without opening a window
    import os
    if os.environ.get("WSS_NO_WINDOW") == "1":
        if not _wait_healthy(port):
            _log("server failed to start")
            _stop_server()
            return 1
        print(f"READY {url}", flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        _stop_server()
        return 0

    global _api
    _api = _Api()
    # the native window opens immediately on a splash and swaps to the app
    # as soon as the server reports healthy
    opened = _open_native_with_api(url, port)
    if not opened:
        if not _wait_healthy(port):
            _log("server failed to start")
            _stop_server()
            return 1
        opened = _open_app_browser(url)
    if not opened:
        import webbrowser
        webbrowser.open(url)
        # a plain browser tab gives nothing to block on. The UI heartbeats
        # the server every minute, so exit once no request has arrived for a
        # few minutes — the tab is gone — instead of lingering forever.
        try:
            from app import server as server_mod
            while True:
                time.sleep(5.0)
                if time.time() - server_mod.last_activity() > 300.0:
                    _log("no UI activity for 5 minutes — shutting down")
                    break
        except KeyboardInterrupt:
            pass

    _stop_server()
    try:
        PORT_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


def _open_native_with_api(url: str, port: int) -> bool:
    try:
        import webview
    except Exception:
        return False
    try:
        # exports use the browser download path; this surfaces each one as a
        # native Save As dialog instead of silently cancelling it
        webview.settings["ALLOW_DOWNLOADS"] = True

        icon = str(ICON_PATH) if ICON_PATH.exists() else None
        window = webview.create_window(
            APP_TITLE, html=_SPLASH_HTML,
            width=1480, height=920, min_size=(1024, 680),
            background_color="#0b101b", js_api=_api,
        )

        def _external_links():
            js = """
            document.addEventListener('click', function (e) {
              var a = e.target.closest && e.target.closest('a[href]');
              if (!a) return;
              var href = a.getAttribute('href') || '';
              if (/^https?:/i.test(href) && a.host !== location.host) {
                e.preventDefault();
                if (window.pywebview) window.pywebview.api.open_external(a.href);
              }
            }, true);
            """
            try:
                window.evaluate_js(js)
            except Exception:
                pass

        window.events.loaded += _external_links

        def _boot():
            if _wait_healthy(port):
                window.load_url(url)
            else:
                window.load_html(_ERROR_HTML)

        # a persistent profile: localStorage niceties (dismissed explainers)
        # survive restarts. The working design itself is persisted
        # server-side and does not depend on this.
        storage = str(Path.home() / ".wing_section_studio" / "webview")
        kwargs = {"func": _boot, "private_mode": False,
                  "storage_path": storage}
        if icon:
            kwargs["icon"] = icon
        for attempt in (kwargs,
                        {k: v for k, v in kwargs.items()
                         if k in ("func", "icon")},
                        {"func": _boot}):
            try:
                webview.start(**attempt)
                return True
            except TypeError:
                continue
        # every start() signature was rejected — no window ever opened, so
        # the caller must fall back to a browser
        _log("pywebview start() rejected every known signature; falling back")
        return False
    except Exception as exc:
        _log(f"native window unavailable ({exc}); falling back")
        return False


if __name__ == "__main__":
    sys.exit(main())
