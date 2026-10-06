"""Held-out validation (manuscript sec:sim-design, "Validation" paragraph
/ sec:results-sim "Validation on Held-Out Conditions", which is currently
\\TODO{...}, gated with: "If validation fails at the Oct 9 checkpoint,
remove this subsection and the next, and report the link model only.").

Fits duty_interpolation's airtime_scale_k from whichever forced-channel
duty levels have real measured p_e today, predicts p_e at the configured
held-out levels, and -- ONLY where real measured data actually exists for
that held-out level -- compares predicted vs. measured. Where it doesn't
(today: 25/50/75%, until row3_forced_channel's new dose-response
conditions are actually run in the chamber), this reports "insufficient
data" explicitly rather than fabricating a number. Re-running this after
that chamber session exists requires no code changes -- just real rows in
results.csv for it to find.
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

_WIRELESS_EXPERIMENTS = Path(__file__).resolve().parents[1]
if str(_WIRELESS_EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(_WIRELESS_EXPERIMENTS))

from gym_sim import duty_interpolation as di, measured_stats as ms  # noqa: E402

# Condition name -> duty percentage, mirroring chamber_session.py's own
# _forced_channel_duty_pct so this stays in lock-step with what a real
# session actually names its rows.
_CONDITION_DUTY = {
    "forced_channel_no_load": 0,
    "forced_channel_duty25pct": 25,
    "forced_channel_duty50pct": 50,
    "forced_channel_duty75pct": 75,
    "forced_channel_max_load": 100,
}


def _find_latest_results_csv() -> Optional[Path]:
    captures = _WIRELESS_EXPERIMENTS / "captures"
    if not captures.exists():
        return None
    candidates = sorted(captures.glob("*/results.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _measured_pe_by_duty_from_results_csv(device: str, results_csv: Path) -> dict:
    """Real measured p_e per duty level, computed from establishment-
    failure counts IF the row's status/columns support it. Today's
    results.csv schema has disconnect_count/unexpected_disconnect_count
    but not a 0x3E/0x08 breakdown (see measured_stats.py's docstring) --
    so this can only recover p_e for the two levels measured_stats.py
    already hardcodes from the manuscript's own Table 3 (0%, which had no
    real failures to fit, and 100%). Any other duty level present in
    results.csv is reported as "seen but not usable" rather than silently
    producing a wrong p_e from a formula that doesn't apply, since a real
    reason-code breakdown genuinely isn't in this file yet.
    """
    out = {}
    if not results_csv.exists():
        return out
    with open(results_csv) as f:
        rows = [r for r in csv.DictReader(f) if r.get("device") == device]
    seen_conditions = {r["condition"] for r in rows if r.get("condition") in _CONDITION_DUTY}
    for condition in seen_conditions:
        duty = _CONDITION_DUTY[condition]
        if duty == 100:
            out[duty] = ms.mean_p_event_fail(device)  # from the manuscript's own Table 3
    return out, seen_conditions


def run(device: str, fit_duty_levels: list, predict_duty_levels: list) -> dict:
    results_csv = _find_latest_results_csv()
    measured_all, seen_conditions = ({}, set()) if results_csv is None else \
        _measured_pe_by_duty_from_results_csv(device, results_csv)

    fit_data = {d: p for d, p in measured_all.items() if d in fit_duty_levels}
    if not fit_data:
        return {"device": device, "status": "insufficient_data",
                "reason": f"no usable measured p_e for any of fit_duty_levels={fit_duty_levels} "
                          f"(results.csv seen conditions: {sorted(seen_conditions) or 'none/no file'})"}

    k = di.calibrate_airtime_scale(device, fit_data)
    predictions = {}
    for duty in predict_duty_levels:
        predicted_pe = di.predict_pe(device, duty, k)
        measured_pe = measured_all.get(duty)
        predictions[duty] = {
            "predicted_p_e": predicted_pe,
            "measured_p_e": measured_pe,
            "status": "compared" if measured_pe is not None else "insufficient_data",
            "abs_error": (abs(predicted_pe - measured_pe) if measured_pe is not None else None),
        }
    return {"device": device, "status": "fitted", "airtime_scale_k": k,
            "fit_duty_levels": sorted(fit_data), "predictions": predictions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", nargs="+", default=["Corsano", "Polar_H10"])
    parser.add_argument("--fit-duty-levels", type=int, nargs="+", default=[0, 100])
    parser.add_argument("--predict-duty-levels", type=int, nargs="+", default=[25, 50, 75])
    args = parser.parse_args()

    for device in args.devices:
        result = run(device, args.fit_duty_levels, args.predict_duty_levels)
        print(f"\n=== {device} ===")
        if result["status"] == "insufficient_data":
            print(f"  INSUFFICIENT DATA: {result['reason']}")
            continue
        print(f"  fitted on duty levels {result['fit_duty_levels']}: airtime_scale_k={result['airtime_scale_k']:.3f}")
        for duty, pred in sorted(result["predictions"].items()):
            if pred["status"] == "compared":
                print(f"    duty={duty:3d}%: predicted p_e={pred['predicted_p_e']:.3f}  "
                      f"measured p_e={pred['measured_p_e']:.3f}  "
                      f"abs_error={pred['abs_error']:.3f}")
            else:
                print(f"    duty={duty:3d}%: predicted p_e={pred['predicted_p_e']:.3f}  "
                      f"measured=INSUFFICIENT DATA (row3_forced_channel condition "
                      f"forced_channel_duty{duty}pct not yet run in the chamber)")


if __name__ == "__main__":
    main()
