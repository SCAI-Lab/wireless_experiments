#!/usr/bin/env python3
"""
End-to-end analysis of the chamber replay through MEDWEAR (paper section
"End to End through MEDWEAR", Table III, Fig. batch-window).

Per Polar H10 ECG sample, four times on the one host clock:
  t_s  sensor time: frame timestamp (refers to the frame's last sample),
       mapped onto the host clock as t_h - added delay, as in
       analyze_session.py (lower-envelope detrended, relative to run minimum)
  t_h  host arrival: HCI time of the frame in the btsnoop capture
  t_r  live path: receipt by a local ROS 2 subscriber of the standardized
       message (the rosbag recorder during the chamber run)
  t_c  durable path: server-side commit time from the replay: the
       X-Stored-At response header of the sample's batch (set after both
       stores committed), falling back to med-api's "InfluxDB OK" log line. Mapped onto the original timeline as
       t_c = t_r + (commit - bridge receipt during replay).

The HCI frames and the bag's /polar_ecg messages are joined one-to-one in
order (the driver publishes each frame's samples in order; every run is
checked for equal counts).

Added delays for M(D): host = frame added delay (analyze_session.py);
live = host + (t_r - t_h); durable = host + (t_c - t_h). M(D) is the share
of expected samples (130 Hz over the planned window) not delivered within D,
counting undelivered samples as missed, as in analyze_session.py.
Staleness (age of information) is sampled at 1 Hz over the planned window.

Usage:
    source /opt/ros/jazzy/setup.bash && source $ROS_WS/install/setup.bash
    python3 analyze_e2e.py --full-tag full_W1 --sweep-tags sweep_W0.1 sweep_W0.5 sweep_W1 sweep_W5 \\
        --table-tag sweep_W0.1 --project medwear_wireless_chamber --check-stores \\
        ../captures/20261007_105222_chamber_1 \\
        ../captures/20261008_010249_chamber_2
Outputs go to results/ (report.md, summary.json, CSVs, table3.tex).
"""

import argparse
import collections
import csv
import json
import os
import re
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))  # analyze_session.py
# Session analysis: analyze_session (v1.4.3, the paper's) stamps a
# notification's host arrival t_h at its LAST ACL fragment and gives the
# censoring-aware host M(D) of table3_hostcens.tex. E2E_SESSION_ANALYSIS
# selects another copy of it.
import importlib  # noqa: E402
A = importlib.import_module(os.environ.get("E2E_SESSION_ANALYSIS", "analyze_session"))

LOG_ROOT = HERE / "logs"
BTSNOOP_EPOCH_S = 62168256000.0      # btsnoop timestamps count from 0000-01-01
DEADLINES_S = [0.1, 0.25, 1.0, 10.0]
ECG_FS = A.ECG_FS

TABLE_ROWS = [
    ("AFH free, 100% load", r"^duty100pct$"),
    ("$k=8$, 75% load", r"^headroom_k8_duty75pct$"),
    ("$k=4$, 75% load", r"^headroom_k4_duty75pct$"),
    ("$k=2$, 75% load", r"^headroom_k2_duty75pct$"),
    # pooled with the confined label, which uses the same channel map (Table II)
    ("$k=0$, 75% load", r"^(headroom_k0|forced_channel)_duty75pct$"),
]

# bridged topic -> (signal_type, device) as uploaded
TOPIC_SIGNAL = {
    "/polar_ecg": ("ECG", "Polar_H10"),
    "/polar_acc": ("IMU", "Polar_H10"),
    "/polar_hr": ("HR", "Polar_H10"),          # published in the gym sessions only
    "/corsano/hr": ("HR", "Corsano"),
    "/corsano/bioz": ("EDA", "Corsano"),
    "/corsano/acceleration": ("IMU", "Corsano"),
}
PLAUSIBILITY_RE = re.compile(r"physiological range|baseline wander", re.I)


def session_label(sdir: Path) -> str:
    m = re.search(r"(chamber_\d+)$", sdir.name)
    return m.group(1) if m else sdir.name


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


# ── original chamber runs: HCI frames + bag ───────────────────────────────────

def btsnoop_t0(path: Path) -> float:
    """Absolute time (unix s) of the first record, as parse_capture's t0."""
    with open(path, "rb") as f:
        head = f.read(16 + 24)
    ts = struct.unpack(">q", head[16 + 16:16 + 24])[0]
    return ts / 1e6 - BTSNOOP_EPOCH_S


def read_bag_ecg(bag: Path):
    import rosbag2_py
    from healthcare_msgs.msg import ECG
    from rclpy.serialization import deserialize_message
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
    r.set_filter(rosbag2_py.StorageFilter(topics=["/polar_ecg"]))
    stamps, recv = [], []
    while r.has_next():
        _, data, t = r.read_next()
        m = deserialize_message(data, ECG)
        stamps.append(m.header.stamp.sec * 10**9 + m.header.stamp.nanosec)
        recv.append(t / 1e9)
    return np.array(stamps, dtype=np.int64), np.array(recv)


