"""btmon capture parsing: startup/shutdown grace windows, RX jitter, and
parse_btmon_text / parse_capture_file."""

import bisect
import subprocess

from .btmon_patterns import (
    ACL_RX_RE,
    ACL_TX_RE,
    CONN_COMPLETE_RE,
    CONN_UPDATE_RE,
    DISCONNECT_RE,
    ENCRYPTION_STATE_RE,
    HANDLE_ADDRESS_RE,
    HANDLE_RE,
    HEADER_TS_RE,
    INTERVAL_RE,
    LATENCY_RE,
    MTU_RE,
    NOTIFICATION_DATA_RE,
    NOTIFICATION_RE,
    NOTIFICATION_TYPE_RE,
    PEER_ADDRESS_RE,
    REASON_RE,
    START_ENCRYPTION_RE,
    STATUS_RE,
    TIMEOUT_RE,
)
from .polar import (
    ATT_ATTRIBUTE_HANDLE_RE,
    POLAR_HR_ATTRIBUTE_HANDLE,
    POLAR_HR_TYPE_MARKER,
    POLAR_PMD_ATTRIBUTE_HANDLE,
    POLAR_PMD_TYPE_MARKER,
    _first_ecg_frame_host_ts,
    compute_acc_completeness,
    compute_ecg_completeness,
    compute_hr_completeness,
)
from .corsano import compute_corsano_completeness


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


