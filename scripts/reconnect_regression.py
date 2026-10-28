#!/usr/bin/env python3
"""
reconnect_regression.py -- chamber-free reconnect test (no WiFi load)
=====================================================================

Forces link drops with no RF involved and checks that the driver recovers
DATA each time, not just the link:

    drop the link for --block-s, release, wait for data, stream, repeat

--drop block (default): `bluetoothctl block <mac>` -- BlueZ disconnects the
  sensor and refuses to reconnect it until `unblock`. The controller stays
  powered throughout. The sensor sees a normal LL terminate, not a
  supervision timeout, so this exercises the driver's reconnect/resume path
  but not the sensor's post-timeout advertising behaviour.
--drop rfkill: soft-blocks hci0, so the sensor sees a real supervision
  timeout. Every block is a full controller shutdown, which can hang the
  controller ("HCI reset during shutdown failed") until a reboot.
For a supervision-timeout drop without touching the controller, shield the
strap by hand (closed metal tin) for the block time instead.

RF is excluded either way, so any failure here is the driver or the host
stack, in ~2-3 minutes instead of a 5-minute interference run per rep.

Per cycle it records (cycles.csv):
  link_back_s        unblock -> Connection Complete
  data_back_s        unblock -> first ECG sample (Polar) / notification
  ttfs_s             Connection Complete -> first sample
  reconnect_ci_ms    interval the reconnect was opened at (with the H10,
                     45 ms means a ~30 s wait before PMD data, 60/30 ms ~1 s)
  data_per_s         samples (Polar) / notifications per second over the
                     --stream-s window after data came back
  first_adv_s / first_connectable_adv_s
                     drop (block) -> first ADV / first ADV_IND|ADV_DIRECT_IND
                     from the sensor on the nRF sniffer (independent radio, so
                     it sees the whole non-connectable window even while hci0
                     is blocked). Run once with --block-s 60 to measure that
                     window without the host reconnecting inside it.
and fails (exit 1) if any cycle doesn't get data back within --recover-s,
or if BlueZ ends up holding a stale "connected" device (see
session_lib.heal_stale_bluez_connection).

Usage:
  python3 scripts/reconnect_regression.py [--device Polar_H10] [--cycles 5]
                                  [--block-s 8] [--recover-s 75] [--stream-s 20]
"""

import argparse
import csv
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from analyze_session import parse_capture
from session_lib import (
    check_bt_controller_ready,
    check_ros_packages,
    disconnect_ble_device,
    get_output_dir,
    heal_stale_bluez_connection,
    kill_stray_driver_processes,
    load_config,
    reset_host_channel_classification,
    setup_logging,
    start_ble_sniffer,
    start_btmon,
    start_driver,
    stop_ble_sniffer,
    stop_btmon,
    stop_driver,
)

log = logging.getLogger("reconnect_regression")

BTSNOOP_EPOCH_DELTA_S = 0x00DCDDB30F2F8000 / 1e6   # btsnoop us since 0 AD -> unix
CONNECTABLE_PDUS = {0, 1}                           # ADV_IND, ADV_DIRECT_IND
POLL_S = 1.0


def rfkill_id(controller):
    for p in Path(f"/sys/class/bluetooth/{controller}").glob("rfkill*/index"):
        return p.read_text().strip()
    raise SystemExit(f"no rfkill device for {controller}")


def rfkill(action, rid):
    subprocess.run(["rfkill", action, rid], check=True, timeout=5)


def bluez_block(action, mac):
    """bluetoothctl block/unblock: disconnects and refuses the device without
    powering the controller down. Bonding keys are kept."""
    r = subprocess.run(["bluetoothctl", action, mac], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"`bluetoothctl {action} {mac}` failed: {(r.stdout + r.stderr).strip()[:200]}")


class ControllerDown(RuntimeError):
    """The adapter itself didn't come back -- an infrastructure failure, not
    a driver result."""


def _powered():
    out = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True,
                         timeout=5).stdout
    return "Powered: yes" in out


def ensure_powered():
    """bluetoothd normally re-powers the adapter on unblock; do it ourselves
    if it didn't. If even that fails the controller is hung ("HCI reset
    during shutdown failed", then every HCI Reset timing out) and only a
    btusb reload or reboot brings it back -- stop instead of
    reporting the remaining cycles as driver failures."""
    for _ in range(10):
        if _powered():
            return
        time.sleep(0.5)
    log.warning("  -> adapter not powered after unblock, powering on")
    subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=15)
    if not _powered():
        raise ControllerDown(
            "controller did not power up after unblock (check `journalctl -k | grep hci0` "
            "for HCI reset timeouts); reload it with `sudo modprobe -r btusb && sudo "
            "modprobe btusb` or reboot")