def session_context(sdir: Path, manifest: list, loginfo: dict) -> dict:
    """The per-session inputs analyze_session.py passes to run_metrics: gym
    background, load-onset offset, and one clock-drift rate per session (the
    median of its no-load runs; gym: AFH-free runs). Without them each run
    gets its own drift fit, which absorbs a growing backlog."""
    offs = [(li["iperf_start"] - li["driver_launch"]).total_seconds() for li in loginfo.values()
            if li.get("iperf_start") and li.get("driver_launch")]
    drift, _ = A.session_drift(str(sdir), manifest)
    return dict(background=A.gym_background(str(sdir)), drift=drift,
                settle_s=float(np.median(offs)) if offs else A.DEFAULT_SETTLE_S)


def load_polar_run(sdir: Path, mrow: dict, loginfo: dict, ctx: dict) -> dict:
    """Per-sample host/live times of one original Polar run."""
    name = Path(mrow["capture_file"]).stem
    out = dict(session=session_label(sdir), run=name, condition=mrow["condition"],
               row=mrow["row"], replicate=mrow["replicate"])
    r, det = A.run_metrics(str(sdir), mrow, loginfo, ctx["background"], ctx["drift"], ctx["settle_s"])
    out["exclude"] = bool(r.get("exclude"))
    out["exclude_reason"] = r.get("exclude_reason", "")
    out["host_miss_pct_analyze_session"] = {D: r.get(f"miss_{int(D * 1000)}ms_pct") for D in DEADLINES_S}
    out["host_age_miss_pct_analyze_session"] = {D: r.get(f"miss_age_{int(D * 1000)}ms_pct") for D in DEADLINES_S}
    # censoring-aware (analyze_session >= 1.4.1): samples acquired in [onset, end - D] only
    out["host_agecens_miss_pct_analyze_session"] = {D: r.get(f"miss_age_cens_{int(D * 1000)}ms_pct") for D in DEADLINES_S}
    if det is None:
        out["error"] = "no ECG frames"
        return out
    t0 = btsnoop_t0(sdir / mrow["capture_file"])
    n = det["n"].astype(int)
    stamps, recv = read_bag_ecg(sdir / f"{name}_rosbag")
    wend = det["wend"]
    out["trailing_frames_unpublished"] = 0
    if n.sum() != len(stamps):
        # Seen only as whole frames missing at the END: frames that reached
        # the host while the driver was still working off a backlog, and
        # were never published because the driver was stopped. A shutdown
        # artifact, not a live-path property: match from the start and end
        # this analysis's window at the last published frame.
        cum = np.cumsum(n)
        f = int(np.searchsorted(cum, len(stamps))) + 1
        if len(stamps) > n.sum() or cum[f - 1] != len(stamps):
            out["error"] = f"HCI samples {n.sum()} != bag messages {len(stamps)}"
            return out
        out["trailing_frames_unpublished"] = len(n) - f
        n, det = n[:f], dict(det, t=det["t"][:f], delay_ms=det["delay_ms"][:f])
        wend = min(wend, float(det["t"][-1]))
    fi = np.repeat(np.arange(len(n)), n)
    k = np.concatenate([np.arange(c) for c in n]) if len(n) else np.array([], int)
    th = (det["t"] + t0)[fi]
    early = np.count_nonzero(stamps / 1e9 < th - 0.002)
    if early:   # published before the frame arrived: the join is wrong
        out["error"] = f"{early} samples published before their HCI frame arrived"
        return out
    d_host = (det["delay_ms"] / 1000)[fi]
    # sensor time on the host clock; frame stamp refers to its last sample
    ts = th - d_host - (n[fi] - 1 - k) / ECG_FS
    # sample age adds the sample's position in its frame (sample k of an
    # n-sample frame is (n-1-k)/fs older than the frame stamp), as the
    # miss_age_* metrics of analyze_session.py
    framing = (n[fi] - 1 - k) / ECG_FS
    out.update(t0=t0, onset=det["onset"], wend=wend, win=max(1.0, wend - det["onset"]),
               th=th, th_rel=th - t0, d_host=d_host, framing=framing, tr=recv, ts=ts, stamps=stamps)
    return out


def miss_pct(run: dict, added: np.ndarray, D: float) -> float:
    w = (run["th_rel"] >= run["onset"]) & (run["th_rel"] <= run["wend"])
    ok = np.count_nonzero(w & (added <= D))   # NaN (undelivered) compares False
    return 100 * max(0.0, 1 - ok / (ECG_FS * run["win"]))


def staleness(run: dict, arrival: np.ndarray) -> dict:
    """Age of the newest sample available to a consumer, sampled at 1 Hz
    over the planned window."""
    ok = ~np.isnan(arrival)
    order = np.argsort(arrival[ok])
    t_arr = arrival[ok][order]
    newest = np.maximum.accumulate(run["ts"][ok][order])
    grid = run["t0"] + np.arange(run["onset"], run["wend"], 1.0)
    idx = np.searchsorted(t_arr, grid, side="right") - 1
    age = np.where(idx >= 0, grid - newest[np.clip(idx, 0, None)], np.nan)
    age = age[~np.isnan(age)]
    res = {"aoi_mean_s": float(age.mean()) if len(age) else float("nan"),
           "aoi_p99_s": pct(age, 99)}
    for D in DEADLINES_S:
        res[f"aoi_over_{D:g}s_pct"] = 100 * float(np.mean(age > D)) if len(age) else float("nan")
    return res


# ── replay outputs ─────────────────────────────────────────────────────────────

