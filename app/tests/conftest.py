"""These validation suites are standalone scripts, not pytest modules: they
run their checks at import time (some against a live server) and exit
nonzero on failure. Letting pytest collect them would execute all of that
during collection and crash on the module-level sys.exit.

Run everything with one command (boots its own scratch server for the API
suite):

    .venv\\Scripts\\python.exe app\\tests\\run_all.py

or any single suite directly:

    .venv\\Scripts\\python.exe app\\tests\\test_manufacturing.py
"""
collect_ignore_glob = ["test_*.py"]
