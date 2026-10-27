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
# average reached at least this fraction of its target bitrate (runs that
# held their load reached 99-100% on 2026-09-26/30). iperf3 reports one
# average, so a load that stops partway shows up as a proportionally low
# value -- or, when ssh/iperf3 dies, as no parseable output at all.
MIN_INDUCED_LOAD_FRACTION = 0.8

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
# Row labels below were re-fixed 2026-09-24, then swapped again 2026-09-25
# when headroom sweep and contention traded row numbers (headroom sweep is
# now Row 5 and runs by default under --row all; contention is now Row 6
# and stays an opt-in extra -- see run_test5_headroom_sweep's docstring in
# chamber_session.py). Today's layout: Row 3 = forced_channel, Row 4 =
# endurance, Row 5 = headroom_sweep, Row 6 = contention (see config.yaml).
# Also added forced_channel_no_load, which belongs here and was simply
# missing: zero WiFi load is zero WiFi load regardless of the forced
# channel map, and the manuscript's own finding for it is "streamed for
# the full duration in all three runs" -- a real baseline, not covered by
# EXPECTED_INSTABILITY_ROW_CONDITIONS below.
#
# For Row 2/3/6 (retry_on_fail=False), landing in this set only adds a
# stricter data LABEL -- it can no longer trigger a retry or abort the
# session (see do_run's retry_on_fail). For Row 1 (retry_on_fail=True), it
# still drives the original retry+abort-on-failure behavior.
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
    # Row 5 headroom sweep's no-load control (one small mask, no WiFi load)
    # -- same "a small mask alone shouldn't destabilize anything" reasoning
    # as forced_channel_no_load above, just at whichever k config.yaml's
    # row5_headroom_sweep.no_load_control_k picks. Only k=2 exists today;
    # add the corresponding condition name here if that config value changes.
    ("row5", "headroom_k2_no_load"),
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
    # Row 5 headroom sweep's WiFi-loaded conditions (k=0/2/4/8 clean
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
    ("row5", "headroom_k0_duty75pct"),
    ("row5", "headroom_k2_duty75pct"),
    ("row5", "headroom_k4_duty75pct"),
    ("row5", "headroom_k8_duty75pct"),
    # Row 8: Row 5's sweep at 100% load (added 2026-10-06) -- same
    # reasoning. Add a line here if row8_headroom_saturation.k_values grows.
    ("row8", "headroom_k2_duty100pct"),
    ("row8", "headroom_k8_duty100pct"),
    # Row 7's scan-interference diagnostic -- same forced_channel_duty50pct
    # setup as Row 3, just with the QBSS scanner toggled between reps. Both
    # conditions force channels + load, so both need this classification --
    # if scan_on turns out to be the actual cause of Row 3's collapses, its
    # disconnects here are the confirming result, not a test failure.
    ("row7", "scan_on"),
    ("row7", "scan_off"),
}

# Rows whose EVERY condition (including no-load ones) deliberately forces a
# host channel classification -- see FORCED_CHANNEL_RANGE / channel_masks.
# Used below to know which rows' captures should NEVER show a forced
# bitmap, so a leak can be detected instead of just prevented. Row 7 reuses
# Row 3's exact FORCED_CHANNEL_RANGE bitmap for both its conditions, so it
# belongs here too -- otherwise every Row 7 run would be misflagged as
# channel-map contamination for legitimately showing the forced map it
# itself set.
FORCED_CHANNEL_ROWS = {"row3", "row5", "row7", "row8"}


def _known_forced_channel_hex_signatures(cfg):
    """Exact 5-byte channel-map hex signatures of every host-forced
    classification this codebase ever sets: Row 3's fixed FORCED_CHANNEL_
    RANGE, plus every k in Row 5's config-driven channel_masks. Used to
    detect contamination precisely -- matching the EXACT bitmap, not just
    a similarly narrow channel COUNT, since real AFH under heavy WiFi load
    can legitimately narrow to a similar count on its own (confirmed: ~15
    channels for Corsano at 100% duty, AFH-free) without ever being
    host-forced. The exact bitmap is what a leaked host classification
    actually produces and what real adaptive narrowing essentially never
    would, since it doesn't follow an arbitrary spread-out pattern like
    Row 5's k=2/4/8 masks."""
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
    column) into avg/min/max for results.csv. This is the CENTRAL's own
    view of the link (this controller's receiver, on the actual data
    channel), as opposed to sniffer_adv_rssi_*, which is the Nordic
    sniffer's OTA view of advertising-channel packets only -- the two
    aren't interchangeable, and before this only the sniffer one made it
    into results.csv at all, even though the central-side number was
    already being collected into every run's own _afh.csv the whole time.
    Returns EMPTY_CONNECTION_RSSI if the file is missing/unreadable or has
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
        # valid readings are -127..+20 dBm. Averaging the sentinel in gave
        # 20261006_000828 row3 Polar forced_channel_max_load rep2 +26.6 dBm.
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
    uncommitted local changes. package.xml's <version> in both ros4hc
    driver packages is an unmaintained "0.0.0" placeholder (confirmed
    2026-09-28), so it carries no real information -- the git state of the
    actual source tree a run was launched against is the only thing that
    really identifies which driver code produced that run's data. Returns
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

    A link that stays up says nothing about whether the device was actually
    measuring: on 2026-09-30/10-01 every baseline passed while the Polar sent
    0 heart-rate values (no skin contact) and the Corsano's ACC/BioZ/stress
    files hadn't grown for days -- the old Corsano driver republished the
    same stale tail every poll, so its topics still had ~75 messages per run.
    So two checks: each topic in the device's config.yaml expected_topics
    must have >= 1 message, and for the Corsano the watch's own file
    counters must have grown (parsed["corsano_new_sensor_data"]), which
    catches stale republishing whatever driver version is installed.

    advisory_topics are checked the same way but only logged as a warning,
    never a problem: the Polar's polar_hr (published only with skin contact)
    moved there 2026-10-06, because in the chamber the strap isn't on skin
    and HR then failed every baseline, though no result uses HR -- ECG/ACC
    (still required) show data flow, and completeness comes from them."""
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

    A missing topic here is often EXPECTED, not a failure -- the ros4hc
    drivers only publish a battery topic when their own --publish-battery
    is enabled (see config.yaml's devices[].battery_topic comments: on by
    default for Corsano, explicitly disabled for Polar_H10 in this
    project's current launch_cmd)."""
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


def evaluate_run_result(parsed, row_label, condition, planned_duration_s, data_problems=None):
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

    # Checked before anything else, including the expected-instability rows:
    # The signature is attempts with NOT ONE success: interference kills some
    # handshakes mid-flight too (09-28 max_load rep1: 1 success, 3 killed by RF
    # with Status "Connection Timeout"), so "any failure" would condemn exactly
    # the genuine degradation runs this session exists to capture. A host bond
    # the sensor has forgotten never encrypts at all. See START_ENCRYPTION_RE.
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