class Capture:
    """Re-parses the growing btsnoop file; times are unix seconds."""

    def __init__(self, path, device):
        self.path, self.device = path, device

    def read(self):
        try:
            ev = parse_capture(self.path, self.device)
        except (OSError, ValueError, IndexError):
            return None
        if ev is None:
            return None
        t0 = ev["t0"] - BTSNOOP_EPOCH_DELTA_S
        polar = "polar" in self.device.lower()
        data = ([(t0 + t, n) for t, _ts, n in ev["ecg"]] if polar
                else [(t0 + t, 1) for t, _h, _l in ev["notif"]])
        return dict(data=data, conns=[(t0 + c[0], c[1]) for c in ev["conns"]],
                    discs=[(t0 + t, rs) for t, rs in ev["discs"]],
                    mgmt_disc_fail=[t0 + t for t, _ in ev["mgmt_disc_fail"]])


def wait_for_data(cap, since, timeout_s):
    """First data unit after `since`, or None on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        c = cap.read()
        if c:
            hit = next((t for t, _ in c["data"] if t > since), None)
            if hit:
                return hit
        time.sleep(POLL_S)
    return None


def sniffer_advs(pcap, mac):
    """(unix_time, pdu_type) for the sensor's advertising PDUs."""
    if not pcap.exists():
        return []
    try:
        out = subprocess.run(
            ["tshark", "-r", str(pcap), "-Y", f"btle.advertising_address == {mac.lower()}",
             "-T", "fields", "-e", "frame.time_epoch", "-e", "btle.advertising_header.pdu_type"],
            capture_output=True, text=True, timeout=60).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    advs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] and parts[1]:
            advs.append((float(parts[0]), int(parts[1], 0)))
    return advs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--device", default="Polar_H10")
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--drop", choices=["block", "rfkill"], default="block",
                    help="how to force the drop (see above; rfkill risks a controller hang)")
    ap.add_argument("--block-s", type=float, default=8.0)
    ap.add_argument("--recover-s", type=float, default=75.0,
                    help="max unblock -> first data (default 75: 6 s supervision timeout "
                         "+ reconnect + the H10's ~30 s PMD start in 45 ms mode)")
    ap.add_argument("--stream-s", type=float, default=20.0,
                    help="streaming time measured after each recovery, before the next drop")
    args = ap.parse_args()

    cfg = load_config(args.config)
    dev = next((d for d in cfg["devices"] if d["name"] == args.device), None)
    if dev is None:
        raise SystemExit(f"--device {args.device!r} not in {args.config}")
    cfg["devices"] = [dev]
    mac, controller = dev["mac_address"], cfg["btmon_controller"]

    out = get_output_dir(cfg) / (time.strftime("%Y%m%d_%H%M%S") + "_reconnect_regression")
    out.mkdir(parents=True)
    setup_logging(out)
    log.info(f"=== reconnect regression: {args.device}, {args.cycles} x {args.drop} "
             f"{args.block_s:.0f}s ===")

    kill_stray_driver_processes(cfg)
    try:
        check_ros_packages(cfg)
        check_bt_controller_ready(controller)
    except Exception as e:
        log.error(f"  !! preflight failed: {e}")
        sys.exit(2)
    heal_stale_bluez_connection(mac)
    reset_host_channel_classification()
    rid = rfkill_id(controller) if args.drop == "rfkill" else None

    stem = out / f"{args.device}_reconnect"
    cap_path, pcap = stem.with_suffix(".btsnoop"), stem.with_suffix(".pcap")
    btmon = start_btmon(controller, cap_path)
    sniffer = start_ble_sniffer(cfg, mac, pcap)
    driver = start_driver(dev["launch_cmd"], log_path=str(stem) + "_driver.log")
    cap = Capture(str(cap_path), args.device)

    cycles, ok, infra_error = [], True, None
    try:
        t_launch = time.time()
        first = wait_for_data(cap, t_launch, 90.0)
        if first is None:
            raise RuntimeError("no data within 90 s of driver launch -- nothing to test")
        log.info(f"  -> streaming {first - t_launch:.1f}s after launch")
        time.sleep(args.stream_s)

        for i in range(1, args.cycles + 1):
            if driver.poll() is not None:
                raise RuntimeError(f"driver exited (code {driver.returncode})")
            log.info(f"=== cycle {i}/{args.cycles}: {args.drop} for {args.block_s:.0f}s ===")
            t_block = time.time()
            if rid is not None:
                rfkill("block", rid)
                time.sleep(args.block_s)
                rfkill("unblock", rid)
            else:
                bluez_block("block", mac)
                time.sleep(args.block_s)
                bluez_block("unblock", mac)
            t_unblock = time.time()
            if rid is not None:
                ensure_powered()
            t_data = wait_for_data(cap, t_unblock, args.recover_s)
            c = cap.read() or dict(conns=[], data=[], discs=[], mgmt_disc_fail=[])
            conn = next((x for x in c["conns"] if x[0] > t_unblock), None)
            row = dict(cycle=i, block_s=args.block_s,
                       link_back_s=(conn[0] - t_unblock) if conn else np.nan,
                       reconnect_ci_ms=conn[1] if conn else np.nan,
                       data_back_s=(t_data - t_unblock) if t_data else np.nan,
                       ttfs_s=(t_data - conn[0]) if (t_data and conn) else np.nan,
                       n_connects=sum(1 for x in c["conns"] if x[0] > t_unblock),
                       n_disconnects=sum(1 for x in c["discs"] if x[0] > t_block),
                       mgmt_disconnect_failed=sum(1 for t in c["mgmt_disc_fail"] if t > t_block))
            if t_data is None:
                ok = False
                log.error(f"  !! cycle {i}: no data within {args.recover_s:.0f}s of unblock "
                          f"(link {'back' if conn else 'NOT back'})")
            else:
                time.sleep(args.stream_s)
                c = cap.read() or c
                n = sum(u for t, u in c["data"] if t_data <= t <= t_data + args.stream_s)
                row["data_per_s"] = n / args.stream_s
                log.info(f"  -> link back {row['link_back_s']:.1f}s (CI {row['reconnect_ci_ms']} ms), "
                         f"data back {row['data_back_s']:.1f}s (ttfs {row['ttfs_s']:.1f}s), "
                         f"{row['data_per_s']:.0f}/s")
            row["_t_block"] = t_block
            cycles.append(row)
    except ControllerDown as e:
        ok, infra_error = False, str(e)
        log.error(f"  !! INFRASTRUCTURE: {e}")
    except RuntimeError as e:
        ok = False
        log.error(f"  !! {e}")
    finally:
        try:
            if rid is not None:
                rfkill("unblock", rid)
            bluez_block("unblock", mac)   # never leave the sensor blocked
        except (subprocess.SubprocessError, RuntimeError) as e:
            log.error(f"  !! cleanup unblock failed ({e}) -- run `bluetoothctl unblock {mac}`"
                      + (f" / `rfkill unblock {rid}`" if rid is not None else ""))
        stop_driver(driver)
        stop_btmon(btmon)
        stop_ble_sniffer(sniffer)
        disconnect_ble_device(mac)
        wedged = heal_stale_bluez_connection(mac)

    # advertising return after each drop, from the independent sniffer
    advs = sniffer_advs(pcap, mac)
    for row in cycles:
        tb = row.pop("_t_block")
        nxt = [a for a in advs if a[0] > tb]
        conn_ = [a for a in nxt if a[1] in CONNECTABLE_PDUS]
        row["first_adv_s"] = (nxt[0][0] - tb) if nxt else np.nan
        row["first_connectable_adv_s"] = (conn_[0][0] - tb) if conn_ else np.nan
        row["n_nonconnectable_before_connectable"] = sum(
            1 for a in nxt if a[1] not in CONNECTABLE_PDUS and (not conn_ or a[0] < conn_[0][0]))
    if cycles and not advs:
        log.warning("  -> no sniffer advertising data: first_adv_s columns are empty")

    if cycles:
        with open(out / "cycles.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in cycles for k in r)))
            w.writeheader()
            w.writerows(cycles)
    recovered = sum(1 for r in cycles if not np.isnan(r["data_back_s"]))
    ok = ok and recovered == args.cycles and not wedged
    summary = dict(device=args.device, cycles=args.cycles, recovered_data=recovered,
                   stale_bluez_connection=wedged, passed=ok, infrastructure_error=infra_error,
                   median_data_back_s=float(np.nanmedian([r["data_back_s"] for r in cycles]))
                   if recovered else None)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    verdict = "INVALID (controller down)" if infra_error else ("PASSED" if ok else "FAILED")
    log.info(f"=== {verdict}: data recovered in {recovered}/{args.cycles} cycles"
             + ("; BlueZ was left with a stale connection" if wedged else "") + f" -> {out} ===")
    sys.exit(2 if infra_error else (0 if ok else 1))


if __name__ == "__main__":
    main()
