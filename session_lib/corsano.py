"""Corsano 287-2B sample-completeness from its file-transfer protocol."""

import re

from .btmon_patterns import (
    NOTIFICATION_DATA_RE,
    NOTIFICATION_RE,
    NOTIFICATION_TYPE_RE,
)


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
    "corsano_file_polls": None,
    "corsano_acc_file_growth_bytes": None,
    "corsano_stress_file_growth_bytes": None,
    "corsano_new_sensor_data": None,
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

    # Whether the watch actually recorded anything during the run, from its
    # own file-size counters. Checked 2026-10-01: in EVERY session since at
    # least 09-22 the ACC, BioZ and Stress files never grew (5113245 /
    # 3885404 / 47916 bytes, identical across all runs) -- the driver just
    # re-downloaded the same unchanged 1 KiB tail every ~4 s, so the ACC
    # "completeness" saw one chunk and reported 100% for data that was
    # never new. Only Activity grew (one 38-byte record per few minutes).
    #
    # The files are circular once the watch's storage is full (2026-10-01:
    # BioZ grew 3886017 -> 3887047 within a run, then dropped to 3883260 as
    # the oldest block was deleted), so: "growth" sums only the size
    # INCREASES between polls (a deletion isn't recorded data), and "new
    # data" means the downloaded tail itself changed, which holds whether
    # or not the size moves.
    def _growth(file_id):
        sizes = [size for fid, size in file_size_samples if fid == file_id]
        if len(sizes) < 2:
            return None
        return sum(max(b - a, 0) for a, b in zip(sizes, sizes[1:]))

    def _tail_changed(file_id):
        tails = [t["data"] for t in transfers if t["file_id"] == file_id]
        return any(a != b for a, b in zip(tails, tails[1:])) if len(tails) >= 2 else None

    acc_growth = _growth(CORSANO_FILE_ID_ACC)
    stress_growth = _growth(CORSANO_FILE_ID_STRESS)
    bioz_growth = _growth(CORSANO_FILE_ID_BIOZ)
    changed = [
        c for c in (_tail_changed(f) for f in (
            CORSANO_FILE_ID_ACC, CORSANO_FILE_ID_BIOZ, CORSANO_FILE_ID_STRESS))
        if c is not None
    ]
    result["corsano_file_polls"] = len(transfers)  # completed request/response cycles
    result["corsano_acc_file_growth_bytes"] = acc_growth
    result["corsano_bioz_file_growth_bytes"] = bioz_growth
    result["corsano_stress_file_growth_bytes"] = stress_growth
    result["corsano_new_sensor_data"] = any(changed) if changed else None
    if acc_growth == 0:
        # Nothing new was recorded, so there was nothing to lose: report "no
        # measurement" rather than a misleading 100%.
        result["corsano_acc_samples_lost"] = None
        result["corsano_acc_completeness_pct"] = None
    return result
