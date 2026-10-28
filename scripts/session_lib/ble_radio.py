"""BLE radio environment: nearby-device density, AFH/channel-map and RSSI
polling via hcitool, and forced host channel classification (Row 3)."""

import csv
import os
import pty
import re
import subprocess
import threading
import time

from .logs import log
from .wifi import (
    _open_session_csv,
    _wall_time,
)
from .btmon_patterns import (
    CONN_COMPLETE_RE,
    HANDLE_RE,
    PEER_ADDRESS_RE,
    STATUS_RE,
)

# =============================================================================
# Nearby BLE device density: count of distinct advertisers during a short
# scan window, a rough background-density proxy.
#
# PRIVACY (GDPR / Swiss FADP): bystanders' device addresses can be personal
# data. count_nearby_ble_devices() uses them only to deduplicate within one
# scan window and returns only the count; addresses are never returned,
# logged or written to disk.
#
# This scans on hci0, the radio the sensor under test is connected through,
# so it uses a short window (default 5 s) at a long interval (default 90 s).
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


def _ble_density_poll_loop(
    controller, out_csv_path, scan_duration_s, poll_interval_s, stop_event
):
    start = time.time()
    header = ["timestamp_s", "nearby_device_count", "wall_time"]
    with _open_session_csv(out_csv_path, header) as f:
        writer = csv.writer(f)
        while not stop_event.wait(poll_interval_s):
            count = count_nearby_ble_devices(controller, scan_duration_s)
            if count is None:
                continue
            writer.writerow([round(time.time() - start, 1), count, _wall_time()])
            f.flush()


def start_ble_density_poller(
    controller, out_csv_path, scan_duration_s=5, poll_interval_s=90
):
    """Start a background thread sampling nearby BLE device density for the
    WHOLE gym session (like start_channel_occupancy_poller, not scoped to
    any one device's run). Pass controller=None or enabled=False upstream
    to skip entirely. It shares hci0 with the sensor connection (see the
    comment above)."""
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
# AFH / channel-map polling, optional and best-effort. Needs `hcitool` to
# send raw HCI commands (LE Read Channel Map), i.e. root or
# CAP_NET_ADMIN/CAP_NET_RAW on the binary; without it, no data:
# `sudo setcap cap_net_raw,cap_net_admin+eip $(which hcitool)`.
# =============================================================================


def _btmon_watch_for_handle(
    controller, mac_address, handle_holder, stop_event, proc_holder=None
):
    """Watch a live, separate btmon monitor for this device's own LE
    Connection Complete event, and keep handle_holder[0] updated with its
    handle, updated on every match so a mid-run reconnect doesn't leave a
    stale handle behind.

    HCI monitor sockets support multiple simultaneous listeners, so running
    this alongside the main capture-writing btmon instance (which writes to
    a file, not stdout) doesn't interfere with it.

    IMPORTANT: btmon fully buffers its stdout (thousands of bytes at a
    time, only flushing at exit) unless stdout is a real terminal.

    proc_holder, if given, is a single-element list this sets to the btmon
    Popen object right after launch, so stop_afh_poller() can terminate it
    directly: this loop checks stop_event only between f.readline() calls,
    which block until btmon produces another line; terminating btmon makes
    the blocked readline() return (EOF)."""
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
    # errors="replace": btmon's text can embed payload bytes that aren't
    # valid UTF-8; everything matched here is ASCII.
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


def _hcitool_command_complete(stdout, ogf, ocf):
    """Return-parameter bytes [num_pkts, opcode_lo, opcode_hi, status, ...]
    of the Command Complete event (0x0e) for THIS command (ogf/ocf) in
    `hcitool cmd` output, or None if there isn't one.

    The first "HCI Event" isn't necessarily the reply. hcitool
    also reads only ONE event and exits, so while the adapter is scanning the
    reply can be missed entirely -- see set_host_channel_classification."""
    opcode = (ogf << 10) | ocf
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"HCI Event: 0x([0-9a-fA-F]{2})", line)
        if not m or int(m.group(1), 16) != 0x0E:
            continue
        payload = []
        for nxt in lines[i + 1 :]:
            if nxt.lstrip().startswith(("<", ">")):
                break
            try:
                payload += [int(b, 16) for b in nxt.split()]
            except ValueError:
                break
        if len(payload) >= 4 and payload[1] | (payload[2] << 8) == opcode:
            return payload
    return None


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
    event_bytes = _hcitool_command_complete(result.stdout, 0x08, 0x0015)
    if event_bytes is None or len(event_bytes) < 11 or event_bytes[3] != 0x00:
        return None, None  # no reply, status != success, or short response
    channel_map = event_bytes[6:11]
    enabled = sum(bin(b).count("1") for b in channel_map)
    return "".join(f"{b:02x}" for b in channel_map), enabled


