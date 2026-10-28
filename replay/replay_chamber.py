#!/usr/bin/env python3
"""
Replay chamber-session rosbags into MedWear through the live ROS 2 bridge.

For every run listed in a session's manifest.csv this:
  1. starts medwear/ros2_to_medwear/medwear_subscriber.py with that run's
     patient/device IDs and --use-sim-time,
  2. plays the run's rosbag in real time (`ros2 bag play --clock`), limited
     to the topics the bridge config subscribes to,
  3. stops the bridge (SIGINT -> it flushes its buffers and drains its upload
     queue), and records the outcome in replay_state.json.

The data therefore takes the same path as live data would: driver topics ->
DDS -> bridge -> /data/batch. Timestamps in MedWear are the original header
stamps from the chamber day, not replay time.

IDs (as agreed for this replay):
    patient_id = <session label>__<row>_<condition>_rep<N>[<--patient-suffix>]
                 e.g. chamber_1__row2_duty50pct_rep1, chamber_1__row2_duty50pct_rep1__W0.5
    device_id  = manifest device                            e.g. Polar_H10, Corsano

Each experiment (--tag) keeps its own state file and logs under logs/<tag>/:
per run the bridge log, a per-batch metrics file (<run>.metrics.jsonl: every
sample's original stamp and bridge receipt time, send/answer times, API
verdict), the med-api log lines of that run (<run>.api.log, whose
"InfluxDB OK" lines give the server-side commit time), and 1 Hz docker
stats of the MedWear containers (<run>.stats.csv).

Everything runs on an isolated ROS domain restricted to localhost, so
replayed topics can't reach (or be polluted by) live drivers on the network.
Finished runs are skipped on the next invocation, so a multi-hour replay can
be interrupted with Ctrl-C and resumed.

Usage:
    source /opt/ros/jazzy/setup.bash && source $ROS_WS/install/setup.bash
    export MEDWEAR_USERNAME=... MEDWEAR_PASSWORD=...
    python3 replay_chamber.py --project <project_id> \\
        ../captures/20261007_105222_chamber_1 \\
        ../captures/20261008_010249_chamber_2

    --dry-run           list runs, IDs and total play time; touch nothing
    --only REGEX        replay only runs whose bag name matches
    --rate R            playback rate (1.0 = real time)

To stop cleanly between runs: touch logs/STOP (removed by run_e2e.sh on start).
"""

import argparse
import csv
import threading
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3
import yaml

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parents[1] / "medwear" / "ros2_to_medwear"  # MedWear next to wireless_experiments
DEFAULT_BRIDGE = BRIDGE_DIR / "medwear_subscriber.py"
DEFAULT_BRIDGE_CONFIG = BRIDGE_DIR / "config.yaml"
LOG_ROOT = HERE / "logs"
# Create this file to stop cleanly before the next run starts (a run stopped
# halfway is replayed from its start on resume, re-sending its batches).
STOP_FILE = LOG_ROOT / "STOP"
STATS_CONTAINERS = ["medwear-api", "medwear-postgres", "medwear-influxdb", "medwear-nginx"]

SUMMARY_RE = re.compile(r"SUMMARY batches_ok=(\d+) batches_failed=(\d+) samples_ok=(\d+)"
                        r" duplicates=(\d+) overlaps=(\d+)")

# Bridge flushes a buffer once it has been idle for gap_threshold_s (1 s by
# default); give it that plus margin after playback ends before stopping it.
POST_PLAY_SETTLE_S = 3.0
BRIDGE_READY_TIMEOUT_S = 30.0
BRIDGE_STOP_TIMEOUT_S = 600.0   # it drains its whole upload queue first


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def session_label(session_dir: Path) -> str:
    m = re.search(r"(chamber_\d+)$", session_dir.name)
    return m.group(1) if m else session_dir.name


def bag_info(bag_dir: Path) -> dict:
    with open(bag_dir / "metadata.yaml") as f:
        return yaml.safe_load(f)["rosbag2_bagfile_information"]


