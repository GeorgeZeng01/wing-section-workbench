@echo off
rem One-click launch: open Wing Section Studio as a native desktop app window.
rem Uses pythonw (no console) and exits immediately.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "%~dp0app\desktop.py"