def parse_btmon_text(text, mac_address=None, planned_duration_s=None):
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
    pmd_hex_frames = []  # list of (handle, hex_data, host_ts_s) -- Polar PMD ACC/ECG
    # notifications. host_ts_s (btmon's own capture-relative clock, same
    # domain as connection-complete events' timestamps) is carried
    # alongside each frame so the real, per-run delay between "connected"
    # and "PMD data actually started arriving" can be measured directly
    # from this capture -- not guessed/assumed. This is deliberately NOT
    # the sensor's own embedded per-sample timestamp used for completeness
    # (a different, device-side clock, only meaningful relative to other
    # PMD frames) -- see _first_pmd_frame_host_ts.
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
            # "LE Connection Update Complete" reports the peer as
            # "Handle: 2048 Address: AA:BB:..." (HANDLE_ADDRESS_RE), NOT
            # "Peer address: ..." (PEER_ADDRESS_RE) -- that's a Connection
            # Complete-only field. Without this fallback, every update
            # event's peer_address came back None and got silently dropped
            # by the expected_mac filter below, so a later interval change
            # (e.g. 60ms -> 45ms mid-run) never showed up in num_updates/
            # update_intervals even though it's right there in the capture.
            peer_m = PEER_ADDRESS_RE.search(window) or HANDLE_ADDRESS_RE.search(window)
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
            reason_m = REASON_RE.search(window)
            disconnects.append(
                {
                    "timestamp_s": current_ts,
                    "address": addr_m.group(1).upper() if addr_m else None,
                    "line_idx": i,
                    "reason_hex": reason_m.group(2).lower() if reason_m else None,
                    "reason_name": reason_m.group(1) if reason_m else None,
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
            if data_m:
                type_str = type_m.group(1).lower() if type_m else ""
                att_handle_m = ATT_ATTRIBUTE_HANDLE_RE.search(window)
                att_handle = att_handle_m.group(1).lower() if att_handle_m else None
                is_pmd = POLAR_PMD_TYPE_MARKER in type_str or att_handle == POLAR_PMD_ATTRIBUTE_HANDLE
                is_hr = POLAR_HR_TYPE_MARKER in type_str or att_handle == POLAR_HR_ATTRIBUTE_HANDLE
                if is_pmd:
                    pmd_hex_frames.append((current_acl_handle, data_m.group(1), current_ts))
                elif is_hr:
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
    scoped_pmd = [
        (h, ts) for handle, h, ts in pmd_hex_frames if handle in connected_handles
    ]
    scoped_pmd_hex = [h for h, ts in scoped_pmd]
    # Which of this device's connections each PMD frame arrived on, so the
    # in-stream loss never pairs frames across a reconnect (the H10 restarts
    # its sample clock there -- see polar._same_connection_pairs).
    conn_starts = sorted(
        e["timestamp_s"]
        for e in successes
        if e["type"] == "complete" and e["timestamp_s"] is not None
    )
    pmd_connection_ids = [
        bisect.bisect_right(conn_starts, ts) if ts is not None else 0
        for h, ts in scoped_pmd
    ]
    scoped_hr_hex = [h for handle, h in hr_hex_frames if handle in connected_handles]
    # Completeness is measured against the planned duration, so count only
    # frames that arrived inside it (from this device's first connection).
    # When the H10's PMD stream starts in ~3.5 s instead of ~32 s, it keeps
    # streaming past the planned end until teardown, and those extra frames
    # pushed 20261006_000828 row5 headroom_k4_duty75pct rep3 to 100.87%.
    window_pmd_hex, window_connection_ids = scoped_pmd_hex, pmd_connection_ids
    try:
        planned_s = float(planned_duration_s)
    except (TypeError, ValueError):
        planned_s = None
    if planned_s and planned_s > 0 and conn_starts:
        window_end = conn_starts[0] + planned_s
        in_window = [
            ts is None or ts <= window_end for h, ts in scoped_pmd
        ]
        window_pmd_hex = [h for h, keep in zip(scoped_pmd_hex, in_window) if keep]
        window_connection_ids = [
            c for c, keep in zip(pmd_connection_ids, in_window) if keep
        ]
    acc_completeness = compute_acc_completeness(
        window_pmd_hex, planned_duration_s, window_connection_ids
    )
    # Same raw notifications, filtered for the ECG frame type instead (see
    # _parse_polar_ecg_frame) -- ACC and ECG share PMD_CHAR2_UUID, so no
    # separate scoping needed.
    ecg_completeness = compute_ecg_completeness(
        window_pmd_hex, planned_duration_s, window_connection_ids
    )

    # Tally by HCI disconnect reason -- across ALL recorded disconnects for
    # this device (not just unexpected_disconnects below, which excludes
    # the startup/shutdown grace window): the manuscript's own Table
    # tab:forced counts every 0x3E/0x08 event, including ones during
    # reconnection churn. Named after the three reasons the manuscript's
    # Methods section explicitly tracks (supervision timeout 0x08,
    # connection-failed-to-establish 0x3E, local host termination 0x16);
    # anything else falls into reason_other rather than being silently
    # dropped.
    _REASON_HEX_NAMES = {
        "08": "supervision_timeout",
        "3e": "establishment_failed",
        "16": "local_host_term",
    }
    disconnect_reason_counts = {"supervision_timeout": 0, "establishment_failed": 0,
                                 "local_host_term": 0, "other": 0}
    for d in disconnects:
        key = _REASON_HEX_NAMES.get(d.get("reason_hex"), "other")
        disconnect_reason_counts[key] += 1

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

    # Real, per-run measured delay between "connected" and "ECG data
    # actually started arriving" -- both timestamps on btmon's own host
    # clock, no cross-clock-domain guessing. See _first_ecg_frame_host_ts.
    first_connect_ts = next(
        (ev["ts"] for ev in timeline if ev["kind"] == "connect"), None
    )
    first_ecg_ts = _first_ecg_frame_host_ts(scoped_pmd)
    ecg_stream_start_delay_s = (
        round(first_ecg_ts - first_connect_ts, 3)
        if first_ecg_ts is not None and first_connect_ts is not None
        else None
    )

    # Encryption handshake outcomes (see START_ENCRYPTION_RE). Scoped to this
    # capture's device when the event names an address, so a stray connection
    # from the other sensor on the shared radio can't be blamed on this run.
    n_encryption_attempts = 0
    n_encryption_successes = 0
    n_encryption_failures = 0
    enc_block = []

    def _tally_encryption(lines):
        nonlocal n_encryption_successes, n_encryption_failures
        state = None
        for l in lines:
            state = ENCRYPTION_STATE_RE.match(l) or state
        if state is None:
            return
        for l in lines:
            addr = HANDLE_ADDRESS_RE.search(l)
            if addr and mac_address and addr.group(1).upper() != mac_address.upper():
                return
        status = None
        for l in lines:
            status = STATUS_RE.search(l) or status
        if (
            status is not None
            and status.group(1).strip().lower() == "success"
            and state.group(1) == "Enabled"
        ):
            n_encryption_successes += 1
        else:
            n_encryption_failures += 1

    for line in text.splitlines():
        if line.startswith((">", "<", "@", "=")):
            _tally_encryption(enc_block)
            enc_block = [line]
            if START_ENCRYPTION_RE.search(line):
                n_encryption_attempts += 1
        else:
            enc_block.append(line)
    _tally_encryption(enc_block)

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
        "disconnect_count": len(disconnects),
        "unexpected_disconnect_count": len(unexpected_disconnects),
        "n_reason_supervision_timeout": disconnect_reason_counts["supervision_timeout"],
        "n_reason_establishment_failed": disconnect_reason_counts["establishment_failed"],
        "n_reason_local_host_term": disconnect_reason_counts["local_host_term"],
        "n_reason_other": disconnect_reason_counts["other"],
        "connected_duration_s": round(connected_duration_s, 3),
        "capture_duration_s": last_ts,
        "ecg_stream_start_delay_s": ecg_stream_start_delay_s,
        "n_encryption_attempts": n_encryption_attempts,
        "n_encryption_successes": n_encryption_successes,
        "n_encryption_failures": n_encryption_failures,
        **jitter,
        **acc_completeness,
        **ecg_completeness,
        **hr_completeness,
        **corsano_completeness,
    }


def parse_capture_file(capture_path, mac_address=None, planned_duration_s=None):
    """Run `btmon -r <file>` and parse its text output.

    Sample completeness (see compute_acc_completeness/compute_ecg_
    completeness/compute_hr_completeness in parse_btmon_text) is
    implemented for Polar H10 only -- its PMD accelerometer/ECG frames and
    standard Heart Rate Measurement notifications all embed enough
    information (the sensor's own timestamp/RR-interval data) to detect
    real dropped samples, not just gaps in our own RX timing. ECG
    completeness was originally computed from this run's own rosbag
    instead (reading the driver's already-decoded /polar_ecg topic) --
    replaced 2026-09-25 with this direct HCI decode once it became clear
    the driver computes the sensor's own per-sample timestamp internally
    (PolarH10.py's ecg_data_conv) but discards it before publishing,
    stamping each ROS message with host wall-clock time instead. Decoding
    the raw PMD notification ourselves (_parse_polar_ecg_frame) recovers
    that sensor-clock information directly, consistent with how ACC
    completeness already works, and needs no rosbag at all. Corsano's
    payload format isn't documented anywhere in this codebase, so the same
    claim can't be made for it: for Corsano, "no unexpected disconnects
    and full jitter percentiles" only supports "no gaps observed," not "no
    delivered samples were lost" -- state that distinction explicitly in
    any write-up that includes Corsano."""
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
    return parse_btmon_text(
        result.stdout, mac_address=mac_address, planned_duration_s=planned_duration_s
    )
