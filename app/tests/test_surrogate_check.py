"""Surrogate cross-check gate — everything except xfoil.exe itself.

The gate's verdict math (verified / capped / unverified), the caching
contract (one XFOIL run per base section and quarter-decade Reynolds
bucket, in process and on disk, across wrapper specs), the kill switch,
and the integration that matters: a capped section's loading fractions,
drag caps, warnings and confidence all move through analysis without
any consumer changing — the gate feeds the numbers they already read.

The XFOIL seam (viscous._xfoil_for_check) is faked throughout; the
measured thresholds behind the constants live in DECISIONS.md and
app_data/surrogate_sweep.json.

Run directly:  python app/tests/test_surrogate_check.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="wss_xcheck_")
os.environ["WSS_DATA_DIR"] = _TMP
os.environ["WSS_EXPORTS_DIR"] = str(Path(_TMP) / "exports")
# the gate is ON for this suite (run_all switches it off for the others,
# whose scratch dirs would otherwise spawn real xfoil.exe runs)
os.environ["WSS_SURROGATE_CHECK"] = "on"

import numpy as np  # noqa: E402

from app.core import analysis, viscous  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


RE = 4.67e5
NCRIT = 7.0
A0, A1, DA = viscous._XCHECK_ALPHAS
N_REQ = int(round((A1 - A0) / DA)) + 1


class FakeXfoil:
    """Seam stand-in: per-base scripted outcomes, calls recorded."""

    def __init__(self):
        self.calls = []
        self.script = {}   # base -> ("full", cl_max) | ("partial",
                           #          n_pts, cl_max) | ("raise",)

    def __call__(self, base, re_key, ncrit):
        self.calls.append((base, re_key, ncrit))
        kind = self.script.get(base, ("full", None))
        if kind[0] == "raise":
            raise RuntimeError("xfoil exploded")
        if kind[0] == "partial":
            _, n, cl_max = kind
            return {"alpha": np.linspace(A0, A0 + n - 1, n),
                    "CL": np.linspace(cl_max - 0.3, cl_max, n)}
        # full convergence tracking the surrogate's own ceiling
        cl_max = kind[1]
        if cl_max is None:
            p = viscous.polar(base, re_key, ncrit, "large")
            cl_max = float(np.max(p["CL"]))
        return {"alpha": np.linspace(A0, A1, N_REQ),
                "CL": np.linspace(cl_max - 1.0, cl_max, N_REQ)}


def reset_gate(fake):
    viscous._xcheck_mem.clear()
    viscous._xcheck_disk_loaded = True   # skip disk unless a test opts in
    viscous._xfoil_for_check = fake


REAL_SEAM = viscous._xfoil_for_check

# ---- constants are ordered sanely (values are MEASURED — see sweep) ----

check("constants: evidence floor < convergence line inside (0,1), "
      "ratio line > 1, cap margin >= 1",
      0.0 < viscous.XCHECK_EVIDENCE_MIN < viscous.XCHECK_CONV_OK < 1.0
      and viscous.XCHECK_RATIO_OK > 1.0
      and viscous.XCHECK_CAP_MARGIN >= 1.0)

# ---- verdict math ----

fake = FakeXfoil()
reset_gate(fake)
try:
    # verified: full convergence, agreement
    fake.script["s1223"] = ("full", None)
    chk = viscous.surrogate_check("s1223", RE, NCRIT)
    check("verified: agreement leaves the surrogate untouched",
          chk["status"] == "verified" and chk["cl_max_usable"] is None
          and chk["confidence_cap"] is None and chk["conv_frac"] == 1.0)
    lim_on = viscous.cl_limit("s1223", RE, NCRIT)
    check("verified: cl_limit carries no check payload",
          "surrogate_check" not in lim_on
          and "CL_max_claimed" not in lim_on)

    # capped: the measured s9104BTE shape of failure — 7 of 31 points,
    # ceiling far under the claim
    fake.script["s9104BTE"] = ("partial", 7, 0.95)
    lim_off_env = dict(os.environ)
    os.environ["WSS_SURROGATE_CHECK"] = "off"
    lim_raw = viscous.cl_limit("s9104BTE", RE, NCRIT)
    os.environ["WSS_SURROGATE_CHECK"] = "on"
    lim = viscous.cl_limit("s9104BTE", RE, NCRIT)
    chk = lim.get("surrogate_check")
    check("capped: status and the measured numbers ride the limit",
          chk is not None and chk["status"] == "capped"
          and chk["conv_frac"] == round(7 / N_REQ, 3)
          and chk["cl_max_xfoil"] == 0.95)
    check("capped: usable CL_max is XFOIL's ceiling x the margin",
          abs(lim["CL_max"] - 0.95 * viscous.XCHECK_CAP_MARGIN) < 1e-6
          and lim["CL_max_claimed"] == lim_raw["CL_max"]
          and lim["CL_max"] < lim_raw["CL_max"])
    check("capped: confidence floored to the convergence fraction",
          abs(lim["confidence"] - round(7 / N_REQ, 3)) < 1e-6
          and lim["confidence"] < lim_raw["confidence"])

    # disagreement with full convergence also caps
    fake.script["e423"] = ("full", 1.0)   # xf ceiling 1.0 vs nf's own max
    lim_e = viscous.cl_limit("e423", RE, NCRIT)
    nf_e = viscous.polar("e423", RE, NCRIT, "large")
    ratio = float(np.max(nf_e["CL"])) / 1.0
    check("capped: full convergence but a ratio past the line still caps",
          (lim_e.get("surrogate_check") or {}).get("status")
          == ("capped" if ratio > viscous.XCHECK_RATIO_OK
              else "verified"),
          f"(ratio {ratio:.2f})")

    # unverifiable: XFOIL cannot see the section at all — recorded,
    # NEVER punished (the s1223 false-positive doctrine)
    fake.script["mid151c"] = ("raise",)
    lim_raw_u_env = os.environ["WSS_SURROGATE_CHECK"]
    os.environ["WSS_SURROGATE_CHECK"] = "off"
    lim_raw_u = viscous.cl_limit("mid151c", RE, NCRIT)
    os.environ["WSS_SURROGATE_CHECK"] = lim_raw_u_env
    lim_u = viscous.cl_limit("mid151c", RE, NCRIT)
    chk_u = lim_u.get("surrogate_check")
    check("unverifiable: recorded but nothing is touched — no cap, no "
          "confidence floor",
          chk_u is not None and chk_u["status"] == "unverifiable"
          and chk_u["cl_max_usable"] is None
          and chk_u["confidence_cap"] is None
          and lim_u["confidence"] == lim_raw_u["confidence"]
          and lim_u["CL_max"] == lim_raw_u["CL_max"]
          and "CL_max_claimed" not in lim_u)

    # a stray converged point below the evidence floor is numeric noise,
    # not a blind-spot measurement (the ah7476/goe804 shape of failure)
    fake.script["goe804"] = ("partial", 1, -0.06)
    chk_s = viscous.surrogate_check("goe804", RE, NCRIT)
    check("evidence floor: one stray XFOIL point is unverifiable, "
          "never a cap",
          chk_s["status"] == "unverifiable"
          and chk_s["cl_max_usable"] is None)

    # ---- caching contract ----
    n0 = len(fake.calls)
    viscous.cl_limit("s9104BTE", RE, NCRIT)
    viscous.cl_limit("s9104BTE", RE * 1.05, NCRIT)   # same quarter-decade
    check("cache: same base and Reynolds bucket never re-runs XFOIL",
          len(fake.calls) == n0)
    viscous.cl_limit("s9104BTE", RE * 3.0, NCRIT)    # far bucket
    check("cache: a distant Reynolds bucket is its own verdict",
          len(fake.calls) == n0 + 1)

    # wrappers share the base's verdict
    n1 = len(fake.calls)
    wrapped = "mfg:thicken:0.0057:shape:+0.0000:+0.0000:+0.0000:1.050:s9104BTE"
    lim_w = viscous.cl_limit(wrapped, RE, NCRIT)
    check("cache: mfg:/shape: wrappers strip to the base — no new run, "
          "same cap",
          len(fake.calls) == n1
          and (lim_w.get("surrogate_check") or {}).get("base")
          == "s9104BTE"
          and abs(lim_w["CL_max"]
                  - 0.95 * viscous.XCHECK_CAP_MARGIN) < 1e-6)
    check("base resolver: nested wrappers unwrap to the library name",
          viscous.check_base(wrapped) == "s9104BTE"
          and viscous.check_base("s1223") == "s1223")

    # ---- disk persistence ----
    saved = dict(viscous._xcheck_mem)
    viscous._xcheck_mem.clear()
    viscous._xcheck_disk_loaded = False   # force the disk path
    n2 = len(fake.calls)
    chk2 = viscous.surrogate_check("s9104BTE", RE, NCRIT)
    check("disk: verdicts survive a fresh process without re-running "
          "XFOIL",
          len(fake.calls) == n2 and chk2["status"] == "capped"
          and viscous._xcheck_path().is_file())
    on_disk = json.loads(viscous._xcheck_path().read_text(
        encoding="utf-8"))
    check("disk: the cache file carries every verdict measured so far",
          len(on_disk) == len(saved))

    # ---- kill switch ----
    os.environ["WSS_SURROGATE_CHECK"] = "off"
    n3 = len(fake.calls)
    lim_off = viscous.cl_limit("s9104BTE", RE, NCRIT)
    check("kill switch: off means no seam call and no payload",
          len(fake.calls) == n3 and "surrogate_check" not in lim_off
          and lim_off["CL_max"] == lim_raw["CL_max"])
    os.environ["WSS_SURROGATE_CHECK"] = "on"

    # ---- the integration that matters: analysis inherits the cap ----
    CFG = {
        "elements": [
            {"airfoil": "s9104BTE", "chord_ratio": 1.0,
             "deflection_deg": 0},
            {"airfoil": "s1223", "chord_ratio": 0.35,
             "deflection_deg": 12, "slot_gap_pct": 1.5,
             "slot_overlap_pct": 3.0}],
        "ride_height_mm": 30, "chord_mm": 350, "span_mm": 1400,
        "speed_ms": 20, "ncrit": 7, "n_panels_per_side": 50,
    }
    cfg = StackConfig.from_dict(CFG)
    os.environ["WSS_SURROGATE_CHECK"] = "off"
    r_off = analysis.analyze(cfg, include_geometry=False)
    os.environ["WSS_SURROGATE_CHECK"] = "on"
    r_on = analysis.analyze(cfg, include_geometry=False)
    e_off, e_on = r_off["elements"][0], r_on["elements"][0]
    check("analysis: the capped ceiling replaces the claim on the card",
          e_on["CL_max_isolated"]
          == round(0.95 * viscous.XCHECK_CAP_MARGIN, 3)
          and e_off["CL_max_isolated"] > 2.0)
    check("analysis: loading fractions move onto the measured ceiling",
          e_on["loading_fraction"] > 2.0 * e_off["loading_fraction"]
          and e_on["loading_status"] == "critical")
    check("analysis: element confidence floored for the trust machinery",
          e_on["nf_confidence"] == round(7 / N_REQ, 3)
          and e_on["surrogate_check"]["status"] == "capped"
          and e_off["surrogate_check"] is None)
    check("analysis: the warning names the cross-check and both numbers",
          any("cross-check capped" in w and "0.9" in w
              for w in r_on["warnings"])
          and not any("cross-check" in w for w in r_off["warnings"]))
    check("analysis: the in-family flap stays untouched",
          r_on["elements"][1]["surrogate_check"] is None
          and r_on["elements"][1]["CL_max_isolated"]
          == r_off["elements"][1]["CL_max_isolated"])
finally:
    viscous._xfoil_for_check = REAL_SEAM
    os.environ["WSS_SURROGATE_CHECK"] = "on"

print(f"\n{sum(results)}/{len(results)} surrogate-check checks passed")
sys.exit(0 if all(results) else 1)
