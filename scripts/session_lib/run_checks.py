"""Run verdict and data-quality checks: pass thresholds, baseline/instability
condition sets, rosbag/battery/channel-map checks and evaluate_run_result."""

import csv
import subprocess
from pathlib import Path

import yaml

from .ble_radio import (
    FORCED_CHANNEL_RANGE,
    _encode_channel_classification,
)
from .logs import log


# A run only counts as a real PASSED if the BLE link stayed up for at least
# this fraction of its planned duration -- the driver process staying alive
# is not sufficient (see do_parse).
MIN_CONNECTED_FRACTION = 0.9

# A run with injected WiFi load only counts if iperf3's own end-of-run
# average reached at least this fraction of its target bitrate. iperf3
# reports one average, so a load that stops partway shows up as a
# proportionally low
# value -- or, when ssh/iperf3 dies, as no parseable output at all.
MIN_INDUCED_LOAD_FRACTION = 0.8

# Conditions with no injected interference (no iperf3 traffic, no generic
# BLE peripherals), so once the sensor connects it's expected to stay
# connected for the rest of the run: any disconnect there is labelled
# directly, not just checked against MIN_CONNECTED_FRACTION.
#
# Row 1's two sub-runs (see run_test1_connection_interval) are both
# no-interference; so are the 0%-duty conditions of Rows 2/5 and the
# forced-channel runs without load. Rows: 3 = forced_channel,
# 4 = endurance, 5 = headroom_sweep, 6 = contention (see config.yaml).
#
# For rows with retry_on_fail=False, this set only adds a stricter label;
# for Row 1 (retry_on_fail=True) a failure is retried and can abort the
# session.
BASELINE_ROW_CONDITIONS = {
    ("row1", "calibration"),
    ("row1", "renegotiation_watch"),
    ("row2", "duty0pct"),
    ("row2", "duty0pct_bracket"),
    ("row3", "forced_channel_no_load"),
    ("row3", "forced_channel_no_load_bracket"),
    ("row4", "duty0pct_long"),
    ("row6", "contention_0"),
    ("row6", "contention_0_bracket"),
    # Row 5 headroom sweep's no-load control (one small mask, no WiFi load),
    # at config.yaml's row5_headroom_sweep.no_load_control_k (k=2).
    ("row5", "headroom_k2_no_load"),
}

# Conditions where WiFi load is combined with a restricted channel map
# (forced_channels, see FORCED_CHANNEL_RANGE): the link may degrade or drop,
# and that is the result, not a failure. See evaluate_run_result: a run
# that never engaged with the link (0 connected time AND 0 attempts) still
# fails.
#
# Row 2's loaded conditions are not included: AFH-free links are expected
# to stay up, so a disconnect there is flagged.
EXPECTED_INSTABILITY_ROW_CONDITIONS = {
    ("row3", "forced_channel_duty25pct"),
    ("row3", "forced_channel_duty50pct"),
    ("row3", "forced_channel_duty75pct"),
    ("row3", "forced_channel_max_load"),
    # Row 5 headroom sweep's WiFi-loaded conditions (k=0/2/4/8 clean
    # channels, all at load_duty_pct% load), for every k.
    ("row5", "headroom_k0_duty75pct"),
    ("row5", "headroom_k2_duty75pct"),
    ("row5", "headroom_k4_duty75pct"),
    ("row5", "headroom_k8_duty75pct"),
    # Row 8: Row 5's sweep at 100% load (row8_headroom_saturation.k_values).
    ("row8", "headroom_k2_duty100pct"),
    ("row8", "headroom_k8_duty100pct"),
    # Row 7's scan-interference diagnostic: Row 3's forced_channel_duty50pct
    # setup with the QBSS scanner toggled between reps.
    ("row7", "scan_on"),
    ("row7", "scan_off"),
}

# Rows whose EVERY condition (including no-load ones) deliberately forces a
# host channel classification -- see FORCED_CHANNEL_RANGE / channel_masks.
# Captures of all other rows must never show a forced bitmap (leak
# detection). Row 7 uses Row 3's FORCED_CHANNEL_RANGE for both conditions.
FORCED_CHANNEL_ROWS = {"row3", "row5", "row7", "row8"}


def _known_forced_channel_hex_signatures(cfg):
    """Exact 5-byte channel-map hex signatures of every host-forced
    classification this codebase ever sets: Row 3's fixed FORCED_CHANNEL_
    RANGE, plus every k in Row 5's config-driven channel_masks. Contamination
    is detected by the exact bitmap, not the channel count: AFH under heavy
    WiFi load can narrow to a similar count on its own."""
    sigs = {_encode_channel_classification(FORCED_CHANNEL_RANGE).hex()}
    for channels in (cfg.get("channel_masks") or {}).values():
        sigs.add(_encode_channel_classification(channels).hex())
    return sigs


