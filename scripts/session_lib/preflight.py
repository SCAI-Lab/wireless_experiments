"""Pre-flight checks: stray processes, device bonding/readiness, BT controller,
WiFi scanning, Pi reachability and iperf3 throughput baseline."""

import json
import os
import re
import shlex
import signal
import statistics
import subprocess
import threading
import time
from pathlib import Path

from .errors import (
    DeviceCheckError,
    DriverExitedError,
    InfrastructureCheckError,
    StrayProcessError,
)
from .logs import log
from .wifi import (
    _ap_channel_from_scan_output,
    _autodetect_wifi_iface,
    _freq_mhz_to_24ghz_channel,
    _ensure_iface_unmanaged,
    _iw_scan,
    _parse_channel_occupancy,
    _qbss_from_scan_output,
    _qbss_iface_is_safe,
)
from .ble_radio import (
    _btmon_watch_for_handle,
    _hcitool_read_channel_map,
)
from .capture import _resolve_sniffer_binary
from .drivers import (
    _kernel_has_link,
    disconnect_ble_device,
    start_driver_with_retry,
    stop_driver,
)


def kill_stray_driver_processes(cfg):
    """Preflight: find and kill any configured device's driver that's
    ALREADY running before this session starts one of its own: it would
    contend for the same BLE connection.

    Matches each device's driver arguments in other processes' command
    lines via `pgrep -af` (which excludes its own PID). Kills automatically
    (SIGTERM, then SIGKILL): this runs before the session's own drivers
    exist, so anything matched is left over from an earlier run. Then
    disconnects the device from BlueZ, as at the end of every run. Raises
    StrayProcessError only if a process survives SIGKILL."""
    for device in cfg["devices"]:
        launch_cmd = device.get("launch_cmd")
        if not launch_cmd:
            continue
        # Match "--mac-address-or-name <MAC>" only: `ros2 run <pkg> <exe>`
        # spawns the driver as a child whose argv lacks the "ros2 run"
        # prefix, and the MAC alone would also match the BLE sniffer's
        # "--follow <MAC>".
        parts = shlex.split(launch_cmd)
        marker = "--mac-address-or-name"
        if marker in parts and parts.index(marker) + 1 < len(parts):
            target = parts[parts.index(marker) + 1]
            pattern = re.escape(f"{marker} {target}") + r"(\s|$)"
        else:
            pattern = re.escape(" ".join(parts))
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


