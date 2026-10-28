"""Corsano 287-2B sample-completeness from its file-transfer protocol."""

import re

from .btmon_patterns import (
    NOTIFICATION_DATA_RE,
    NOTIFICATION_RE,
    NOTIFICATION_TYPE_RE,
)


# =============================================================================
# Corsano sample-completeness (opcodes, byte layout and CRC-8 as in the
# corsano_ros driver).
#
# btmon shows no Type:/UUID annotations for these characteristics on a
# reconnect (BlueZ uses its cached GATT table for a bonded device), so they
# are identified by attribute handle (CORSANO_WRITE_CHAR_HANDLE etc. below),
# which is fixed by the device firmware.
#
# Unlike Polar's streaming, the Corsano driver POLLS on-device files at 0.1 s
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

# GATT attribute handles of the Corsano 287-2B (fixed by its firmware; a
# different unit or firmware may need them re-derived from a capture).
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

    Identifies characteristics by the attribute handles above, falling back
    to a UUID Type: match if btmon has one (after a fresh pairing)."""
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
    "Packet drop detected" check).

    Each downloaded buffer is one packet (retrieve_data.py passes the whole
    buffer to process_metric_array as a single metric).

    The index identifies which chunk of the device's accel file the
    tail-window download landed on, so consecutive polls often see the same
    index: a repeat is skipped, and only an index change is checked for
    whether it was the next chunk or a skipped one."""
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

    # Whether the watch recorded anything during the run, from its own
    # file-size counters (if a file never grows, the driver re-downloads the
    # same tail and completeness would report 100% for data that was never
    # new). The files are circular once the watch's storage is full, so
    # "growth" sums only the size INCREASES between polls, and "new data"
    # means the downloaded tail itself changed.
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
