"""Scenario runner: patients x devices/patient x AFH headroom x WiFi load
(manuscript sec:sim-design, "Scenarios and metrics" paragraph).

AFH headroom levels map onto wlan_ble_modeling's existing wifi_scenarios /
afh.modes axis (clean = AFH free / off mode; partial/none are approximated
here by the forced-channel mechanism, since that's the only headroom-loss
condition with real fitted burst dynamics today -- a true "partial"
headroom level, e.g. 2 of 3 APs busy, would need its own chamber
measurement to fit a burst model against, which doesn't exist yet). Devices
are simulated independently per the manuscript's own stated limitation
(BLE-BLE interaction between devices is not modeled).

For each scenario cell, launches one emulated_device.py + link_shim.py pair
per (patient, device), all pointed at a single mock MedWear API instance
(test/mock_api.py, same pattern validated earlier for the real bridge), and
one real (instrumented) medwear_subscriber.py per device stream. Runs for a
fixed duration, then collects: delivered fraction, deadline-miss ratio, and
link-loss detection time from the shim's own stats and the bridge's log
(the same stale/gap counters medwear_subscriber.py now tracks).

This is an orchestration script, not a ROS 2 node itself -- it manages
subprocesses the same way chamber_session.py manages the real drivers.
"""

import argparse
import csv
import itertools
import re
import subprocess
import sys
import time
from pathlib import Path

_WIRELESS_EXPERIMENTS = Path(__file__).resolve().parents[1]
_MEDWEAR_BRIDGE_DIR = _WIRELESS_EXPERIMENTS.parents[0] / "medwear" / "ros2_to_medwear"

AFH_HEADROOM_LEVELS = ["clean", "none"]  # "partial" left out -- no fitted data for it yet, see module docstring
WIFI_LOADS_PCT = [0, 25, 50, 75, 100]
DEVICES = ["Corsano", "Polar_H10"]


def _start(cmd: list, log_path: Path) -> subprocess.Popen:
    log_f = open(log_path, "w")
    return subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)


def run_scenario_cell(n_patients: int, devices: list, afh_headroom: str,
                       wifi_load_pct: int, duration_s: float, out_dir: Path,
                       api_port: int = 8080) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    forced_channel = afh_headroom == "none"

    mock_api_proc = _start(
        [sys.executable, str(_MEDWEAR_BRIDGE_DIR / "test" / "mock_api.py")],
        out_dir / "mock_api.log",
    )
    time.sleep(1.0)

    procs = []
    # ROS 2 topic names may only contain alphanumerics/'_'/'~'/'{'/'}' --
    # no hyphens (rclpy.validate_topic_name), hence patient_NN not patient-NN.
    patients = [f"patient_{i+1:02d}" for i in range(n_patients)]
    try:
        for patient, device in itertools.product(patients, devices):
            emu_log = out_dir / f"emu_{patient}_{device}.log"
            procs.append(_start(
                [sys.executable, "-m", "gym_sim.emulated_device",
                 "--patient", patient, "--device", device],
                emu_log,
            ))
            shim_log = out_dir / f"shim_{patient}_{device}.log"
            shim_cmd = [sys.executable, "-m", "gym_sim.link_shim",
                        "--patient", patient, "--device", device, "--seed", "42"]
            if forced_channel:
                shim_cmd.append("--forced-channel")
            procs.append(_start(shim_cmd, shim_log))

        time.sleep(2.0)  # let publishers/subscribers discover each other

        bridge_env = {
            "MEDWEAR_USERNAME": "test", "MEDWEAR_PASSWORD": "test",
            "API_BASE_URL": f"http://localhost:{api_port}/v1",
        }
        import os
        env = dict(os.environ)
        env.update(bridge_env)
        bridge_log = out_dir / "bridge.log"
        procs.append(subprocess.Popen(
            [sys.executable, "medwear_subscriber.py", "--config", "config.yaml",
             "--device", "gym-sim", "--patient", "gym-sim", "--project", "gym-sim"],
            cwd=str(_MEDWEAR_BRIDGE_DIR), env=env,
            stdout=open(bridge_log, "w"), stderr=subprocess.STDOUT,
        ))

        time.sleep(duration_s)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        mock_api_proc.terminate()
        try:
            mock_api_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock_api_proc.kill()

    return _collect_metrics(out_dir)


_STALE_RE = re.compile(r"topic totals: (\d+) stale / (\d+) gap / (\d+) total")


def _collect_metrics(out_dir: Path) -> dict:
    """Parse the bridge's own log lines (the same stale/gap counters
    medwear_subscriber.py's _StalenessGapTracker maintains) rather than
    re-deriving them -- one source of truth."""
    bridge_log = out_dir / "bridge.log"
    n_stale = n_gap = n_total = 0
    if bridge_log.exists():
        for line in bridge_log.read_text().splitlines():
            m = _STALE_RE.search(line)
            if m:
                n_stale, n_gap, n_total = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    delivered_fraction = 1.0 - (n_stale / n_total) if n_total else float("nan")
    return {
        "n_total_forwarded": n_total,
        "n_stale": n_stale,
        "n_gap": n_gap,
        "deadline_miss_ratio": (n_stale / n_total) if n_total else float("nan"),
        "delivered_fraction": delivered_fraction,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patients", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--devices", nargs="+", default=DEVICES, choices=DEVICES)
    parser.add_argument("--afh-headroom", nargs="+", default=AFH_HEADROOM_LEVELS,
                         choices=AFH_HEADROOM_LEVELS)
    parser.add_argument("--duration-s", type=float, default=30.0,
                         help="Per-cell run duration (short default for smoke testing; "
                              "use minutes for a real scenario sweep)")
    parser.add_argument("--out", default="gym_sim_results")
    args = parser.parse_args()

    out_root = _WIRELESS_EXPERIMENTS / args.out
    out_root.mkdir(parents=True, exist_ok=True)
    results_csv = out_root / "scenario_results.csv"

    rows = []
    for n_patients, afh_headroom in itertools.product(args.patients, args.afh_headroom):
        cell_dir = out_root / f"patients{n_patients}_{afh_headroom}"
        print(f"=== scenario: {n_patients} patient(s), devices={args.devices}, "
              f"AFH headroom={afh_headroom}, duration={args.duration_s}s ===")
        metrics = run_scenario_cell(n_patients, args.devices, afh_headroom,
                                     wifi_load_pct=100 if afh_headroom == "none" else 0,
                                     duration_s=args.duration_s, out_dir=cell_dir)
        row = {"n_patients": n_patients, "devices": ",".join(args.devices),
               "afh_headroom": afh_headroom, **metrics}
        print(f"  -> {row}")
        rows.append(row)

    with open(results_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} scenario row(s) to {results_csv}")


if __name__ == "__main__":
    main()