def read_metrics(tag: str, label: str, run: str) -> list:
    p = LOG_ROOT / tag / label / f"{run}.metrics.jsonl"
    if not p.exists():
        return []
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


# MedWear with two-phase ingest logs "COMMIT OK" once the batch is complete in
# both stores (after "InfluxDB OK"); older versions only "InfluxDB OK"
_INFLUX_OK_RE = re.compile(r"(InfluxDB OK|COMMIT OK) \| batch_uuid=([0-9a-f-]+)")


def read_commit_times(tag: str, label: str, run: str) -> dict:
    """server batch_uuid -> unix time of med-api's "COMMIT OK" (else "InfluxDB OK") log line."""
    p = LOG_ROOT / tag / label / f"{run}.api.log"
    out, committed = {}, set()
    if not p.exists():
        return out
    for line in open(p):
        try:
            rec = json.loads(line[line.index("{"):])
        except ValueError:
            continue
        m = _INFLUX_OK_RE.search(rec.get("message", ""))
        if m and m.group(2) not in committed:
            t = datetime.strptime(rec["asctime"], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
            out[m.group(2)] = t.timestamp()
            if m.group(1) == "COMMIT OK":
                committed.add(m.group(2))
    return out


_INFLUX_WRITE_RE = re.compile(r"InfluxDB write OK \| batch_uuid=([0-9a-f-]+) .*duration_s=([0-9.]+)")


def read_influx_durations(tag: str, label: str, run: str) -> dict:
    """server batch_uuid -> InfluxDB write duration (s, ms resolution) logged by med-api."""
    p = LOG_ROOT / tag / label / f"{run}.api.log"
    out = {}
    if p.exists():
        for line in open(p):
            m = _INFLUX_WRITE_RE.search(line)
            if m:
                out[m.group(1)] = float(m.group(2))
    return out


def match_batch(run: dict, order: np.ndarray, sorted_stamps: np.ndarray, rec: dict):
    """Indices into the original run of a batch's samples (by header stamp),
    the hit mask over the batch, and each sample's bridge receipt time."""
    stamps = rec["msg_stamp0_ns"] + np.array(rec["msg_stamp_us"], dtype=np.int64) * 1000
    recv = rec["msg_recv0"] + np.array(rec["msg_recv_us"]) / 1e6
    j = np.clip(np.searchsorted(sorted_stamps, stamps - 1000), 0, len(sorted_stamps) - 1)
    hit = np.abs(sorted_stamps[j] - stamps) <= 2000      # metrics keep us resolution
    return order[j[hit]], hit, recv


def durable_latency(run: dict, tag: str) -> "tuple[np.ndarray, dict]":
    """Per-sample t_c - t_h for one original run under replay `tag` (NaN if
    never committed), plus bookkeeping."""
    recs = read_metrics(tag, run["session"], run["run"])
    commits = read_commit_times(tag, run["session"], run["run"])
    order = np.argsort(run["stamps"])
    sorted_stamps = run["stamps"][order]
    lat = np.full(len(run["stamps"]), np.nan)
    info = collections.Counter()
    for rec in recs:
        if rec["signal_type"] != "ECG":
            continue
        info["batches"] += 1
        if not rec["ok"] or rec.get("result_status") == "duplicate":
            info["not_committed"] += 1
            continue
        # commit time: X-Stored-At header (after both stores committed),
        # else med-api's "InfluxDB OK" log line, else client receipt
        t_c = rec.get("server_stored") or commits.get(rec.get("server_batch_uuid"))
        if t_c is None:
            info["commit_from_client"] += 1
            t_c = rec["post_end"]          # fallback: client receipt of the answer
        idx, hit, recv = match_batch(run, order, sorted_stamps, rec)
        info["unmatched_samples"] += int((~hit).sum())
        live = run["tr"][idx] - run["th"][idx]
        lat[idx] = (t_c - recv[hit]) + live
    info["samples_committed"] = int((~np.isnan(lat)).sum())
    return lat, dict(info)


# Steps of the durable path, in order, for every committed Polar ECG sample.
# The first is the radio's added delay (relative to the run minimum, as for
# M(D)); the others are absolute. Their sum is the durable added delay that
# M_durable uses. Batch-level steps count once per sample of the batch.
STAGES = [
    ("ble", "BLE link + host stack (added delay, t_h - t_s)"),
    ("driver", "Driver to standardized ROS 2 message (t_r - t_h)"),
    ("batching", "Bridge batch window (sample receipt -> batch closed)"),
    ("queue", "Upload queue (batch closed -> request sent)"),
    ("transport", "HTTPS to the API: TLS, nginx, auth (sent -> received)"),
    ("validation", "Schema and plausibility validation"),
    ("postgres", "PostgreSQL commit (batch metadata)"),
    ("influx", "InfluxDB write (samples)"),
]
STAGE_TOTAL = ("total", "Total, sensor to durable commit")
STAGE_REPLY = ("reply", "Acknowledgment back to the bridge (not in total)")


def stage_samples(run: dict, tag: str) -> dict:
    """Per committed ECG sample of one original run under replay `tag`: the
    time spent in each step of STAGES (s). Server times come from med-api's
    X-Received-At / X-Validated-At / X-Stored-At headers (us); the commit is
    split with the InfluxDB write duration med-api logs (ms resolution)."""
    recs = read_metrics(tag, run["session"], run["run"])
    durs = read_influx_durations(tag, run["session"], run["run"])
    order = np.argsort(run["stamps"])
    sorted_stamps = run["stamps"][order]
    out = collections.defaultdict(list)
    for rec in recs:
        if (rec["signal_type"] != "ECG" or not rec["ok"] or rec.get("result_status") == "duplicate"
                or not all(rec.get(k) for k in ("server_received", "server_validated", "server_stored"))):
            continue
        idx, hit, recv = match_batch(run, order, sorted_stamps, rec)
        n = len(idx)
        if not n:
            continue
        commit = rec["server_stored"] - rec["server_validated"]
        influx = durs.get(rec.get("server_batch_uuid"), np.nan)
        out["ble"].append(run["d_host"][idx])
        out["driver"].append(run["tr"][idx] - run["th"][idx])
        out["batching"].append(rec["enqueued"] - recv[hit])
        for name, v in (("queue", rec["post_start"] - rec["enqueued"]),
                        ("transport", rec["server_received"] - rec["post_start"]),
                        ("validation", rec["server_validated"] - rec["server_received"]),
                        ("postgres", commit - influx), ("influx", influx),
                        ("reply", rec["post_end"] - rec["server_stored"])):
            out[name].append(np.full(n, v))
    res = {k: np.concatenate(v) for k, v in out.items()}
    if res:
        # PostgreSQL + InfluxDB = commit even where the log duration is missing
        commit_all = res["postgres"] + res["influx"]
        res["total"] = sum(res[k] for k in ("ble", "driver", "batching", "queue", "transport", "validation")) \
            + np.where(np.isnan(commit_all), np.nan, commit_all)
    return res


def stage_table(arrs: dict) -> list:
    """Rows (step, n, mean, p50, p95, p99 in ms) for pooled stage_samples."""
    rows = []
    for key, label in STAGES + [STAGE_TOTAL, STAGE_REPLY]:
        a = arrs.get(key)
        a = a[~np.isnan(a)] if a is not None else np.array([])
        rows.append(dict(step=key, label=label, n=len(a),
                         mean_ms=1e3 * float(a.mean()) if len(a) else float("nan"),
                         p50_ms=1e3 * pct(a, 50), p95_ms=1e3 * pct(a, 95), p99_ms=1e3 * pct(a, 99)))
    return rows


def read_stats_cpu(tag: str, label: str, run: str, container="medwear-api") -> float:
    p = LOG_ROOT / tag / label / f"{run}.stats.csv"
    if not p.exists():
        return float("nan")
    vals = []
    with open(p) as f:
        for row in csv.DictReader(f):
            if row["container"] == container:
                try:
                    vals.append(float(row["cpu_pct"]))
                except ValueError:
                    pass
    return float(np.mean(vals)) if vals else float("nan")


def bag_duration_s(sdir: Path, run: str) -> float:
    with open(sdir / f"{run}_rosbag" / "metadata.yaml") as f:
        return yaml.safe_load(f)["rosbag2_bagfile_information"]["duration"]["nanoseconds"] / 1e9


def bag_counts(sdir: Path, run: str) -> dict:
    with open(sdir / f"{run}_rosbag" / "metadata.yaml") as f:
        info = yaml.safe_load(f)["rosbag2_bagfile_information"]
    return {t["topic_metadata"]["name"]: t["message_count"] for t in info["topics_with_message_count"]}


# ── commit accounting over the full replay ────────────────────────────────────

def commit_accounting(sessions: list, tag: str) -> dict:
    acc = collections.Counter()
    errors = collections.Counter()
    plaus = []
    expected = collections.Counter()      # messages per topic in the bags
    committed = collections.Counter()     # messages per topic in committed batches
    hours = 0.0
    n_runs = 0
    for sdir in sessions:
        label = session_label(sdir)
        with open(sdir / "manifest.csv", newline="") as f:
            manifest = list(csv.DictReader(f))
        for m in manifest:
            run = Path(m["capture_file"]).stem
            n_runs += 1
            hours += bag_duration_s(sdir, run) / 3600
            for topic, c in bag_counts(sdir, run).items():
                if topic in TOPIC_SIGNAL and TOPIC_SIGNAL[topic][1] == m["device"]:
                    expected[topic] += c
            for rec in read_metrics(tag, label, run):
                acc["batches"] += 1
                acc["attempts"] += rec["attempts"]
                acc[f"http_{rec['http_status']}"] += 1
                topic = next(t for t, (s, d) in TOPIC_SIGNAL.items()
                             if s == rec["signal_type"] and d == rec["device_id"])
                n_msgs = len(rec["msg_stamp_us"])
                if rec["ok"]:
                    acc["samples_committed"] += rec["n_samples"] if rec["result_status"] != "duplicate" else 0
                    acc[f"status_{rec['result_status']}"] += 1
                    acc["overlap_warnings"] += bool(rec.get("overlap_warning"))
                    if rec["result_status"] != "duplicate":
                        # IMU: one batch per axis; count each message once (x axis)
                        if rec["signal_type"] != "IMU" or rec["schema_id"].endswith("_x_v1"):
                            committed[topic] += n_msgs
                else:
                    err = rec.get("error") or {}
                    code = err.get("error") or f"HTTP_{rec['http_status']}"
                    errors[code] += 1
                    acc["samples_rejected"] += rec["n_samples"]
                    text = json.dumps(err)
                    if PLAUSIBILITY_RE.search(text):
                        plaus.append(dict(session=label, run=run, n_samples=rec["n_samples"],
                                          batch_start_ns=rec["msg_stamp0_ns"], detail=text[:300]))
    missing = {t: expected[t] - committed[t] for t in expected}
    return dict(n_recordings=n_runs, hours=hours, batches=acc, errors=dict(errors),
                plausibility_rejections=plaus, expected_msgs=dict(expected),
                committed_msgs=dict(committed), missing_msgs=missing)


def plausibility_context(rej: list, sessions: list) -> list:
    """Distance of each ECG plausibility rejection to the nearest
    (re)connection on the HCI capture: is it a reconnect burst?"""
    by_label = {session_label(s): s for s in sessions}
    out = []
    for p in rej:
        sdir = by_label[p["session"]]
        ev = A.parse_capture(str(sdir / f"{p['run']}.btsnoop"), "Polar_H10")
        t = p["batch_start_ns"] / 1e9
        conns = [c[0] + ev["t0"] - BTSNOOP_EPOCH_S for c in ev["conns"]]
        d = min((t - c for c in conns if c <= t), default=float("nan"))
        out.append(dict(p, s_after_last_connection=d))
    return out


# ── stores ─────────────────────────────────────────────────────────────────────

# Full-replay patient IDs only (chamber_1__row2_duty50pct_rep1), not the W
# sweep's or trials' suffixed ones (…__W0.5, …__trial)
FULL_PATIENT_RE = r"^chamber_[0-9]+__([a-z0-9]+_)*rep[0-9]+$"


def check_stores(project: str) -> dict:
    """Committed samples per signal in PostgreSQL and InfluxDB."""
    sql = ("SELECT device_id, signal_type, count(*), sum(sample_count) FROM measurement_batch "
           f"WHERE patient_id ~ '{FULL_PATIENT_RE}' "
           "AND is_deleted != true GROUP BY 1, 2 ORDER BY 1, 2")
    pg = subprocess.run(["docker", "exec", "medwear-postgres", "psql", "-U", "medwear", "-d", project,
                         "-AtF,", "-c", sql], capture_output=True, text=True)
    pg_rows = [line.split(",") for line in pg.stdout.split()] if pg.returncode == 0 else []
    flux = (f'from(bucket: "wearables") |> range(start: 2026-01-01T00:00:00Z) '
            f'|> filter(fn: (r) => r.project_id == "{project}" and r._field == "value" '
            f'and r.patient_id =~ /{FULL_PATIENT_RE}/) '
            f'|> group(columns: ["device_id", "signal_type"]) |> count()')
    influx = subprocess.run(
        ["docker", "exec", "-i", "medwear-api", "sh", "-c",
         f'python3 -c "import os,sys; from influxdb_client import InfluxDBClient; '
         f'c=InfluxDBClient(url=os.environ[\'INFLUX_URL\'],token=os.environ[\'INFLUX_TOKEN\'],'
         f'org=os.environ[\'INFLUX_ORG\'],timeout=600000); '
         f'[print(r.values[\'device_id\'],r.values[\'signal_type\'],r.get_value()) '
         f'for t in c.query_api().query(sys.stdin.read()) for r in t.records]"'],
        input=flux, capture_output=True, text=True)
    return dict(postgres=pg_rows or pg.stderr.strip()[:300],
                influx=influx.stdout.split("\n") if influx.returncode == 0 else influx.stderr.strip()[-300:])


# ── main ───────────────────────────────────────────────────────────────────────

def tag_W(tag: str) -> str:
    """Batch window of a sweep tag: sweep_W0.1 and sweep_W0.1_dns -> "0.1"."""
    return re.search(r"_W([0-9.]+)", tag).group(1)


def cond_label(condition: str) -> "str | None":
    for label, rx in TABLE_ROWS:
        if re.search(rx, condition):
            return label
    return None


def main() -> None:
    global LOG_ROOT
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sessions", nargs="+", type=Path)
    p.add_argument("--full-tag", help="tag of the full replay (commit accounting)")
    p.add_argument("--sweep-tags", nargs="*", default=[], help="tags of the W sweep, e.g. sweep_W0.1")
    p.add_argument("--table-tag", help="tag whose durable path fills Table III (caption: W=0.1 s)")
    p.add_argument("--project", help="MedWear project database, for --check-stores")
    p.add_argument("--check-stores", action="store_true")
    p.add_argument("--out", type=Path, default=HERE / "results")
    p.add_argument("--logs", type=Path, default=LOG_ROOT,
                   help="replay logs, one folder per tag (default logs/)")
    args = p.parse_args()
    LOG_ROOT = args.logs.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    sessions = [s.resolve() for s in args.sessions]
    summary: dict = {}

    # 1. commit accounting over the full replay
    if args.full_tag:
        acc = commit_accounting(sessions, args.full_tag)
        acc["plausibility_rejections"] = plausibility_context(acc["plausibility_rejections"], sessions)
        summary["full_replay"] = acc
        if args.check_stores and args.project:
            summary["stores"] = check_stores(args.project)

    # 2. original Polar runs: live path
    runs = []
    for sdir in sessions:
        with open(sdir / "manifest.csv", newline="") as f:
            manifest = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
        loginfo, _ = A.parse_log(str(sdir / "session.log"))
        ctx = session_context(sdir, manifest, loginfo)
        drift_ppm = "none" if ctx["drift"] is None else f"{ctx['drift'] * 1e6:.1f} ppm"
        print(f"{sdir.name}: drift {drift_ppm}, settle {ctx['settle_s']:.1f} s", file=sys.stderr)
        for m in manifest:
            if m["device"] != "Polar_H10":
                continue
            print(f"loading {sdir.name}/{m['capture_file']}", file=sys.stderr)
            runs.append(load_polar_run(sdir, m, loginfo, ctx))

    per_run = []
    pooled = collections.defaultdict(lambda: collections.defaultdict(list))
    for run in runs:
        row = {k: run.get(k) for k in ("session", "run", "condition", "row", "replicate",
                                       "exclude", "exclude_reason", "error")}
        row["table_row"] = cond_label(run["condition"])
        if "th" not in run:
            per_run.append(row)
            continue
        live = run["tr"] - run["th"]
        row.update(live_p50_ms=1e3 * pct(live, 50), live_p99_ms=1e3 * pct(live, 99),
                   live_max_ms=1e3 * float(live.max()), n_samples=len(live))
        row["trailing_frames_unpublished"] = run["trailing_frames_unpublished"]
        for D in DEADLINES_S:
            row[f"M_host_{D:g}s"] = miss_pct(run, run["d_host"], D)
            row[f"M_live_{D:g}s"] = miss_pct(run, run["d_host"] + live, D)
            # cross-check against analyze_session.py (same window unless truncated)
            row[f"M_host_{D:g}s_analyze_session"] = run["host_miss_pct_analyze_session"][D]
            # on sample age (adds the framing), the metric of the paper's host columns
            age = run["d_host"] + run["framing"]
            row[f"M_hostage_{D:g}s"] = miss_pct(run, age, D)
            row[f"M_liveage_{D:g}s"] = miss_pct(run, age + live, D)
            row[f"M_hostage_{D:g}s_analyze_session"] = run["host_age_miss_pct_analyze_session"][D]
            v = run["host_agecens_miss_pct_analyze_session"].get(D)
            row[f"M_hostagecens_{D:g}s_analyze_session"] = float(v) if v is not None else float("nan")
        row.update({f"host_{k}": v for k, v in staleness(run, run["th"]).items()})
        row.update({f"live_{k}": v for k, v in staleness(run, run["tr"]).items()})
        key = row["table_row"] or run["condition"]
        if not run["exclude"]:
            pooled[key]["live"].append(live)
        for tag in args.sweep_tags:
            lat, info = durable_latency(run, tag)
            if not info.get("batches"):
                continue
            W = tag_W(tag)
            row[f"W{W}_durable_p50_s"] = pct(lat[~np.isnan(lat)], 50)
            row[f"W{W}_durable_p99_s"] = pct(lat[~np.isnan(lat)], 99)
            row[f"W{W}_samples_committed"] = info["samples_committed"]
            row[f"W{W}_unmatched"] = info.get("unmatched_samples", 0)
            row[f"W{W}_commit_from_client"] = info.get("commit_from_client", 0)
            row[f"W{W}_batches"] = info["batches"]
            row[f"W{W}_req_per_s"] = info["batches"] / bag_duration_s(
                next(s for s in sessions if session_label(s) == run["session"]), run["run"])
            row[f"W{W}_api_cpu_pct"] = read_stats_cpu(tag, run["session"], run["run"])
            for D in DEADLINES_S:
                row[f"W{W}_M_durable_{D:g}s"] = miss_pct(run, run["d_host"] + lat, D)
                row[f"W{W}_M_durableage_{D:g}s"] = miss_pct(run, run["d_host"] + run["framing"] + lat, D)
            row.update({f"W{W}_durable_{k}": v for k, v in staleness(run, run["th"] + lat).items()})
            if not run["exclude"]:
                pooled[key][f"durable_W{W}"].append(lat[~np.isnan(lat)])
        per_run.append(row)

    cols = sorted({k for r in per_run for k in r}, key=lambda c: (c not in per_run[0], c))
    with open(args.out / "per_run.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(per_run)

    # per condition: pooled percentiles, run-mean M(D)
    good = [r for r in per_run if not r.get("exclude") and "live_p50_ms" in r]
    conds = []
    for key in sorted({r["table_row"] or r["condition"] for r in good}):
        rs = [r for r in good if (r["table_row"] or r["condition"]) == key]
        live = np.concatenate(pooled[key]["live"])
        c = dict(condition=key, n_runs=len(rs), live_p50_ms=1e3 * pct(live, 50),
                 live_p99_ms=1e3 * pct(live, 99))
        for col in rs[0]:
            if col.startswith(("M_", "W", "host_aoi", "live_aoi")) and isinstance(rs[0][col], float):
                vals = [r[col] for r in rs if isinstance(r.get(col), float)]
                c[col] = float(np.mean(vals)) if vals else float("nan")
                c[f"{col}_median"] = float(np.median(vals)) if vals else float("nan")
        for name, arrs in pooled[key].items():
            if name.startswith("durable_") and arrs:
                a = np.concatenate(arrs)
                c[f"{name}_pooled_p50_s"], c[f"{name}_pooled_p99_s"] = pct(a, 50), pct(a, 99)
        conds.append(c)
    ccols = sorted({k for c in conds for k in c}, key=lambda c: (c != "condition", c))
    with open(args.out / "per_condition.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ccols)
        w.writeheader()
        w.writerows(conds)

    # time spent in each step of the durable path, per experiment
    stage_tags = ([args.full_tag] if args.full_tag else []) + [t for t in args.sweep_tags if t != args.full_tag]
    stage_rows = []
    for tag in stage_tags:
        pool = collections.defaultdict(list)
        for run in runs:
            if "th" in run and not run["exclude"]:
                for k, v in stage_samples(run, tag).items():
                    pool[k].append(v)
        if pool:
            rows = stage_table({k: np.concatenate(v) for k, v in pool.items()})
            stage_rows += [dict(tag=tag, **r) for r in rows]
    summary["stages"] = stage_rows
    if stage_rows:
        with open(args.out / "stages.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(stage_rows[0]))
            w.writeheader()
            w.writerows(stage_rows)

    # headline numbers
    all_live = np.concatenate([np.concatenate(v["live"]) for v in pooled.values() if v["live"]])
    summary["live"] = dict(p50_ms=1e3 * pct(all_live, 50), p99_ms=1e3 * pct(all_live, 99),
                           n_samples=len(all_live),
                           max_abs_diff_M_live_minus_host_pp={
                               f"{D:g}s": max(abs(r[f"M_live_{D:g}s"] - r[f"M_host_{D:g}s"]) for r in good)
                               for D in DEADLINES_S})
    sweep = []
    for tag in args.sweep_tags:
        W = tag_W(tag)
        rs = [r for r in good if f"W{W}_batches" in r]
        if not rs:
            continue
        durs = [np.concatenate(v[f"durable_W{W}"]) for v in pooled.values() if v.get(f"durable_W{W}")]
        d = np.concatenate(durs) if durs else np.array([])
        s = dict(W=float(W), n_runs=len(rs), durable_p50_s=pct(d, 50), durable_p99_s=pct(d, 99),
                 req_per_s=float(np.mean([r[f"W{W}_req_per_s"] for r in rs])),
                 api_cpu_pct=float(np.nanmean([r[f"W{W}_api_cpu_pct"] for r in rs])))
        for D in DEADLINES_S:
            s[f"M_durable_{D:g}s"] = float(np.mean([r[f"W{W}_M_durable_{D:g}s"] for r in rs]))
            s[f"met_{D:g}s_pct_of_samples"] = 100 * float(np.mean(d <= D)) if len(d) else float("nan")
            s[f"aoi_over_{D:g}s_pct"] = float(np.mean([r[f"W{W}_durable_aoi_over_{D:g}s_pct"] for r in rs]))
        s["aoi_mean_s"] = float(np.mean([r[f"W{W}_durable_aoi_mean_s"] for r in rs]))
        sweep.append(s)
    sweep.sort(key=lambda s: s["W"])
    summary["w_sweep"] = sweep
    if sweep:
        with open(args.out / "w_sweep.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(sweep[0]))
            w.writeheader()
            w.writerows(sweep)

    # Table III
    tW = tag_W(args.table_tag) if args.table_tag else None
    lines = []
    for label, _ in TABLE_ROWS:
        c = next((c for c in conds if c["condition"] == label), None)
        if c is None:
            lines.append(f"{label} & -- & -- & -- & -- \\\\")
            continue
        # as in the paper: sample age (framing included), medians of runs
        dur = c.get(f"W{tW}_M_durableage_10s_median", float("nan")) if tW else float("nan")
        lines.append(f"{label} & {c['live_p50_ms']:.1f}/{c['live_p99_ms']:.1f} & "
                     f"{c['M_hostage_0.1s_median']:.1f} & {c['M_hostage_0.25s_median']:.1f} & "
                     f"{c['M_hostage_1s_median']:.1f} & {dur:.1f} \\\\")
    (args.out / "table3.tex").write_text("\n".join(lines) + "\n")
    # same table with the host columns censoring-aware, as analyze_session >= 1.4.1
    # reports them (durable column unchanged: full window)
    lines = []
    for label, _ in TABLE_ROWS:
        c = next((c for c in conds if c["condition"] == label), None)
        if c is None:
            continue
        dur = c.get(f"W{tW}_M_durableage_10s_median", float("nan")) if tW else float("nan")
        h = [c.get(f"M_hostagecens_{D}s_analyze_session_median", float("nan")) for D in ("0.1", "0.25", "1")]
        lines.append(f"{label} & {c['live_p50_ms']:.1f}/{c['live_p99_ms']:.1f} & "
                     f"{h[0]:.1f} & {h[1]:.1f} & {h[2]:.1f} & {dur:.1f} \\\\")
    (args.out / "table3_hostcens.tex").write_text("\n".join(lines) + "\n")

    with open(args.out / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    write_report(args.out, summary, conds, tW)
    print(f"Wrote {args.out}/report.md", file=sys.stderr)


def write_report(out: Path, s: dict, conds: list, tW) -> None:
    L = ["# End to end through MEDWEAR: measured values", ""]
    f = s.get("full_replay")
    if f:
        b = f["batches"]
        L += ["## Full replay", "",
              f"- Recordings: {f['n_recordings']}, {f['hours']:.1f} h, "
              f"{b.get('samples_committed', 0) / 1e6:.2f} million samples committed "
              f"({b.get('batches', 0)} batches, {b.get('attempts', 0)} requests incl. retries)",
              f"- Rejections by code: {f['errors'] or 'none'}",
              f"- Duplicates answered by the API: {b.get('status_duplicate', 0)}; "
              f"checksum mismatches: {f['errors'].get('CHECKSUM_MISMATCH', 0)}; "
              f"overlap warnings: {b.get('overlap_warnings', 0)}",
              f"- ECG plausibility rejections: {len(f['plausibility_rejections'])}"]
        for pr in f["plausibility_rejections"]:
            L.append(f"  - {pr['session']}/{pr['run']}: {pr['n_samples']} samples, "
                     f"{pr['s_after_last_connection']:.1f} s after last connection — {pr['detail'][:160]}")
        L.append("- Messages in bags vs committed, per topic:")
        for t, e in f["expected_msgs"].items():
            L.append(f"  - {t}: {e} in bags, {f['committed_msgs'].get(t, 0)} committed, "
                     f"{f['missing_msgs'][t]} missing")
        if "stores" in s:
            L += [f"- PostgreSQL (device, signal, batches, samples): {s['stores']['postgres']}",
                  f"- InfluxDB points (device, signal, count): {s['stores']['influx']}"]
        L.append("")
    lv = s["live"]
    L += ["## Live path (original chamber runs, HCI arrival → ROS 2 subscriber)", "",
          f"- All Polar ECG samples: median {lv['p50_ms']:.1f} ms, p99 {lv['p99_ms']:.1f} ms "
          f"(n = {lv['n_samples']})",
          f"- Largest per-run |M_live − M_host|: {lv['max_abs_diff_M_live_minus_host_pp']} percentage points",
          "", "| Condition | runs | live p50/p99 (ms) | M_host(250ms) | M_live(250ms) | M_host(1s) | M_live(1s) |",
          "|---|---|---|---|---|---|---|"]
    for c in conds:
        L.append(f"| {c['condition']} | {c['n_runs']} | {c['live_p50_ms']:.1f}/{c['live_p99_ms']:.1f} | "
                 f"{c['M_host_0.25s']:.2f} | {c['M_live_0.25s']:.2f} | {c['M_host_1s']:.2f} | {c['M_live_1s']:.2f} |")
    if s["w_sweep"]:
        L += ["", "## Durable path and batch window W (Polar ECG, table conditions)", "",
              "| W (s) | runs | req/s | med-api CPU (%) | commit added p50/p99 (s) | met 10 s (% samples) "
              "| met 250 ms (% samples) | M_durable(10 s) | M_durable(250 ms) | AoI mean (s) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for w in s["w_sweep"]:
            L.append(f"| {w['W']:g} | {w['n_runs']} | {w['req_per_s']:.2f} | {w['api_cpu_pct']:.2f} | "
                     f"{w['durable_p50_s']:.2f}/{w['durable_p99_s']:.2f} | {w['met_10s_pct_of_samples']:.2f} | "
                     f"{w['met_0.25s_pct_of_samples']:.2f} | {w['M_durable_10s']:.2f} | "
                     f"{w['M_durable_0.25s']:.2f} | {w['aoi_mean_s']:.2f} |")
        ws = {w["W"]: w for w in s["w_sweep"]}
        if 0.1 in ws and 5.0 in ws:
            L.append(f"\n- Message-rate reduction W=0.1→5 s: {ws[0.1]['req_per_s'] / ws[5.0]['req_per_s']:.1f}-fold")
        if 0.1 in ws and 1.0 in ws:
            L.append(f"- W=1 s vs 0.1 s in M(10 s): "
                     f"{ws[1.0]['M_durable_10s'] - ws[0.1]['M_durable_10s']:+.2f} percentage points")
    if s.get("stages"):
        L += ["", "## Time per step, sensor to durable commit (Polar ECG, committed samples)", "",
              "Batch-level steps count once per sample of the batch. The BLE step is the added delay "
              "relative to each run's minimum (as for M(D)); all other steps are absolute. Means add up "
              "to the total; percentiles do not. PostgreSQL = commit time minus med-api's logged InfluxDB "
              "write duration (1 ms resolution). stages.csv has the same numbers."]
        for tag in dict.fromkeys(r["tag"] for r in s["stages"]):
            L += ["", f"### {tag}", "", "| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |", "|---|---|---|---|---|"]
            for r in (r for r in s["stages"] if r["tag"] == tag):
                b = "**" if r["step"] == "total" else ""
                L.append(f"| {b}{r['label']}{b} | {r['mean_ms']:.1f} | {r['p50_ms']:.1f} | "
                         f"{r['p95_ms']:.1f} | {r['p99_ms']:.1f} |")
    if tW:
        L.append(f"\nTable III durable column from W = {tW} s; LaTeX rows in table3.tex.")
    (out / "report.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