def list_runs(session_dir: Path) -> list:
    label = session_label(session_dir)
    runs = []
    with open(session_dir / "manifest.csv", newline="") as f:
        for row in csv.DictReader(f):
            name = Path(row["capture_file"]).stem
            runs.append({
                "key": f"{label}/{name}",
                "name": name,
                "bag": session_dir / f"{name}_rosbag",
                "device": row["device"],
                "patient": f"{label}__{row['row']}_{row['condition']}_rep{row['replicate']}",
                "manifest_status": row["status"],
            })
    return runs


def state_file(tag: str) -> Path:
    return LOG_ROOT / tag / "replay_state.json"


def load_state(tag: str) -> dict:
    if state_file(tag).exists():
        return json.loads(state_file(tag).read_text())
    return {}


def save_state(tag: str, state: dict) -> None:
    path = state_file(tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


class DockerStats:
    """Samples `docker stats` (CPU %, memory) of the MedWear containers at
    ~1 Hz into a CSV, each line stamped with the host wall clock."""

    def __init__(self, path: Path):
        self.path = path
        self.proc = None
        self.thread = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["docker", "stats", "--format", "{{.Name}},{{.CPUPerc}},{{.MemUsage}}", *STATS_CONTAINERS],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        return self

    def _pump(self) -> None:
        with open(self.path, "w") as f:
            f.write("time,container,cpu_pct,mem\n")
            for line in self.proc.stdout:
                # streaming output is prefixed with terminal clear codes
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                name, _, rest = line.partition(",")
                if name in STATS_CONTAINERS:
                    cpu, _, mem = rest.partition(",")
                    f.write(f"{time.time():.3f},{name},{cpu.rstrip('%')},{mem}\n")

    def __exit__(self, *exc):
        # `docker stats` can ignore SIGTERM (seen in a container talking to a
        # Podman socket); never let it hold up the replay.
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.thread.join(timeout=5)


class ProcStats:
    """Same CSV as DockerStats, measured from /proc instead of cgroups.

    For the SLURM node: every container there shares the job's cgroup (no
    per-container cgroups without delegation), so `docker stats` reports the
    whole job for each container. Here each container's CPU is the summed
    utime+stime of its process tree (root = the container's init PID, which
    the replay container sees with --pid=host), as % of one CPU like
    `docker stats`; memory is the tree's summed RSS (docker stats reports
    cgroup memory, which also counts page cache)."""

    TICK = os.sysconf("SC_CLK_TCK")
    PAGE = os.sysconf("SC_PAGE_SIZE")

    def __init__(self, path: Path, interval: float = 1.0):
        self.path = path
        self.interval = interval
        self.stop = threading.Event()
        self.thread = None

    @staticmethod
    def _procs() -> dict:
        """pid -> (ppid, utime+stime, rss pages, cutime+cstime) for every process."""
        procs = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    s = f.read()
            except OSError:
                continue
            fields = s[s.rindex(")") + 2:].split()     # after "(comm) "
            procs[int(d)] = (int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[21]),
                             int(fields[13]) + int(fields[14]))
        return procs

    @staticmethod
    def _tree(root: int, procs: dict) -> list:
        children = {}
        for pid, (ppid, *_) in procs.items():
            children.setdefault(ppid, []).append(pid)
        out, todo = [], [root]
        while todo:
            pid = todo.pop()
            if pid in procs:
                out.append(pid)
                todo.extend(children.get(pid, []))
        return out

    def _roots(self) -> dict:
        roots = {}
        for name in STATS_CONTAINERS:
            out = subprocess.run(["docker", "inspect", "--format", "{{.State.Pid}}", name],
                                 capture_output=True, text=True)
            if out.returncode == 0 and out.stdout.strip().isdigit():
                roots[name] = int(out.stdout.strip())
        return roots

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self) -> None:
        with open("/proc/meminfo") as f:
            total_gib = int(f.readline().split()[1]) / 2**20
        roots = self._roots()
        prev, prev_pids, prev_t = {}, {}, None
        with open(self.path, "w") as f:
            f.write("time,container,cpu_pct,mem\n")
            while not self.stop.is_set():
                t = time.time()
                procs = self._procs()
                if any(root not in procs for root in roots.values()):
                    roots = self._roots()         # a container was restarted
                cur, cur_pids = {}, {}
                for name, root in roots.items():
                    pids = self._tree(root, procs)
                    # per pid: (own ticks, ticks of its reaped children)
                    ticks = {pid: (procs[pid][1], procs[pid][3]) for pid in pids}
                    if prev_t is not None:
                        # live processes: own ticks since the last sample (a new
                        # one counts from 0: it started within the interval);
                        # processes that ended: their whole lifetime arrives in
                        # the parent's cutime/cstime, minus what was already
                        # counted while they were alive (short-lived workers,
                        # e.g. PostgreSQL backends, finish between samples)
                        used = 0
                        for pid, (own, reaped) in ticks.items():
                            p_own, p_reaped = prev.get(pid, (0, reaped))
                            used += max(0, own - p_own) + max(0, reaped - p_reaped)
                        used -= sum(prev[pid][0] for pid in prev_pids.get(name, ()) if pid not in ticks)
                        used = max(0, used)
                        cpu = 100.0 * used / self.TICK / (t - prev_t)
                        rss = sum(procs[pid][2] for pid in pids) * self.PAGE / 2**20
                        f.write(f"{t:.3f},{name},{cpu:.2f},{rss:.1f}MiB / {total_gib:.2f}GiB\n")
                    cur.update(ticks)
                    cur_pids[name] = list(ticks)
                f.flush()
                prev, prev_pids, prev_t = cur, cur_pids, t   # rebuilt each time: PIDs get reused
                self.stop.wait(self.interval)

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)


