#!/usr/bin/env python3
"""
Driver A/B harness for the live path (plan A in RUNS.md): how much of the
live path's tail is the Polar driver publishing every ECG sample separately,
and what one message per BLE frame (--ecg-per-frame) leaves.

For one recorded run, the harness takes every PMD data notification (ECG and
ACC frames, raw bytes) from the run's btsnoop capture and hands each to the
real driver (polar_ros2 PolarH10, its own constructor, publishers and
pmd_data_conv) on an asyncio loop, at the frame's original HCI time relative
to the run start, as bleak's notification callback would. `ros2 bag record -a`
runs as a separate process, as in the chamber sessions, and stamps each
message's receipt. Everything runs on an isolated, localhost-only ROS domain.

Live latency per ECG sample = bag receipt - the frame's scheduled time (its
HCI time on the harness timeline). If the loop falls behind, frames are fed
late and that lateness counts, as it would for a real driver. Not covered: the
BlueZ/D-Bus/bleak hop between the HCI and the callback (compare the
per-sample variant against the original recording for that).

    driver_ab.py run CAPTURE.btsnoop --variant per_sample|per_frame --out DIR
    driver_ab.py batch --variants per_sample per_frame --out ab_runs SESSION_DIR... [--only REGEX]
    driver_ab.py analyze --out ab_runs [--original results_slurm_stages/per_run.csv]
"""

import argparse
import asyncio
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))  # analyze_session.py
import analyze_session as A  # noqa: E402

DRIVER_PKG = Path(os.environ.get("ROS_WS", Path.home() / "ros_ws")) / "src/ros4healthcare/ros2_hc_drv/polar_ros2"
ROS_DOMAIN = "77"
PMD_ECG, PMD_ACC = 0x00, 0x02


def pmd_frames(capture: Path) -> list:
    """(HCI time [s], value bytes) of every PMD data notification (ECG and ACC
    frames) on the handle carrying the ECG frames, in capture order."""
    frames = []
    for t, op, p in A.read_btsnoop(str(capture)):
        if op != A.ACL_RX or len(p) < 12:
            continue
        if (int.from_bytes(p[0:2], "little") >> 12) & 3 == 1:      # continuation fragment
            continue
        if int.from_bytes(p[6:8], "little") != 4 or p[8] != 0x1B:   # ATT notification only
            continue
        length = int.from_bytes(p[4:6], "little") - 3
        value = bytes(p[11:11 + length])
        if len(value) == length and value and value[0] in (PMD_ECG, PMD_ACC):
            frames.append((t, int.from_bytes(p[9:11], "little"), value))
    handles = [h for _t, h, v in frames if v[0] == PMD_ECG]
    if not handles:
        return []
    h = max(set(handles), key=handles.count)
    return [(t, v) for t, hh, v in frames if hh == h]


def ros_env() -> dict:
    env = dict(os.environ, ROS_DOMAIN_ID=ROS_DOMAIN, ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST")
    env.pop("ROS_LOCALHOST_ONLY", None)
    return env


def run_one(capture: Path, variant: str, out: Path, max_s: "float | None" = None) -> None:
    frames = pmd_frames(capture)
    if not frames:
        raise SystemExit(f"{capture}: no PMD frames")
    if max_s:                                  # trial runs: first max_s seconds only
        frames = [f for f in frames if f[0] - frames[0][0] <= max_s]
    out.mkdir(parents=True, exist_ok=False)
    os.environ.update(ros_env())
    import rclpy
    sys.path.insert(0, str(DRIVER_PKG))
    from polar_ros2.submodules.PolarH10 import PolarH10

    rec = subprocess.Popen(["ros2", "bag", "record", "-a", "-o", str(out / "bag")],
                           env=ros_env(), stdout=subprocess.DEVNULL, stderr=open(out / "record.log", "w"))
    rclpy.init()
    node = rclpy.create_node("polar_connector")
    driver = PolarH10(node, types.SimpleNamespace(address="A0:9E:1A:DF:7F:6C"), "hci0",
                      publish_acceleration=True, publish_hr=False, publish_ecg=True,
                      publish_battery=False, ecg_per_frame=(variant == "per_frame"))
    # wait until the recorder has subscribed (it polls for new topics)
    deadline = time.monotonic() + 30
    while (driver.pub_ecg.get_subscription_count() == 0 or driver.pub_acc.get_subscription_count() == 0):
        if time.monotonic() > deadline or rec.poll() is not None:
            raise SystemExit("recorder did not subscribe")
        time.sleep(0.1)
    time.sleep(1.0)

    feeds = []

    async def feed() -> None:
        loop = asyncio.get_running_loop()
        t0_hci = frames[0][0]
        t0_mono, t0_wall = loop.time() + 0.5, time.time() + 0.5
        for t_hci, value in frames:
            delay = t0_mono + (t_hci - t0_hci) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            t_feed = time.time()
            driver.pmd_data_conv(None, bytearray(value))
            n = (len(value) - 10) // 3 if value[0] == PMD_ECG else None
            feeds.append((value[0], n, t_hci - t0_hci, t0_wall + (t_hci - t0_hci), t_feed, time.time()))

    asyncio.run(feed())
    time.sleep(2.0)
    rec.send_signal(signal.SIGINT)
    rec.wait(timeout=30)
    node.destroy_node()
    rclpy.shutdown()
    with open(out / "feeds.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pmd_type", "ecg_samples", "t_hci_rel", "t_target", "t_feed", "t_done"])
        w.writerows(feeds)
    (out / "run.json").write_text(json.dumps(dict(capture=str(capture), variant=variant,
                                                  frames=len(frames), host=os.uname().nodename)))


