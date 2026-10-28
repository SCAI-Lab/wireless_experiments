"""Polar H10 sample-completeness (ACC, ECG, HR/RR) from PMD/HR notifications."""

import re


POLAR_PMD_TYPE_MARKER = "fb005c82"  # PMD_CHAR2_UUID (ACC and ECG both notify here)
POLAR_HR_TYPE_MARKER = "2a37"  # standard Heart Rate Measurement characteristic
POLAR_ACC_SAMPLING_FREQ_HZ = 200  # per PolarH10.py's ACC_SAMPLING_FREQ

# Fallback for when Type: is absent: on a reconnect to a bonded device BlueZ
# uses its cached GATT table, so btmon has no UUID for a notification, only
# its ATT handle. These ATT attribute handles are fixed by the H10's
# firmware (0x0032: PMD data, ECG and ACC; 0x0010: HR Measurement).
POLAR_PMD_ATTRIBUTE_HANDLE = "0x0032"
POLAR_HR_ATTRIBUTE_HANDLE = "0x0010"
ATT_ATTRIBUTE_HANDLE_RE = re.compile(r"Handle:\s*(0x[0-9A-Fa-f]+)")


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
    "acc_samples_skipped": None,
    "acc_stream_completeness_pct": None,
}


def _measured_sample_rate(pmd_frames, nominal_hz, min_pairs=10, max_deviation=0.05):
    """The stream's real sample rate on the sensor's own clock: median of
    n_samples / (last_ts - previous frame's last_ts) over back-to-back frame
    pairs (no frame lost in between). Uses only each frame's LAST-sample
    timestamp, which the sensor sends; first_ts is derived from the nominal
    rate by the frame parsers. The H10's ACC runs at about 203.9 Hz
    (nominal 200), its ECG at 130 Hz. Falls back to nominal_hz with too few
    pairs, or if the estimate is implausibly far from nominal."""
    rates = []
    for prev, cur in _same_connection_pairs(pmd_frames):
        dt = cur[1] - prev[1]
        expected_dt = cur[2] / nominal_hz
        if 0 < dt < 1.5 * expected_dt:
            rates.append(cur[2] / dt)
    if len(rates) < min_pairs:
        return float(nominal_hz)
    rates.sort()
    measured = rates[len(rates) // 2]
    if abs(measured - nominal_hz) > max_deviation * nominal_hz:
        return float(nominal_hz)
    return measured


def _planned_window_samples(pmd_frames, sampling_freq_hz, planned_duration_s):
    """Shared completeness math for compute_acc_completeness/compute_ecg_
    completeness. received/lost/total are measured against the run's whole
    PLANNED duration, not just the span between the first and last frame
    that arrived (a run connected for 43 s of a 300 s plan is ~14%
    complete), as for Corsano.

    Falls back to the span-based method (gaps between consecutive received
    frames only) when planned_duration_s isn't available.

    The expected sample count uses the rate the sensor actually delivers
    (see _measured_sample_rate), not the nominal one."""
    received = sum(f[2] for f in pmd_frames)
    rate_hz = _measured_sample_rate(pmd_frames, sampling_freq_hz)
    try:
        planned_s = float(planned_duration_s)
    except (TypeError, ValueError):
        planned_s = None
    if planned_s and planned_s > 0:
        total = round(planned_s * rate_hz)
        lost = max(total - received, 0)
        return received, lost, total
    lost = _skipped_samples(pmd_frames, rate_hz)
    return received, lost, received + lost


def _same_connection_pairs(pmd_frames):
    """Consecutive frame pairs that arrived on the same BLE connection.

    The H10 can restart its PMD sample clock on a new connection, and a
    cross-connection pair would count the time spent disconnected as
    in-stream loss."""
    return [
        (prev, cur)
        for prev, cur in zip(pmd_frames, pmd_frames[1:])
        if prev[3] == cur[3]
    ]


def _parsed_frames(pmd_hex_frames, parse_frame, connection_ids):
    """Parse PMD frames with parse_frame, tagging each with the connection
    it arrived on, ordered by connection then sensor time. connection_ids
    (parallel to pmd_hex_frames) defaults to treating all as one."""
    if connection_ids is None:
        connection_ids = [0] * len(pmd_hex_frames)
    frames = [
        (*parsed, conn)
        for parsed, conn in (
            (parse_frame(h), c) for h, c in zip(pmd_hex_frames, connection_ids)
        )
        if parsed is not None
    ]
    frames.sort(key=lambda f: (f[3], f[0]))
    return frames


def _skipped_samples(pmd_frames, rate_hz):
    """Samples the sensor timestamped but never delivered, between the first
    and last frame that did arrive: for each consecutive pair, the samples
    the sensor clock says elapsed (last_ts difference x rate) minus the
    samples the later frame carried, counting only pairs on the same
    connection (see _same_connection_pairs). Unlike the planned-window number this
    ignores time with no stream at all (before the first frame, e.g. the H10's
    ~31 s PMD start delay at 45 ms CI, or while disconnected -- no timestamps
    exist there), so it measures only in-stream loss."""
    skipped = 0
    for prev, cur in _same_connection_pairs(pmd_frames):
        elapsed = round((cur[1] - prev[1]) * rate_hz)
        skipped += max(elapsed - cur[2], 0)
    return skipped


def _stream_completeness(pmd_frames, sampling_freq_hz):
    """(skipped_samples, stream_completeness_pct) for the frames that
    arrived -- see _skipped_samples."""
    rate_hz = _measured_sample_rate(pmd_frames, sampling_freq_hz)
    received = sum(f[2] for f in pmd_frames)
    skipped = _skipped_samples(pmd_frames, rate_hz)
    total = received + skipped
    return skipped, (round(100 * received / total, 2) if total else None)


def compute_acc_completeness(
    pmd_hex_frames, planned_duration_s=None, connection_ids=None
):
    """Sample completeness for the PMD accelerometer stream, from the
    sensor's OWN embedded per-frame timestamps rather than our host's RX
    arrival timing -- see _planned_window_samples for how received/lost/
    total are actually measured. Returns EMPTY_ACC_COMPLETENESS if no valid
    ACC frames were found."""
    frames = _parsed_frames(pmd_hex_frames, _parse_polar_acc_frame, connection_ids)
    if not frames:
        return dict(EMPTY_ACC_COMPLETENESS)
    received, lost, total = _planned_window_samples(
        frames, POLAR_ACC_SAMPLING_FREQ_HZ, planned_duration_s
    )
    skipped, stream_pct = _stream_completeness(frames, POLAR_ACC_SAMPLING_FREQ_HZ)
    return {
        "acc_pmd_frame_count": len(frames),
        "acc_samples_received": received,
        "acc_samples_lost": lost,
        "acc_completeness_pct": round(100 * received / total, 2) if total else None,
        "acc_samples_skipped": skipped,
        "acc_stream_completeness_pct": stream_pct,
    }


POLAR_ECG_SAMPLING_FREQ_HZ = 130  # per PolarH10.py's ECG_SAMPLING_FREQ


def _parse_polar_ecg_frame(hex_data):
    """Parse one Polar PMD ECG notification frame, like
    _parse_polar_acc_frame (PolarH10.py's pmd_data_conv: data[0]==0x00 is
    ECG, 0x02 is ACC). Returns (first_sample_ts_s, last_sample_ts_s,
    n_samples) on the sensor's own internal clock, or None if this isn't a
    complete/valid ECG frame.

    Mirrors PolarH10.py's ecg_data_conv. The driver publishes ECG with the
    host's clock, so only the raw notification carries the sensor
    timestamps that reveal dropped frames."""
    try:
        data = bytes.fromhex(hex_data)
    except ValueError:
        return None
    if len(data) < 11 or data[0] != 0x00:
        return None
    last_sample_ts = int.from_bytes(data[1:9], "little") / 1e9
    # data[9] is skipped, unused -- matches ecg_data_conv exactly (unlike
    # ACC, ECG has no separate frame_type/resolution byte; step is fixed).
    step = 3
    samples = data[10:]
    n_samples = len(samples) // step
    if n_samples == 0:
        return None
    time_step = 1.0 / POLAR_ECG_SAMPLING_FREQ_HZ
    first_sample_ts = last_sample_ts - (n_samples - 1) * time_step
    return first_sample_ts, last_sample_ts, n_samples


EMPTY_ECG_COMPLETENESS = {
    "ecg_pmd_frame_count": 0,
    "ecg_samples_received": None,
    "ecg_samples_lost": None,
    "ecg_completeness_pct": None,
    "ecg_samples_skipped": None,
    "ecg_stream_completeness_pct": None,
}


def compute_ecg_completeness(
    pmd_hex_frames, planned_duration_s=None, connection_ids=None
):
    """Sample completeness for the PMD ECG stream, from the sensor's OWN
    embedded per-frame timestamps; see _planned_window_samples. Returns
    EMPTY_ECG_COMPLETENESS if no valid ECG frames were found."""
    frames = _parsed_frames(pmd_hex_frames, _parse_polar_ecg_frame, connection_ids)
    if not frames:
        return dict(EMPTY_ECG_COMPLETENESS)
    received, lost, total = _planned_window_samples(
        frames, POLAR_ECG_SAMPLING_FREQ_HZ, planned_duration_s
    )
    skipped, stream_pct = _stream_completeness(frames, POLAR_ECG_SAMPLING_FREQ_HZ)
    return {
        "ecg_pmd_frame_count": len(frames),
        "ecg_samples_received": received,
        "ecg_samples_lost": lost,
        "ecg_completeness_pct": round(100 * received / total, 2) if total else None,
        "ecg_samples_skipped": skipped,
        "ecg_stream_completeness_pct": stream_pct,
    }


def _first_ecg_frame_host_ts(scoped_pmd_with_ts):
    """Host-clock (btmon capture-relative) timestamp of the first real ECG
    frame (data[0]==0x00) among (hex_data, host_ts_s) pairs -- same clock
    domain as connection-complete events, so the per-run PMD-ECG startup
    delay is this minus the connection time. Returns None if no ECG frame
    was found."""
    ecg_ts = [
        ts for hex_data, ts in scoped_pmd_with_ts
        if _parse_polar_ecg_frame(hex_data) is not None and ts is not None
    ]
    return min(ecg_ts) if ecg_ts else None


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
    vs. this run's connected duration. Less reliable than
    compute_acc_completeness (the RR buffer is finite, and skin-contact
    loss looks like transmission loss): a cross-check only."""
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