ALL_BLE_DATA_CHANNELS = list(range(37))
FORCED_CHANNEL_RANGE = list(range(12, 20))  # 12-19 inclusive: fully inside WiFi ch6


def _encode_channel_classification(usable_channels):
    """5-byte (37-bit) little-endian Channel_Classification bitmap for
    HCI_LE_Set_Host_Channel_Classification: bit i = 1 means BLE data
    channel i (0-36) is usable, 0 means excluded. The controller requires
    at least 2 usable channels (not enforced here)."""
    bitmap = 0
    for ch in usable_channels:
        if not 0 <= ch <= 36:
            raise ValueError(f"BLE data channel index out of range (0-36): {ch}")
        bitmap |= 1 << ch
    return bitmap.to_bytes(5, "little")


HCITOOL_REPLY_ATTEMPTS = 5


def set_host_channel_classification(usable_channels):
    """Send HCI_LE_Set_Host_Channel_Classification (OGF 0x08, OCF 0x0014)
    to restrict which BLE data channels the controller's AFH may use to
    exactly usable_channels, so the link cannot hop away from the WiFi on
    them. Same hcitool raw-HCI path (and capabilities) as LE Read Channel
    Map. Returns True only on a confirmed Success status; never raises.
    Callers abort the attempt on False."""
    classification = _encode_channel_classification(usable_channels)
    hex_bytes = [f"0x{b:02x}" for b in classification]

    for _ in range(HCITOOL_REPLY_ATTEMPTS):
        try:
            result = subprocess.run(
                ["hcitool", "cmd", "0x08", "0x0014", *hex_bytes],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        # [num_hci_cmd_pkts(1), opcode_lo, opcode_hi, status(1)]
        event_bytes = _hcitool_command_complete(result.stdout, 0x08, 0x0014)
        if event_bytes is not None:
            return event_bytes[3] == 0x00
    return False


def reset_host_channel_classification():
    """Restore normal, unrestricted AFH (all 37 data channels usable) before
    the next test, so a forced map can't leak into it. Returns True on
    confirmed success, as set_host_channel_classification."""
    return set_host_channel_classification(ALL_BLE_DATA_CHANNELS)


def _hcitool_read_rssi(handle):
    """Send raw HCI Read_RSSI (OGF 0x05 Status Parameters, OCF 0x0005) for
    the connection handle, and parse the Command Complete event bytes:
    [num_pkts(1), opcode(2), status(1), conn_handle(2), rssi(1, signed)].
    Returns None on any failure (permission, no response, unexpected
    format, or non-success status). By handle, as the channel-map read:
    `hcitool rssi <mac>` fails for LE connections.
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
    event_bytes = _hcitool_command_complete(result.stdout, 0x05, 0x0005)
    if event_bytes is None or len(event_bytes) < 7 or event_bytes[3] != 0x00:
        return None  # no reply, status != success, or short response
    rssi_byte = event_bytes[6]
    return rssi_byte - 256 if rssi_byte >= 128 else rssi_byte  # signed 8-bit


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
    # Terminate the watcher's btmon directly: it can be blocked in readline()
    # (see _btmon_watch_for_handle).
    proc = proc_holder[0]
    if proc is not None:
        proc.terminate()
    watcher_thread.join(timeout=5)
    poll_thread.join(timeout=5)
    if watcher_thread.is_alive() or poll_thread.is_alive():
        log.warning("     AFH poller thread(s) didn't stop cleanly within 5s")
