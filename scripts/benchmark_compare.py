"""Before/after comparison table from two benchmark JSONs.

    .venv\\Scripts\\python.exe scripts\\benchmark_compare.py ^
        docs\\benchmarks\\baseline_main.json docs\\benchmarks\\after_upgrade.json

Prints a Markdown table joined on the stable scenario keys, using the
version-independent `measured` block (full-paneling physics of each
winner), so the numbers compare like for like even though the internal
objective changed meaning between the versions.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def cell(sc, key, digits=1):
    if not sc or not sc.get("supported"):
        return "n/a"
    m = (sc.get("winner") or {}).get("measured") or {}
    v = m.get(key)
    if v is None:
        return "–"
    if isinstance(v, list):
        return "/".join(f"{x:.2f}" for x in v)
    return f"{v:.{digits}f}"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    before = load(sys.argv[1])
    after = load(sys.argv[2])
    keys = list(dict.fromkeys(list(before["scenarios"]) +
                              list(after["scenarios"])))
    print(f"| scenario | metric | before ({before['git_rev']}) "
          f"| after ({after['git_rev']}) |")
    print("|---|---|---|---|")
    for k in keys:
        b = before["scenarios"].get(k)
        a = after["scenarios"].get(k)
        rows = [
            ("downforce N", "downforce_n", 1),
            ("drag N", "drag_n", 2),
            ("L/D", "ld", 2),
            ("max loading frac", "frac_max", 3),
            ("slot gaps %c", "gaps_pct", 2),
        ]
        for label, key, d in rows:
            print(f"| {k} | {label} | {cell(b, key, d)} "
                  f"| {cell(a, key, d)} |")
        def notes_of(sc, key):
            if not sc:
                return "–"
            if not sc.get("supported"):
                return "refused (option not supported on this version)"
            notes = [n for n in (sc.get("target_note"),
                                 sc.get("load_cap_note"),
                                 sc.get("objective")) if n]
            # a version that predates the objective option ACCEPTED it
            # silently and ran the default target tracker — say so (only
            # judgeable on completed runs; older records omit the field
            # on failures too)
            if (key.startswith("max_downforce") and not sc.get("objective")
                    and sc.get("state") == "done"):
                notes.append("objective option silently ignored "
                             "(ran target-mode default)")
            if sc.get("state") != "done":
                notes.append(f"state={sc['state']}")
            return ", ".join(notes) if notes else "–"

        nb, na = notes_of(b, k), notes_of(a, k)
        if (nb, na) != ("–", "–"):
            print(f"| {k} | notes | {nb} | {na} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