def check_devices_unbonded(cfg, timeout_s=10.0):
    """Pre-flight: log which configured devices are bonded on this host.

    Informational only: a bond is not itself a fault (the Corsano is bonded
    and encrypts fine). evaluate_run_result() fails a run whose capture
    shows a failed encryption handshake, which happens when the sensor has
    forgotten a bond the host still uses (the link then dies with reason
    0x08, like interference). The drivers do not need a bond, and
    start_pairing_agent() can create one during a session.
    """
    bonded = []
    for device in cfg["devices"]:
        mac = device.get("mac_address")
        if not mac:
            continue
        try:
            result = subprocess.run(
                ["bluetoothctl", "info", mac],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            log.warning(
                "  -> bluetoothctl not found, skipping the stale-bond check"
            )
            return
        except subprocess.TimeoutExpired:
            log.warning(
                f"  -> `bluetoothctl info {mac}` timed out, skipping the "
                f"stale-bond check for {device['name']}"
            )
            continue
        for line in result.stdout.splitlines():
            if line.strip().lower().startswith("bonded:"):
                if line.split(":", 1)[1].strip().lower() == "yes":
                    bonded.append(f"{device['name']} ({mac})")
                break
    if bonded:
        log.info(
            "  -> bonded on this host: " + ", ".join(bonded)
            + " -- fine in itself (the Corsano watch is legitimately bonded); "
            "each run's capture is checked for a FAILED handshake, which is "
            "what actually breaks a session"
        )
    else:
        log.info("  -> no devices bonded on this host")


def check_devices_ready(cfg, probe_duration_s=8, check_afh=True):
    """Pre-flight sanity check: briefly launch each configured device's driver
    (retrying a flaky initial connection) and confirm it stays up rather than
    exiting immediately (e.g. because the sensor is powered off, out of range,
    or not paired). Mirrors the field guide's "arrival sanity check" step, run
    automatically before any test.

    check_afh=True also attempts one real LE Read Channel Map on the first
    device's probe connection, and ABORTS (not just warns) if it fails: the
    session's results depend on the channel map."""
    devices = cfg["devices"]
    device_names = [d["name"] for d in devices]
    log.info(
        f"=== Sanity check: verifying {len(devices)} device(s) are up and "
        f"connected -- {device_names} ({probe_duration_s}s probe each) ==="
    )
    failures = []
    afh_verified = not check_afh
    afh_handle_seen = False
    for device in devices:
        log.info(f"  -> checking {device['name']} ({device['launch_cmd']})")
        mac = device.get("mac_address")
        if mac and _kernel_has_link(mac):
            # A link that already exists produces no LE Connection Complete
            # for the probe, so the AFH watcher below would never see a
            # handle -- and whatever holds it (a driver started outside this
            # session, BlueZ auto-reconnect) would contend with every run.
            log.warning(
                f"     {device['name']} ({mac}) is already connected before its "
                f"probe -- disconnecting it first"
            )
            disconnect_ble_device(mac)
            time.sleep(2.0)
            if _kernel_has_link(mac):
                log.error(
                    f"     {device['name']}: FAILED -- still connected after "
                    f"disconnect; something outside this session holds the link "
                    f"(check `pgrep -af {mac}` and `hcitool con`)"
                )
                failures.append(device["name"])
                continue
        afh_watcher = None
        if not afh_verified and check_afh and device.get("mac_address"):
            # Start watching BEFORE connecting: the LE Connection Complete
            # event fires only once, as the connection is made.
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
            # Terminate the underlying btmon directly: it can be blocked in
            # readline() (see _btmon_watch_for_handle).
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
                    afh_handle_seen = True
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
        if not afh_handle_seen:
            cause = (
                "btmon never reported an LE Connection Complete for any probe, "
                "so there was no handle to read. Usually the device was already "
                "connected before its probe (a driver left running outside this "
                "session -- check `pgrep -af mac-address-or-name` and "
                "`hcitool con`), or btmon lacks CAP_NET_RAW/CAP_NET_ADMIN "
                "(`getcap $(which btmon)`)."
            )
        else:
            cause = (
                "a connection handle was found but LE Read Channel Map failed on "
                "it -- hcitool likely lacks permission to send raw HCI commands "
                "(needs root or CAP_NET_ADMIN/CAP_NET_RAW). Check `getcap "
                "$(which hcitool)`; if empty, run once: `sudo setcap "
                "cap_net_raw,cap_net_admin+eip $(which hcitool)`."
            )
        raise InfrastructureCheckError(
            f"could not read the LE channel map on any connected device's "
            f"probe connection: {cause} Without this, every run's AFH "
            f"channel-map data will be silently empty -- and AFH behavior is "
            f"central to explaining this session's results, so this aborts "
            f"rather than just warning."
        )
    log.info("=== Sanity check passed: all devices connected ===")


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


def check_ble_sniffer_ready(cfg):
    """If ble_sniffer is enabled, require the dongle and nrfutil-ble-sniffer
    to be present before the session starts (start_ble_sniffer only warns
    per run). Set ble_sniffer.enabled: false to run without it."""
    sniffer_cfg = cfg.get("ble_sniffer") or {}
    if not sniffer_cfg.get("enabled"):
        log.info("  -> BLE sniffer disabled in config, no over-the-air capture")
        return
    serial_port = sniffer_cfg.get("serial_port")
    if not serial_port or not Path(serial_port).exists():
        raise InfrastructureCheckError(
            f"BLE sniffer serial port {serial_port or '(unset)'} not found -- "
            f"plug in the nRF sniffer dongle, or set ble_sniffer.enabled: false "
            f"in the config to run without over-the-air capture"
        )
    if not _resolve_sniffer_binary():
        raise InfrastructureCheckError(
            "nrfutil-ble-sniffer not found -- install it, or set "
            "ble_sniffer.enabled: false in the config"
        )
    log.info(f"  -> BLE sniffer found on {serial_port}")


def _iface_admin_up(iface):
    """True/False from the IFF_UP bit in /sys/class/net/<iface>/flags, or
    None if the interface doesn't exist."""
    try:
        flags = int(Path(f"/sys/class/net/{iface}/flags").read_text().strip(), 16)
    except (OSError, ValueError):
        return None
    return bool(flags & 0x1)


def check_wifi_scan_ready(cfg, need_qbss, attempts=3):
    """Preflight: run one real `iw scan` on each WiFi adapter the session
    will poll (ambient_scan.iface for channel occupancy, and qbss_iface when
    rows using QBSS will run) and ABORT if it can't produce the data those
    pollers record: both pollers skip a failed scan and log it at DEBUG only.

    Unmanages the interface first, exactly as the pollers do, so this tests
    the same state they will run in. Retries "Device or resource busy"
    (-16) a few times -- a one-off collision with another scan on the radio,
    not a broken setup."""
    checks = {}  # iface -> list of what it's needed for
    ambient_cfg = cfg.get("ambient_scan") or {}
    if ambient_cfg.get("enabled") and ambient_cfg.get("iface") is not False:
        checks.setdefault(ambient_cfg.get("iface"), []).append("occupancy")
    iperf_cfg = (cfg.get("row2_duty_cycle") or {}).get("iperf") or {}
    qbss_bssid = iperf_cfg.get("qbss_bssid")
    if need_qbss and qbss_bssid and iperf_cfg.get("qbss_iface") is not False:
        checks.setdefault(iperf_cfg.get("qbss_iface"), []).append("qbss")
    if not checks:
        return

    for iface, uses in checks.items():
        if not iface:
            iface = _autodetect_wifi_iface()
            if not iface:
                # Same outcome as the pollers themselves: nothing to poll.
                log.warning(
                    f"  -> no WiFi scan interface configured or auto-detected -- "
                    f"{'/'.join(uses)} data will not be recorded this session"
                )
                continue
        what = " + ".join("channel occupancy" if u == "occupancy" else "QBSS" for u in uses)
        log.info(f"  -> checking WiFi scanning on {iface} ({what})...")
        if not _qbss_iface_is_safe(iface):
            # The pollers refuse this iface and warn on their own.
            continue
        _ensure_iface_unmanaged(iface)
        up = _iface_admin_up(iface)
        if up is None:
            raise InfrastructureCheckError(
                f"WiFi scan interface {iface} does not exist -- is the USB WiFi "
                f"adapter plugged in? (ambient_scan.iface / qbss_iface in config.yaml)"
            )
        if not up:
            raise InfrastructureCheckError(
                f"WiFi scan interface {iface} is DOWN, so every {what} scan would "
                f"fail with 'Network is down' and record nothing. Run "
                f"`sudo ip link set {iface} up`, then start the session again. "
                f"(Don't use `nmcli ... managed yes`: this check unmanages the "
                f"adapter, and NetworkManager takes it down again as it lets go "
                f"-- that loop stopped a session on 2026-10-06. Brought up while "
                f"unmanaged, it stays up until it's replugged or the laptop "
                f"reboots.)"
            )
        result = None
        for _ in range(attempts):
            try:
                result = subprocess.run(
                    ["iw", "dev", iface, "scan"],
                    capture_output=True, text=True, timeout=15,
                )
            except FileNotFoundError:
                raise InfrastructureCheckError("`iw` is not installed -- can't scan WiFi")
            except subprocess.TimeoutExpired:
                result = None
                continue
            if result.returncode == 0 or "(-16)" not in result.stderr:
                break
            time.sleep(2)
        if result is None or result.returncode != 0:
            err = "timed out" if result is None else (
                f"returncode={result.returncode}: {result.stderr.strip()[:200]}"
            )
            raise InfrastructureCheckError(
                f"test WiFi scan on {iface} failed ({err}) -- {what} data would "
                f"be empty for the whole session"
            )
        if "occupancy" in uses:
            snapshot = _parse_channel_occupancy(result.stdout)
            if not snapshot:
                raise InfrastructureCheckError(
                    f"test WiFi scan on {iface} succeeded but found no 2.4GHz "
                    f"networks at all -- channel occupancy would be empty; check "
                    f"the adapter/antenna"
                )
            aps = sum(v["ap_count"] for v in snapshot.values())
            log.info(f"     channel occupancy OK: {aps} APs on {len(snapshot)} channel(s)")
        if "qbss" in uses:
            assert qbss_bssid  # "qbss" is only added when it's set
            if not re.search(rf"^BSS {re.escape(qbss_bssid)}\b", result.stdout,
                             re.IGNORECASE | re.MULTILINE):
                raise InfrastructureCheckError(
                    f"QBSS target AP {qbss_bssid} not found in a scan from {iface} "
                    f"-- is the test AP powered on? (row2_duty_cycle.iperf.qbss_bssid)"
                )
            pct = _qbss_from_scan_output(result.stdout, qbss_bssid)
            if pct is None:
                raise InfrastructureCheckError(
                    f"QBSS target AP {qbss_bssid} is visible from {iface} but its "
                    f"scan result has no BSS Load (channel utilisation) IE -- QBSS "
                    f"would be empty for every run"
                )
            log.info(f"     QBSS OK: {qbss_bssid} reports {pct}% channel utilization")
            expected_channel = iperf_cfg.get("ap_channel")
            if expected_channel is not None:
                channel = _ap_channel_from_scan_output(result.stdout, qbss_bssid)
                if channel != expected_channel:
                    raise InfrastructureCheckError(
                        f"test AP {qbss_bssid} is on channel {channel}, not "
                        f"{expected_channel} -- every load condition (and the "
                        f"forced-channel mask, chosen to sit inside channel "
                        f"{expected_channel}) assumes that channel. Fix the AP, or "
                        f"row2_duty_cycle.iperf.ap_channel if it moved on purpose"
                    )
                log.info(f"     test AP on channel {channel}, as expected")


def _scan_bsses(scan_stdout):
    """{bssid: {"ssid", "channel", "signal_dbm"}} for every 2.4 GHz BSS."""
    out = {}
    for block in re.split(r"^BSS ", scan_stdout, flags=re.MULTILINE)[1:]:
        bssid = block[:17].lower()
        freq = re.search(r"freq:\s*([\d.]+)", block)
        channel = _freq_mhz_to_24ghz_channel(float(freq.group(1))) if freq else None
        if channel is None:
            continue
        sig = re.search(r"signal:\s*(-?[\d.]+)", block)
        ssid = re.search(r"^\s*SSID:\s*(.*)$", block, re.MULTILINE)
        name = ssid.group(1).strip() if ssid else ""
        if re.fullmatch(r"(\\x00)*", name):  # iw prints hidden SSIDs as \x00...
            name = ""
        out[bssid] = {
            "ssid": name,
            "channel": channel,
            "signal_dbm": float(sig.group(1)) if sig else None,
        }
    return out


def check_no_foreign_wifi(cfg, session_dir, test_mode, scans=2):
    """Chamber RF check: no WiFi AP other than the test AP may be on a
    channel overlapping the test channel, or the "no load" conditions
    aren't interference-free.

    A 20 MHz channel-6 signal spreads into channels 3-9 (any AP within 3
    channels overlaps it), so those are checked: config
    foreign_wifi_check.fail_channels, default test-AP channel +/-3. A
    foreign AP there aborts a real session and only warns in --test-mode.
    Foreign APs on other channels only warn: they don't touch BLE 12-19,
    but they do sit on Row 5's added "clean" channels (e.g. WiFi 1 covers
    BLE 0-9). Unions `scans` scans, since one scan can miss a beacon.
    Sees only APs (beacons), not client devices. Writes the result to
    <session_dir>/foreign_wifi_check.json."""
    fcfg = cfg.get("foreign_wifi_check") or {}
    if fcfg.get("enabled") is False:
        log.info("  -> foreign WiFi check disabled (foreign_wifi_check.enabled)")
        return
    iperf_cfg = (cfg.get("row2_duty_cycle") or {}).get("iperf") or {}
    iface = (cfg.get("ambient_scan") or {}).get("iface") or iperf_cfg.get("qbss_iface")
    if not iface:
        log.warning("  -> foreign WiFi check skipped: no scan adapter configured")
        return
    test_bssid = (iperf_cfg.get("qbss_bssid") or "").lower()
    ap_channel = iperf_cfg.get("ap_channel", 6)
    fail_channels = set(fcfg.get("fail_channels") or range(ap_channel - 3, ap_channel + 4))
    log.info(
        f"  -> checking for foreign WiFi APs on {iface} ({scans} scans; "
        f"channels {sorted(fail_channels)} must be clear apart from the test AP)..."
    )
    seen = {}
    for i in range(scans):
        for _ in range(4):  # retry "Device or resource busy" (-16), as check_wifi_scan_ready does
            try:
                result = _iw_scan(iface)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                raise InfrastructureCheckError(f"foreign WiFi check: scan on {iface} failed: {e}")
            if result.returncode == 0 or "(-16)" not in result.stderr:
                break
            time.sleep(2)
        if result.returncode != 0:
            if i == scans - 1 and not seen:
                raise InfrastructureCheckError(
                    f"foreign WiFi check: scan on {iface} failed: {result.stderr.strip()[:200]}"
                )
            time.sleep(2)
            continue
        seen.update(_scan_bsses(result.stdout))
        if i < scans - 1:
            time.sleep(2)
    foreign = {b: v for b, v in seen.items() if b != test_bssid}
    blocking = {b: v for b, v in foreign.items() if v["channel"] in fail_channels}
    other = {b: v for b, v in foreign.items() if v["channel"] not in fail_channels}
    (Path(session_dir) / "foreign_wifi_check.json").write_text(json.dumps({
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "iface": iface,
        "test_ap": test_bssid,
        "test_ap_seen": test_bssid in seen,
        "fail_channels": sorted(fail_channels),
        "foreign_overlapping": blocking,
        "foreign_other_channels": other,
    }, indent=2) + "\n")

    def _fmt(aps):
        return "; ".join(
            f"{v['ssid'] or '<hidden>'} ({b}) ch {v['channel']}, {v['signal_dbm']} dBm"
            for b, v in sorted(aps.items(), key=lambda kv: kv[1]["channel"])
        )

    if other:
        log.warning(
            f"     {len(other)} foreign AP(s) on other channels (not overlapping "
            f"BLE 12-19, but on Row 5's added channels): {_fmt(other)}"
        )
    if blocking:
        message = (
            f"{len(blocking)} foreign WiFi AP(s) on channels overlapping the test "
            f"channel {ap_channel}: {_fmt(blocking)} -- the chamber isn't "
            f"RF-clean; 'no load' runs would carry this interference. Find and "
            f"switch them off (or check the chamber door/seal), then restart."
        )
        if not test_mode:
            raise InfrastructureCheckError(message)
        log.warning(f"     {message} -- continuing anyway (test mode)")
        return
    log.info(
        f"     no foreign APs on channels {sorted(fail_channels)} "
        f"({len(seen)} BSS(es) seen in total)"
    )


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


def check_pi_wifi_link(iperf_cfg, timeout_s=5):
    """Verify the Pi sends its iperf3 traffic over WiFi, associated to the
    test AP (qbss_bssid) on ap_channel. Reachability alone doesn't show that:
    with an Ethernet cable plugged into the Pi, the route to server_ip goes
    over the cable and the "WiFi load" never reaches the air -- in test mode
    the throughput baseline check only warns, so nothing else would catch it."""
    ssh_target = f"{iperf_cfg['pi_user']}@{iperf_cfg['pi_host']}"
    server_ip = iperf_cfg["server_ip"]
    log.info(f"  -> checking the Pi's route to {server_ip} goes over the test AP...")
    remote_cmd = (
        f"dev=$(ip -o route get {shlex.quote(server_ip)} | "
        f"sed -n 's/.* dev \\([^ ]*\\).*/\\1/p'); "
        f'echo "dev=$dev"; '
        f'[ -d "/sys/class/net/$dev/wireless" ] && echo wireless=yes || echo wireless=no; '
        f'iw dev "$dev" link 2>&1'
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout_s}",
             ssh_target, remote_cmd],
            capture_output=True, text=True, timeout=timeout_s + 10,
        )
    except subprocess.TimeoutExpired:
        raise InfrastructureCheckError(f"WiFi link check on {ssh_target} timed out")
    out = result.stdout
    dev_m = re.search(r"^dev=(\S*)", out, re.MULTILINE)
    dev = dev_m.group(1) if dev_m else ""
    if not dev:
        raise InfrastructureCheckError(
            f"could not find the Pi's route to {server_ip} "
            f"({(result.stderr or out).strip()[:200]})"
        )
    if "wireless=yes" not in out:
        raise InfrastructureCheckError(
            f"the Pi reaches {server_ip} via {dev}, which is not a WiFi interface "
            f"-- iperf3 traffic would bypass the air entirely. Unplug the Pi's "
            f"Ethernet cable (or fix its routing) so traffic goes over WiFi"
        )
    bssid = iperf_cfg.get("qbss_bssid")
    link_m = re.search(r"Connected to ([0-9a-fA-F:]{17})", out)
    if not link_m:
        raise InfrastructureCheckError(
            f"the Pi's WiFi interface {dev} is not associated to any AP "
            f"(`iw dev {dev} link`: {out.strip().splitlines()[-1][:120]})"
        )
    if bssid and link_m.group(1).lower() != bssid.lower():
        raise InfrastructureCheckError(
            f"the Pi's {dev} is associated to {link_m.group(1)}, not the test AP "
            f"{bssid} -- the load would land on the wrong network"
        )
    freq_m = re.search(r"freq:\s*(\d+)", out)
    channel = _freq_mhz_to_24ghz_channel(int(freq_m.group(1))) if freq_m else None
    expected_channel = iperf_cfg.get("ap_channel")
    if expected_channel is not None and channel != expected_channel:
        raise InfrastructureCheckError(
            f"the Pi's link to {link_m.group(1)} is on channel {channel}, not "
            f"{expected_channel}"
        )
    log.info(f"     Pi {dev} associated to {link_m.group(1)} on channel {channel}")


