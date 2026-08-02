"""The drag comparison column and its bound semantics.

Pins cfd_run.delta_cd: measured profile Cd against the attached-flow
estimate's CD_profile_stack, and the upper-bound flag that must ride with
it whenever the estimate's polar lookup was capped.

WHAT THIS COLUMN IS NOT. It was introduced believing separation reads louder
in drag than in lift. Six converged Fluent 2D solves refuted that: an
ATTACHED validated baseline reads +626.1% and +524.2%, while designs every
other channel calls separated read +582.1%, +523.9%, +442.7% and +395.5%.
The classes overlap completely and the healthiest design in the set reads
HIGHEST. is_upper_bound came back True on all six — the polar lookup was
capped every time — so what the column tracks is how far the capped
attached-flow estimate falls short of a loaded ground-effect stack's real
drag, and that gap is 4-6x regardless of what the flow is doing.

It is kept because a 5x gap is real information about the ESTIMATE: it says
the estimate's drag is not usable for this design. It must never be read as
a statement about attachment, and this suite pins the arithmetic and the
bound semantics only.

What this suite deliberately does NOT pin: any threshold on the delta. There
is no threshold to pin — the classes do not separate.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_delta_cd.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import cfd_run  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def panel(cd_profile, lower_bound=False, **extra):
    p = {"c_est": 3.0, "c_free": 2.0, "c_ground": 3.2,
         "cd_profile": cd_profile,
         "cd_profile_is_lower_bound": lower_bound,
         "cd_capped_roles": ["main"] if lower_bound else [],
         "k_g_used": 0.4, "downforce_n": 500.0}
    p.update(extra)
    return p


# ---- unmeasurable inputs return None, never a pass ----

check("no panel estimate -> (None, False)",
      cfd_run.delta_cd(0.4, None) == (None, False))
check("panel present but no cd_profile key -> (None, False)",
      cfd_run.delta_cd(0.4, {"c_est": 3.0}) == (None, False))
check("cd_profile explicitly None -> (None, False)",
      cfd_run.delta_cd(0.4, panel(None)) == (None, False))
check("degenerate (near-zero) estimate -> (None, False), no ZeroDivision",
      cfd_run.delta_cd(0.4, panel(1e-12)) == (None, False))
check("exactly zero estimate -> (None, False)",
      cfd_run.delta_cd(0.4, panel(0.0)) == (None, False))

# ---- the ratio itself ----

d, b = cfd_run.delta_cd(0.4, panel(0.1))
check("4x the estimate reads +300% — arithmetic only, NOT a verdict on "
      "attachment (an attached baseline measured +626%)",
      d == 300.0 and b is False, f"({d}, {b})")
d, _ = cfd_run.delta_cd(0.1, panel(0.1))
check("measured equals estimate -> 0.0%", d == 0.0, f"({d})")
d, _ = cfd_run.delta_cd(0.08, panel(0.1))
check("measured below estimate reads negative", d == -20.0, f"({d})")
d, _ = cfd_run.delta_cd(0.13579, panel(0.1))
check("rounded to one decimal like delta_cl_pct", d == 35.8, f"({d})")
d, _ = cfd_run.delta_cd(0.0, panel(0.1))
check("zero measured drag is a number, not an error", d == -100.0, f"({d})")

# ---- bound semantics: a capped polar understates the estimate, so the
#      ratio OVERSTATES the excess and the delta is an upper bound ----

d, b = cfd_run.delta_cd(0.4, panel(0.1, lower_bound=True))
check("capped polar lookup flags the delta as an upper bound",
      d == 300.0 and b is True, f"({d}, {b})")
d, b = cfd_run.delta_cd(0.4, panel(0.1, lower_bound=False))
check("uncapped lookup does not flag a bound", b is False, f"({b})")
_, b = cfd_run.delta_cd(0.4, panel(0.1, cd_profile_is_lower_bound=None))
check("missing/None cap flag coerces to False, never to True", b is False)
check("the flag is a real bool, not the source list/truthy value",
      isinstance(cfd_run.delta_cd(0.4, panel(0.1, lower_bound=True))[1],
                 bool))

# ---- every engine carries the column, on the same basis ----

SRC = {name: (ROOT / "app" / "core" / f"{name}.py").read_text(
    encoding="utf-8") for name in ("cfd_run", "fluent2d_run", "fluent_run")}

for name, src in SRC.items():
    check(f"{name} publishes delta_cd_pct", '"delta_cd_pct"' in src)
    check(f"{name} publishes delta_cd_is_upper_bound",
          '"delta_cd_is_upper_bound"' in src)
    check(f"{name} carries the cap flag into the panel dict",
          '"cd_profile_is_lower_bound"' in src)

check("fluent2d compares the CHORD-referenced mean, not the raw one",
      "cfd_run.delta_cd(cdc_mean, panel)" in SRC["fluent2d_run"])
check("openfoam compares its own tail mean",
      "delta_cd(cd_mean, panel)" in SRC["cfd_run"])
check("3D fluent compares its own tail mean",
      "cfd_run.delta_cd(cd_mean, panel)" in SRC["fluent_run"])
check("the helper is defined once and shared, not copied per engine",
      SRC["cfd_run"].count("def delta_cd(") == 1
      and "def delta_cd(" not in SRC["fluent2d_run"]
      and "def delta_cd(" not in SRC["fluent_run"])
check("comparison is against profile drag, never the total",
      'co["CD_profile_stack"]' in SRC["cfd_run"]
      and "drag_total_n" not in SRC["cfd_run"])

print(f"\n{sum(results)}/{len(results)} delta_cd checks passed")
sys.exit(0 if all(results) else 1)