def save_api_log(path: Path, since: float, until: float) -> None:
    """Keep the med-api log lines of this run that mention a batch or an error."""
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    out = subprocess.run(["docker", "logs", "--since", fmt(since), "--until", fmt(until),
                          "medwear-api"], capture_output=True, text=True)
    with open(path, "w") as f:
        for line in (out.stdout + out.stderr).splitlines():
            if "batch_uuid=" in line or '"ERROR"' in line or '"WARNING"' in line:
                f.write(line + "\n")


def check_api(base_url: str) -> None:
    """Fail fast on a wrong URL or bad credentials, before hours of replay."""
    if os.getenv("MEDWEAR_TOKEN"):
        return
    user, pw = os.getenv("MEDWEAR_USERNAME"), os.getenv("MEDWEAR_PASSWORD")
    if not (user and pw):
        sys.exit("Set MEDWEAR_USERNAME and MEDWEAR_PASSWORD (or MEDWEAR_TOKEN).")
    r = requests.post(f"{base_url.rstrip('/')}/data/auth/token",
                      json={"username": user, "password": pw}, timeout=15, verify=False)
    if r.status_code != 200:
        sys.exit(f"Login to {base_url} failed ({r.status_code}): {r.text[:200]}")
    log(f"API login OK at {base_url} as {user}")