def measure_max_throughput(iperf_cfg, duration_s=10, timeout_s=None):
    """Run a real iperf3 throughput test from the Pi to the server through the
    actual link, to get a fresh baseline for deriving Row 2's duty-cycle
    bitrates -- instead of relying on a number hand-recorded from a past dry
    run.

    UDP with -b 0 (send as fast as possible), the traffic mode used to apply
    the load (see run_iperf_background in chamber_session.py). The reported
    bits_per_second is what was RECEIVED, so it reflects any loss at
    saturation. -O 2 runs a 2 s warm-up before the -t window and leaves it
    out of the average."""
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
    """Compare a freshly measured throughput against the expected chamber
    baseline (expected_max_mbps). In the chamber a large deviation means
    something is wrong (AP on the wrong channel, door open, cable fault).

    strict=False (test mode) downgrades a mismatch to a warning."""
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


MAX_THROUGHPUT_FILE = "max_throughput.json"
_LOGGED_MAX_RE = re.compile(r"derived bitrate_by_duty from measured ([\d.]+) Mbit/s max")


def _saved_max_throughput(session_dir):
    """(max_mbps, source) a session already measured, or None. Prefers
    max_throughput.json (written by resolve_duty_bitrates); without it,
    falls back to the FIRST measurement logged in session.log."""
    if session_dir is None:
        return None
    path = Path(session_dir) / MAX_THROUGHPUT_FILE
    try:
        return float(json.loads(path.read_text())["max_mbps"]), path.name
    except FileNotFoundError:
        pass
    except (ValueError, KeyError, TypeError) as e:
        raise InfrastructureCheckError(f"{path} is unreadable ({e}) -- fix or remove it")
    try:
        with open(Path(session_dir) / "session.log") as f:
            for line in f:
                m = _LOGGED_MAX_RE.search(line)
                if m:
                    return float(m.group(1)), f"session.log ({line[:19]})"
    except FileNotFoundError:
        pass
    return None