def batch(args) -> None:
    for sdir in args.sessions:
        with open(sdir / "manifest.csv", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["device"] == "Polar_H10"]
        for r in rows:
            name = Path(r["capture_file"]).stem
            if args.only and not re.search(args.only, name):
                continue
            for v in args.variants:
                out = args.out / v / sdir.name / name
                if (out / "feeds.csv").exists():
                    continue
                if out.exists():              # interrupted earlier: redo
                    subprocess.run(["rm", "-rf", str(out)], check=True)
                print(f"{time.strftime('%H:%M:%S')} {v} {sdir.name}/{name}", flush=True)
                subprocess.run([sys.executable, __file__, "run", str(sdir / r["capture_file"]),
                                "--variant", v, "--out", str(out)], check=True)


def read_bag_ecg(bag: Path) -> list:
    """(receipt [s], samples) per /polar_ecg message."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag)), rosbag2_py.ConverterOptions("", ""))
    types_ = {t.name: t.type for t in r.get_all_topics_and_types()}
    r.set_filter(rosbag2_py.StorageFilter(topics=["/polar_ecg"]))
    out = []
    while r.has_next():
        _t, data, t_ns = r.read_next()
        out.append((t_ns / 1e9, len(deserialize_message(data, get_message(types_["/polar_ecg"])).ecg)))
    return out


def latencies(rundir: Path) -> np.ndarray:
    """Per ECG sample: bag receipt - its frame's scheduled time (s)."""
    with open(rundir / "feeds.csv", newline="") as f:
        ecg = [(int(r["ecg_samples"]), float(r["t_target"])) for r in csv.DictReader(f) if r["pmd_type"] == "0"]
    msgs = read_bag_ecg(rundir / "bag")
    target = np.repeat([t for _n, t in ecg], [n for n, _t in ecg])
    recv = np.repeat([t for t, _n in msgs], [n for _t, n in msgs])
    if len(recv) != len(target):
        raise ValueError(f"{rundir}: {len(recv)} samples received, {len(target)} fed")
    return recv - target


def analyze(args) -> None:
    orig = {}
    if args.original and args.original.exists():
        for r in csv.DictReader(open(args.original)):
            if r.get("live_p99_ms"):
                orig[(r["session"], r["run"])] = r
    rows = []
    for feeds in sorted(args.out.glob("*/*/*/feeds.csv")):
        rundir = feeds.parent
        variant, session, run = rundir.parts[-3:]
        lat = latencies(rundir) * 1e3
        label = re.search(r"(chamber_\d+)$", session)
        o = orig.get((label.group(1) if label else session, run), {})
        rows.append(dict(variant=variant, session=session, run=run, n=len(lat),
                         p50_ms=np.percentile(lat, 50), p99_ms=np.percentile(lat, 99), max_ms=lat.max(),
                         orig_p50_ms=o.get("live_p50_ms", ""), orig_p99_ms=o.get("live_p99_ms", ""),
                         condition=o.get("table_row") or o.get("condition", "")))
    if not rows:
        raise SystemExit("no runs")
    with open(args.out / "ab_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['variant']:10s} {r['run'][:52]:52s} p50 {r['p50_ms']:6.1f} p99 {r['p99_ms']:7.1f} "
              f"max {r['max_ms']:7.1f}   original p50/p99 {r['orig_p50_ms'][:5]}/{r['orig_p99_ms'][:6]}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("capture", type=Path)
    r.add_argument("--variant", choices=["per_sample", "per_frame"], required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--max-seconds", type=float, help="feed only the first seconds (trials)")
    b = sub.add_parser("batch")
    b.add_argument("sessions", nargs="+", type=Path)
    b.add_argument("--variants", nargs="+", default=["per_sample", "per_frame"])
    b.add_argument("--out", type=Path, default=HERE / "ab_runs")
    b.add_argument("--only", help="regex on the capture name")
    a = sub.add_parser("analyze")
    a.add_argument("--out", type=Path, default=HERE / "ab_runs")
    a.add_argument("--original", type=Path, default=HERE / "results_slurm_stages/per_run.csv")
    args = p.parse_args()
    {"run": lambda: run_one(args.capture, args.variant, args.out, args.max_seconds), "batch": lambda: batch(args),
     "analyze": lambda: analyze(args)}[args.cmd]()


if __name__ == "__main__":
    main()