def wait_for_subscriptions(log_path: Path, n_topics: int, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + BRIDGE_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if log_path.read_text(errors="replace").count("Subscribed:") >= n_topics:
            return True
        time.sleep(0.2)
    return False


def stop(proc: subprocess.Popen, timeout: float) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def replay_run(run: dict, args, cfg_topics: list, env: dict) -> dict:
    run_log_dir = LOG_ROOT / args.tag / run["key"].split("/")[0]
    run_log_dir.mkdir(parents=True, exist_ok=True)
    bridge_log = run_log_dir / f"{run['name']}.bridge.log"
    play_log = run_log_dir / f"{run['name']}.play.log"
    metrics = run_log_dir / f"{run['name']}.metrics.jsonl"
    metrics.unlink(missing_ok=True)   # the bridge appends

    bag_topics = {t["topic_metadata"]["name"] for t in bag_info(run["bag"])["topics_with_message_count"]
                  if t["message_count"] > 0}
    # each data topic plus its ...Info topic (<topic>_info), which the bridge
    # uses for device identity, units and channel metadata
    wanted = set(cfg_topics) | {f"{t}_info" for t in cfg_topics}
    topics = sorted(bag_topics & wanted)
    if not bag_topics & set(cfg_topics):
        return {"status": "skipped", "reason": "no bridged topics in bag"}

    bridge_cmd = ["python3", str(args.bridge), "--config", str(args.bridge_config),
                  "--device", run["device"], "--patient", run["patient"] + args.patient_suffix,
                  "--project", args.project, "--use-sim-time",
                  "--upload-workers", str(args.upload_workers), "--metrics-file", str(metrics)]
    if args.batch_window is not None:
        bridge_cmd += ["--batch-window", str(args.batch_window)]
    play_cmd = ["ros2", "bag", "play", str(run["bag"]), "--clock", "100",
                "--rate", str(args.rate), "--disable-keyboard-controls",
                "--topics", *topics]

    t_start = time.time()
    Stats = ProcStats if os.environ.get("E2E_STATS") == "proc" else DockerStats
    with open(bridge_log, "w") as blog, Stats(run_log_dir / f"{run['name']}.stats.csv"):
        bridge = subprocess.Popen(bridge_cmd, stdout=blog, stderr=subprocess.STDOUT, env=env)
        try:
            if not wait_for_subscriptions(bridge_log, len(cfg_topics), bridge):
                return {"status": "failed", "reason": f"bridge did not start, see {bridge_log}"}
            time.sleep(1.0)  # let DDS discovery settle before the first message
            with open(play_log, "w") as plog:
                play_rc = subprocess.run(play_cmd, stdout=plog, stderr=subprocess.STDOUT,
                                         env=env).returncode
            time.sleep(POST_PLAY_SETTLE_S)
        finally:
            bridge_rc = stop(bridge, BRIDGE_STOP_TIMEOUT_S)
    time.sleep(1.0)   # let the last API log lines reach docker
    save_api_log(run_log_dir / f"{run['name']}.api.log", t_start - 1, time.time())

    m = None
    for m in SUMMARY_RE.finditer(bridge_log.read_text(errors="replace")):
        pass
    result = {"topics": topics, "play_rc": play_rc, "bridge_rc": bridge_rc,
              "bridge_log": str(bridge_log.relative_to(HERE))}
    if m:
        result.update(batches_ok=int(m[1]), batches_failed=int(m[2]), samples_ok=int(m[3]),
                      duplicates=int(m[4]), overlaps=int(m[5]))
    result.update(classify_failures(metrics))
    # A validation rejection (HTTP 4xx other than 429, e.g. an ECG
    # plausibility check) is MedWear correctly refusing data: report it, but
    # it is not a delivery failure. Only transport/server errors fail a run.
    # (The bridge exits 2 whenever any batch was not committed.)
    ok = (play_rc == 0 and bridge_rc in (0, 2) and m is not None
          and result["undelivered_batches"] == 0)
    result["status"] = "ok" if ok else "failed"
    return result


def classify_failures(metrics: Path) -> dict:
    """Split a run's uncommitted batches into validation rejections and
    delivery failures, from the bridge's per-batch metrics."""
    rejected, undelivered = [], 0
    if metrics.exists():
        for line in open(metrics):
            x = json.loads(line)
            if x["ok"]:
                continue
            st = x.get("http_status")
            if st is not None and 400 <= st < 500 and st != 429:
                err = x.get("error") or {}
                rejected.append({"code": err.get("error"), "detail": str(err.get("detail"))[:200],
                                 "signal_type": x["signal_type"], "n_samples": x["n_samples"],
                                 "msg_stamp0_ns": x["msg_stamp0_ns"]})
            else:
                undelivered += 1
    return {"rejected_batches": rejected, "undelivered_batches": undelivered}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sessions", nargs="+", type=Path, help="chamber session directories")
    p.add_argument("--project", required=True, help="MedWear project_id")
    p.add_argument("--api", default=os.getenv("API_BASE_URL", "https://localhost/v1"),
                   help="MedWear API base URL (default: $API_BASE_URL or https://localhost/v1)")
    p.add_argument("--rate", type=float, default=1.0, help="playback rate (default 1.0 = real time)")
    p.add_argument("--only", help="regex; replay only runs whose bag name matches")
    p.add_argument("--force", action="store_true", help="re-replay runs already marked ok")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--domain-id", default="87", help="isolated ROS_DOMAIN_ID (default 87)")
    p.add_argument("--tag", default="default",
                   help="experiment name: state and logs go to logs/<tag>/ (e.g. full_W1, sweep_W0.5)")
    p.add_argument("--batch-window", type=float, help="bridge batch window W in s (default: config)")
    p.add_argument("--upload-workers", type=int, default=4, help="bridge upload threads (default 4)")
    p.add_argument("--patient-suffix", default="",
                   help="appended to every patient_id, e.g. __W0.5 to keep W-sweep runs apart")
    p.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    p.add_argument("--bridge-config", type=Path, default=DEFAULT_BRIDGE_CONFIG)
    args = p.parse_args()

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with open(args.bridge_config) as f:
        cfg_topics = [t["topic"] for t in yaml.safe_load(f).get("topics", [])]

    runs = [r for s in args.sessions for r in list_runs(s.resolve())]
    if args.only:
        runs = [r for r in runs if re.search(args.only, r["name"])]
    state = load_state(args.tag)
    todo = [r for r in runs if args.force or state.get(r["key"], {}).get("status") != "ok"]

    total_s = sum(bag_info(r["bag"])["duration"]["nanoseconds"] / 1e9 for r in todo) / args.rate
    log(f"{len(runs)} runs selected, {len(runs) - len(todo)} already done, {len(todo)} to replay "
        f"(~{total_s / 3600:.1f} h of playback at rate {args.rate})")

    if args.dry_run:
        for r in todo:
            dur = bag_info(r["bag"])["duration"]["nanoseconds"] / 1e9
            print(f"  {r['key']:70s} device={r['device']:10s} "
                  f"patient={r['patient'] + args.patient_suffix:50s} {dur:7.0f}s")
        return

    check_api(args.api)
    env = dict(os.environ, API_BASE_URL=args.api, ROS_DOMAIN_ID=args.domain_id,
               ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", PYTHONUNBUFFERED="1")

    n_fail = 0
    for i, r in enumerate(todo, 1):
        if STOP_FILE.exists():
            log(f"Stop requested ({STOP_FILE}); stopping before {r['key']}. "
                "Re-run the same command to resume.")
            sys.exit(3)
        log(f"[{i}/{len(todo)}] {r['key']} → patient={r['patient'] + args.patient_suffix} device={r['device']}")
        started = datetime.now().isoformat(timespec="seconds")
        try:
            result = replay_run(r, args, cfg_topics, env)
        except KeyboardInterrupt:
            log("Interrupted; progress saved. Re-run the same command to resume.")
            sys.exit(130)
        result.update(patient_id=r["patient"] + args.patient_suffix, device_id=r["device"], project_id=args.project,
                      api=args.api, started=started, finished=datetime.now().isoformat(timespec="seconds"))
        result.update(tag=args.tag, batch_window=args.batch_window,
                      upload_workers=args.upload_workers)
        state[r["key"]] = result
        save_state(args.tag, state)
        n_fail += result["status"] == "failed"
        log(f"    {result['status']}: {result.get('batches_ok', 0)} batches, "
            f"{result.get('samples_ok', 0)} samples, {result.get('batches_failed', 0)} failed"
            + (f" ({result['reason']})" if "reason" in result else ""))

    log(f"Done: {len(todo) - n_fail} ok/skipped, {n_fail} failed. State: {state_file(args.tag)}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