def resolve_duty_bitrates(cfg, strict=True, session_dir=None):
    """Return {duty: bitrate_str} for every non-zero duty level in
    row2_duty_cycle.duty_levels. If row2_duty_cycle.iperf.bitrate_by_duty is
    set in config, it's used as an explicit override (and must cover every
    non-zero level); otherwise a fresh throughput baseline is measured via
    iperf3 and each level's bitrate is derived as that percentage of it.

    strict=False (test mode) passes through to check_throughput_baseline, so
    a chamber-baseline mismatch warns instead of aborting.

    With session_dir, the max is measured once per session and saved there
    (max_throughput.json); a --resume reuses it instead of measuring again,
    so every run in a session gets the same bitrates."""
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

    iperf_cfg = r["iperf"]
    saved = _saved_max_throughput(session_dir)
    if saved:
        max_mbps, source = saved
        log.info(
            f"  -> reusing this session's max throughput {max_mbps:.1f} Mbit/s "
            f"(from {source}) -- not re-measuring on resume, so every run in "
            f"the session gets the same bitrates"
        )
    else:
        # Median of several measurements: every load level scales with it.
        runs = int(iperf_cfg.get("max_throughput_runs", 3))
        duration_s = int(iperf_cfg.get("max_throughput_duration_s", 30))
        samples = [
            measure_max_throughput(iperf_cfg, duration_s=duration_s)
            for _ in range(runs)
        ]
        max_mbps = statistics.median(samples)
        log.info(
            f"     median of {runs} x {duration_s}s: {max_mbps:.1f} Mbit/s "
            f"(runs: {', '.join(f'{m:.1f}' for m in samples)}; "
            f"spread {max(samples) - min(samples):.1f} Mbit/s)"
        )
        if session_dir is not None:
            (Path(session_dir) / MAX_THROUGHPUT_FILE).write_text(
                json.dumps(
                    {
                        "max_mbps": max_mbps,
                        "samples_mbps": samples,
                        "duration_s": duration_s,
                        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    indent=2,
                )
                + "\n"
            )
    check_throughput_baseline(max_mbps, iperf_cfg, strict=strict)
    derived = {d: f"{max_mbps * d / 100:.1f}M" for d in duty_levels}
    log.info(
        f"  -> derived bitrate_by_duty from measured {max_mbps:.1f} Mbit/s max: {derived}"
    )
    return derived