EMPTY_CONNECTION_RSSI = {
    "connection_rssi_sample_count": 0,
    "connection_rssi_avg_dbm": None,
    "connection_rssi_min_dbm": None,
    "connection_rssi_max_dbm": None,
}


HCI_RSSI_MIN_DBM = -127
HCI_RSSI_MAX_DBM = 20


def aggregate_connection_rssi(afh_csv_path):
    """Summarize the connection RSSI already sampled every poll_interval_s
    by the AFH poller's _hcitool_read_rssi call (HCI Read RSSI on this
    run's own live connection handle -- see _afh_poll_loop's rssi_dbm
    column) into avg/min/max for results.csv: the central's own view of the
    link on the data channels, unlike sniffer_adv_rssi_* (the sniffer's
    view of advertising packets). Returns EMPTY_CONNECTION_RSSI if the file
    is missing/unreadable or has
    no valid samples (e.g. the connection never came up, or hcitool
    wasn't available -- see _hcitool_read_rssi)."""
    if not afh_csv_path.exists():
        return dict(EMPTY_CONNECTION_RSSI)
    try:
        with open(afh_csv_path) as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return dict(EMPTY_CONNECTION_RSSI)
    values = []
    for row in rows:
        raw = (row.get("rssi_dbm") or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # HCI Read RSSI returns 127 ("not available") while the link is down;
        # valid readings are -127..+20 dBm.
        if not HCI_RSSI_MIN_DBM <= value <= HCI_RSSI_MAX_DBM:
            continue
        values.append(value)
    if not values:
        return dict(EMPTY_CONNECTION_RSSI)
    return {
        "connection_rssi_sample_count": len(values),
        "connection_rssi_avg_dbm": round(sum(values) / len(values), 1),
        "connection_rssi_min_dbm": min(values),
        "connection_rssi_max_dbm": max(values),
    }


def driver_git_version(driver_source_dir):
    """Best-effort 'driver version' for results.csv: the driver package's
    own git commit hash (short), +dirty appended if that source tree has
    uncommitted local changes (package.xml's <version> is a "0.0.0"
    placeholder). Returns
    None if driver_source_dir isn't configured, doesn't exist, or isn't
    inside a git repo -- never raises."""
    if not driver_source_dir or not Path(driver_source_dir).is_dir():
        return None
    try:
        commit = subprocess.run(
            ["git", "-C", str(driver_source_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if commit.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "-C", str(driver_source_dir), "status", "--porcelain", "--", "."],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    dirty = "+dirty" if status.returncode == 0 and status.stdout.strip() else ""
    return commit.stdout.strip() + dirty


def rosbag_topic_counts(rosbag_dir):
    """{topic: message_count} from a run's rosbag metadata.yaml (written when
    the recorder stops), or None if it's missing/unreadable."""
    meta = Path(rosbag_dir) / "metadata.yaml" if rosbag_dir else None
    if not meta or not meta.is_file():
        return None
    try:
        info = yaml.safe_load(meta.read_text())["rosbag2_bagfile_information"]
        return {
            t["topic_metadata"]["name"]: int(t["message_count"])
            for t in info.get("topics_with_message_count") or []
        }
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return None


def check_data_published(device_cfg, rosbag_dir, parsed):
    """Problems with the sensor data a run's driver published, as a list of
    strings (empty = fine), or None if it couldn't be checked (no rosbag).

    A link that stays up says nothing about whether the device was
    measuring, so two checks: each topic in the device's config.yaml
    expected_topics must have >= 1 message, and for the Corsano the watch's
    own file counters must have grown (parsed["corsano_new_sensor_data"]),
    which catches a driver republishing stale data.

    advisory_topics are checked the same way but only logged as a warning
    (e.g. the Polar's polar_hr, published only with skin contact)."""
    expected = (device_cfg or {}).get("expected_topics") or []
    advisory = (device_cfg or {}).get("advisory_topics") or []
    problems = []
    counts = rosbag_topic_counts(rosbag_dir)
    if (expected or advisory) and counts is None:
        return None
    for topic in expected:
        full = topic if topic.startswith("/") else f"/{topic}"
        if counts.get(full, 0) == 0:
            problems.append(f"no messages on {full}")
    for topic in advisory:
        full = topic if topic.startswith("/") else f"/{topic}"
        if counts.get(full, 0) == 0:
            log.warning(f"     (advisory) no messages on {full} -- not failing the run")
    if parsed.get("corsano_new_sensor_data") is False:
        problems.append(
            "the watch recorded no new ACC/BioZ/stress data (its files didn't grow)"
        )
    return problems


def extract_battery_pct_from_rosbag(rosbag_dir, topic):
    """Read the LAST sensor_msgs/BatteryState message on `topic` from this
    run's own rosbag (see start_rosbag_recording -- rosbag2's mcap storage
    on this ROS Jazzy setup) and return its percentage (0-100, 1 decimal),
    or None if the bag/topic doesn't exist or has no messages on it.
    Never raises: gracefully returns None if rosbag2_py/rclpy/sensor_msgs
    aren't importable (ROS environment not sourced) too.

    A missing topic is often expected: the drivers publish a battery topic
    only with --publish-battery (see config.yaml's devices[].battery_topic)."""
    if not rosbag_dir or not topic or not Path(rosbag_dir).is_dir():
        return None
    try:
        from rosbag2_py import ConverterOptions, SequentialReader, StorageFilter, StorageOptions
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import BatteryState
    except ImportError:
        return None
    full_topic = topic if topic.startswith("/") else f"/{topic}"
    reader = SequentialReader()
    try:
        reader.open(
            StorageOptions(uri=str(rosbag_dir), storage_id="mcap"),
            ConverterOptions("", ""),
        )
    except RuntimeError:
        return None
    topic_types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    if topic_types.get(full_topic) != "sensor_msgs/msg/BatteryState":
        return None
    reader.set_filter(StorageFilter(topics=[full_topic]))
    last_pct = None
    while reader.has_next():
        _, data, _ = reader.read_next()
        last_pct = deserialize_message(data, BatteryState).percentage
    return round(last_pct * 100, 1) if last_pct is not None else None


def check_channel_map_contamination(afh_csv_path, forced_signatures):
    """Read one run's AFH CSV and check whether it ever shows a channel-map
    hex EXACTLY matching a known host-forced classification, for a run that
    was never supposed to force one. Returns a description string if found,
    else None. Never raises -- a missing/unreadable file just means
    nothing to check.

    The host classification is persistent controller state, so an
    interrupted forced-channel run can leave it set for later runs. This is
    the detection side; _attempt_run and chamber_session.py's forced-channel
    rows reset it."""
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


def evaluate_run_result(parsed, row_label, condition, planned_duration_s, data_problems=None):
    """Decide the real PASSED/FAILED verdict for one run from its parsed
    capture: shared by do_run's immediate post-run check and do_parse's
    end-of-session summary.

    Returns (status, error, reason_kind), where reason_kind is None on a
    pass, "baseline" for a baseline-stability failure, or "connectivity" for
    the generic connected-time failure.

    For rows with retry_on_fail=False a FAILED is only an annotation of
    that attempt; for Row 1 it is retried and can abort the session.

    PASS/FAIL means "ran as expected for this condition", not "stayed
    connected": for EXPECTED_INSTABILITY_ROW_CONDITIONS this checks only
    that the run engaged with the link (some connected time, or a
    connection attempt). A run with 0 connected time AND 0 attempts fails."""
    try:
        planned_s = float(planned_duration_s)
    except (TypeError, ValueError):
        planned_s = None

    # Checked before anything else, including the expected-instability rows.
    # The signature is encryption attempts with NOT ONE success (interference
    # also kills some handshakes): a host bond the sensor has forgotten never
    # encrypts at all. See START_ENCRYPTION_RE.
    if parsed.get("n_encryption_attempts") and not parsed.get("n_encryption_successes"):
        return (
            "FAILED",
            (
                f"{parsed['n_encryption_failures']} failed encryption "
                f"handshake(s) and not one success -- this host holds a bond "
                f"the sensor no longer "
                f"honours, so every connection dies at its supervision "
                f"timeout and is recorded as a reason-0x08 drop. Clear it "
                f"with `bluetoothctl remove <mac>` and rerun; the drivers do "
                f"not need a bond"
            ),
            "pairing",
        )

    is_expected_instability = (row_label, condition) in EXPECTED_INSTABILITY_ROW_CONDITIONS
    if is_expected_instability:
        engaged = (
            parsed["connected_duration_s"] > 0
            or parsed["n_reason_establishment_failed"] > 0
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

    # Baselines must also show the device measuring, not just connected --
    # see check_data_published. Not applied to interference conditions,
    # where missing data can be the effect being measured.
    if connected_ok and baseline_ok and is_baseline and data_problems:
        return (
            "FAILED",
            (
                "baseline sensor data not published -- "
                + "; ".join(data_problems)
                + " (is the device worn with skin contact?)"
            ),
            "no_data",
        )
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
