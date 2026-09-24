#!/usr/bin/env python3
"""
Shared infrastructure for chamber_session.py (Rows 1-5, the chamber day) and
gym_session.py (Row 6, the separate real-gym visit): logging, config
loading, btmon/driver process control, the pre-flight device sanity check,
run execution, and capture parsing.
"""

import csv
import json
import logging
import os
import pty
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

log = logging.getLogger("session_lib")


class _ConsoleColorFormatter(logging.Formatter):
    """Colorizes console output: PASSED results green, FAILED/error output
    red, and "=== ... ===" section banners (a new row, row block, or check
    starting) blue. Plain text otherwise -- this never touches the file
    handler, so the on-disk log stays clean and grep/diff-friendly."""

    RESET = "\033[0m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    BLUE = "\033[34m"

    def format(self, record):
        text = super().format(record)
        message = record.getMessage()
        if "RESULT: PASSED" in message:
            color = self.GREEN
        elif record.levelno >= logging.ERROR or "RESULT: FAILED" in message:
            color = self.RED
        elif message.strip().startswith("==="):
            color = self.BLUE
        else:
            return text
        return f"{color}{text}{self.RESET}"


def setup_logging(session_dir):
    """Configure logging to both the console and a log file inside this
    session's own directory, so the full history of a session is on disk,
    not just scrollback."""
    log_path = session_dir / "session.log"
    root = logging.getLogger()
    # DEBUG-level records (e.g. the periodic driver heartbeat) are written to
    # the file but filtered out of the console, which shows a live progress
    # bar instead -- see sleep_and_watch_driver.
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    if sys.stdout.isatty():
        console_handler.setFormatter(
            _ConsoleColorFormatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    else:
        console_handler.setFormatter(fmt)
    root.handlers = [file_handler, console_handler]
    log.info(f"logging to {log_path}")
    return log_path


# =============================================================================
# Config loading
# =============================================================================


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def get_output_dir(cfg):
    """The base directory under which every session gets its own timestamped
    subdirectory (see get_session_dir)."""
    d = Path(cfg["session"]["output_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


SESSION_DIR_RE = re.compile(r"^\d{8}_\d{6}$")


def get_session_dir(cfg, session_ts):
    """Each session (a 'run' invocation) gets its own timestamped directory
    under the base output_dir, holding that session's log, captures,
    manifest.csv, and results.csv together as a self-contained unit."""
    d = get_output_dir(cfg) / session_ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_latest_session_ts(cfg):
    """Return the timestamp (directory name) of the most recently created
    session under the base output_dir, or None if no sessions exist yet.
    Used so `parse` can default to the latest session when --session isn't
    given."""
    base = get_output_dir(cfg)
    session_names = sorted(
        p.name for p in base.iterdir() if p.is_dir() and SESSION_DIR_RE.match(p.name)
    )
    return session_names[-1] if session_names else None


def manifest_path(session_dir):
    return session_dir / "manifest.csv"


def results_path(cfg, session_dir):
    name = Path(cfg["session"]["results_csv"]).name
    return session_dir / name


def run_key(row_label, device_name, condition, replicate):
    """Identifies one (row, device, condition, replicate) run, for tracking
    which runs a resumed session can skip."""
    return (row_label, device_name, condition, str(replicate))


def load_completed_keys(cfg, session_dir):
    """Return the run_key()s that `run --resume` should skip.

    For a retryable row (retry_on_fail=True, currently only Row 1), only a
    real PASSED counts as done -- a FAILED run there means the setup was
    broken and should be retried on resume, same as before.

    For a no-retry row (retry_on_fail=False, Rows 2/3/5), ANY recorded
    attempt -- PASSED or FAILED -- counts as done. A FAILED attempt there is
    itself the recorded data point (the interference did its job), not an
    error to retry; without this, --resume would silently re-run it under a
    capture filename with no _attemptN suffix (do_run only adds that suffix
    inside its retry loop, which no-retry rows never enter) and overwrite
    the original failed capture, destroying the evidence.

    Reads manifest.csv, NOT results.csv (originally read results.csv --
    real bug, found 2026-09-24 after a session crashed with an uncaught
    ValueError mid-run: only main()'s RunAbortedError handler calls
    do_parse before exiting, so a ValueError/StrayProcessError/
    InfrastructureCheckError/DeviceCheckError crash -- anything else --
    leaves results.csv missing or stale. With results.csv missing, this
    returned an empty set, so --resume would have treated every already-
    completed run as not done and re-run it, overwriting the ORIGINAL
    completed captures under the same no-suffix filename described above
    -- silently destroying real, already-collected data, not just wasting
    chamber time. manifest.csv is written incrementally by append_manifest
    inside _attempt_run itself, so it survives any crash and already
    carries the same status/retry_on_fail columns this needs."""
    path = manifest_path(session_dir)
    if not path.exists():
        return set()
    with open(path) as f:
        keys = set()
        for row in csv.DictReader(f):
            status = row.get("status")
            retry_on_fail = str(row.get("retry_on_fail", "True")).strip().lower()
            if status == "PASSED" or (status == "FAILED" and retry_on_fail == "false"):
                keys.add(
                    run_key(
                        row["row"], row["device"], row["condition"], row["replicate"]
                    )
                )
        return keys


def load_failed_keys(session_dir):
    """Return the run_key()s recorded as FAILED in manifest.csv, regardless
    of retry_on_fail -- the counterpart to load_completed_keys, used by
    --rerun-failed to find what to re-attempt."""
    path = manifest_path(session_dir)
    if not path.exists():
        return set()
    with open(path) as f:
        return {
            run_key(row["row"], row["device"], row["condition"], row["replicate"])
            for row in csv.DictReader(f)
            if row.get("status") == "FAILED"
        }


def archive_failed_capture_for_rerun(session_dir, manifest_row):
    """Before re-running a FAILED (row, device, condition, replicate),
    rename its existing capture and associated files (pcap, afh.csv,
    rosbag dir) out of the way first. No-retry rows (2/3/4/5/6) always
    reuse the same capture filename with no attempt suffix (see
    load_completed_keys's docstring) -- without this, do_run's rerun
    would silently overwrite the original failed evidence rather than
    sitting alongside it. Best-effort: a rename failure just logs a
    warning and continues -- that one file's old copy would then be
    overwritten by the rerun (the same risk as not having this safeguard
    at all), but it shouldn't block the rerun itself."""
    capture_file = session_dir / manifest_row["capture_file"]
    if not capture_file.exists():
        return
    ts = re.sub(r"[^0-9]", "", manifest_row.get("start_time", "")) or "unknowntime"
    archived_stem = f"{capture_file.stem}_prerun_failed_{ts}"
    candidates = [
        (capture_file, archived_stem + capture_file.suffix),
        (capture_file.with_suffix(".pcap"), archived_stem + ".pcap"),
        (capture_file.with_name(capture_file.stem + "_afh.csv"), archived_stem + "_afh.csv"),
        (capture_file.with_name(capture_file.stem + "_rosbag"), archived_stem + "_rosbag"),
    ]
    for src, new_name in candidates:
        if not src.exists():
            continue
        dst = src.with_name(new_name)
        try:
            src.rename(dst)
            log.info(
                f"  -> archived {src.name} -> {dst.name} (preserving the "
                f"failed attempt before rerun)"
            )
        except OSError as e:
            log.warning(
                f"  -> could not archive {src.name} before rerun: {e} -- "
                f"it may be overwritten by the rerun"
            )


def prepare_rerun_failed(session_dir, completed):
    """For --rerun-failed: archives every FAILED run's existing captures
    (see archive_failed_capture_for_rerun) and returns a NEW completed-keys
    set with those keys removed, so do_run's skip-if-completed check
    treats them as not yet done and re-attempts them. Returns `completed`
    unchanged if nothing is recorded as FAILED.

    Deliberately does NOT filter by failure_kind or row: for no-retry rows
    (2/3/4/5/6), a FAILED result is often ITSELF the intended data point
    (interference causing a real disconnect, or -- since 2026-09-24's
    evaluate_run_result redesign -- specifically the runs that DIDN'T match
    "ran as expected"), not something to blindly retry away. Re-running one
    of those means deliberately discarding that recorded outcome for a
    fresh attempt -- this function makes that possible and preserves the
    original evidence, but the decision to invoke --rerun-failed on an
    interference row's results is the operator's, not something this
    silently assumes is always correct."""
    failed_keys = load_failed_keys(session_dir)
    if not failed_keys:
        log.info("--rerun-failed: nothing recorded as FAILED, nothing to do")
        return completed
    log.info(
        f"--rerun-failed: re-attempting {len(failed_keys)} run(s) previously "
        f"recorded as FAILED: {sorted(failed_keys)}"
    )
    with open(manifest_path(session_dir)) as f:
        manifest_rows = list(csv.DictReader(f))
    for row in manifest_rows:
        key = run_key(row["row"], row["device"], row["condition"], row["replicate"])
        if key in failed_keys:
            archive_failed_capture_for_rerun(session_dir, row)
    return completed - failed_keys


def append_manifest(session_dir, row_dict):
    path = manifest_path(session_dir)
    is_new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "session_id",
                "row",
                "device",
                "condition",
                "replicate",
                "capture_file",
                "start_time",
                "duration_s",
                "status",
                "error",
                "induced_load_mbps",
                "packet_loss_pct",
                "iperf_jitter_ms",
                "qbss_channel_utilization_pct",
                "mac_address",
                "sniffer_pcap_file",
                "connection_established",
                "retry_on_fail",
            ],
        )
        if is_new:
            writer.writeheader()
        writer.writerow(row_dict)


# =============================================================================
# btmon capture control
# =============================================================================


def start_btmon(controller, capture_path):
    """Start btmon in the background, writing to capture_path (btsnoop format)."""
    log.info(f"  -> starting btmon on {controller} -> {capture_path}")
    cmd = ["btmon", "-i", controller, "-w", str(capture_path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)  # give it a moment to attach before anything else starts
    log.info(f"     btmon running (pid {proc.pid})")
    return proc


def stop_btmon(proc):
    log.info(f"  -> stopping btmon (pid {proc.pid})")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning(f"     btmon (pid {proc.pid}) didn't exit cleanly, terminating")
        proc.terminate()


# =============================================================================
# rosbag recording -- ALL topics, one bag PER RUN (same granularity as
# start_btmon/start_ble_sniffer/start_afh_poller, all of which are already
# per-run, not per-session), not one long rolling session-spanning
# recording: a single run is already duration-bounded by the row's own
# config (300s/1800s/etc), so there's no giant-file problem to solve at
# that scope, and one bag per run keeps rosbag data lined up 1:1 with each
# run's own .btsnoop/afh.csv/manifest row, without needing to slice a
# multi-hour session-wide bag back apart later to find one run's data.
# Started/stopped inside _attempt_run, same lifecycle as start_btmon.
# =============================================================================


def start_rosbag_recording(out_dir):
    """Start `ros2 bag record -a` (every topic currently visible) for one
    run, no duration limit -- each run is already bounded by its own
    planned duration, so there's nothing to split. Writes into out_dir (a
    bag DIRECTORY: metadata.yaml plus a storage file) -- ros2 bag record
    refuses to write into an already-existing directory, so out_dir must
    not exist yet. Returns None (never raises) if `ros2` isn't on PATH or
    the recorder exits immediately -- rosbag capture is a best-effort
    supplement to the BLE/HCI-level captures the run already relies on,
    not something that should abort a real chamber run."""
    log.info(f"  -> starting rosbag recording (all topics) -> {out_dir}")
    cmd = ["ros2", "bag", "record", "-a", "-o", str(out_dir)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.warning(
            "  -> `ros2 bag record` not found -- skipping rosbag capture for "
            "this run (is the ROS 2 environment sourced?)"
        )
        return None
    time.sleep(1.0)
    if proc.poll() is not None:
        log.warning(
            f"  -> rosbag recorder exited immediately (code {proc.returncode}) "
            f"-- skipping rosbag capture for this run (check that {out_dir} "
            f"doesn't already exist)"
        )
        return None
    log.info(f"     rosbag recorder running (pid {proc.pid})")
    return proc


def stop_rosbag_recording(proc):
    """SIGINT (not SIGTERM/kill) so it can flush and finalize its storage
    file and metadata.yaml -- an abrupt kill risks an unreadable/corrupt
    bag. Longer timeout than stop_btmon's since flushing a run's worth of
    accumulated topics (up to 30min for Row 4) takes real time."""
    if proc is None:
        return
    log.info(f"  -> stopping rosbag recorder (pid {proc.pid})")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        log.warning(
            f"     rosbag recorder (pid {proc.pid}) didn't exit cleanly, terminating"
        )
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# =============================================================================
# BLE sniffer (Nordic nRF Sniffer for Bluetooth LE) -- optional, physical
# over-the-air capture, separate from btmon's HCI-level view of our own
# controller. See ble_sniffer in config.yaml for what this does and doesn't
# cover.
# =============================================================================


def _resolve_sniffer_binary():
    """Locate the nrfutil-ble-sniffer binary: prefer PATH, fall back to the
    default nrfutil install location."""
    import shutil

    found = shutil.which("nrfutil-ble-sniffer")
    if found:
        return found
    default = Path.home() / ".nrfutil" / "bin" / "nrfutil-ble-sniffer"
    return str(default) if default.exists() else None


def start_ble_sniffer(cfg, mac_address, pcap_path):
    """Launch the Nordic sniffer as a background process capturing to
    pcap_path, following the given device's advertising traffic. Returns
    the Popen handle, or None if sniffing is disabled/unavailable (never
    raises -- a missing dongle shouldn't fail the actual test)."""
    sniffer_cfg = cfg.get("ble_sniffer") or {}
    if not sniffer_cfg.get("enabled"):
        return None
    serial_port = sniffer_cfg.get("serial_port")
    if not serial_port or not Path(serial_port).exists():
        log.warning(
            f"  -> BLE sniffer serial port {serial_port or '(unset)'} not found, "
            f"skipping over-the-air capture for this run"
        )
        return None
    binary = _resolve_sniffer_binary()
    if not binary:
        log.warning("  -> nrfutil-ble-sniffer not found, skipping over-the-air capture")
        return None
    try:
        proc = subprocess.Popen(
            [
                binary,
                "sniff",
                "--port",
                serial_port,
                "--follow",
                f"{mac_address} random",
                "--output-pcap-file",
                str(pcap_path),
                "--log-output",
                "stdout",
                "--log-level",
                "warn",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning(f"  -> could not start BLE sniffer: {e}")
        return None
    log.info(
        f"  -> BLE sniffer running on {serial_port} (pid {proc.pid}) -> {pcap_path}"
    )
    return proc


def stop_ble_sniffer(proc):
    """Stop a sniffer started by start_ble_sniffer(). SIGTERM confirmed (by
    direct testing) to flush and close the pcap file cleanly."""
    if proc is None:
        return
    log.info(f"  -> stopping BLE sniffer (pid {proc.pid})")
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        log.warning(f"     BLE sniffer (pid {proc.pid}) didn't exit cleanly, killing")
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


EMPTY_SNIFFER_METRICS = {
    "sniffer_adv_packet_count": None,
    "sniffer_adv_crc_error_count": None,
    "sniffer_adv_crc_error_rate": None,
    "sniffer_adv_rssi_avg_dbm": None,
    "sniffer_adv_rssi_min_dbm": None,
    "sniffer_adv_rssi_max_dbm": None,
}


def parse_sniffer_pcap(pcap_path, mac_address):
    """Extract advertising-channel over-the-air metrics for one device from
    a Nordic sniffer capture, via tshark. This is real, physically measured
    CRC/RSSI data -- btmon can never show this, since it only reports what
    OUR OWN controller successfully received, not what was actually
    transmitted over the air. Covers advertising channels (37/38/39) only;
    see the module docstring above for why the data connection's channels
    aren't covered. Returns EMPTY_SNIFFER_METRICS (all None) if the pcap is
    missing, empty, or tshark isn't available -- never raises."""
    if not mac_address or not pcap_path or not Path(pcap_path).exists():
        return dict(EMPTY_SNIFFER_METRICS)
    try:
        result = subprocess.run(
            [
                "tshark",
                "-r",
                str(pcap_path),
                "-Y",
                f"btle.advertising_address == {mac_address.lower()}",
                "-T",
                "fields",
                "-e",
                "nordic_ble.crcok",
                "-e",
                "nordic_ble.rssi",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return dict(EMPTY_SNIFFER_METRICS)
    if result.returncode != 0:
        return dict(EMPTY_SNIFFER_METRICS)

    total = 0
    crc_bad = 0
    rssi_values = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        crcok = parts[0] if len(parts) > 0 else ""
        rssi = parts[1] if len(parts) > 1 else ""
        total += 1
        if crcok == "False":
            crc_bad += 1
        if rssi:
            try:
                rssi_values.append(int(rssi))
            except ValueError:
                pass

    if total == 0:
        return dict(EMPTY_SNIFFER_METRICS)
    return {
        "sniffer_adv_packet_count": total,
        "sniffer_adv_crc_error_count": crc_bad,
        "sniffer_adv_crc_error_rate": round(crc_bad / total, 4),
        "sniffer_adv_rssi_avg_dbm": (
            round(sum(rssi_values) / len(rssi_values), 1) if rssi_values else None
        ),
        "sniffer_adv_rssi_min_dbm": min(rssi_values) if rssi_values else None,
        "sniffer_adv_rssi_max_dbm": max(rssi_values) if rssi_values else None,
    }


def _qbss_iface_is_safe(iface):
    """Refuse to scan from an interface that's likely sharing a physical
    radio/antenna with the Bluetooth controller under test. Confirmed by
    direct testing on 2026-09-22: on this machine, wlp0s20f3 (iwlwifi
    driver, Intel Meteor Lake PCH CNVi WiFi) and hci0 (Intel AX211
    Bluetooth) are the SAME physical combo module despite enumerating on
    different buses (PCIe vs USB) -- scanning from wlp0s20f3 mid-run would
    force WiFi/BT antenna time-sharing and perturb the very BLE link being
    measured, which would be worse than collecting no QBSS data at all.
    Heuristic: refuses any iface using the iwlwifi driver, since virtually
    all modern Intel WiFi ships as a CNVi combo card paired with Bluetooth
    on the same module. A genuinely separate USB WiFi dongle (ath9k_htc,
    rt2800usb, rtl8xxxu, etc.) is safe."""
    driver_path = Path(f"/sys/class/net/{iface}/device/driver")
    if not driver_path.exists():
        return False  # can't verify -- fail closed, not "assume safe"
    try:
        driver = driver_path.resolve(strict=True).name
    except OSError:
        return False
    return driver != "iwlwifi"


def poll_qbss_channel_utilization(iface, bssid):
    """Best-effort attempt at a real airtime signal: scan and read the
    target bssid's advertised QBSS Load IE (802.11e/k channel utilization,
    0-255 scale, 255 == 100% busy), if the AP broadcasts one. This is the
    only real-airtime cross-check available without a dedicated monitor-
    mode radio -- see the ble_sniffer/row2_duty_cycle comments in
    config.yaml for why a proper per-second airtime capture isn't
    implemented. Returns a dict with a 0-100 percent value, or None if the
    interface, AP, or IE isn't available -- never raises, since this must
    never fail an actual test. One sample; see start_qbss_poller for
    repeated sampling during a run.

    Two real bugs fixed here (confirmed live, 2026-09-23, against the real
    WirelessTests/TL-MR6400 AP once a genuinely separate USB WiFi adapter
    was available to test against for the first time -- qbss_iface had
    been null since this was written, so neither bug had ever been
    exercised before):
    1. `iw dev <iface> scan bssid <bssid>` -- "bssid" is not a valid scan
       filter on this iw version (6.7; usage: scan [-u] [freq ...]
       [duration ...] [ies ...] [meshid ...] [...] [ssid ...|passive], no
       bssid option at all) -- the command always failed with a usage
       error and non-zero exit, so this function could never have returned
       real data. Fixed by scanning broadly (no filter) instead.
    2. A broad scan returns EVERY visible AP's own "BSS Load" block, each
       with its own "channel utilisation: N/255" line -- searching the
       whole scan output with one global regex (the original approach)
       would silently match whichever AP's line happens to come first,
       not necessarily the target bssid's. Fixed by first locating the
       target's own "BSS <bssid>(...)" section and searching only within
       it (up to the next "BSS " line or end of output).

    A THIRD real (non-code) failure mode, found diagnosing why an entire
    real session's row3 (forced-channel) block came back with every QBSS
    sample blank while row2 on the same session had real values throughout
    (2026-09-24): nl80211 only allows ONE scan in flight per physical
    radio -- a second concurrent `iw scan` on the same iface fails with
    "Device or resource busy" (confirmed by firing two at once: one
    returncode=0, the other returncode=240). That session's row3 window
    overlapped with unrelated manual `iw scan` testing on this same
    adapter (developing poll_channel_occupancy_snapshot/count_nearby_ble_
    devices below), so the two competed and the session's own polls lost
    that race every time. The busy/AP-not-found/no-IE cases were
    previously indistinguishable (all just silently returned None) --
    logged at DEBUG now so a repeat doesn't need this same forensic
    reconstruction.
    """
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "scan"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        log.debug(
            f"  -> QBSS scan on {iface} failed (returncode={result.returncode}): "
            f"{result.stderr.strip()[:200]}"
        )
        return None
    section_re = re.compile(
        rf"^BSS {re.escape(bssid)}\b[^\n]*\n(.*?)(?=^BSS |\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    section_m = section_re.search(result.stdout)
    if not section_m:
        return None
    m = re.search(r"channel utilisation:\s*(\d+)/255", section_m.group(1), re.IGNORECASE)
    if not m:
        return None
    raw = int(m.group(1))
    return {"qbss_channel_utilization_pct": round(100 * raw / 255, 1)}


def _qbss_poll_loop(iface, bssid, samples, poll_interval_s, stop_event):
    while not stop_event.wait(poll_interval_s):
        result = poll_qbss_channel_utilization(iface, bssid)
        if result:
            samples.append(result["qbss_channel_utilization_pct"])


def start_qbss_poller(iface, bssid, poll_interval_s=30):
    """Start a background thread sampling QBSS channel utilization every
    poll_interval_s WHILE the run is in progress -- polling only once after
    the run ends (an earlier version of this function) measures an idle
    channel, defeating the whole point. Refuses to start (returns None, logs
    once) if iface fails _qbss_iface_is_safe -- actively perturbing the BLE
    measurement it's trying to explain would be worse than no data."""
    if not iface or not bssid:
        return None
    if not _qbss_iface_is_safe(iface):
        log.warning(
            f"  -> QBSS poll skipped: {iface} looks like it may share a "
            f"physical radio/antenna with the Bluetooth controller "
            f"(iwlwifi driver) -- scanning from it mid-run would perturb "
            f"the BLE link being measured. Use a separate USB WiFi adapter "
            f"instead (see config.yaml's qbss_iface comment)."
        )
        return None
    samples = []
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_qbss_poll_loop,
        args=(iface, bssid, samples, poll_interval_s, stop_event),
        daemon=True,
    )
    thread.start()
    log.info(
        f"  -> QBSS channel-utilization poller running (every "
        f"{poll_interval_s}s) on {iface}"
    )
    return thread, stop_event, samples


def stop_qbss_poller(poller):
    """Stop the poller started by start_qbss_poller() and return
    {"qbss_channel_utilization_pct": avg-over-the-run} from whatever samples
    were collected, or None if none were (e.g. the run was shorter than one
    poll_interval_s, or every scan failed)."""
    if poller is None:
        return None
    thread, stop_event, samples = poller
    stop_event.set()
    thread.join(timeout=5)
    if not samples:
        return None
    return {"qbss_channel_utilization_pct": round(sum(samples) / len(samples), 1)}


# =============================================================================
# Per-channel (1-13) WiFi occupancy -- for the passive real-gym visit
# (gym_session.py), not the chamber rows. Where poll_qbss_channel_utilization
# above tracks ONE known AP's own advertised load, this surveys every
# visible 2.4GHz channel via a broad scan, so a gym with several APs on
# different channels doesn't get summarized by only its busiest one --
# needed to answer "how much AFH headroom does a real gym leave" (manuscript
# Table tab:gym-observation) for channels the config doesn't already know
# about. Same _qbss_iface_is_safe() gate and separate-adapter requirement.
# =============================================================================

WIFI_24GHZ_CHANNELS = list(range(1, 14))


def _freq_mhz_to_24ghz_channel(freq_mhz):
    """2412 MHz (ch1) .. 2472 MHz (ch13), 5 MHz spacing (802.11 channel
    numbering). Returns None for anything outside that band (5/6GHz APs,
    which aren't part of the BLE-relevant 2.4GHz picture here)."""
    channel = round((freq_mhz - 2407) / 5)
    return channel if 1 <= channel <= 13 else None


def _parse_channel_occupancy(scan_stdout):
    """Bucket every BSS in a broad `iw scan` by its 2.4GHz channel. Returns
    {channel: {"ap_count": N, "mean_channel_utilization_pct": X or None}}
    for channels 1-13 that had at least one visible AP -- channels with no
    APs are left out (the caller/CSV writer fills the full 1-13 range with
    zeros so "channel had nothing on it" is distinguishable from "poll
    failed"). Per-AP channel utilization only averages over APs that
    actually advertise a BSS Load IE (many consumer routers don't), so a
    channel's count can be nonzero while its utilization is None."""
    channels = {}
    for bss_block in re.split(r"^BSS ", scan_stdout, flags=re.MULTILINE)[1:]:
        freq_m = re.search(r"freq:\s*([\d.]+)", bss_block)
        if not freq_m:
            continue
        channel = _freq_mhz_to_24ghz_channel(float(freq_m.group(1)))
        if channel is None:
            continue
        entry = channels.setdefault(channel, {"ap_count": 0, "utilizations": []})
        entry["ap_count"] += 1
        util_m = re.search(r"channel utilisation:\s*(\d+)/255", bss_block, re.IGNORECASE)
        if util_m:
            entry["utilizations"].append(100.0 * int(util_m.group(1)) / 255.0)
    return {
        ch: {
            "ap_count": v["ap_count"],
            "mean_channel_utilization_pct": (
                round(sum(v["utilizations"]) / len(v["utilizations"]), 1)
                if v["utilizations"] else None
            ),
        }
        for ch, v in channels.items()
    }


def poll_channel_occupancy_snapshot(iface):
    """One broad scan, bucketed per 2.4GHz channel. Returns None on any
    scan failure (interface, permissions, driver) -- never raises. Same
    concurrent-scan caveat as poll_qbss_channel_utilization: nl80211 only
    allows one scan in flight per radio, so a second scan on this same
    iface (e.g. manual `iw scan` testing, or another poller on the same
    adapter) fails with "Device or resource busy" -- logged at DEBUG so a
    string of blank polls is diagnosable rather than just silent."""
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "scan"], capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        log.debug(
            f"  -> Channel-occupancy scan on {iface} failed "
            f"(returncode={result.returncode}): {result.stderr.strip()[:200]}"
        )
        return None
    return _parse_channel_occupancy(result.stdout)


def _channel_occupancy_poll_loop(iface, out_csv_path, poll_interval_s, stop_event):
    start = time.time()
    with open(out_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_s", "channel", "ap_count", "mean_channel_utilization_pct"])
        f.flush()
        while not stop_event.wait(poll_interval_s):
            snapshot = poll_channel_occupancy_snapshot(iface)
            ts = round(time.time() - start, 1)
            if snapshot is None:
                continue  # scan failed this cycle -- skip, don't write fabricated zeros
            for channel in WIFI_24GHZ_CHANNELS:
                entry = snapshot.get(channel, {"ap_count": 0, "mean_channel_utilization_pct": None})
                writer.writerow([ts, channel, entry["ap_count"], entry["mean_channel_utilization_pct"]])
            f.flush()


def start_channel_occupancy_poller(iface, out_csv_path, poll_interval_s=60):
    """Start a background thread scanning all 13 2.4GHz channels every
    poll_interval_s for the WHOLE gym session (unlike the per-run AFH/QBSS
    pollers, this is meant to be started once and run across every device
    in the visit -- background WiFi occupancy doesn't belong to any one
    device's run). Refuses to start (returns None, logs once) if iface
    fails _qbss_iface_is_safe(), same reasoning as start_qbss_poller."""
    if not iface:
        return None
    if not _qbss_iface_is_safe(iface):
        log.warning(
            f"  -> Channel-occupancy poll skipped: {iface} looks like it may "
            f"share a physical radio/antenna with the Bluetooth controller "
            f"(iwlwifi driver) -- scanning from it would perturb the BLE "
            f"link being measured. Use a separate USB WiFi adapter instead."
        )
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_channel_occupancy_poll_loop,
        args=(iface, out_csv_path, poll_interval_s, stop_event),
        daemon=True,
    )
    thread.start()
    log.info(
        f"  -> Per-channel WiFi occupancy poller running (every "
        f"{poll_interval_s}s, channels 1-13) on {iface} -> {out_csv_path}"
    )
    return thread, stop_event


def stop_channel_occupancy_poller(poller):
    if poller is None:
        return
    thread, stop_event = poller
    stop_event.set()
    thread.join(timeout=5)


# =============================================================================
# Nearby BLE device density -- for the passive real-gym visit, a rough
# background-BLE-density proxy (count of distinct advertisers during a
# short scan window), not a precise census.
#
# PRIVACY (GDPR / Swiss FADP): a public space's BLE scan picks up MAC
# addresses from bystanders' phones, watches, and other personal devices,
# which can count as personal data. This is designed so that constraint
# can't be violated by a future edit: count_nearby_ble_devices() below
# builds a Python set of addresses ONLY to deduplicate within one scan
# window, and returns nothing but the integer len() of that set -- the
# addresses themselves are never returned, logged, or written to disk, and
# go out of scope (garbage-collected) the moment the function returns.
# _ble_density_poll_loop only ever writes that integer to CSV. Before using
# this for a real visit: get the facility's permission, let staff know
# you're there, and confirm with them that no ethics review is needed (the
# sensors are on a mannequin and no patient data is collected, but the
# facility should confirm that holds for their specific space/policy).
#
# Unlike the per-channel WiFi poller (separate USB adapter, no
# perturbation), this scans on hci0 -- the SAME radio the sensor under
# test is connected through. Scanning puts the adapter into a mode that
# competes for airtime with the active connection, so this deliberately
# uses a short window (default 5s) at a long interval (default 90s) to
# keep that perturbation brief and infrequent rather than pretending it
# doesn't exist.
# =============================================================================

_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")


def count_nearby_ble_devices(controller, scan_duration_s=5):
    """Count distinct BLE advertisers seen in a short scan window. Returns
    an int, or None on any failure (missing hcitool, permission, etc.) --
    never raises. See the module-level comment above: the individual
    addresses used to deduplicate never leave this function."""
    try:
        proc = subprocess.Popen(
            ["hcitool", "-i", controller, "lescan", "--duplicates"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
    except FileNotFoundError:
        return None
    time.sleep(scan_duration_s)
    proc.terminate()
    try:
        stdout, _ = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, _ = proc.communicate(timeout=3)
        except Exception:
            return None
    addresses = set()
    for line in stdout.splitlines():
        m = _BLE_ADDRESS_RE.match(line.strip())
        if m:
            addresses.add(m.group(1))
    return len(addresses)  # `addresses` itself is discarded here, on return


def _ble_density_poll_loop(controller, out_csv_path, scan_duration_s, poll_interval_s, stop_event):
    start = time.time()
    with open(out_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_s", "nearby_device_count"])
        f.flush()
        while not stop_event.wait(poll_interval_s):
            count = count_nearby_ble_devices(controller, scan_duration_s)
            if count is None:
                continue
            writer.writerow([round(time.time() - start, 1), count])
            f.flush()


def start_ble_density_poller(controller, out_csv_path, scan_duration_s=5, poll_interval_s=90):
    """Start a background thread sampling nearby BLE device density for the
    WHOLE gym session (like start_channel_occupancy_poller, not scoped to
    any one device's run). Pass controller=None or enabled=False upstream
    to skip entirely -- there's no safety auto-detection here the way
    _qbss_iface_is_safe gates the WiFi poller, since this deliberately DOES
    share hci0 with the sensor connection (see module comment); the
    short/infrequent defaults are the mitigation, not an interface check."""
    if not controller:
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_ble_density_poll_loop,
        args=(controller, out_csv_path, scan_duration_s, poll_interval_s, stop_event),
        daemon=True,
    )
    thread.start()
    log.info(
        f"  -> Nearby-BLE-device-density poller running ({scan_duration_s}s scan "
        f"every {poll_interval_s}s, aggregate count only -- no MACs logged) "
        f"on {controller} -> {out_csv_path}"
    )
    return thread, stop_event


def stop_ble_density_poller(poller):
    if poller is None:
        return
    thread, stop_event = poller
    stop_event.set()
    thread.join(timeout=5)


# =============================================================================
# AFH / channel-map polling -- optional, best-effort. Needs `hcitool` to be
# able to send raw HCI commands (LE Read Channel Map), which requires root
# or CAP_NET_ADMIN/CAP_NET_RAW on the hcitool binary (confirmed by direct
# testing: `hcitool cmd` fails with "Operation not permitted" for a normal
# user, unlike btmon which apparently already has the capability set). If
# that grant isn't in place, this silently produces no data -- same
# graceful-degradation contract as the BLE sniffer and QBSS poll. To enable
# it: `sudo setcap cap_net_raw,cap_net_admin+eip $(which hcitool)` once.
# =============================================================================


def _btmon_watch_for_handle(
    controller, mac_address, handle_holder, stop_event, proc_holder=None
):
    """Watch a live, separate btmon monitor for this device's own LE
    Connection Complete event, and keep handle_holder[0] updated with its
    handle -- updated (not just set-once) on every successful match, so a
    mid-run reconnect (e.g. the driver's own startup connect/disconnect/
    reconnect sequence) doesn't leave a stale handle behind.

    Replaces an earlier `hcitool con`-based handle lookup: confirmed via
    real captures (2026-09-23) that it silently failed every single time --
    zero LE Read Channel Map commands ever appeared in any capture, meaning
    the handle was never found (hcitool con not listing the LE link, a
    permission issue, or both -- inconclusive from the symptom alone). The
    handle is already right there in the connection-complete event btmon
    itself reports, using the exact same regexes parse_btmon_text already
    trusts for this, so there's no separate lookup to fail.

    HCI monitor sockets support multiple simultaneous listeners, so running
    this alongside the main capture-writing btmon instance (which writes to
    a file, not stdout) doesn't interfere with it.

    IMPORTANT: btmon fully buffers its stdout (thousands of bytes at a
    time, only flushing at exit) unless stdout is a real terminal --
    confirmed by direct testing (2026-09-23): a plain subprocess.PIPE (and
    even `stdbuf -oL`, which fails to override it) sat completely silent
    for the whole run, while a pseudo-terminal produced immediate,
    line-by-line output. So this allocates a real pty for btmon's stdout,
    with color explicitly disabled (--color=never) since a pty also
    enables ANSI color codes, which would otherwise get interleaved into
    the exact text this regex-matches against (confirmed via the same
    testing) -- and --no-pager, since a pty could otherwise make btmon
    think it should page long output through `less`, which would hang
    this reader waiting for a keypress that never comes.

    proc_holder, if given, is a single-element list this sets to the btmon
    Popen object right after launch, so stop_afh_poller() can terminate it
    directly -- confirmed via real sessions (2026-09-22) that just setting
    stop_event isn't enough: this loop only checks stop_event BETWEEN
    f.readline() calls, and readline() blocks until btmon actually produces
    another line. If nothing else on hci0 happens to generate output during
    the shutdown window, the thread sits blocked past any join() timeout,
    logging a false "didn't stop cleanly" warning on every single run.
    Terminating the process directly closes its end of the pty, which makes
    the blocked readline() return immediately (EOF) instead of waiting on
    ambient traffic that may never come."""
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["btmon", "-i", controller, "--color=never", "--no-pager"],
            stdout=slave_fd,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        os.close(master_fd)
        os.close(slave_fd)
        return
    if proc_holder is not None:
        proc_holder[0] = proc
    os.close(slave_fd)
    # errors="replace": btmon's live text output can embed raw bytes from a
    # real device's payload (e.g. a manufacturer-data or name field) that
    # aren't valid UTF-8 -- confirmed live (2026-09-24, a real row3 forced-
    # channel session): the default strict decoding crashed this whole
    # thread with UnicodeDecodeError on a real capture. Everything this
    # loop actually matches on (CONN_COMPLETE_RE, HANDLE_ADDRESS_RE, event
    # names) is plain ASCII, so replacing an occasional bad byte elsewhere
    # in the line can't affect the matching -- silently losing the thread
    # (and with it every AFH/RSSI sample for the rest of the run) would be
    # far worse than a corrupted byte in text we don't parse anyway.
    f = os.fdopen(master_fd, "r", buffering=1, encoding="utf-8", errors="replace")
    expected_mac = mac_address.upper()
    try:
        while not stop_event.is_set():
            try:
                line = f.readline()
            except OSError:
                break
            if not line:
                break
            if not CONN_COMPLETE_RE.search(line):
                continue
            window_lines = [line]
            for _ in range(10):
                try:
                    nxt = f.readline()
                except OSError:
                    break
                if not nxt:
                    break
                window_lines.append(nxt)
            window = "".join(window_lines)
            status_m = STATUS_RE.search(window)
            handle_m = HANDLE_RE.search(window)
            peer_m = PEER_ADDRESS_RE.search(window)
            if (
                status_m
                and status_m.group(1) == "Success"
                and handle_m
                and peer_m
                and peer_m.group(1).upper() == expected_mac
            ):
                handle_holder[0] = int(handle_m.group(1))
    finally:
        try:
            f.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def _hcitool_read_channel_map(handle):
    """Send raw HCI LE Read Channel Map (OGF 0x08, OCF 0x0015) for handle,
    and parse the Command Complete event bytes: [num_pkts(1), opcode(2),
    status(1), conn_handle(2), channel_map(5)]. Returns (hex_map,
    enabled_count) or (None, None) on any failure (permission, no response,
    unexpected format)."""
    lo, hi = handle & 0xFF, (handle >> 8) & 0xFF
    try:
        result = subprocess.run(
            ["hcitool", "cmd", "0x08", "0x0015", f"0x{lo:02x}", f"0x{hi:02x}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    lines = result.stdout.splitlines()
    for i, line in enumerate(lines):
        if "HCI Event" in line and i + 1 < len(lines):
            byte_strs = lines[i + 1].split()
            try:
                event_bytes = [int(b, 16) for b in byte_strs]
            except ValueError:
                return None, None
            if len(event_bytes) < 11 or event_bytes[3] != 0x00:
                return None, None  # status != success, or short/unexpected response
            channel_map = event_bytes[6:11]
            enabled = sum(bin(b).count("1") for b in channel_map)
            return "".join(f"{b:02x}" for b in channel_map), enabled
    return None, None


# =============================================================================
# Forced BLE channel classification -- the Row 6 "mechanism" test. Row 2's
# duty100pct showed AFH settling to a stable, narrowed channel map (e.g. 15
# enabled channels for Corsano) and holding the link with zero unexpected
# disconnects -- this forces the OPPOSITE: no AFH escape route, so any
# degradation Row 2 didn't show can no longer be attributed to "AFH routed
# around it." Directly comparing Row 2's duty100pct (AFH free) against Row
# 6's forced-channel + max-load condition (AFH constrained to exactly the
# channels WiFi channel 6 occupies) is what actually quantifies how much
# AFH is protecting the link, rather than assuming it.
#
# BLE's 37 data channels (indices 0-36, distinct from the 3 fixed
# advertising channels) sit at 2404 + 2*i MHz. WiFi channel 6 (2437 MHz,
# 20-22MHz wide) occupies roughly 2427-2447 MHz, which computes to data
# channels ~12-21. This uses channels 12-19 specifically -- narrower than
# the full computed overlap, but matching the exact range used in the
# published ANSI C63.27 BLE5 coexistence study this design was modeled on
# (doi:10.1109/MEMC.2020.9328232), for direct comparability with that
# methodology rather than an independently-chosen range.
# =============================================================================

ALL_BLE_DATA_CHANNELS = list(range(37))
FORCED_CHANNEL_RANGE = list(range(12, 20))  # 12-19 inclusive, per C63.27 study


def _encode_channel_classification(usable_channels):
    """5-byte (37-bit) little-endian Channel_Classification bitmap for
    HCI_LE_Set_Host_Channel_Classification: bit i = 1 means BLE data
    channel i (0-36) is usable, 0 means excluded. Per spec, the controller
    requires at least 2 usable channels -- not enforced here, since a
    caller passing fewer is a real config error worth a loud failure, not
    a silently "corrected" one."""
    bitmap = 0
    for ch in usable_channels:
        if not 0 <= ch <= 36:
            raise ValueError(f"BLE data channel index out of range (0-36): {ch}")
        bitmap |= 1 << ch
    return bitmap.to_bytes(5, "little")


def set_host_channel_classification(usable_channels):
    """Send HCI_LE_Set_Host_Channel_Classification (OGF 0x08, OCF 0x0014)
    to restrict which BLE data channels the controller's AFH may use to
    exactly usable_channels -- forces co-channel operation with whatever
    WiFi is occupying the excluded channels, instead of letting AFH route
    around it. Reuses the same hcitool raw-HCI-command path (and the same
    cap_net_raw/cap_net_admin capability) already validated for LE Read
    Channel Map. Returns True only on a confirmed Success status from the
    controller -- never raises, so a permission/hardware failure here
    degrades to "condition not actually applied" rather than crashing the
    run; callers should treat a False return as reason to abort the
    attempt, since silently NOT forcing the channel map would invalidate
    the whole point of this condition."""
    classification = _encode_channel_classification(usable_channels)
    hex_bytes = [f"0x{b:02x}" for b in classification]
    try:
        result = subprocess.run(
            ["hcitool", "cmd", "0x08", "0x0014", *hex_bytes],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    lines = result.stdout.splitlines()
    for i, line in enumerate(lines):
        if "HCI Event" in line and i + 1 < len(lines):
            byte_strs = lines[i + 1].split()
            try:
                event_bytes = [int(b, 16) for b in byte_strs]
            except ValueError:
                return False
            # [num_hci_cmd_pkts(1), opcode_lo, opcode_hi, status(1)]
            return len(event_bytes) >= 4 and event_bytes[3] == 0x00
    return False


def reset_host_channel_classification():
    """Restore normal, unrestricted AFH (all 37 data channels usable) --
    ALWAYS call this before moving on to another test, or a forced, narrow
    channel map would silently leak into unrelated runs on the same
    controller. Returns True on confirmed success, same as
    set_host_channel_classification."""
    return set_host_channel_classification(ALL_BLE_DATA_CHANNELS)


def _hcitool_read_rssi(handle):
    """Send raw HCI Read_RSSI (OGF 0x05 Status Parameters, OCF 0x0005) for
    the connection handle, and parse the Command Complete event bytes:
    [num_pkts(1), opcode(2), status(1), conn_handle(2), rssi(1, signed)].
    Returns None on any failure (permission, no response, unexpected
    format, or non-success status).

    Originally implemented as `hcitool rssi <mac_address>`, which looks up
    the connection by MAC via HCIGETCONNINFO -- confirmed broken for LE
    connections on this system (live test, 2026-09-23): with a real,
    active LE connection visible in `hcitool con`, `hcitool rssi <mac>`
    still failed with "Get connection info failed: No such file or
    directory" every time, which is why rssi_dbm was blank in every real
    AFH CSV despite channel_map_hex populating correctly right next to it
    -- the channel-map read already went by HANDLE (_hcitool_read_channel_
    map), never through the broken by-MAC path. Switching RSSI to the same
    by-handle raw-HCI approach fixed it: a live test against a real Polar
    H10 connection returned status=0x00, handle matching, rssi=0xCB (-53
    dBm, a plausible real value) on the first try.
    """
    lo, hi = handle & 0xFF, (handle >> 8) & 0xFF
    try:
        result = subprocess.run(
            ["hcitool", "cmd", "0x05", "0x0005", f"0x{lo:02x}", f"0x{hi:02x}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.splitlines()
    for i, line in enumerate(lines):
        if "HCI Event" in line and i + 1 < len(lines):
            byte_strs = lines[i + 1].split()
            try:
                event_bytes = [int(b, 16) for b in byte_strs]
            except ValueError:
                return None
            if len(event_bytes) < 7 or event_bytes[3] != 0x00:
                return None  # status != success, or short/unexpected response
            rssi_byte = event_bytes[6]
            return rssi_byte - 256 if rssi_byte >= 128 else rssi_byte  # signed 8-bit
    return None


def _afh_poll_loop(
    mac_address, out_csv_path, poll_interval_s, stop_event, handle_holder
):
    warned = False
    start = time.time()
    with open(out_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["timestamp_s", "enabled_channel_count", "channel_map_hex", "rssi_dbm"]
        )
        f.flush()
        while not stop_event.wait(poll_interval_s):
            handle = handle_holder[0]
            if handle is None:
                if not warned:
                    log.warning(
                        f"  -> AFH poller: haven't seen a Connection Complete "
                        f"event for {mac_address} on the live btmon watcher "
                        f"yet -- will keep trying silently"
                    )
                    warned = True
                continue
            channel_map_hex, enabled_count = _hcitool_read_channel_map(handle)
            rssi = _hcitool_read_rssi(handle)
            writer.writerow(
                [round(time.time() - start, 1), enabled_count, channel_map_hex, rssi]
            )
            f.flush()


def start_afh_poller(controller, mac_address, out_csv_path, poll_interval_s=15):
    """Start two background threads: one watching a live btmon monitor for
    this device's connection handle (see _btmon_watch_for_handle), and one
    polling its AFH channel map and connection RSSI every poll_interval_s
    once that handle is known, writing rows to out_csv_path. Returns
    (watcher_thread, poll_thread, stop_event, proc_holder) to pass to
    stop_afh_poller(), or None if mac_address isn't given (nothing to
    poll)."""
    if not mac_address:
        return None
    handle_holder = [None]
    proc_holder = [None]
    stop_event = threading.Event()
    watcher_thread = threading.Thread(
        target=_btmon_watch_for_handle,
        args=(controller, mac_address, handle_holder, stop_event, proc_holder),
        daemon=True,
    )
    watcher_thread.start()
    poll_thread = threading.Thread(
        target=_afh_poll_loop,
        args=(mac_address, out_csv_path, poll_interval_s, stop_event, handle_holder),
        daemon=True,
    )
    poll_thread.start()
    log.info(
        f"  -> AFH/RSSI poller running (every {poll_interval_s}s) -> {out_csv_path}"
    )
    return watcher_thread, poll_thread, stop_event, proc_holder


def stop_afh_poller(poller):
    if poller is None:
        return
    watcher_thread, poll_thread, stop_event, proc_holder = poller
    stop_event.set()
    # Terminate the watcher's btmon process directly rather than just
    # relying on it noticing stop_event -- it can be blocked in a readline()
    # that won't return until btmon produces another line, which may not
    # happen before any join() timeout if hci0 is otherwise quiet.
    proc = proc_holder[0]
    if proc is not None:
        proc.terminate()
    watcher_thread.join(timeout=5)
    poll_thread.join(timeout=5)
    if watcher_thread.is_alive() or poll_thread.is_alive():
        log.warning("     AFH poller thread(s) didn't stop cleanly within 5s")


# Note: the AFH preflight check used to live here as a standalone
# _check_afh_readable() helper, but that shape made it too easy to call
# with the watcher started AFTER the connection already existed (the
# original bug) -- the check is now inlined directly in check_devices_ready
# below, where the watcher-before-connect ordering is enforced structurally.


# =============================================================================
# Device driver control
# =============================================================================


class DriverExitedError(Exception):
    """Raised when a device driver process exits on its own during a run."""

    def __init__(self, launch_cmd, returncode):
        self.launch_cmd = launch_cmd
        self.returncode = returncode
        super().__init__(
            f"driver process exited early (returncode={returncode}): {launch_cmd}"
        )


class ChannelClassificationError(Exception):
    """Raised when forcing the BLE channel map (Row 6's mechanism test)
    fails -- treated as a full run failure, not a degrade-gracefully case
    like QBSS/AFH-read, because silently NOT applying the forced channel
    map would invalidate the whole point of the condition: the run would
    look like a normal AFH-free test while being labeled as the
    channel-restricted one."""


def start_driver(launch_cmd):
    log.info(f"  -> launching driver: {launch_cmd}")
    proc = subprocess.Popen(
        shlex.split(launch_cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    log.info(f"     driver running (pid {proc.pid})")
    return proc


def stop_driver(proc):
    log.info(f"  -> stopping driver (pid {proc.pid})")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=8)
    except ProcessLookupError:
        pass  # already exited on its own
    except subprocess.TimeoutExpired:
        log.warning(f"     driver (pid {proc.pid}) didn't exit cleanly, killing")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def disconnect_ble_device(mac_address, timeout_s=5):
    """Explicitly tell BlueZ to disconnect this device once we're done with
    it. Killing our driver process only stops OUR process -- BlueZ still
    treats a bonded, trusted device as eligible for background auto-
    reconnect, and will silently reconnect to it the moment it's back in
    range/advertising. Confirmed via real captures: a "finished" device
    reconnected in the background and stayed connected through an entire
    subsequent test on a different device, causing that other device to
    drop roughly every 45s from BLE-BLE contention on the single shared
    radio -- not RF interference, not a device firmware issue."""
    if not mac_address:
        return
    try:
        result = subprocess.run(
            ["bluetoothctl", "disconnect", mac_address],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        log.warning("  -> bluetoothctl not found, skipping explicit BLE disconnect")
        return
    except subprocess.TimeoutExpired:
        log.warning(f"  -> `bluetoothctl disconnect {mac_address}` timed out")
        return
    if result.returncode == 0:
        log.info(
            f"  -> disconnected {mac_address} from BlueZ "
            f"(preventing background auto-reconnect)"
        )
    else:
        log.warning(
            f"  -> `bluetoothctl disconnect {mac_address}` returned "
            f"{result.returncode}: {(result.stdout or result.stderr).strip()}"
        )


def start_pairing_agent():
    """Register a non-interactive BlueZ pairing agent (capability
    NoInputNoOutput) as the system default for the life of this process, so
    pairing with a known chamber sensor doesn't wait on a human clicking
    "Allow" in the desktop's own Bluetooth agent (e.g. GNOME's) -- a device
    with no input/output capability is defined by the BLE spec to pair via
    "Just Works" with no confirmation needed, so this is a legitimate
    capability declaration, not a security bypass, for devices we already
    trust in a controlled chamber session. Returns the Popen handle (or None
    if bluetoothctl isn't available) -- pass it to stop_pairing_agent() when
    the session ends so the desktop's own agent can take back over."""
    try:
        proc = subprocess.Popen(
            ["bluetoothctl", "--agent", "NoInputNoOutput"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        log.warning(
            "  -> bluetoothctl not found -- pairing prompts will need manual "
            "approval"
        )
        return None
    log.info(
        "  -> registered a NoInputNoOutput pairing agent (auto-approves "
        "pairing with chamber sensors, no manual 'Allow' needed)"
    )
    return proc


def stop_pairing_agent(proc):
    """Release the pairing agent started by start_pairing_agent(), so the
    desktop's own Bluetooth agent resumes handling pairing prompts normally
    once the session ends."""
    if proc is None:
        return
    try:
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.wait(timeout=5)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.info("  -> released the pairing agent")


def _render_progress_bar(elapsed, duration_s, suffix="", width=30):
    frac = min(elapsed / duration_s, 1.0) if duration_s else 1.0
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    line = f"  [{bar}] {elapsed:.0f}/{duration_s:.0f}s {suffix}"
    sys.stdout.write("\r" + line.ljust(78))
    sys.stdout.flush()


def _clear_progress_bar():
    sys.stdout.write("\n")
    sys.stdout.flush()


def sleep_and_watch_driver(
    duration_s, driver_proc, launch_cmd, poll_interval=1.0, heartbeat_s=30.0
):
    """Sleep for duration_s, polling driver_proc so a crash/exit is caught
    quickly instead of only surfacing once the full duration has elapsed.
    On an interactive terminal, shows a live in-place progress bar instead of
    spamming the console with a fresh line every heartbeat_s; the same
    periodic detail is still written to the session log file (at DEBUG level,
    so it doesn't also flood the console) so nothing is lost from the audit
    trail on a long run like the gym visit's ~1hr captures."""
    elapsed = 0.0
    next_heartbeat = heartbeat_s
    show_bar = sys.stdout.isatty()
    try:
        while elapsed < duration_s:
            step = min(poll_interval, duration_s - elapsed)
            time.sleep(step)
            elapsed += step
            returncode = driver_proc.poll()
            if returncode is not None:
                raise DriverExitedError(launch_cmd, returncode)
            if show_bar:
                _render_progress_bar(elapsed, duration_s, f"(pid {driver_proc.pid})")
            if next_heartbeat <= elapsed < duration_s:
                log.debug(
                    f"     ...{elapsed:.0f}s / {duration_s:.0f}s elapsed, "
                    f"driver still running (pid {driver_proc.pid})"
                )
                next_heartbeat += heartbeat_s
    finally:
        if show_bar:
            _clear_progress_bar()


def start_driver_with_retry(launch_cmd, grace_s=5.0, max_attempts=3, retry_delay_s=3.0):
    """Launch the driver and confirm it survives an initial grace period
    before trusting it. Many failures seen in practice are a flaky initial
    BLE connection/pairing attempt (driver exits cleanly, returncode 0,
    within the first second or two) rather than a real hardware problem, so
    retry the launch a few times before giving up. Returns the live driver
    process, or raises DriverExitedError after the final failed attempt."""
    for attempt in range(1, max_attempts + 1):
        driver_proc = start_driver(launch_cmd)
        try:
            sleep_and_watch_driver(
                grace_s,
                driver_proc,
                launch_cmd,
                poll_interval=1.0,
                heartbeat_s=grace_s + 1,
            )
            return driver_proc
        except DriverExitedError as e:
            stop_driver(driver_proc)
            if attempt < max_attempts:
                log.warning(
                    f"  -> driver launch attempt {attempt}/{max_attempts} failed "
                    f"({e}), retrying in {retry_delay_s}s..."
                )
                time.sleep(retry_delay_s)
            else:
                log.error(
                    f"  -> driver launch failed after {max_attempts} attempts: {e}"
                )
                raise


class DeviceCheckError(Exception):
    """Raised when one or more devices fail the pre-flight connectivity check."""


class StrayProcessError(Exception):
    """Raised when a device driver process is already running before this
    session starts one of its own for the same device."""


def kill_stray_driver_processes(cfg):
    """Preflight: find and kill any configured device's driver that's
    ALREADY running before this session starts one of its own -- a stray,
    already-running driver contending for the same physical BLE connection
    can silently corrupt results (a real, practical risk: identified while
    investigating an otherwise-unexplained progressively-worsening pattern
    across reps during live testing, 2026-09-23 -- that specific case was
    ruled out, but the risk itself is real regardless).

    Matches on each device's own launch_cmd appearing (as a substring/
    regex) in another process's command line, via `pgrep -af` -- pgrep
    excludes its own PID from results by default, so this doesn't
    self-match. Matching on the full launch_cmd rather than just the MAC
    address matters: an earlier version matched on MAC address alone and
    false-positived on this session's OWN legitimate BLE sniffer process
    (nrfutil-ble-sniffer ... --follow <mac> ...), which also carries the
    device's MAC in its own args for an unrelated reason.

    Kills automatically (SIGTERM, then SIGKILL if still alive after a
    short wait) rather than just warning -- anything this matches is
    necessarily debris from an earlier, uncleanly-ended run (this check
    runs before this session's own driver processes exist, so it can never
    match one of THIS session's own), never something a live session
    depends on. Also explicitly disconnects the device from BlueZ
    afterward, same as the end of every normal run -- killing the process
    alone doesn't release a bonded device's background connection. Raises
    StrayProcessError only if a process refuses to die even after
    SIGKILL -- that's a real, unusual failure worth aborting for, not
    something to silently paper over."""
    for device in cfg["devices"]:
        launch_cmd = device.get("launch_cmd")
        if not launch_cmd:
            continue
        # Match on "--mac-address-or-name <MAC> ..." rather than the full
        # launch_cmd -- confirmed via real orphaned processes (2026-09-23)
        # that `ros2 run <pkg> <exe> ...` spawns the actual driver as a
        # CHILD process whose own argv drops the "ros2 run <pkg>" prefix
        # entirely (it's just ".../<exe> --mac-address-or-name ..."), so
        # matching the full launch_cmd string only caught the wrapper,
        # leaving the real driver process (the one actually holding the
        # BLE connection) alive and unkilled. The "--mac-address-or-name
        # <MAC>" suffix is present in both the wrapper's and the child's
        # argv, and -- unlike the MAC address alone -- doesn't collide
        # with the BLE sniffer's unrelated "--follow <MAC> random" flag.
        pattern = launch_cmd
        marker = "--mac-address-or-name"
        if marker in launch_cmd:
            pattern = launch_cmd[launch_cmd.index(marker) :]
        try:
            result = subprocess.run(
                ["pgrep", "-af", "--", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        matches = [line for line in result.stdout.splitlines() if line.strip()]
        if not matches:
            continue
        log.warning(
            f"  -> found {len(matches)} stray {device['name']} driver "
            f"process(es) already running -- killing before this session "
            f"starts its own (first: {matches[0]})"
        )
        pids = [int(line.split(None, 1)[0]) for line in matches]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(2)

        def _alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        still_alive = [pid for pid in pids if _alive(pid)]
        for pid in still_alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if still_alive:
            time.sleep(1)
            truly_stuck = [pid for pid in still_alive if _alive(pid)]
            if truly_stuck:
                raise StrayProcessError(
                    f"could not kill stray {device['name']} process(es) "
                    f"{truly_stuck} even after SIGKILL -- check manually "
                    f"before continuing"
                )
        disconnect_ble_device(device.get("mac_address"))
        log.info(f"  -> stray {device['name']} process(es) cleaned up")


def check_devices_ready(cfg, probe_duration_s=8, check_afh=True):
    """Pre-flight sanity check: briefly launch each configured device's driver
    (retrying a flaky initial connection) and confirm it stays up rather than
    exiting immediately (e.g. because the sensor is powered off, out of range,
    or not paired). Mirrors the field guide's "arrival sanity check" step, run
    automatically before any test.

    check_afh=True also attempts one real LE Read Channel Map on the first
    device's probe connection, and ABORTS (not just warns) if it fails --
    the channel map is now central to explaining the session's results (is
    AFH routing around WiFi-busy channels, or not?), so a silent empty-CSV
    degradation here would be far more misleading than for the other
    best-effort metrics (sniffer, QBSS), which don't carry the same
    explanatory weight."""
    devices = cfg["devices"]
    device_names = [d["name"] for d in devices]
    log.info(
        f"=== Sanity check: verifying {len(devices)} device(s) are up and "
        f"connected -- {device_names} ({probe_duration_s}s probe each) ==="
    )
    failures = []
    afh_verified = not check_afh
    for device in devices:
        log.info(f"  -> checking {device['name']} ({device['launch_cmd']})")
        afh_watcher = None
        if not afh_verified and check_afh and device.get("mac_address"):
            # Start watching BEFORE connecting: the LE Connection Complete
            # event only fires once, right as the connection is made -- a
            # watcher started AFTER the driver already connected (the
            # original bug here) silently finds nothing, confirmed by
            # direct testing (2026-09-23), since the one-time event has
            # already passed by the time it starts listening.
            handle_holder = [None]
            afh_proc_holder = [None]
            afh_stop_event = threading.Event()
            afh_thread = threading.Thread(
                target=_btmon_watch_for_handle,
                args=(
                    cfg["btmon_controller"],
                    device["mac_address"],
                    handle_holder,
                    afh_stop_event,
                    afh_proc_holder,
                ),
                daemon=True,
            )
            afh_thread.start()
            time.sleep(0.3)  # let btmon actually attach before connecting
            afh_watcher = (afh_thread, afh_stop_event, handle_holder, afh_proc_holder)

        def _stop_afh_watcher(watcher):
            # Terminate the underlying btmon process directly rather than
            # just setting stop_event -- it can be blocked in a readline()
            # that won't return until btmon produces another line, which
            # may never happen before any join() timeout on an otherwise
            # quiet hci0 (confirmed via real sessions, 2026-09-22).
            thread, stop_evt, _handle_holder, proc_holder = watcher
            stop_evt.set()
            if proc_holder[0] is not None:
                proc_holder[0].terminate()
            thread.join(timeout=5)

        try:
            driver_proc = start_driver_with_retry(
                device["launch_cmd"], grace_s=probe_duration_s
            )
        except DriverExitedError as e:
            log.error(f"     {device['name']}: FAILED -- {e}")
            failures.append(device["name"])
            if afh_watcher:
                _stop_afh_watcher(afh_watcher)
            continue
        log.info(f"     {device['name']}: OK, streaming normally")
        if afh_watcher:
            _, _, handle_holder, _ = afh_watcher
            for _ in range(8):
                handle = handle_holder[0]
                if handle is not None:
                    channel_map_hex, enabled_count = _hcitool_read_channel_map(handle)
                    if channel_map_hex is not None:
                        log.info(
                            f"     AFH channel map readable ({enabled_count} "
                            f"channels enabled) -- hcitool has the permission "
                            f"it needs"
                        )
                        afh_verified = True
                        break
                time.sleep(1.0)
            _stop_afh_watcher(afh_watcher)
        stop_driver(driver_proc)
        disconnect_ble_device(device.get("mac_address"))

    if failures:
        raise DeviceCheckError(
            f"device(s) not ready: {', '.join(failures)} -- check power, "
            f"pairing, and BLE range before continuing"
        )
    if check_afh and not afh_verified:
        raise InfrastructureCheckError(
            "could not read the LE channel map on any connected device's "
            "probe connection -- hcitool likely lacks permission to send "
            "raw HCI commands (confirmed: `hcitool cmd` needs root or "
            "CAP_NET_ADMIN/CAP_NET_RAW). Run once: `sudo setcap "
            "cap_net_raw,cap_net_admin+eip $(which hcitool)`, then retry. "
            "Without this, every run's AFH channel-map data will be silently "
            "empty -- and AFH behavior is central to explaining this "
            "session's results, so this aborts rather than just warning."
        )
    log.info("=== Sanity check passed: all devices connected ===")


class InfrastructureCheckError(Exception):
    """Raised when required infrastructure (BT controller, Pi, etc.) isn't reachable."""


def check_bt_controller_ready(controller, timeout_s=5):
    """Verify the local Bluetooth controller used for btmon capture is up,
    on both the host laptop and (implicitly) the adapter itself -- without
    this, every btmon capture in the session would fail before it starts."""
    log.info(f"  -> checking Bluetooth controller {controller}...")
    try:
        result = subprocess.run(
            ["hciconfig", controller], capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError:
        log.warning(
            f"     hciconfig not found on this host, skipping controller check "
            f"for {controller} (btmon will fail later if it's not actually up)"
        )
        return
    except subprocess.TimeoutExpired:
        raise InfrastructureCheckError(f"hciconfig {controller} timed out")

    if result.returncode != 0 or "UP RUNNING" not in result.stdout:
        raise InfrastructureCheckError(
            f"Bluetooth controller {controller} is not up on this laptop -- "
            f"run `sudo hciconfig {controller} up` and retry"
        )
    log.info(f"     {controller}: UP RUNNING")


def check_pi_reachable(iperf_cfg, timeout_s=5):
    """Verify the Raspberry Pi used for Row 2's iperf3 traffic is SSH-reachable
    and has iperf3 installed, before the run gets to that condition and stalls."""
    ssh_target = f"{iperf_cfg['pi_user']}@{iperf_cfg['pi_host']}"
    log.info(f"  -> checking Pi at {ssh_target}...")
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout_s}",
                ssh_target,
                "which iperf3",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s + 5,
        )
    except subprocess.TimeoutExpired:
        raise InfrastructureCheckError(
            f"Pi at {ssh_target} did not respond within {timeout_s}s -- "
            f"check it's powered on, on the network, and SSH keys are set up"
        )
    if result.returncode != 0:
        raise InfrastructureCheckError(
            f"Pi at {ssh_target} is unreachable or iperf3 isn't installed there "
            f"-- check SSH access and that `iperf3` is on the Pi's PATH"
        )
    log.info(f"     {ssh_target}: reachable, iperf3 found")


def measure_max_throughput(iperf_cfg, duration_s=10, timeout_s=None):
    """Run a real iperf3 throughput test from the Pi to the server through the
    actual link, to get a fresh baseline for deriving Row 2's duty-cycle
    bitrates -- instead of relying on a number hand-recorded from a past dry
    run, which can go stale as the RF environment changes day to day.

    UDP with -b 0 (send as fast as possible), matching the traffic mode
    actually used to apply the duty-cycle load (see run_iperf_background in
    chamber_session.py) -- measuring the ceiling over TCP while applying
    load over UDP would size "100%" against the wrong link characteristic
    (TCP's ceiling differs from UDP's: different congestion control,
    aggregation, ACK overhead). The reported bits_per_second is what was
    actually RECEIVED, so it already reflects any loss at saturation --
    exactly "how much can this link really deliver," not what was offered.
    -O 2 omits the first 2s from the reported average, to skip ramp-up.

    NOTE: expected_max_mbps in config.yaml was originally recorded from a
    TCP measurement -- since this now measures UDP instead, that number
    needs re-validating against a fresh UDP measurement (e.g. via
    --test-mode, which only warns instead of aborting on a mismatch) before
    trusting max_mbps_tolerance_pct's check at the next real session."""
    ssh_target = f"{iperf_cfg['pi_user']}@{iperf_cfg['pi_host']}"
    payload = iperf_cfg.get("udp_payload_bytes", 1200)
    remote_cmd = (
        f"iperf3 -c {iperf_cfg['server_ip']} -u -b 0 -l {payload} "
        f"-t {duration_s} -O 2 -J"
    )
    log.info(
        f"  -> measuring max UDP throughput via {ssh_target} ({duration_s}s iperf3 test)..."
    )
    try:
        result = subprocess.run(
            ["ssh", ssh_target, remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout_s or (duration_s + 15),
        )
    except subprocess.TimeoutExpired:
        raise InfrastructureCheckError(
            f"throughput measurement via {ssh_target} timed out"
        )
    if result.returncode != 0:
        raise InfrastructureCheckError(
            f"throughput measurement failed via {ssh_target}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        data = json.loads(result.stdout)
        end = (
            data["end"].get("sum")
            or data["end"].get("sum_received")
            or data["end"]["sum_sent"]
        )
        bps = end["bits_per_second"]
        loss_pct = end.get("lost_percent")
    except (json.JSONDecodeError, KeyError) as e:
        raise InfrastructureCheckError(
            f"could not parse iperf3 JSON output from {ssh_target}: {e}"
        )
    mbps = bps / 1_000_000
    loss_note = f", {loss_pct:.1f}% loss at saturation" if loss_pct is not None else ""
    log.info(f"     measured max UDP throughput: {mbps:.1f} Mbit/s{loss_note}")
    return mbps


def check_throughput_baseline(measured_mbps, iperf_cfg, strict=True):
    """Compare a freshly measured throughput against the expected anechoic-
    chamber baseline recorded during the pre-day dry run. This link's max
    throughput should be stable and repeatable in a controlled chamber, so a
    large deviation usually means something's actually wrong (AP on the wrong
    channel, chamber door/seal not closed, a cable fault) rather than normal
    day-to-day variance -- worth stopping for rather than silently deriving
    duty-cycle bitrates from a measurement that doesn't reflect the intended
    controlled environment.

    strict=False (test mode) downgrades a baseline mismatch to a warning
    instead of raising -- for dry runs outside the chamber, where ambient RF
    won't match expected_max_mbps by design, not because anything is wrong."""
    expected = iperf_cfg.get("expected_max_mbps")
    if expected is None:
        log.warning(
            "     no expected_max_mbps configured -- skipping baseline check "
            "(can't tell if this measurement is normal for the chamber)"
        )
        return
    tolerance_pct = iperf_cfg.get("max_mbps_tolerance_pct", 20)
    lo = expected * (1 - tolerance_pct / 100)
    hi = expected * (1 + tolerance_pct / 100)
    if not (lo <= measured_mbps <= hi):
        message = (
            f"measured throughput {measured_mbps:.1f} Mbit/s is outside the "
            f"expected anechoic-chamber baseline of {expected} Mbit/s "
            f"+/-{tolerance_pct}% ({lo:.1f}-{hi:.1f} Mbit/s) -- check AP "
            f"channel/config, chamber door/seal, and cabling before "
            f"continuing, or update expected_max_mbps if the baseline itself "
            f"has legitimately changed"
        )
        if strict:
            raise InfrastructureCheckError(message)
        log.warning(f"     {message} -- continuing anyway (test mode)")
        return
    log.info(
        f"     measured {measured_mbps:.1f} Mbit/s is within expected baseline "
        f"{expected} Mbit/s +/-{tolerance_pct}% ({lo:.1f}-{hi:.1f} Mbit/s)"
    )


def resolve_duty_bitrates(cfg, measure_duration_s=10, strict=True):
    """Return {duty: bitrate_str} for every non-zero duty level in
    row2_duty_cycle.duty_levels. If row2_duty_cycle.iperf.bitrate_by_duty is
    set in config, it's used as an explicit override (and must cover every
    non-zero level); otherwise a fresh throughput baseline is measured via
    iperf3 and each level's bitrate is derived as that percentage of it.

    strict=False (test mode) passes through to check_throughput_baseline, so
    a chamber-baseline mismatch warns instead of aborting."""
    r = cfg["row2_duty_cycle"]
    duty_levels = sorted({d for d in r["duty_levels"] if d > 0})
    if not duty_levels:
        return {}

    configured = r["iperf"].get("bitrate_by_duty")
    if configured:
        missing = [d for d in duty_levels if d not in configured]
        if missing:
            raise ValueError(
                f"row2_duty_cycle.iperf.bitrate_by_duty is set but missing "
                f"entries for duty level(s) {missing} -- add them, or remove "
                f"bitrate_by_duty entirely to auto-measure and derive instead"
            )
        log.info(f"  -> using configured bitrate_by_duty override: {configured}")
        return {d: str(configured[d]) for d in duty_levels}

    max_mbps = measure_max_throughput(r["iperf"], duration_s=measure_duration_s)
    check_throughput_baseline(max_mbps, r["iperf"], strict=strict)
    derived = {d: f"{max_mbps * d / 100:.1f}M" for d in duty_levels}
    log.info(
        f"  -> derived bitrate_by_duty from measured {max_mbps:.1f} Mbit/s max: {derived}"
    )
    return derived


# =============================================================================
# Run execution
# =============================================================================


class RunAbortedError(Exception):
    """Raised when a test fails and the session should stop -- either the
    user chose to stop when asked, or there was no one to ask (a
    non-interactive terminal) so the old automatic-abort behavior applies."""


def _attempt_run(
    cfg,
    session_dir,
    row_label,
    device_name,
    launch_cmd,
    condition,
    replicate,
    duration_s,
    session_id,
    capture_file,
    mac_address=None,
    total_replicates=None,
    extra=None,
    retry_launch=True,
):
    """Execute exactly one attempt of a run (btmon + driver + optional iperf
    + watch), write its manifest row, and return (status, error) -- the REAL
    verdict, checked immediately via evaluate_run_result() when the driver
    survives, not just whether the process stayed alive. Never raises for a
    failed attempt; do_run decides what to do about that.

    retry_launch=False (Row 2/3) launches the driver exactly once instead of
    retrying a flaky initial connection -- a failed connection attempt under
    interference is itself the KPI, not noise to retry away. See
    connection_established in the manifest row: distinguishes "never
    connected" from "connected, then dropped later"."""
    rep_label = (
        f"{replicate}/{total_replicates}" if total_replicates else str(replicate)
    )
    log.info(
        f"=== {row_label} | {device_name} | {condition} | replicate {rep_label} ==="
    )
    start_time = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"  -> start time: {start_time}, planned duration: {duration_s}s")
    log.info(f"  -> capture file: {capture_file}")

    btmon_proc = start_btmon(cfg["btmon_controller"], capture_file)
    sniffer_pcap = capture_file.with_suffix(".pcap")
    sniffer_proc = start_ble_sniffer(cfg, mac_address, sniffer_pcap)
    afh_csv = capture_file.with_name(capture_file.stem + "_afh.csv")
    afh_poller = start_afh_poller(cfg["btmon_controller"], mac_address, afh_csv)
    rosbag_proc = None
    if (cfg.get("rosbag") or {}).get("enabled"):
        rosbag_dir = capture_file.with_name(capture_file.stem + "_rosbag")
        rosbag_proc = start_rosbag_recording(rosbag_dir)

    iperf_proc = None
    driver_proc = None
    qbss_poller = None
    qbss = None
    connection_established = False
    forced_channels_applied = False
    try:
        if not (extra and extra.get("forced_channels")):
            # This run must be AFH-free. The controller's channel
            # classification is persistent hardware state -- NOT tied to
            # any one process -- so a run that itself never calls
            # set_host_channel_classification has no way to know whether a
            # PREVIOUS run (or an interrupted/crashed session) already left
            # one active. Confirmed as a real incident (2026-09-24): a Row 3
            # run was interrupted when a session was stopped and resumed,
            # and its forced classification leaked into two subsequent
            # "AFH-free" Row 2 runs, both showing exactly FORCED_CHANNEL_
            # RANGE's bitmap for their entire duration. Resetting is cheap
            # and idempotent (a no-op if already unrestricted), so this
            # runs unconditionally rather than trying to first detect
            # whether a reset is actually needed -- there's no HCI command
            # to read back "what classification is currently set" without
            # an active connection to query its negotiated map against, so
            # a blind, confirmed reset is the only real verification
            # available before a connection even exists. See do_parse's
            # check_channel_map_contamination for the detection-side
            # backstop, in case this guard is ever bypassed.
            if not reset_host_channel_classification():
                raise ChannelClassificationError(
                    "could not confirm the channel classification was reset "
                    "before this AFH-free run -- refusing to proceed, since "
                    "starting without confirming a clean (unrestricted) map "
                    "risks silently repeating a real past contamination "
                    "incident rather than producing valid AFH-free data"
                )

        driver_proc = start_driver_with_retry(
            launch_cmd, max_attempts=3 if retry_launch else 1
        )
        connection_established = True

        if extra and extra.get("forced_channels"):
            forced = extra["forced_channels"]
            log.info(
                f"  -> forcing BLE channel classification to {forced} "
                f"(no AFH escape route)"
            )
            if not set_host_channel_classification(forced):
                raise ChannelClassificationError(
                    f"failed to force channel classification to {forced} -- "
                    f"hcitool likely lacks permission (see the AFH preflight "
                    f"check's setcap guidance) or the controller rejected it"
                )
            forced_channels_applied = True

        settle = 0
        if extra and extra.get("start_iperf"):
            settle = extra["iperf_cfg"].get("settle_delay_s", 0)
            if settle:
                log.info(
                    f"  -> waiting {settle}s for BLE connection to settle before traffic"
                )
                sleep_and_watch_driver(settle, driver_proc, launch_cmd)
            iperf_proc = extra["start_iperf_fn"](
                extra["iperf_cfg"], duration_s - settle
            )
            # Start QBSS sampling only once traffic is actually flowing, and
            # stop it as soon as the watched duration ends (below) -- an
            # earlier version sampled once, after teardown, which measured
            # an idle channel and defeated the whole point.
            if extra.get("qbss_iface") and extra.get("qbss_bssid"):
                qbss_poller = start_qbss_poller(
                    extra["qbss_iface"], extra["qbss_bssid"]
                )

        remaining = duration_s - settle
        log.info(f"  -> running for {remaining}s more ({duration_s}s total)...")
        sleep_and_watch_driver(remaining, driver_proc, launch_cmd)
        # Stop QBSS sampling as soon as the watched duration ends (traffic
        # is still flowing or just finished), not after the driver/btmon/
        # sniffer teardown below -- by then the channel is idle again.
        qbss = stop_qbss_poller(qbss_poller)
        qbss_poller = None  # already stopped; don't stop it again in a later path
    except (DriverExitedError, ChannelClassificationError) as e:
        log.error(f"  !! run aborted: {e}")
        if iperf_proc:
            iperf_proc.terminate()
        if driver_proc is not None:
            stop_driver(driver_proc)
        stop_btmon(btmon_proc)
        stop_ble_sniffer(sniffer_proc)
        stop_afh_poller(afh_poller)
        stop_rosbag_recording(rosbag_proc)
        stop_qbss_poller(qbss_poller)
        if forced_channels_applied:
            reset_host_channel_classification()
        disconnect_ble_device(mac_address)
        append_manifest(
            session_dir,
            {
                "session_id": session_id,
                "row": row_label,
                "device": device_name,
                "condition": condition,
                "replicate": replicate,
                "capture_file": capture_file.name,
                "start_time": start_time,
                "duration_s": duration_s,
                "status": "FAILED",
                "error": str(e),
                "mac_address": mac_address or "",
                "sniffer_pcap_file": sniffer_pcap.name,
                "connection_established": connection_established,
                "retry_on_fail": retry_launch,
            },
        )
        log.error(f"  !! RESULT: FAILED -- {e}")
        return "FAILED", str(e)

    induced_load_mbps = None
    udp_stats = None
    if iperf_proc:
        try:
            stdout_text, _ = iperf_proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            iperf_proc.kill()
            stdout_text, _ = iperf_proc.communicate()
        parse_fn = extra.get("parse_iperf_fn") if extra else None
        if parse_fn:
            induced_load_mbps = parse_fn(stdout_text)
            target_mbps = extra.get("target_mbps") if extra else None
            if induced_load_mbps is None:
                log.warning(
                    "  -> could not confirm induced load -- iperf3 output was "
                    "unparseable (ssh/iperf3 may have failed silently; the "
                    "interference for this run may not have been applied)"
                )
            elif target_mbps:
                pct = 100 * induced_load_mbps / target_mbps
                log.info(
                    f"  -> induced load (iperf3 actual): {induced_load_mbps:.1f} "
                    f"Mbit/s ({pct:.0f}% of {target_mbps:.1f} Mbit/s target)"
                )
                if induced_load_mbps < 0.5 * target_mbps:
                    log.warning(
                        f"  -> induced load only reached {pct:.0f}% of target -- "
                        f"this run's interference condition may not have been "
                        f"applied as intended"
                    )
            else:
                log.info(
                    f"  -> induced load (iperf3 actual): {induced_load_mbps:.1f} Mbit/s"
                )
        udp_parse_fn = extra.get("parse_udp_stats_fn") if extra else None
        if udp_parse_fn:
            udp_stats = udp_parse_fn(stdout_text)
            if udp_stats:
                log.info(
                    f"  -> iperf3 UDP: {udp_stats['packet_loss_pct']:.2f}% packet "
                    f"loss, jitter {udp_stats['iperf_jitter_ms']:.2f}ms (Pi-side, "
                    f"transport-level -- separate from BLE's own metrics)"
                )
    stop_driver(driver_proc)
    stop_btmon(btmon_proc)
    stop_ble_sniffer(sniffer_proc)
    stop_afh_poller(afh_poller)
    stop_rosbag_recording(rosbag_proc)
    if forced_channels_applied:
        if not reset_host_channel_classification():
            log.warning(
                "  -> could not confirm the forced BLE channel classification "
                "was reset -- check before trusting the NEXT run's results"
            )
    disconnect_ble_device(mac_address)

    if qbss:
        log.info(
            f"  -> AP channel utilization (QBSS, best-effort, sampled "
            f"during traffic): {qbss['qbss_channel_utilization_pct']}%"
        )

    append_manifest(
        session_dir,
        {
            "session_id": session_id,
            "row": row_label,
            "device": device_name,
            "condition": condition,
            "replicate": replicate,
            "capture_file": capture_file.name,
            "start_time": start_time,
            "duration_s": duration_s,
            "status": "PASSED",
            "error": "",
            "induced_load_mbps": (
                "" if induced_load_mbps is None else round(induced_load_mbps, 2)
            ),
            "packet_loss_pct": (udp_stats or {}).get("packet_loss_pct", ""),
            "iperf_jitter_ms": (udp_stats or {}).get("iperf_jitter_ms", ""),
            "qbss_channel_utilization_pct": (qbss or {}).get(
                "qbss_channel_utilization_pct", ""
            ),
            "mac_address": mac_address or "",
            "sniffer_pcap_file": sniffer_pcap.name,
            "connection_established": connection_established,
            "retry_on_fail": retry_launch,
        },
    )
    log.info(f"  -> saved {capture_file.name}")
    log.info(
        f"=== test finished: {row_label} | {device_name} | {condition} | "
        f"rep{replicate} -- verifying BLE link ==="
    )

    # The driver surviving doesn't mean the BLE link stayed up -- check that
    # immediately (same criteria do_parse uses at session end) so a real
    # failure surfaces now, while the operator is still at the chamber,
    # instead of only showing up when results.csv is parsed much later.
    if not capture_file.exists():
        return "FAILED", "capture file missing after run"
    parsed = parse_capture_file(capture_file, mac_address=mac_address)
    log.info(
        f"  -> metrics: interval={parsed['initial_interval_ms']}ms, "
        f"mtu={parsed['mtu']}, connected={parsed['connected_duration_s']}s, "
        f"disconnects={parsed['disconnect_count']} "
        f"({parsed['unexpected_disconnect_count']} unexpected), "
        f"failed_attempts={parsed['num_failed_connection_attempts']}"
    )
    log.info(
        f"     RX inter-arrival jitter (n={parsed['rx_sample_count']}): "
        f"p50={parsed['jitter_p50_ms']}ms p95={parsed['jitter_p95_ms']}ms "
        f"p99={parsed['jitter_p99_ms']}ms p99.9={parsed['jitter_p999_ms']}ms"
    )
    if parsed["acc_pmd_frame_count"]:
        log.info(
            f"     Polar ACC sample completeness: "
            f"{parsed['acc_samples_received']} received, "
            f"{parsed['acc_samples_lost']} lost "
            f"({parsed['acc_completeness_pct']}%, from the sensor's own "
            f"embedded timestamps -- not just RX arrival gaps)"
        )
    if parsed["hr_rr_total_s"] is not None:
        log.info(
            f"     Polar HR/RR completeness (approximate): "
            f"{parsed['hr_rr_total_s']}s of RR-intervals received vs. "
            f"{parsed['connected_duration_s']}s connected "
            f"({parsed['hr_rr_completeness_pct']}%)"
        )
    if parsed["corsano_acc_packet_count"] is not None:
        log.info(
            f"     Corsano ACC completeness: "
            f"{parsed['corsano_acc_packet_count']} chunks, "
            f"{parsed['corsano_acc_samples_lost']} lost "
            f"({parsed['corsano_acc_completeness_pct']}%)"
        )
    if parsed["corsano_bioz_completeness_pct"] is not None:
        log.info(
            f"     Corsano BioZ completeness: "
            f"{parsed['corsano_bioz_bytes_downloaded']}/"
            f"{parsed['corsano_bioz_file_growth_bytes']} bytes "
            f"({parsed['corsano_bioz_completeness_pct']}%)"
        )
    if induced_load_mbps is not None:
        log.info(f"     induced load (iperf3 actual): {induced_load_mbps:.1f} Mbit/s")
    sniffer_metrics = parse_sniffer_pcap(sniffer_pcap, mac_address)
    if sniffer_metrics["sniffer_adv_packet_count"] is not None:
        log.info(
            f"     over-the-air (adv channels): "
            f"n={sniffer_metrics['sniffer_adv_packet_count']}, "
            f"CRC errors={sniffer_metrics['sniffer_adv_crc_error_count']} "
            f"({sniffer_metrics['sniffer_adv_crc_error_rate']:.1%}), "
            f"RSSI avg={sniffer_metrics['sniffer_adv_rssi_avg_dbm']}dBm "
            f"[{sniffer_metrics['sniffer_adv_rssi_min_dbm']}, "
            f"{sniffer_metrics['sniffer_adv_rssi_max_dbm']}]"
        )
    real_status, real_error, _ = evaluate_run_result(
        parsed, row_label, condition, duration_s
    )
    if real_status != "PASSED":
        log.error(f"  !! RESULT: FAILED -- {real_error}")
        return "FAILED", real_error
    log.info("  -> RESULT: PASSED")
    return "PASSED", ""


def do_run(
    cfg,
    session_dir,
    row_label,
    device_name,
    launch_cmd,
    condition,
    replicate,
    duration_s,
    session_id,
    mac_address=None,
    total_replicates=None,
    extra=None,
    retry_on_fail=True,
):
    """Run one test, retrying automatically-unresolvable failures by asking
    the operator to retry or stop -- on a real terminal. Non-interactively
    (no one to ask), a failure aborts the session immediately, same as
    before this per-test check existed.

    retry_on_fail=False (Row 2/3, where interference-induced instability IS
    the thing being measured) launches exactly once, never prompts, and
    never raises RunAbortedError on failure -- the failed attempt is logged
    and recorded as-is, and the session continues to the next replicate/
    condition. Retrying-until-PASS on these rows would silently discard the
    exact data point interference testing exists to capture."""
    label = f"{row_label} | {device_name} | {condition} | rep{replicate}"
    safe_condition = condition.replace("%", "pct")

    if not retry_on_fail:
        capture_file = (
            session_dir
            / f"{row_label}_{device_name}_{safe_condition}_rep{replicate}.btsnoop"
        )
        status, error = _attempt_run(
            cfg,
            session_dir,
            row_label,
            device_name,
            launch_cmd,
            condition,
            replicate,
            duration_s,
            session_id,
            capture_file,
            mac_address=mac_address,
            total_replicates=total_replicates,
            extra=extra,
            retry_launch=False,
        )
        if status != "PASSED":
            log.warning(
                f"  -> {label} FAILED (no retry -- interference-row data "
                f"point, recording as-is and continuing): {error}"
            )
        return

    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f"_attempt{attempt}"
        capture_file = (
            session_dir
            / f"{row_label}_{device_name}_{safe_condition}_rep{replicate}{suffix}.btsnoop"
        )
        status, error = _attempt_run(
            cfg,
            session_dir,
            row_label,
            device_name,
            launch_cmd,
            condition,
            replicate,
            duration_s,
            session_id,
            capture_file,
            mac_address=mac_address,
            total_replicates=total_replicates,
            extra=extra,
            retry_launch=True,
        )
        if status == "PASSED":
            return

        log.error(f"  !! TEST FAILED (attempt {attempt}): {label} -- {error}")
        if not sys.stdin.isatty():
            log.error(
                "  -> not an interactive terminal, no one to ask -- " "aborting session"
            )
            raise RunAbortedError(f"{label}: {error}")

        while True:
            answer = (
                input(
                    f"     Retry '{label}', or stop the session? [r/s] (default: r): "
                )
                .strip()
                .lower()
            )
            if answer in ("", "r", "retry"):
                attempt += 1
                log.info(f"  -> retrying {label} (attempt {attempt})...")
                break
            if answer in ("s", "stop"):
                log.error(f"  -> stopping session at user request after {label} failed")
                raise RunAbortedError(f"user stopped after {label} failed: {error}")
            print("     Please enter 'r' to retry or 's' to stop.")


# =============================================================================
# Capture parsing
# =============================================================================

# A run only counts as a real PASSED if the BLE link stayed up for at least
# this fraction of its planned duration -- the driver process staying alive
# is not sufficient (see do_parse).
MIN_CONNECTED_FRACTION = 0.9

# Conditions with no injected interference (no iperf3 traffic, no generic
# BLE peripherals), so once the sensor connects it's expected to stay
# connected for the rest of the run. Any disconnect there is a real anomaly
# worth labeling directly -- not just checked against the looser
# MIN_CONNECTED_FRACTION threshold above, which a quick disconnect/reconnect
# could still slip under. This is distinct from Row 2/3's non-zero-load
# conditions, where interference is deliberately present and some
# instability is the thing being measured.
#
# Row 1's two sub-runs (see run_test1_connection_interval) are both
# no-interference by construction. Row 2/5's 0%-duty conditions are the
# equivalent for those rows. This set was previously {("row1", "baseline")}
# -- stale after Row 1 was restructured into calibration/renegotiation_watch,
# which meant this check silently never fired for Row 1 at all.
#
# Row labels below were re-fixed 2026-09-24: this set still had the PRE-
# renumbering row labels (contention was "row3", endurance's duty0pct_long
# was under "row5") from before the chamber design settled on today's
# layout (Row 3 = forced_channel, Row 4 = endurance, Row 5 = contention,
# see config.yaml) -- meaning row5/row3's baseline conditions below had
# silently never matched anything for a while, the same class of bug as
# the Row 1 staleness above. Also added forced_channel_no_load, which
# belongs here and was simply missing: zero WiFi load is zero WiFi load
# regardless of the forced channel map, and the manuscript's own finding
# for it is "streamed for the full duration in all three runs" -- a real
# baseline, not covered by EXPECTED_INSTABILITY_ROW_CONDITIONS below.
#
# For Row 2/3/5 (retry_on_fail=False), landing in this set only adds a
# stricter data LABEL -- it can no longer trigger a retry or abort the
# session (see do_run's retry_on_fail). For Row 1 (retry_on_fail=True), it
# still drives the original retry+abort-on-failure behavior.
BASELINE_ROW_CONDITIONS = {
    ("row1", "calibration"),
    ("row1", "renegotiation_watch"),
    ("row2", "duty0pct"),
    ("row2", "duty0pct_bracket"),
    ("row3", "forced_channel_no_load"),
    ("row4", "duty0pct_long"),
    ("row5", "contention_0"),
    ("row5", "contention_0_bracket"),
    # Row 6 headroom sweep's no-load control (one small mask, no WiFi load)
    # -- same "a small mask alone shouldn't destabilize anything" reasoning
    # as forced_channel_no_load above, just at whichever k config.yaml's
    # row6_headroom_sweep.no_load_control_k picks. Only k=2 exists today;
    # add the corresponding condition name here if that config value changes.
    ("row6", "headroom_k2_no_load"),
}

# Conditions where WiFi load is deliberately combined with NO AFH escape
# route (forced_channels, see FORCED_CHANNEL_RANGE) -- the mechanism test's
# whole point, and by now (2026-09-24) a repeatedly-confirmed finding
# across sessions, not a hypothesis: the link is EXPECTED to degrade or
# collapse here (Table tab:forced in the manuscript reports these
# disconnects as the result, not as failures). Per the user's own framing:
# under these conditions "we do expect disconnects... the test should not
# fail but rather pass." See evaluate_run_result for exactly what "ran as
# expected" means here -- it's not simply "always PASS": a run that never
# engaged with the link at all (0 connected time AND 0 attempts) is still
# a real infrastructure failure needing a rerun (the manuscript excluded
# exactly this case for its own full-load replicates: "followed a failed
# host-side disconnect and no connection attempt was recorded"), not a
# valid instance of the phenomenon being measured.
#
# Deliberately NOT including Row 2's non-zero-duty conditions here, even
# though WiFi load is also present there: Row 2's established, repeatedly-
# confirmed finding (until a 2026-09-24 session showed real Corsano
# disconnects at duty75/100pct, still unexplained) is the OPPOSITE --
# AFH-free links are expected to stay up even at 100% load. Reclassifying
# Row 2 here would mask that anomaly as "expected" instead of flagging it
# for investigation, which is exactly backwards.
EXPECTED_INSTABILITY_ROW_CONDITIONS = {
    ("row3", "forced_channel_duty25pct"),
    ("row3", "forced_channel_duty50pct"),
    ("row3", "forced_channel_duty75pct"),
    ("row3", "forced_channel_max_load"),
    # Row 6 headroom sweep's WiFi-loaded conditions (k=0/2/4/8 clean
    # channels, all at load_duty_pct% load) -- same principle as Row 3
    # above, generalized: a forced/restricted channel classification plus
    # WiFi load makes some amount of instability a valid outcome to
    # measure, not a test failure, REGARDLESS of k. This is deliberately
    # NOT narrowed to just k=0 (the one condition with an already-confirmed
    # collapse pattern): whether k=2/4/8 also degrades, or the controller
    # cleanly escapes onto the added clean channels instead, is the open
    # question this row exists to answer -- either outcome is valid data,
    # so neither should be labeled a failure. Only truly zero engagement
    # (0 connected time AND 0 attempts, see evaluate_run_result) still
    # fails, same as Row 3.
    ("row6", "headroom_k0_duty75pct"),
    ("row6", "headroom_k2_duty75pct"),
    ("row6", "headroom_k4_duty75pct"),
    ("row6", "headroom_k8_duty75pct"),
}

# Rows whose EVERY condition (including no-load ones) deliberately forces a
# host channel classification -- see FORCED_CHANNEL_RANGE / channel_masks.
# Used below to know which rows' captures should NEVER show a forced
# bitmap, so a leak can be detected instead of just prevented.
FORCED_CHANNEL_ROWS = {"row3", "row6"}


def _known_forced_channel_hex_signatures(cfg):
    """Exact 5-byte channel-map hex signatures of every host-forced
    classification this codebase ever sets: Row 3's fixed FORCED_CHANNEL_
    RANGE, plus every k in Row 6's config-driven channel_masks. Used to
    detect contamination precisely -- matching the EXACT bitmap, not just
    a similarly narrow channel COUNT, since real AFH under heavy WiFi load
    can legitimately narrow to a similar count on its own (confirmed: ~15
    channels for Corsano at 100% duty, AFH-free) without ever being
    host-forced. The exact bitmap is what a leaked host classification
    actually produces and what real adaptive narrowing essentially never
    would, since it doesn't follow an arbitrary spread-out pattern like
    Row 6's k=2/4/8 masks."""
    sigs = {_encode_channel_classification(FORCED_CHANNEL_RANGE).hex()}
    for channels in (cfg.get("channel_masks") or {}).values():
        sigs.add(_encode_channel_classification(channels).hex())
    return sigs


def check_channel_map_contamination(afh_csv_path, forced_signatures):
    """Read one run's AFH CSV and check whether it ever shows a channel-map
    hex EXACTLY matching a known host-forced classification, for a run that
    was never supposed to force one. Returns a description string if found,
    else None. Never raises -- a missing/unreadable file just means
    nothing to check (best-effort, same as the AFH poller itself).

    Real, confirmed incident this exists for (2026-09-24): a Row 3 forced-
    channel run was interrupted (the session was stopped, then resumed) and
    its host classification -- persistent controller state, not tied to
    any one process -- was never reset. It leaked into two subsequent
    "AFH-free" Row 2 runs (Corsano duty50pct rep3, duty0pct_bracket), both
    showing channel_map_hex=00f00f0000 (exactly FORCED_CHANNEL_RANGE) for
    their entire duration. See the proactive reset in _attempt_run and the
    block-level reset in chamber_session.py's forced-channel rows for the
    prevention side of this fix -- this is the detection side, so a leak
    that somehow still occurs (e.g. a future forced-channel row added
    without going through those same guards) gets caught and excluded
    automatically on the next parse, not just found by manual CSV
    inspection."""
    if not afh_csv_path.exists():
        return None
    try:
        with open(afh_csv_path) as f:
            afh_rows = list(csv.DictReader(f))
    except OSError:
        return None
    for afh_row in afh_rows:
        hexmap = (afh_row.get("channel_map_hex") or "").strip().lower()
        if hexmap in forced_signatures:
            return (
                f"channel map at t={afh_row.get('timestamp_s')}s shows a "
                f"host-forced classification ({hexmap}) even though this "
                f"condition should be AFH-free -- a previous forced-channel "
                f"run's classification leaked into this one (the controller "
                f"setting is persistent hardware state, not reset "
                f"automatically between runs unless explicitly told to)"
            )
    return None

# Every run ends with the script itself calling stop_driver(), which sends
# SIGINT and triggers the driver's own graceful BLE disconnect -- confirmed
# against a real capture, where stop_driver was logged ~247s into a planned
# 240s run and the only Disconnect Complete event landed at ~251.6s, with
# stop_btmon following a few seconds after that. That expected teardown
# disconnect must not count as an instability, or every successful baseline
# run would fail this check. Only a disconnect well before the capture ends
# (i.e. not in this trailing window) counts as a real, unexpected drop.
#
# This used to be a flat 20s for every run regardless of the device's own
# negotiated supervision timeout -- an arbitrary constant an external review
# flagged as needing justification. It's now a FLOOR: the actual grace
# window (see _grace_window_s below) is max(this floor, 2x the run's own
# negotiated supervision timeout), so a device negotiating a long timeout
# doesn't get an unfairly short grace window relative to how long BlueZ
# itself would wait before giving up on a real drop.
SHUTDOWN_GRACE_S = 20

# Symmetric to SHUTDOWN_GRACE_S, but at the start: confirmed on a real,
# fully-isolated Polar_H10 capture (Corsano explicitly disconnected from
# BlueZ before this test even started, so no cross-device contamination) --
# the driver connects, our OWN host issues an explicit disconnect command
# (@ MGMT Command: Disconnect, not the peripheral dropping itself) at ~12s,
# reconnects, does the same thing again at ~17s, reconnects a third time,
# and is then rock-solid for the rest of the run. This looks like the
# polar_ros2 driver's own connect/probe/reconnect startup sequence, not
# instability -- the same category of "expected, not a real failure" as the
# end-of-run teardown above, just at the other end of the capture. Also a
# floor now, see _grace_window_s.
STARTUP_GRACE_S = 20


def _grace_window_s(initial_timeout_ms):
    """The startup/shutdown grace window for one run: max(the 20s floor
    above, 2x this run's own negotiated supervision timeout) -- "should have
    reconnected by now" is naturally scaled to how long BlueZ itself would
    wait before declaring the link dead, not an arbitrary flat constant.
    Falls back to the floor if no successful connection (and thus no
    timeout) was ever recorded."""
    if not initial_timeout_ms:
        return SHUTDOWN_GRACE_S
    return max(SHUTDOWN_GRACE_S, 2 * initial_timeout_ms / 1000)


CONN_COMPLETE_RE = re.compile(r"LE (?:Enhanced )?Connection Complete")
CONN_UPDATE_RE = re.compile(r"LE Connection Update Complete")
DISCONNECT_RE = re.compile(r"Disconnect Complete")
STATUS_RE = re.compile(r"Status:\s+(\S.*?)\s*\(0x[0-9A-Fa-f]+\)")
INTERVAL_RE = re.compile(r"Connection [Ii]nterval:\s+([\d.]+)\s*msec")
LATENCY_RE = re.compile(r"Connection [Ll]atency:\s+([\d.]+)\s*msec")
TIMEOUT_RE = re.compile(r"Supervision [Tt]imeout:\s+(\d+)\s*msec")
MTU_RE = re.compile(r"(?:Client RX MTU|MTU):\s+(\d+)")
HANDLE_RE = re.compile(r"Handle:\s*(\d+)")
# HCI handle numbers are assigned dynamically by the controller and are NOT
# fixed per device -- confirmed on real hardware, the same MAC got handle
# 2048 in one session and 2049 in another. Only the peer's MAC address
# reliably identifies which device a connect/disconnect event belongs to.
# Without this, a stray connection from a DIFFERENT device captured on the
# same shared radio (e.g. one that didn't disconnect cleanly from a prior
# test) gets silently attributed to whatever device this capture is
# nominally for -- confirmed as the actual cause of repeated
# "unexpected disconnect" failures previously misattributed to Polar_H10.
PEER_ADDRESS_RE = re.compile(r"Peer address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
HANDLE_ADDRESS_RE = re.compile(
    r"Handle:\s*\d+\s*Address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
)
# btmon prints a timestamp at the end of each top-level packet header line,
# e.g. "> HCI Event: LE Meta Event (0x3e) plen 31   #325 [hci0] 11.661860".
# Sub-event detail lines (LE Enhanced Connection Complete, Status, Disconnect
# reason, etc.) that follow don't carry their own timestamp, so we track the
# most recently seen header timestamp and attach it to whatever comes after.
HEADER_TS_RE = re.compile(r"\[hci\d+\]\s+([\d.]+)\s*$")
# Every incoming ACL Data RX packet on the connection carries the sensor's
# GATT notifications (its actual data samples), each with its own timestamp
# on the same line, e.g. "> ACL Data RX: Handle 2048 flags 0x02 dlen 7  ...
# [hci0] 25.429800". This is validated against real hardware captures in
# captures/*.btsnoop, not just a guess at the format.
ACL_RX_RE = re.compile(r"^> ACL Data RX: Handle (\d+)")
ACL_TX_RE = re.compile(r"^< ACL Data TX: Handle (\d+)")

# Polar H10 sample-completeness (deferred TODO from parse_capture_file,
# implemented once the actual wire format was confirmed against both the
# polar_ros2 driver source (test_ws/src/ros4healthcare/ros2_hc_drv/
# polar_ros2/polar_ros2/submodules/PolarH10.py) and a real capture
# (captures/20260918_152906/row1_Polar_H10_baseline_rep1.btsnoop) -- e.g. a
# real ACC frame's Data: 026a8ac782b54e5208013ffcebffd6ff... decodes as
# byte0=0x02 (ACC), bytes1-8=6a8ac782b54e5208 (LE ns timestamp of the last
# sample in this frame), byte9=01 (frame_type -> 16 bit/axis), remainder =
# packed XYZ samples. This is real, physically-embedded data from the
# sensor's OWN internal clock -- unlike RX inter-arrival jitter (which only
# measures OUR reception timing), comparing consecutive frames' embedded
# timestamps against the known fixed sample rate reveals samples the
# sensor actually produced that never reached us at all: "no delivered
# samples lost" vs. the weaker "no gaps observed" RX jitter alone can
# support.
#
# Standard Heart Rate Measurement notifications (0x2A37) are also
# recoverable this way, though only as an approximate cross-check: Polar's
# firmware buffers RR-intervals and backfills multiple into the next
# notification if an earlier one didn't get sent, so the SUM of all
# received RR-intervals approximates real elapsed on-body time -- but the
# buffer isn't unlimited, so this can still underestimate loss under
# extreme/sustained delay. See compute_hr_completeness's docstring.
NOTIFICATION_RE = re.compile(r"ATT: Handle Value Notification")
NOTIFICATION_TYPE_RE = re.compile(r"Type:\s*(.+?)\s*$", re.MULTILINE)
NOTIFICATION_DATA_RE = re.compile(r"Data:\s*([0-9A-Fa-f]+)")
POLAR_PMD_TYPE_MARKER = "fb005c82"  # PMD_CHAR2_UUID (ACC and ECG both notify here)
POLAR_HR_TYPE_MARKER = "2a37"  # standard Heart Rate Measurement characteristic
POLAR_ACC_SAMPLING_FREQ_HZ = 200  # per PolarH10.py's ACC_SAMPLING_FREQ


def _parse_polar_acc_frame(hex_data):
    """Parse one Polar PMD ACC notification frame. Returns
    (first_sample_ts_s, last_sample_ts_s, n_samples), both timestamps on
    the sensor's own internal clock (arbitrary epoch, only valid relative
    to other frames from the same connection) -- or None if this isn't a
    complete/valid ACC frame (e.g. it was actually an ECG frame, which
    shares the same characteristic but starts with 0x00)."""
    try:
        data = bytes.fromhex(hex_data)
    except ValueError:
        return None
    if len(data) < 11 or data[0] != 0x02:
        return None
    last_sample_ts = int.from_bytes(data[1:9], "little") / 1e9
    frame_type = data[9]
    resolution = (frame_type + 1) * 8
    step = -(-resolution // 8)  # ceil division
    samples = data[10:]
    n_samples = len(samples) // (3 * step)
    if n_samples == 0:
        return None
    time_step = 1.0 / POLAR_ACC_SAMPLING_FREQ_HZ
    first_sample_ts = last_sample_ts - (n_samples - 1) * time_step
    return first_sample_ts, last_sample_ts, n_samples


EMPTY_ACC_COMPLETENESS = {
    "acc_pmd_frame_count": 0,
    "acc_samples_received": None,
    "acc_samples_lost": None,
    "acc_completeness_pct": None,
}


def compute_acc_completeness(pmd_hex_frames):
    """Sample completeness for the PMD accelerometer stream, from the
    sensor's OWN embedded per-frame timestamps rather than our host's RX
    arrival timing. Walks consecutive frames (sorted by embedded
    timestamp): if the next frame's first sample starts later than this
    frame's last sample + one nominal time_step (with a 1.5x tolerance for
    float rounding), that gap represents real samples the sensor produced
    but that never reached us -- counted via round(gap / time_step).
    Returns EMPTY_ACC_COMPLETENESS if no valid ACC frames were found."""
    frames = [
        parsed
        for parsed in (_parse_polar_acc_frame(h) for h in pmd_hex_frames)
        if parsed is not None
    ]
    if not frames:
        return dict(EMPTY_ACC_COMPLETENESS)
    frames.sort(key=lambda f: f[0])
    time_step = 1.0 / POLAR_ACC_SAMPLING_FREQ_HZ
    received = sum(f[2] for f in frames)
    lost = 0
    for prev, cur in zip(frames, frames[1:]):
        expected_next_first_ts = prev[1] + time_step
        gap_s = cur[0] - expected_next_first_ts
        if gap_s > 1.5 * time_step:
            lost += round(gap_s / time_step)
    total = received + lost
    return {
        "acc_pmd_frame_count": len(frames),
        "acc_samples_received": received,
        "acc_samples_lost": lost,
        "acc_completeness_pct": round(100 * received / total, 2) if total else None,
    }


def _parse_polar_hr_frame(hex_data):
    """Parse one standard Heart Rate Measurement (0x2A37) notification,
    returning the RR-intervals (seconds) it carries. See PolarH10.py's
    hr_data_conv for the reference parse this mirrors."""
    try:
        data = bytes.fromhex(hex_data)
    except ValueError:
        return []
    if len(data) < 2:
        return []
    flags = data[0]
    uint8_format = (flags & 1) == 0
    energy_expenditure = ((flags >> 3) & 1) == 1
    rr_present = ((flags >> 4) & 1) == 1
    if not rr_present:
        return []
    idx = 2 if uint8_format else 3
    if energy_expenditure:
        idx += 2
    rr_list = []
    while idx + 1 < len(data):
        raw = data[idx] | (data[idx + 1] << 8)
        rr_list.append(raw / 1024.0)
        idx += 2
    return rr_list


EMPTY_HR_COMPLETENESS = {"hr_rr_total_s": None, "hr_rr_completeness_pct": None}


def compute_hr_completeness(hr_hex_frames, connected_duration_s):
    """Approximate sample completeness for the standard HR/RR stream: sum
    of all received RR-intervals (real elapsed heartbeat time, since
    Polar's firmware buffers and backfills them into later notifications)
    vs. this run's actual connected duration. This is NOT as reliable as
    compute_acc_completeness -- the RR buffer isn't unlimited, so sustained
    delay can still silently drop old RR-intervals before they're ever
    sent, and any genuine skin-contact loss looks identical to transmission
    loss here. Treat this as a cross-check, not the primary claim."""
    total_rr_s = sum(
        rr for hex_data in hr_hex_frames for rr in _parse_polar_hr_frame(hex_data)
    )
    if not hr_hex_frames:
        return dict(EMPTY_HR_COMPLETENESS)
    if not connected_duration_s or connected_duration_s <= 0:
        return {"hr_rr_total_s": round(total_rr_s, 1), "hr_rr_completeness_pct": None}
    pct = 100 * total_rr_s / connected_duration_s
    return {
        "hr_rr_total_s": round(total_rr_s, 1),
        "hr_rr_completeness_pct": round(min(pct, 100.0), 2),
    }


def _jitter_percentile(sorted_diffs_ms, p):
    """Nearest-rank percentile (0 <= p <= 1) of an already-sorted list."""
    if not sorted_diffs_ms:
        return None
    idx = min(int(round(p * (len(sorted_diffs_ms) - 1))), len(sorted_diffs_ms) - 1)
    return round(sorted_diffs_ms[idx], 3)


def compute_jitter_percentiles(rx_timestamps_s):
    """Inter-arrival interval percentiles (ms) between consecutive ACL Data RX
    packets on the connection. This is NOT full end-to-end pipeline latency
    (sensor -> BLE -> gateway -> ROS2 -> MQTT -> actuation) -- that needs
    timestamps at every hop, which btmon alone can't provide. It's the
    right-scoped p50/p95/p99/p99.9 metric for this BLE-connection-level
    validation test: how consistent the over-the-air data delivery timing is."""
    diffs_ms = sorted(
        (b - a) * 1000.0 for a, b in zip(rx_timestamps_s, rx_timestamps_s[1:])
    )
    return {
        "rx_sample_count": len(rx_timestamps_s),
        "jitter_p50_ms": _jitter_percentile(diffs_ms, 0.50),
        "jitter_p95_ms": _jitter_percentile(diffs_ms, 0.95),
        "jitter_p99_ms": _jitter_percentile(diffs_ms, 0.99),
        "jitter_p999_ms": _jitter_percentile(diffs_ms, 0.999),
    }


# =============================================================================
# Corsano sample-completeness. Wire format (opcodes, byte layout, CRC-8) is
# now validated against a real capture (captures/20260923_004345,
# F1:3B:40:28:CB:EC, 2026-09-23) -- confirmed by decoding real payloads,
# e.g. attribute handle 0x0010's write `161a0917002c207800` matches
# APP_CMD_UPDATE_TIME (cmd=22) with year=2026/month=09/day=23, the exact
# real date of that session.
#
# IMPORTANT: btmon does NOT show Type:/UUID annotations for these
# characteristics on a normal reconnect -- BlueZ serves simplepyble a
# cached GATT table for an already-bonded device instead of re-running
# discovery over the air, so the ATT exchanges that would teach btmon the
# handle-to-UUID mapping never happen on the wire. Identification here is
# by real, validated attribute handle number instead (see
# CORSANO_WRITE_CHAR_HANDLE etc. below) -- these are device-firmware-fixed,
# not the dynamic per-session values seen elsewhere in this file, but WILL
# need re-deriving for a different Corsano unit/firmware revision.
#
# SEPARATE, more important finding from that same real capture: BioZ, ACC,
# and Stress files showed ZERO growth across the entire 300s session
# (byte-for-byte identical downloaded data on every single poll) -- only
# Activity grew (by exactly one 38-byte record). The BLE link and download
# mechanism are both genuinely working (Activity proves it) -- this means
# BioZ/ACC/Stress RECORDING isn't actually active on the device, a
# corsano_ros-side gap (no equivalent of _set_activity_plan() for these
# streams; _enable_bioz_streaming() uses fire-and-forget driver.write()
# with no ack tracking) unrelated to BLE/interference entirely. Worth
# fixing before trusting these streams' chamber results at all -- see the
# session write-up for the details behind this.
#
# Corsano's architecture is also fundamentally different from Polar's: it's
# not continuous streaming, it's 0.1s-interval POLLING of on-device files
# (CMD_GET_FILE_SIZE, then CMD_START_STREAMING_FILE_WITH_SIZE[_OFFSET] to
# download a tail window). Only ACC packets carry a real embedded sequence
# number (0-255 wraparound) that's independent of polling cadence -- exactly
# analogous to Polar's PMD timestamps. Activity/Stress records carry an
# embedded absolute timestamp, but since the driver always requests just the
# single latest record, their "completeness" really measures "did the 0.1s
# poll rate keep up with record generation," not raw BLE transmission loss.
# BioZ records carry neither, so its completeness (if any) has to come from
# the device's own file-size counter (CMD_GET_FILE_SIZE) growing faster than
# what got downloaded in that window, not from anything in the payload.
# =============================================================================

CORSANO_WRITE_CHAR_MARKER = "6e400002"
CORSANO_FILE_RX_MARKER = "6e400003"
CORSANO_COMMAND_RX_MARKER = "6e400004"
CORSANO_FILE_ID_ACTIVITY = 2
CORSANO_FILE_ID_BIOZ = 14
CORSANO_FILE_ID_STRESS = 21
CORSANO_FILE_ID_ACC = 22
CORSANO_CMD_GET_FILE_SIZE = 46
CORSANO_CMD_STREAM_SIZE = 48
CORSANO_CMD_STREAM_SIZE_OFFSET = 68
CORSANO_ACC_SAMPLE_RATE_HZ = 32  # accelerometer_parser.py's ACC_SR

WRITE_RE = re.compile(r"ATT: Write (?:Command|Request)")


def _corsano_crc8(data):
    """CRC-8 matching corsano_enums.check_crc = crcmod.mkCrcFun(0x107, 0xff,
    False, 0) -- poly 0x07 (implicit top bit), init 0xFF, no reflection, no
    final XOR. Reimplemented dependency-free (crcmod isn't a dependency of
    this project) via the standard bit-by-bit CRC-8 algorithm."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


ATT_HANDLE_RE = re.compile(r"Handle:\s*(0x[0-9A-Fa-f]+)")

# Real, validated attribute handles for the Corsano unit tested 2026-09-23
# (F1:3B:40:28:CB:EC) -- NOT derived from GATT UUID discovery, because btmon
# never shows Type:/UUID annotations for this device's characteristics at
# all on a normal reconnect: BlueZ serves simplepyble a cached GATT table
# for an already-bonded device instead of re-running discovery over the
# air, so the ATT exchanges that would teach btmon the handle-to-UUID
# mapping (Read By Type / Find Information) simply never happen on the
# wire. Confirmed by decoding real payloads instead: handle 0x0010's write
# `161a0917002c207800` matches APP_CMD_UPDATE_TIME (cmd=22) with year=2026,
# month=09, day=23 -- the exact real date/time of the session that produced
# it -- and handle 0x0015 carries a clean 7-byte CMD_GET_FILE_SIZE (cmd=46,
# 0x2e) response shape. These are GATT attribute handles (fixed per device
# firmware), not the dynamic per-session ACL connection handles seen
# elsewhere in this file, so they should stay stable across sessions with
# this same physical unit -- but WILL need re-deriving the same way for a
# different Corsano unit or firmware revision.
CORSANO_WRITE_CHAR_HANDLE = "0x0010"
CORSANO_FILE_RX_HANDLE = "0x0012"
CORSANO_COMMAND_RX_HANDLE = "0x0015"


def _corsano_extract_frames(text):
    """Single pass over btmon -r output text, reconstructing Corsano's
    request/response traffic into per-transfer byte buffers -- mirroring
    CorsanoDriver._on_file_data's own self.buffer.write(data) accumulation,
    just done here from a capture instead of live. Returns
    (transfers, file_size_samples):
      transfers: one dict per completed WRITE_CHAR(cmd=48/68) -> FILE_RX_CHAR*
        cycle: {"file_id", "requested_size", "offset", "data": bytes}
      file_size_samples: [(file_id, size), ...] in capture order, from
        CMD_GET_FILE_SIZE (cmd=46) responses on COMMAND_RX_CHAR

    Identifies characteristics primarily by the real, validated attribute
    handle numbers above (see their comment for why UUID Type: annotations
    can't be relied on for this device), falling back to a UUID Type: match
    if btmon happens to have one (e.g. after a fresh, unbonded pairing that
    actually ran full discovery over the air)."""
    lines = text.splitlines()
    transfers = []
    file_size_samples = []
    current_request = None
    current_data = bytearray()

    def _flush():
        nonlocal current_request, current_data
        if current_request is not None and current_data:
            transfers.append({**current_request, "data": bytes(current_data)})
        current_request = None
        current_data = bytearray()

    def _is_write_char(window, handle):
        return handle == CORSANO_WRITE_CHAR_HANDLE or (
            (m := NOTIFICATION_TYPE_RE.search(window))
            and CORSANO_WRITE_CHAR_MARKER in m.group(1).lower()
        )

    def _is_file_rx(window, handle):
        return handle == CORSANO_FILE_RX_HANDLE or (
            (m := NOTIFICATION_TYPE_RE.search(window))
            and CORSANO_FILE_RX_MARKER in m.group(1).lower()
        )

    def _is_command_rx(window, handle):
        return handle == CORSANO_COMMAND_RX_HANDLE or (
            (m := NOTIFICATION_TYPE_RE.search(window))
            and CORSANO_COMMAND_RX_MARKER in m.group(1).lower()
        )

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if WRITE_RE.search(line):
            window = "\n".join(lines[i : i + 4])
            handle_m = ATT_HANDLE_RE.search(window)
            data_m = NOTIFICATION_DATA_RE.search(window)
            handle = handle_m.group(1).lower() if handle_m else None
            if handle and _is_write_char(window, handle) and data_m:
                try:
                    req = bytes.fromhex(data_m.group(1))
                except ValueError:
                    req = b""
                if len(req) >= 6 and req[0] == CORSANO_CMD_STREAM_SIZE:
                    _flush()
                    current_request = {
                        "file_id": req[1],
                        "requested_size": int.from_bytes(req[2:6], "little"),
                        "offset": 0,
                    }
                elif len(req) >= 10 and req[0] == CORSANO_CMD_STREAM_SIZE_OFFSET:
                    _flush()
                    current_request = {
                        "file_id": req[1],
                        "requested_size": int.from_bytes(req[2:6], "little"),
                        "offset": int.from_bytes(req[6:10], "little"),
                    }
        elif NOTIFICATION_RE.search(line):
            window = "\n".join(lines[i : i + 4])
            handle_m = ATT_HANDLE_RE.search(window)
            data_m = NOTIFICATION_DATA_RE.search(window)
            handle = handle_m.group(1).lower() if handle_m else None
            if handle and data_m:
                try:
                    payload = bytes.fromhex(data_m.group(1))
                except ValueError:
                    payload = b""
                if _is_file_rx(window, handle) and current_request is not None:
                    current_data.extend(payload)
                elif _is_command_rx(window, handle) and len(payload) >= 6:
                    if payload[0] == CORSANO_CMD_GET_FILE_SIZE:
                        size = int.from_bytes(payload[2:6], "little", signed=True)
                        file_size_samples.append((payload[1], size))
        i += 1
    _flush()
    return transfers, file_size_samples


EMPTY_CORSANO_COMPLETENESS = {
    "corsano_acc_packet_count": None,
    "corsano_acc_samples_lost": None,
    "corsano_acc_completeness_pct": None,
    "corsano_activity_record_count": None,
    "corsano_activity_gap_s_max": None,
    "corsano_stress_record_count": None,
    "corsano_stress_gap_s_max": None,
    "corsano_bioz_bytes_downloaded": None,
    "corsano_bioz_file_growth_bytes": None,
    "corsano_bioz_completeness_pct": None,
}


def _corsano_acc_completeness(transfers):
    """ACC packets carry a real embedded 0-255 wraparound sequence number
    (metric_array header byte 0, see accelerometer_parser.py's own
    "Packet drop detected" check) -- this is the one Corsano stream with a
    completeness signal as direct as Polar's PMD timestamps.

    IMPORTANT: each downloaded buffer is ONE packet, not several stacked
    ones -- confirmed from retrieve_data.py's own call, process_metric_array
    (buffer_data, 0, metric_id=0x2B, metric_size=len(buffer_data)), which
    passes the WHOLE buffer as a single metric's size. An earlier version of
    this function wrongly treated a transfer as multiple back-to-back
    packets; caught by a synthetic-data test before this was ever trusted
    against anything real.

    The index appears to identify which internal chunk of the device's own
    accel file this tail-window download landed on, NOT a per-notification
    counter -- since each 0.1s poll re-downloads roughly the same ~1024-byte
    tail, most consecutive polls will see the SAME index (the file hasn't
    advanced to a new chunk yet). That repeat is not new data and not loss,
    so it's skipped rather than compared -- only an actual index CHANGE is
    checked for whether it was the next consecutive chunk or a skipped one."""
    acc_transfers = [t for t in transfers if t["file_id"] == CORSANO_FILE_ID_ACC]
    if not acc_transfers:
        return {}
    last_index = None
    packet_count = 0
    lost = 0
    for t in acc_transfers:
        data = t["data"]
        if len(data) < 4:
            continue
        index = data[0]
        if last_index is not None and index == last_index:
            continue  # same chunk re-downloaded, not new data
        if last_index is not None:
            expected = (last_index + 1) % 256
            if index != expected:
                lost += (index - expected) % 256
        last_index = index
        packet_count += 1
    total = packet_count + lost
    return {
        "corsano_acc_packet_count": packet_count,
        "corsano_acc_samples_lost": lost,
        "corsano_acc_completeness_pct": (
            round(100 * packet_count / total, 2) if total else None
        ),
    }


def _corsano_latest_record_gaps(
    transfers, file_id, record_size, ts_offset, ts_len, ts_scale
):
    """Shared logic for Activity/Stress: the driver always requests just the
    single latest record (offset = file_size - record_size), so this
    measures poll-cadence continuity (did we catch every record the device
    generated) via each record's own embedded timestamp, not raw BLE loss
    the way ACC's index does. ts_scale converts the embedded integer to
    seconds (1 for Activity's whole-second timestamp, 1/1000 for Stress's
    millisecond one)."""
    records = []
    for t in transfers:
        if t["file_id"] != file_id or len(t["data"]) < record_size:
            continue
        rec = t["data"][-record_size:]
        if _corsano_crc8(rec[:-1]) != rec[-1]:
            continue
        ts = (
            int.from_bytes(rec[ts_offset : ts_offset + ts_len], "little", signed=False)
            * ts_scale
        )
        records.append(ts)
    if not records:
        return None, None
    distinct = [records[0]]
    for prev, cur in zip(records, records[1:]):
        if cur != prev:
            distinct.append(cur)
    gaps = [b - a for a, b in zip(distinct, distinct[1:])]
    return len(distinct), (max(gaps) if gaps else None)


def _corsano_bioz_completeness(transfers, file_size_samples):
    """BioZ (OHR) packets carry no embedded absolute timestamp or sequence
    number (see bioz_parser.py -- its record_index/timestamp are both
    locally synthesized by the parser, not read from the device), so the
    only real completeness signal is the device's own file-size counter
    (CMD_GET_FILE_SIZE) growing faster than what actually got downloaded in
    that window."""
    bioz_transfers = [t for t in transfers if t["file_id"] == CORSANO_FILE_ID_BIOZ]
    bytes_downloaded = sum(len(t["data"]) for t in bioz_transfers)
    bioz_sizes = [
        size for fid, size in file_size_samples if fid == CORSANO_FILE_ID_BIOZ
    ]
    if not bioz_transfers and not bioz_sizes:
        return {}
    growth = (max(bioz_sizes) - min(bioz_sizes)) if len(bioz_sizes) >= 2 else None
    pct = None
    if growth and growth > 0:
        pct = round(min(100.0, 100 * bytes_downloaded / growth), 2)
    return {
        "corsano_bioz_bytes_downloaded": bytes_downloaded,
        "corsano_bioz_file_growth_bytes": growth,
        "corsano_bioz_completeness_pct": pct,
    }


def compute_corsano_completeness(text):
    """Top-level entry point: parse btmon -r output text for Corsano's
    command/file-channel traffic and compute completeness for ACC/Activity/
    Stress/BioZ. Wire format validated against a real capture -- see the
    module note above this section. Returns EMPTY_CORSANO_COMPLETENESS (all
    None) if no Corsano command/file traffic is found at all (e.g. a Polar
    capture)."""
    transfers, file_size_samples = _corsano_extract_frames(text)
    if not transfers and not file_size_samples:
        return dict(EMPTY_CORSANO_COMPLETENESS)
    result = dict(EMPTY_CORSANO_COMPLETENESS)
    result.update(_corsano_acc_completeness(transfers))
    activity_count, activity_gap = _corsano_latest_record_gaps(
        transfers, CORSANO_FILE_ID_ACTIVITY, 38, 0, 4, 1.0
    )
    if activity_count is not None:
        result["corsano_activity_record_count"] = activity_count
        result["corsano_activity_gap_s_max"] = activity_gap
    stress_count, stress_gap = _corsano_latest_record_gaps(
        transfers, CORSANO_FILE_ID_STRESS, 18, 0, 4, 1.0 / 1000
    )
    if stress_count is not None:
        result["corsano_stress_record_count"] = stress_count
        result["corsano_stress_gap_s_max"] = stress_gap
    result.update(_corsano_bioz_completeness(transfers, file_size_samples))
    return result


def parse_btmon_text(text, mac_address=None):
    """Scan btmon -r output text for connection and disconnection events.
    Returns a dict summary, including how much of the capture was actually
    connected -- a driver process staying alive says nothing about whether
    the underlying BLE link is still up, so this is tracked independently.

    mac_address, when given, restricts everything (connects, disconnects,
    RX/jitter) to that device's own peer address. btmon on a shared adapter
    captures ALL Bluetooth activity, not just this test's device, and HCI
    handle numbers are assigned dynamically by the controller -- NOT fixed
    per device (confirmed on real hardware: the same MAC got handle 2048 in
    one session and 2049 in another) -- so without this filter, a stray
    connection left over from a different device/test gets silently
    attributed to whatever device this capture is nominally for. This was
    confirmed as the actual cause of repeated "unexpected disconnect"
    failures previously misattributed to Polar_H10: the disconnects were
    really Corsano's own stray background reconnects."""
    expected_mac = mac_address.upper() if mac_address else None
    lines = text.splitlines()
    events = (
        []
    )  # list of dicts: {type, status, handle, peer_address, timestamp_s, interval, latency, timeout, line_idx}
    disconnects = []  # list of dicts: {timestamp_s, address, line_idx}
    acl_rx = []  # list of (handle, timestamp_s) for every ACL Data RX packet
    mtu_candidates = []  # list of (handle, mtu_value), in file order
    pmd_hex_frames = []  # list of (handle, hex_data) -- Polar PMD ACC/ECG notifications
    hr_hex_frames = (
        []
    )  # list of (handle, hex_data) -- standard HR (0x2A37) notifications
    current_ts = None
    first_ts = None
    last_ts = None
    current_acl_handle = None

    i = 0
    while i < len(lines):
        line = lines[i]
        ts_m = HEADER_TS_RE.search(line)
        if ts_m:
            current_ts = float(ts_m.group(1))
            if first_ts is None:
                first_ts = current_ts
            last_ts = current_ts

        if CONN_COMPLETE_RE.search(line) or CONN_UPDATE_RE.search(line):
            ev_type = "complete" if CONN_COMPLETE_RE.search(line) else "update"
            window = "\n".join(lines[i : i + 10])
            status_m = STATUS_RE.search(window)
            interval_m = INTERVAL_RE.search(window)
            latency_m = LATENCY_RE.search(window)
            timeout_m = TIMEOUT_RE.search(window)
            handle_m = HANDLE_RE.search(window)
            peer_m = PEER_ADDRESS_RE.search(window)
            events.append(
                {
                    "type": ev_type,
                    "status": status_m.group(1) if status_m else None,
                    "handle": handle_m.group(1) if handle_m else None,
                    "peer_address": peer_m.group(1).upper() if peer_m else None,
                    "timestamp_s": current_ts,
                    "interval_ms": float(interval_m.group(1)) if interval_m else None,
                    "latency_ms": float(latency_m.group(1)) if latency_m else None,
                    "timeout_ms": int(timeout_m.group(1)) if timeout_m else None,
                    "line_idx": i,
                }
            )
        elif DISCONNECT_RE.search(line):
            window = "\n".join(lines[i : i + 6])
            addr_m = HANDLE_ADDRESS_RE.search(window)
            disconnects.append(
                {
                    "timestamp_s": current_ts,
                    "address": addr_m.group(1).upper() if addr_m else None,
                    "line_idx": i,
                }
            )
        else:
            rx_m = ACL_RX_RE.match(line)
            if rx_m:
                current_acl_handle = rx_m.group(1)
                if current_ts is not None:
                    acl_rx.append((rx_m.group(1), current_ts))
            else:
                tx_m = ACL_TX_RE.match(line)
                if tx_m:
                    current_acl_handle = tx_m.group(1)

        mtu_m = MTU_RE.search(line)
        if mtu_m:
            mtu_candidates.append((current_acl_handle, int(mtu_m.group(1))))

        if NOTIFICATION_RE.search(line):
            window = "\n".join(lines[i : i + 4])
            type_m = NOTIFICATION_TYPE_RE.search(window)
            data_m = NOTIFICATION_DATA_RE.search(window)
            if type_m and data_m:
                type_str = type_m.group(1).lower()
                if POLAR_PMD_TYPE_MARKER in type_str:
                    pmd_hex_frames.append((current_acl_handle, data_m.group(1)))
                elif POLAR_HR_TYPE_MARKER in type_str:
                    hr_hex_frames.append((current_acl_handle, data_m.group(1)))
        i += 1

    if expected_mac:
        events = [e for e in events if e["peer_address"] == expected_mac]
        disconnects = [d for d in disconnects if d["address"] == expected_mac]

    # Only trust events that actually succeeded -- a timed-out or rejected
    # attempt (e.g. "Status: Unknown Connection Identifier") has no real
    # connection interval, and counting it would misreport the link's
    # actual behavior.
    successes = [e for e in events if e["status"] == "Success"]
    failed_attempts = [
        e for e in events if e["type"] == "complete" and e["status"] != "Success"
    ]
    initial = next((e for e in successes if e["type"] == "complete"), None)
    updates = [e for e in successes if e["type"] == "update"]

    # Only count RX packets on a handle that actually completed a successful
    # connection -- ignoring, e.g., leftover traffic from a failed attempt.
    connected_handles = {
        e["handle"]
        for e in successes
        if e["type"] == "complete" and e["handle"] is not None
    }
    rx_timestamps = sorted(ts for handle, ts in acl_rx if handle in connected_handles)
    jitter = compute_jitter_percentiles(rx_timestamps)

    # Same handle-scoping as above: an MTU value logged for a different,
    # unrelated connection on the shared adapter shouldn't be reported as
    # this device's negotiated MTU.
    mtu = next(
        (v for h, v in mtu_candidates if h in connected_handles),
        None,
    )

    # Same handle-scoping again: only count PMD/HR notifications on a
    # handle that belongs to this device's own successful connection.
    scoped_pmd_hex = [h for handle, h in pmd_hex_frames if handle in connected_handles]
    scoped_hr_hex = [h for handle, h in hr_hex_frames if handle in connected_handles]
    acc_completeness = compute_acc_completeness(scoped_pmd_hex)

    # Exclude the expected end-of-run teardown disconnect (see
    # SHUTDOWN_GRACE_S / _grace_window_s) from the count of real, unexpected
    # drops. grace_s is scaled to this run's own negotiated supervision
    # timeout rather than a flat constant -- see _grace_window_s.
    grace_s = _grace_window_s(initial["timeout_ms"] if initial else None)
    unexpected_disconnects = [
        d
        for d in disconnects
        if d["timestamp_s"] is not None
        and last_ts is not None
        and first_ts is not None
        and (last_ts - d["timestamp_s"]) > grace_s
        and (d["timestamp_s"] - first_ts) > grace_s
    ]

    # Reconstruct a connected/disconnected timeline from successful connects
    # and disconnects, in chronological order, so a link that drops mid-run
    # and never reconnects is visible instead of implicitly reading as "up
    # the whole time" just because the run as a whole was marked PASSED.
    timeline = sorted(
        [
            {"kind": "connect", "ts": e["timestamp_s"]}
            for e in successes
            if e["type"] == "complete" and e["timestamp_s"] is not None
        ]
        + [
            {"kind": "disconnect", "ts": d["timestamp_s"]}
            for d in disconnects
            if d["timestamp_s"] is not None
        ],
        key=lambda ev: ev["ts"],
    )
    connected_duration_s = 0.0
    connected_since = None
    for ev in timeline:
        if ev["kind"] == "connect":
            connected_since = ev["ts"]
        elif connected_since is not None:
            connected_duration_s += ev["ts"] - connected_since
            connected_since = None
    if connected_since is not None and last_ts is not None:
        # Still connected when the capture ended.
        connected_duration_s += last_ts - connected_since

    hr_completeness = compute_hr_completeness(scoped_hr_hex, connected_duration_s)
    # Not MAC/handle-scoped like everything else above -- Corsano's WRITE_CHAR/
    # FILE_RX_CHAR/COMMAND_RX_CHAR UUIDs are specific to its own proprietary
    # service, so there's no cross-device contamination risk the way generic
    # ACL handles have, as long as only one Corsano-protocol device is ever
    # on the shared radio at once (true for this project).
    corsano_completeness = compute_corsano_completeness(text)

    return {
        "initial_interval_ms": initial["interval_ms"] if initial else None,
        "initial_latency_ms": initial["latency_ms"] if initial else None,
        "initial_timeout_ms": initial["timeout_ms"] if initial else None,
        "num_updates": len(updates),
        "update_intervals": ";".join(
            f"{u['interval_ms']}" for u in updates if u["interval_ms"] is not None
        ),
        "mtu": mtu,
        "num_failed_connection_attempts": len(failed_attempts),
        "disconnect_count": len(disconnects),
        "unexpected_disconnect_count": len(unexpected_disconnects),
        "connected_duration_s": round(connected_duration_s, 3),
        "capture_duration_s": last_ts,
        **jitter,
        **acc_completeness,
        **hr_completeness,
        **corsano_completeness,
    }


def parse_capture_file(capture_path, mac_address=None):
    """Run `btmon -r <file>` and parse its text output.

    Sample completeness (see compute_acc_completeness/compute_hr_completeness
    in parse_btmon_text) is implemented for Polar H10 only -- its PMD
    accelerometer frames and standard Heart Rate Measurement notifications
    both embed enough information (the sensor's own timestamp/RR-interval
    data) to detect real dropped samples, not just gaps in our own RX
    timing. Corsano's payload format isn't documented anywhere in this
    codebase, so the same claim can't be made for it: for Corsano, "no
    unexpected disconnects and full jitter percentiles" only supports "no
    gaps observed," not "no delivered samples were lost" -- state that
    distinction explicitly in any write-up that includes Corsano."""
    # errors="replace": same reasoning as _btmon_watch_for_handle -- a real
    # capture's raw payload bytes can be invalid UTF-8, and the default
    # strict decoding crashed do_parse entirely on a real session
    # (2026-09-24, UnicodeDecodeError surfacing as a misleading "Config
    # error" since it's a ValueError subclass). parse_btmon_text only
    # matches plain-ASCII patterns, so a replaced byte elsewhere doesn't
    # affect parsing -- losing the whole session's results.csv would be far
    # worse than a mangled byte in text that isn't parsed anyway.
    result = subprocess.run(
        ["btmon", "-r", str(capture_path)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return parse_btmon_text(result.stdout, mac_address=mac_address)


def evaluate_run_result(parsed, row_label, condition, planned_duration_s):
    """Decide the real PASSED/FAILED verdict for one run from its parsed
    capture. A driver process staying alive says nothing about whether the
    BLE link itself stayed up, so this is the single source of truth for
    that verdict -- shared between do_run's immediate post-run check (so a
    real failure surfaces before the operator walks away) and do_parse's
    end-of-session batch summary, so the two never drift apart.

    Returns (status, error, reason_kind), where reason_kind is None on a
    pass, "baseline" for a baseline-stability failure, or "connectivity" for
    the generic connected-time failure -- callers that need to distinguish
    the two (e.g. for log message wording) use this instead of re-deriving
    the same logic themselves.

    Note: for Row 2/3/5, do_run no longer retries or gates the session on
    this verdict (see retry_on_fail in do_run) -- a FAILED here is just a
    data annotation on that attempt, not something that triggers a retry or
    aborts the session. It still matters for Row 1, where retry+abort-on-
    failure remains the behavior (a Row 1 failure means the setup is
    broken, not that interference is doing its job).

    PASS/FAIL means "ran as expected for this condition," not simply
    "stayed connected" -- added 2026-09-24 per an explicit request: now
    that several sessions have repeatedly confirmed what Row 3's forced-
    channel-with-load conditions actually do (degrade or collapse, see
    EXPECTED_INSTABILITY_ROW_CONDITIONS), marking that as FAILED just
    because MIN_CONNECTED_FRACTION wasn't met mislabels the intended
    result as an error. For those conditions specifically, this checks
    only whether the run actually engaged with the link at all (some
    connected time, or at least a real connection attempt) -- not how
    little of the planned duration it stayed up. A run with truly nothing
    (0 connected time AND 0 attempts) still fails: that's not "the
    mechanism worked," it's "the test never ran," matching the
    manuscript's own reasoning for excluding exactly that case."""
    try:
        planned_s = float(planned_duration_s)
    except (TypeError, ValueError):
        planned_s = None

    is_expected_instability = (row_label, condition) in EXPECTED_INSTABILITY_ROW_CONDITIONS
    if is_expected_instability:
        engaged = (
            parsed["connected_duration_s"] > 0
            or parsed["num_failed_connection_attempts"] > 0
        )
        if engaged:
            return "PASSED", "", None
        return (
            "FAILED",
            (
                "never connected and no connection attempt was recorded -- "
                "not a valid instance of the expected degradation/collapse "
                "(nothing to measure), needs a rerun rather than counting "
                "as this condition's result"
            ),
            "connectivity",
        )

    connected_ok = (
        planned_s is None
        or parsed["connected_duration_s"] >= MIN_CONNECTED_FRACTION * planned_s
    )
    is_baseline = (row_label, condition) in BASELINE_ROW_CONDITIONS
    baseline_ok = not is_baseline or parsed["unexpected_disconnect_count"] == 0

    if connected_ok and baseline_ok:
        return "PASSED", "", None
    if not baseline_ok:
        return (
            "FAILED",
            (
                f"{parsed['unexpected_disconnect_count']} unexpected "
                f"disconnect(s) during baseline (excludes the expected "
                f"end-of-run teardown) -- expected a stable connection "
                f"with no interference present"
            ),
            "baseline",
        )
    return (
        "FAILED",
        (
            f"BLE link only connected {parsed['connected_duration_s']:.1f}s of "
            f"{planned_s:.0f}s planned ({parsed['disconnect_count']} disconnect(s))"
        ),
        "connectivity",
    )


def do_parse(cfg, session_dir, session_ts):
    """Parse every capture recorded in this session's own manifest.csv into
    this session's own results.csv. Each session directory is self-contained,
    so every manifest row here belongs to this one session -- no need to
    filter by session_id."""
    manifest = manifest_path(session_dir)
    if not manifest.exists():
        log.warning(
            f"No manifest.csv found in {session_dir} — run some sessions first."
        )
        return

    out_path = results_path(cfg, session_dir)
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"=== Parsing session {session_ts} captures into {out_path} ===")
    forced_channel_signatures = _known_forced_channel_hex_signatures(cfg)

    with open(manifest) as f:
        rows = list(csv.DictReader(f))

    # Fallback for manifests recorded before the mac_address column existed
    # -- look it up from the current config by device name instead, so an
    # old session can still be re-parsed with correct per-device filtering.
    mac_by_device_name = {d["name"]: d.get("mac_address") for d in cfg["devices"]}

    fieldnames = [
        "session_id",
        "row",
        "device",
        "condition",
        "replicate",
        "capture_file",
        "status",
        "error",
        "generated_at",
        "duration_s",
        "initial_interval_ms",
        "initial_latency_ms",
        "initial_timeout_ms",
        "num_updates",
        "update_intervals",
        "mtu",
        "num_failed_connection_attempts",
        "disconnect_count",
        "unexpected_disconnect_count",
        "connected_duration_s",
        "capture_duration_s",
        "induced_load_mbps",
        "rx_sample_count",
        "jitter_p50_ms",
        "jitter_p95_ms",
        "jitter_p99_ms",
        "jitter_p999_ms",
        "sniffer_adv_packet_count",
        "sniffer_adv_crc_error_count",
        "sniffer_adv_crc_error_rate",
        "sniffer_adv_rssi_avg_dbm",
        "sniffer_adv_rssi_min_dbm",
        "sniffer_adv_rssi_max_dbm",
        "packet_loss_pct",
        "iperf_jitter_ms",
        "qbss_channel_utilization_pct",
        "connection_established",
        "retry_on_fail",
        "acc_pmd_frame_count",
        "acc_samples_received",
        "acc_samples_lost",
        "acc_completeness_pct",
        "hr_rr_total_s",
        "hr_rr_completeness_pct",
        "corsano_acc_packet_count",
        "corsano_acc_samples_lost",
        "corsano_acc_completeness_pct",
        "corsano_activity_record_count",
        "corsano_activity_gap_s_max",
        "corsano_stress_record_count",
        "corsano_stress_gap_s_max",
        "corsano_bioz_bytes_downloaded",
        "corsano_bioz_file_growth_bytes",
        "corsano_bioz_completeness_pct",
    ]

    num_passed = 0
    num_failed = 0
    session_results = []  # (label, status, reason) for every row in this session
    # Connection-establishment success rate per (row, device, condition):
    # distinguishes "never connected" from "connected, then dropped later" --
    # see connection_established in _attempt_run. [established_count, total]
    establishment_stats = {}

    def _track_establishment(row):
        key = (row["row"], row["device"], row["condition"])
        stats = establishment_stats.setdefault(key, [0, 0])
        stats[1] += 1
        if str(row.get("connection_established", "")).strip().lower() in ("true", "1"):
            stats[0] += 1

    def _label(row):
        return f"{row['row']} | {row['device']} | {row['condition']} | rep{row['replicate']}"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            status = row.get("status", "PASSED")  # older manifests predate this column
            _track_establishment(row)
            if status == "FAILED":
                num_failed += 1
                writer.writerow(
                    {
                        "session_id": row.get("session_id", ""),
                        "row": row["row"],
                        "device": row["device"],
                        "condition": row["condition"],
                        "replicate": row["replicate"],
                        "capture_file": row["capture_file"],
                        "status": status,
                        "error": row.get("error", ""),
                        "generated_at": generated_at,
                        "duration_s": row.get("duration_s", ""),
                        "connection_established": row.get("connection_established", ""),
                        "retry_on_fail": row.get("retry_on_fail", ""),
                    }
                )
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- "
                    f"{row.get('error', '')}, skipping parse"
                )
                session_results.append((_label(row), "FAILED", row.get("error", "")))
                continue

            capture_path = session_dir / row["capture_file"]
            if not capture_path.exists():
                log.warning(f"  ! missing capture file: {capture_path}, skipping")
                num_failed += 1
                session_results.append((_label(row), "FAILED", "capture file missing"))
                continue
            mac_address = row.get("mac_address") or mac_by_device_name.get(
                row["device"]
            )
            parsed = parse_capture_file(capture_path, mac_address=mac_address)
            sniffer_pcap_file = row.get("sniffer_pcap_file")
            sniffer_metrics = parse_sniffer_pcap(
                session_dir / sniffer_pcap_file if sniffer_pcap_file else None,
                mac_address,
            )
            planned_duration_s = row.get("duration_s", "")

            # The driver process staying alive (status == "PASSED" here) says
            # nothing about whether the underlying BLE link actually stayed
            # up -- so the real pass/fail verdict is decided here, from the
            # parsed capture, and can downgrade a live-process run to FAILED.
            final_status, final_error, failure_kind = evaluate_run_result(
                parsed, row["row"], row["condition"], planned_duration_s
            )

            # Channel-map contamination check: only for rows that never
            # force a classification themselves (see FORCED_CHANNEL_ROWS) --
            # a leaked forced map here invalidates the run regardless of
            # what evaluate_run_result decided from the link metrics alone.
            if row["row"] not in FORCED_CHANNEL_ROWS:
                afh_csv_path = capture_path.with_name(
                    capture_path.stem + "_afh.csv"
                )
                contamination = check_channel_map_contamination(
                    afh_csv_path, forced_channel_signatures
                )
                if contamination:
                    final_status, final_error, failure_kind = (
                        "FAILED",
                        contamination,
                        "channel_map_contamination",
                    )
                    log.error(
                        f"  ! {row['capture_file']}: EXCLUDED -- {contamination}"
                    )
            link_ok = final_status == "PASSED"

            out_row = {
                "session_id": row.get("session_id", ""),
                "row": row["row"],
                "device": row["device"],
                "condition": row["condition"],
                "replicate": row["replicate"],
                "capture_file": row["capture_file"],
                "status": final_status,
                "error": final_error,
                "generated_at": generated_at,
                "duration_s": planned_duration_s,
                "induced_load_mbps": row.get("induced_load_mbps", ""),
                "packet_loss_pct": row.get("packet_loss_pct", ""),
                "iperf_jitter_ms": row.get("iperf_jitter_ms", ""),
                "qbss_channel_utilization_pct": row.get(
                    "qbss_channel_utilization_pct", ""
                ),
                "connection_established": row.get("connection_established", ""),
                "retry_on_fail": row.get("retry_on_fail", ""),
                **parsed,
                **sniffer_metrics,
            }
            writer.writerow(out_row)
            if link_ok:
                num_passed += 1
            else:
                num_failed += 1
            log.info(
                f"  parsed {row['capture_file']}: "
                f"interval={parsed['initial_interval_ms']}ms, "
                f"updates={parsed['num_updates']}, mtu={parsed['mtu']}, "
                f"connected={parsed['connected_duration_s']}s, "
                f"disconnects={parsed['disconnect_count']} "
                f"({parsed['unexpected_disconnect_count']} unexpected), "
                f"failed_attempts={parsed['num_failed_connection_attempts']}"
            )
            log.info(
                f"     RX inter-arrival jitter (n={parsed['rx_sample_count']}): "
                f"p50={parsed['jitter_p50_ms']}ms p95={parsed['jitter_p95_ms']}ms "
                f"p99={parsed['jitter_p99_ms']}ms p99.9={parsed['jitter_p999_ms']}ms"
            )
            if parsed["rx_sample_count"] and parsed["rx_sample_count"] < 1000:
                log.warning(
                    f"     only {parsed['rx_sample_count']} RX samples -- p99.9 "
                    f"is not statistically reliable with this few, treat as "
                    f"indicative only"
                )
            if parsed["acc_pmd_frame_count"]:
                log.info(
                    f"     Polar ACC sample completeness: "
                    f"{parsed['acc_samples_received']} received, "
                    f"{parsed['acc_samples_lost']} lost "
                    f"({parsed['acc_completeness_pct']}%)"
                )
            if parsed["hr_rr_total_s"] is not None:
                log.info(
                    f"     Polar HR/RR completeness (approximate): "
                    f"{parsed['hr_rr_completeness_pct']}%"
                )
            if parsed["corsano_acc_packet_count"] is not None:
                log.info(
                    f"     Corsano ACC completeness: "
                    f"{parsed['corsano_acc_completeness_pct']}%"
                )
            if parsed["corsano_bioz_completeness_pct"] is not None:
                log.info(
                    f"     Corsano BioZ completeness: "
                    f"{parsed['corsano_bioz_completeness_pct']}%"
                )
            if failure_kind == "baseline":
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- baseline stability "
                    f"check: {final_error}"
                )
            elif failure_kind == "connectivity":
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- {final_error} -- "
                    f"the BLE link did not stay up for the full run"
                )
            session_results.append((_label(row), final_status, final_error or None))

    log.info(f"Saved results to {out_path}")
    log.info(f"Summary: {num_passed} passed, {num_failed} failed")

    if session_results:
        log.info(f"=== Session {session_ts} results ===")
        session_passed = sum(
            1 for _, status, _ in session_results if status == "PASSED"
        )
        session_failed = len(session_results) - session_passed
        for label, status, reason in session_results:
            if reason:
                log.info(f"  RESULT: {status} -- {label} -- {reason}")
            else:
                log.info(f"  RESULT: {status} -- {label}")
        log.info(
            f"=== Session summary: {session_passed} passed, {session_failed} failed ==="
        )
        if establishment_stats:
            log.info("=== Connection-establishment success rate ===")
            for (row_label, device, condition), (established, total) in sorted(
                establishment_stats.items()
            ):
                pct = 100 * established / total if total else 0.0
                log.info(
                    f"  {row_label} | {device} | {condition}: "
                    f"{established}/{total} established ({pct:.0f}%)"
                )
