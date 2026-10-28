"""Per-run capture processes: btmon, rosbag recording and the optional nRF BLE sniffer."""

import signal
import subprocess
import time
from pathlib import Path

from .logs import log


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
# rosbag recording: all topics, one bag per run (like the btsnoop and AFH
# captures), started and stopped inside _attempt_run.
# =============================================================================


def start_rosbag_recording(out_dir):
    """Start `ros2 bag record -a` (every topic currently visible) for one
    run, without a duration limit. Writes the bag directory out_dir, which
    must not exist yet. Returns None (never raises) if `ros2` isn't on PATH
    or the recorder exits immediately: the bag is best-effort and never
    aborts a run."""
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
    """SIGINT (not SIGTERM/kill) so it can finalize its storage file and
    metadata.yaml; a longer timeout than stop_btmon's for long runs."""
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
# BLE sniffer (Nordic nRF Sniffer for Bluetooth LE): optional over-the-air
# capture, separate from btmon's HCI view. See ble_sniffer in config.yaml.
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
    raises)."""
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
    # Address type per device (default random); the H10's is public.
    address_type = next(
        (d.get("address_type", "random") for d in cfg.get("devices", [])
         if (d.get("mac_address") or "").upper() == (mac_address or "").upper()),
        "random",
    )
    try:
        proc = subprocess.Popen(
            [
                binary,
                "sniff",
                "--port",
                serial_port,
                "--follow",
                f"{mac_address} {address_type}",
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
    """Stop a sniffer started by start_ble_sniffer(); SIGTERM closes the
    pcap file cleanly."""
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
    a Nordic sniffer capture, via tshark: over-the-air CRC/RSSI, which btmon
    (our own controller's view) cannot show. Advertising channels (37/38/39)
    only. Returns EMPTY_SNIFFER_METRICS (all None) if the pcap is
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
