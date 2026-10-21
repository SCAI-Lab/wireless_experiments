#!/usr/bin/env python3
"""
Background RF monitor -- runs SEPARATELY from the sensor tests
==============================================================

Its own process, its own radios, its own files. It never talks to the BLE
controller under test (hci0) and never transmits:

  - WiFi airtime: the USB T3U Plus (rtw_8822bu) in passive MONITOR mode,
    parked on the forced WiFi channel. Every received frame's on-air time
    (tshark's wlan_radio.duration, preamble included -- checked 2026-10-12
    against hand-built radiotap frames: 1500 B at 54 Mb/s -> 244 us = 20
    preamble + 224 data) is summed per window: airtime_pct = sum/window.
    A lower bound: frames the adapter can't decode (too weak, collided,
    non-WiFi energy) and inter-frame gaps/backoff aren't counted. The same
    tool and metric run in the chamber put both sites on one axis.
  - Network count per band: every sweep_interval_s the adapter hops through
    the sweep channels (still monitor mode, so still passive -- no probe
    requests) and counts distinct beaconing BSSIDs, plus a per-channel
    airtime snapshot.
  - Bluetooth crowding: the nRF52840 sniffer, NOT following any device,
    streamed through a FIFO into tshark. Distinct CRC-ok advertiser
    addresses are counted per ble_window_s IN MEMORY and only the counts
    are written -- no pcap, no address ever reaches disk. (A 2026-10-12
    desk test: 45 real advertisers in 30 s vs 840 "addresses" from
    CRC-failed packets, hence the CRC filter.)
  - People count: entered by hand with the `people` subcommand.

The sensor tests (gym_session.py) never start or stop this; the two are
joined afterwards by wall-clock time (gym_session.py parse / covariates).
Everything lands in <session_dir>/background/.

Normally started by `gym_session.py run`, which runs the survey, starts
this monitor as a child process (`run --quiet`), runs the sensors and stops
the monitor at the end. On its own (e.g. in a second terminal):

    python3 background_monitor.py survey      # new captures/gym_<date>_<time>/; picks the busiest channel
    python3 background_monitor.py run         # until Ctrl+C
    python3 background_monitor.py people 14

Without --session, every command uses the most recent gym_ session.

Chamber airtime check (same tool, same metric, chamber_session.py untouched):

    python3 background_monitor.py run --session <chamber TS> --channel 6

Other:
    python3 background_monitor.py check               # 10 s airtime + 10 s BLE count, prints only

Needs: sudo once at start (monitor-mode switch + dumpcap); iw already has
cap_net_admin for channel changes; the user in `dialout` for the sniffer.
"""

import argparse
import csv
import json
import logging
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from session_lib import get_output_dir, load_config
from session_lib.capture import _resolve_sniffer_binary
from session_lib.wifi import _qbss_iface_is_safe

log = logging.getLogger("background_monitor")

BACKGROUND_DIR = "background"
FORCED_CHANNEL_FILE = "forced_channel.json"

# Seconds a frame's timestamp may trail wall time through dumpcap -> tshark
# before a window is closed and summarized.
PIPELINE_LAG_S = 2.0
# After a channel change, drop this much so frames still in flight from the
# previous channel aren't counted on the new one.
RETUNE_SETTLE_S = 0.2

BLE_WARMUP_S = 5.0

ADV_PDU_TYPES_WITH_ADVA = {0x0, 0x1, 0x2, 0x4, 0x6}  # ADV_IND/DIRECT/NONCONN/SCAN_RSP/SCAN_IND


def _mcfg(cfg):
    return cfg.get("background_monitor") or {}


def background_dir(session_dir):
    d = Path(session_dir) / BACKGROUND_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_logging(bg_dir, console=True):
    """monitor.log always; the console only when run on its own (as
    gym_session.py's child it would scribble over the run progress bar)."""
    log.setLevel(logging.DEBUG)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(bg_dir / "monitor.log")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        log.addHandler(sh)


def _wall(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


class CsvLog:
    """Append-only CSV that writes its header once, flushed every row so a
    crash loses nothing and gym_session.py can read it live."""

    def __init__(self, path, fields):
        self.path = Path(path)
        self.fields = fields
        new = not self.path.exists() or self.path.stat().st_size == 0
        if not new:
            # Resuming a session written before a column was added: keep
            # its header so rows stay aligned (the new column is dropped).
            with open(self.path, newline="") as f:
                self.fields = next(csv.reader(f), None) or fields
        self.f = open(self.path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=self.fields, extrasaction="ignore")
        if new:
            self.w.writeheader()
            self.f.flush()

    def write(self, row):
        self.w.writerow(row)
        self.f.flush()

    def close(self):
        self.f.close()


# =============================================================================
# WiFi: monitor mode + airtime
# =============================================================================


def _run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if check and r.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def enter_monitor_mode(iface):
    """Switch iface to monitor mode. Refuses the internal iwlwifi radio: it
    shares an antenna with hci0 (see _qbss_iface_is_safe)."""
    if not _qbss_iface_is_safe(iface):
        raise RuntimeError(f"{iface} may share a radio with the BT controller -- refusing")
    # NetworkManager would otherwise grab it back and scan on it.
    _run(["nmcli", "device", "set", iface, "managed", "no"], check=False)
    _run(["sudo", "ip", "link", "set", iface, "down"])
    _run(["sudo", "iw", "dev", iface, "set", "type", "monitor"])
    _run(["sudo", "ip", "link", "set", iface, "up"])
    log.info(f"{iface} is in monitor mode (passive, never transmits)")


def leave_monitor_mode(iface):
    for cmd in (
        ["sudo", "-n", "ip", "link", "set", iface, "down"],
        ["sudo", "-n", "iw", "dev", iface, "set", "type", "managed"],
        ["sudo", "-n", "ip", "link", "set", iface, "up"],
    ):
        _run(cmd, check=False)
    log.info(f"{iface} back in managed mode")


def tune(iface, channel):
    """Returns True if the adapter accepted the channel (a regulatory domain
    can disable some, e.g. 5 GHz channels under the world domain)."""
    width = ["HT20"] if channel <= 14 else []
    r = _run(["iw", "dev", iface, "set", "channel", str(channel), *width], check=False)
    if r.returncode != 0:
        log.debug(f"could not tune {iface} to channel {channel}: {r.stderr.strip()}")
        return False
    return True


class FrameBuffer:
    """Recent decoded frames (t, channel, duration_us, type_subtype, bssid),
    fed by the tshark reader thread. BSSIDs are only kept here, in memory,
    to count distinct beaconing networks -- they're never written."""

    def __init__(self, keep_s=600):
        self.rows = deque()
        self.lock = threading.Lock()
        self.keep_s = keep_s

    def add(self, row):
        with self.lock:
            self.rows.append(row)
            cutoff = row[0] - self.keep_s
            while self.rows and self.rows[0][0] < cutoff:
                self.rows.popleft()

    def summarize(self, t0, t1, channel):
        frames = no_dur = mgmt = ctrl = data = 0
        dur_us = 0
        bssids = set()
        with self.lock:
            rows = [r for r in self.rows if t0 <= r[0] < t1 and r[1] == channel]
        for _, _, dur, subtype, bssid in rows:
            frames += 1
            if dur is None:
                no_dur += 1
            else:
                dur_us += dur
            ftype = (subtype >> 4) & 0x3 if subtype is not None else None
            if ftype == 0:
                mgmt += 1
                if subtype == 0x08 and bssid:  # beacon
                    bssids.add(bssid)
            elif ftype == 1:
                ctrl += 1
            elif ftype == 2:
                data += 1
        span = max(t1 - t0, 1e-6)
        return {
            "airtime_pct": round(min(100.0, 100.0 * dur_us / 1e6 / span), 2),
            "frames": frames,
            "frames_no_duration": no_dur,
            "mgmt_frames": mgmt,
            "ctrl_frames": ctrl,
            "data_frames": data,
            "beaconing_networks": len(bssids),
        }


def _int_or_none(s, base=10):
    try:
        return int(s, base)
    except (TypeError, ValueError):
        return None


class WifiCapture:
    """sudo dumpcap (monitor iface) | tshark (unprivileged) -> FrameBuffer."""

    FIELDS = ["frame.time_epoch", "wlan_radio.channel", "wlan_radio.duration",
              "wlan.fc.type_subtype", "wlan.bssid"]

    def __init__(self, iface, bg_dir):
        self.iface = iface
        self.buf = FrameBuffer()
        self.err = open(bg_dir / "dumpcap.err", "a")
        self.dumpcap = self.tshark = self.thread = None

    def start(self):
        # Headers are enough for airtime: wlan_radio.duration uses the
        # frame's reported length, not the captured bytes.
        self.dumpcap = subprocess.Popen(
            ["sudo", "-n", "dumpcap", "-q", "-i", self.iface, "-s", "256", "-w", "-"],
            stdout=subprocess.PIPE, stderr=self.err,
        )
        cmd = ["tshark", "-l", "-n", "-r", "-", "-T", "fields", "-E", "separator=\t"]
        for f in self.FIELDS:
            cmd += ["-e", f]
        self.tshark = subprocess.Popen(
            cmd, stdin=self.dumpcap.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        self.dumpcap.stdout.close()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        log.info(f"WiFi capture running on {self.iface} (dumpcap pid {self.dumpcap.pid})")

    def _read(self):
        for line in self.tshark.stdout:
            p = line.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            try:
                t = float(p[0])
            except ValueError:
                continue
            ch = _int_or_none(p[1])
            dur = _int_or_none(p[2])
            st = _int_or_none(p[3], 0) if p[3] else None
            self.buf.add((t, ch, dur, st, p[4] or None))

    def alive(self):
        return self.dumpcap.poll() is None and self.tshark.poll() is None

    def stop(self):
        for p in (self.tshark, self.dumpcap):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        # dumpcap runs as root; terminate() from us may not reach it.
        if self.dumpcap and self.dumpcap.poll() is None:
            _run(["sudo", "-n", "kill", str(self.dumpcap.pid)], check=False)


# Kernel messages meaning the rtw88 receive path is handing mac80211 corrupt
# frames. 2026-10-13 (gym_20261013_133642): from 14:03:54 the T3U logged
# "Rate marked as a VHT rate but data is invalid" WARNs and "unused phy
# status page" nonstop for 110 min. Airtime read 92-100 % while beaconing
# networks fell from ~100 to 2-13: garbage rates give garbage durations.
# An interface bounce (the next session's monitor start) cleared it.
DRIVER_FAULT_RE = re.compile(
    r"rtw_8822bu .*unused phy status page"
    r"|Rate marked as a \S+ rate but data is invalid"
    r"|WARNING: .* at net/mac80211/rx\.c")
RTW_MODULE = "rtw88_8822bu"


class DriverErrorWatch:
    """Follows the kernel log (journalctl -k -f; the adm group can read it,
    no sudo) and keeps the times of receive-path errors from the WiFi
    driver, for count(t0, t1) per window."""

    def __init__(self):
        self.times = deque(maxlen=200000)
        self.lock = threading.Lock()
        self.proc = None

    def start(self):
        if not shutil.which("journalctl"):
            log.warning("journalctl not found -- WiFi driver errors are not watched")
            return False
        self.proc = subprocess.Popen(
            ["journalctl", "-k", "-f", "-n", "0", "-o", "short-unix"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._read, daemon=True).start()
        log.info("watching the kernel log for WiFi driver receive errors")
        return True

    def _read(self):
        for line in self.proc.stdout:
            if DRIVER_FAULT_RE.search(line):
                try:
                    t = float(line.split(" ", 1)[0])
                except ValueError:
                    t = time.time()
                with self.lock:
                    self.times.append(t)

    def count(self, t0, t1):
        with self.lock:
            return sum(1 for t in self.times if t0 <= t < t1)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class CollapseCheck:
    """Beacons keep coming when a channel is busy, so a window whose
    beaconing-network count collapses (below `frac` of the recent healthy
    median) while airtime jumps (at or above `airtime_pct`) is the receiver
    decoding garbage, not load. The baseline is seeded from the survey's
    windows on the forced channel, so a fault in the very first window is
    caught too."""

    def __init__(self, frac, airtime_pct, seed=(), keep=20, min_base=8):
        self.frac, self.airtime_pct, self.min_base = frac, airtime_pct, min_base
        self.nets = deque(seed, maxlen=keep)

    def collapsed(self, s):
        if len(self.nets) < 3 or s["airtime_pct"] in ("", None):
            return False
        base = statistics.median(self.nets)
        return (base >= self.min_base and s["beaconing_networks"] < self.frac * base
                and s["airtime_pct"] >= self.airtime_pct)

    def healthy(self, s):
        self.nets.append(s["beaconing_networks"])


def _survey_networks(bg_dir, channel):
    """The survey's beaconing-network counts on `channel` (seed for
    CollapseCheck); empty if there was no survey."""
    path = Path(bg_dir) / "survey.csv"
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return [int(r["beaconing_networks"]) for r in csv.DictReader(f)
                if str(r.get("wifi_channel")) == str(channel)
                and str(r.get("beaconing_networks", "")).isdigit()]


def restart_wifi(cap, iface, bg_dir, channel, reload_module=False):
    """Stop the capture, bounce the interface out of and back into monitor
    mode (optionally reloading the driver module first), restart the
    capture and retune. Returns the new WifiCapture."""
    cap.stop()
    leave_monitor_mode(iface)
    if reload_module:
        log.error(f"reloading the {RTW_MODULE} driver module")
        for cmd in (["sudo", "-n", "modprobe", "-r", RTW_MODULE],
                    ["sudo", "-n", "modprobe", RTW_MODULE]):
            r = _run(cmd, check=False)
            if r.returncode != 0:
                log.error(f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip()[:200]}")
            time.sleep(2)
        for _ in range(30):  # wait for the interface to come back
            if Path(f"/sys/class/net/{iface}").exists():
                break
            time.sleep(0.5)
        time.sleep(2)
    enter_monitor_mode(iface)
    cap = WifiCapture(iface, bg_dir)
    cap.start()
    tune(iface, channel)
    time.sleep(RETUNE_SETTLE_S)
    return cap


def dwell(iface, channel, seconds):
    """Tune, settle, listen `seconds`; returns (t0, t1), or None if the
    channel couldn't be tuned. Summarize later with cap.buf.summarize once
    PIPELINE_LAG_S has passed, so a sweep pays the lag once, not per channel."""
    if not tune(iface, channel):
        return None
    time.sleep(RETUNE_SETTLE_S)
    t0 = time.time()
    time.sleep(seconds)
    return t0, time.time()


SWEEP_FIELDS = ["sweep_id", "wall_time", "t_start", "t_end", "band", "wifi_channel",
                "airtime_pct", "frames", "frames_no_duration", "beaconing_networks"]


def sweep(cap, iface, channels_24, channels_5, dwell_s, sweep_id, out, faulty=None):
    """One pass over the sweep channels. Returns {band: networks} where
    networks is the sum of per-channel distinct beaconing BSSIDs (a network
    heard on two overlapping 2.4 GHz channels is counted once per channel
    -- comparable across sessions, not an exact census). faulty(t0, t1)
    True -> that dwell is blanked and its band's total is "" (unknown)."""
    windows = []
    for band, chans in (("2.4GHz", channels_24), ("5GHz", channels_5)):
        for ch in chans:
            w = dwell(iface, ch, dwell_s)
            if w is not None:
                windows.append((band, ch, *w))
    time.sleep(PIPELINE_LAG_S)
    totals = {}
    for band, ch, t0, t1 in windows:
        s = cap.buf.summarize(t0, t1, ch)
        if faulty and faulty(t0, t1):
            s["airtime_pct"] = s["beaconing_networks"] = ""
            totals[band] = ""
        out.write({"sweep_id": sweep_id, "wall_time": _wall(t0),
                   "t_start": round(t0, 3), "t_end": round(t1, 3), "band": band,
                   "wifi_channel": ch, "airtime_pct": s["airtime_pct"],
                   "frames": s["frames"], "frames_no_duration": s["frames_no_duration"],
                   "beaconing_networks": s["beaconing_networks"]})
        if totals.get(band, 0) != "":
            totals[band] = totals.get(band, 0) + s["beaconing_networks"]
    return totals


# =============================================================================
# BLE advertiser counting (nRF sniffer, in memory only)
# =============================================================================


class BleCounter:
    """nrfutil-ble-sniffer (no --follow) -> FIFO in a private temp dir ->
    tshark -> per-window distinct-advertiser counts. The FIFO holds nothing
    once read; the temp dir is removed on stop."""

    FIELDS = ["frame.number", "btle.advertising_address",
              "btle.advertising_header.pdu_type", "nordic_ble.crcok", "nordic_ble.rssi"]

    def __init__(self, port, out, window_s=60, nearby_rssi_dbm=-80, min_packets=2):
        self.port = port
        self.out = out
        self.window_s = window_s
        self.nearby_rssi = nearby_rssi_dbm
        self.min_packets = min_packets
        self.tmp = None
        self.sniffer = self.tshark = self.thread = None

    def start(self):
        binary = _resolve_sniffer_binary()
        if not binary or not Path(self.port).exists():
            log.warning(f"BLE sniffer unavailable (binary={binary}, port={self.port}) -- "
                        f"no Bluetooth crowding counts this session")
            return False
        self.tmp = tempfile.mkdtemp(prefix="blecount_")
        fifo = os.path.join(self.tmp, "adv.fifo")
        os.mkfifo(fifo, 0o600)
        cmd = ["tshark", "-l", "-n", "-r", fifo, "-T", "fields", "-E", "separator=\t"]
        for f in self.FIELDS:
            cmd += ["-e", f]
        # Reader first: opening the FIFO blocks until the sniffer opens it.
        self.tshark = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
        self.sniffer = subprocess.Popen(
            [binary, "sniff", "--port", self.port, "--output-pcap-file", fifo,
             "--log-output", "stdout", "--log-level", "error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        log.info(f"BLE advertiser counter running on {self.port} "
                 f"({self.window_s}s windows, counts only)")
        return True

    def _flush(self, w0, w1, counts, max_rssi, packets, crc_bad):
        seen = [a for a, n in counts.items() if n >= self.min_packets]
        nearby = [a for a in seen if max_rssi.get(a, -999) >= self.nearby_rssi]
        total = packets + crc_bad
        self.out.write({
            "wall_time": _wall(w0), "t_start": round(w0, 3), "t_end": round(w1, 3),
            "advertisers": len(seen), "advertisers_nearby": len(nearby),
            "adv_packets": packets,
            "crc_error_pct": round(100 * crc_bad / total, 1) if total else "",
        })

    def _read(self):
        # Windows run on ARRIVAL time, not the pcap timestamps: the
        # sniffer's own clock jumped minutes ahead within 23 s in a
        # 2026-10-12 test. Arrival lags the air by ~1 s, steady. The first
        # seconds are a burst of whatever the dongle buffered before the
        # FIFO opened (~5x the steady rate), so they're skipped.
        w0 = None
        first = None
        counts, max_rssi = {}, {}
        packets = crc_bad = 0
        for line in self.tshark.stdout:
            p = line.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            t = time.time()
            if first is None:
                first = t
            if t < first + BLE_WARMUP_S:
                continue
            if w0 is None:
                w0 = t
            while t >= w0 + self.window_s:
                self._flush(w0, w0 + self.window_s, counts, max_rssi, packets, crc_bad)
                counts, max_rssi = {}, {}  # addresses dropped every window
                packets = crc_bad = 0
                w0 += self.window_s
            if p[3] not in ("1", "True"):
                crc_bad += 1
                continue
            pdu = _int_or_none(p[2], 0)
            addr = p[1]
            if pdu not in ADV_PDU_TYPES_WITH_ADVA or not addr:
                continue
            packets += 1
            counts[addr] = counts.get(addr, 0) + 1
            rssi = _int_or_none(p[4])
            if rssi is not None and rssi > max_rssi.get(addr, -999):
                max_rssi[addr] = rssi

    def alive(self):
        return self.sniffer.poll() is None and self.tshark.poll() is None

    def stop(self):
        for p in (self.sniffer, self.tshark):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


BLE_FIELDS = ["wall_time", "t_start", "t_end", "advertisers", "advertisers_nearby",
              "adv_packets", "crc_error_pct"]


# =============================================================================
# Subcommands
# =============================================================================


GYM_SESSION_PREFIX = "gym_"
GYM_SESSION_RE = re.compile(r"^gym_\d{8}_\d{6}$")


def new_gym_session_dir(cfg):
    """captures/gym_YYYYmmdd_HHMMSS. The prefix also keeps chamber_session.py's
    own "latest session" lookup (bare timestamps only) from ever picking up
    a gym session."""
    d = get_output_dir(cfg) / (GYM_SESSION_PREFIX + time.strftime("%Y%m%d_%H%M%S"))
    d.mkdir(parents=True)
    return d


def resolve_session_dir(cfg, session):
    """An existing session dir: `session` as given (a chamber session works
    too, for the chamber airtime check), or with gym_ added, or -- when
    None -- the most recent gym_ session. Exits with a message if none."""
    base = get_output_dir(cfg)
    if session:
        for name in (session, GYM_SESSION_PREFIX + session):
            if (base / name).is_dir():
                return base / name
        sys.exit(f"No session directory {base / session}")
    names = sorted(p.name for p in base.iterdir() if p.is_dir() and GYM_SESSION_RE.match(p.name))
    if not names:
        sys.exit("No gym sessions found -- run `background_monitor.py survey` first")
    return base / names[-1]


def _resolve_session_dir(cfg, session, create):
    if create and not session:
        return new_gym_session_dir(cfg)
    return resolve_session_dir(cfg, session)


def _check_sudo():
    """Ask for the password once, then keep sudo's timestamp fresh so a
    capture restart or the final switch back to managed mode, hours later,
    still works without a prompt."""
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        sys.exit("sudo is needed for monitor mode and dumpcap")

    def keepalive():
        while True:
            time.sleep(240)
            subprocess.run(["sudo", "-n", "-v"], capture_output=True)

    threading.Thread(target=keepalive, daemon=True).start()


def _write_meta(bg_dir, mcfg, iface, extra):
    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001 -- best-effort
            return ""
    driver = ""
    try:
        driver = os.path.basename(os.readlink(f"/sys/class/net/{iface}/device/driver"))
    except OSError:
        pass
    meta = {
        "started_at": _wall(time.time()),
        "wifi_iface": iface, "wifi_driver": driver,
        "tshark": ver(["tshark", "--version"]),
        "nrfutil_ble_sniffer": ver([_resolve_sniffer_binary() or "true", "--version"]),
        "settings": mcfg, **extra,
    }
    path = bg_dir / "monitor_meta.json"
    n = 2
    while path.exists():
        path = bg_dir / f"monitor_meta_{n}.json"
        n += 1
    path.write_text(json.dumps(meta, indent=2) + "\n")


def cmd_survey(cfg, args):
    """Hop channels 1-13 in monitor mode, measure airtime on each, and pick
    the busiest as the session's forced channel."""
    mcfg = _mcfg(cfg)
    session_dir = _resolve_session_dir(cfg, args.session, create=True)
    bg = background_dir(session_dir)
    _setup_logging(bg)
    iface = mcfg["wifi_iface"]
    channels = mcfg.get("sweep_channels_24", list(range(1, 14)))
    dwell_s = mcfg.get("survey_dwell_s", 15)
    passes = mcfg.get("survey_passes", 2)
    log.info(f"Survey: session {session_dir.name}, {len(channels)} channels x "
             f"{dwell_s}s x {passes} passes (~{len(channels) * dwell_s * passes / 60:.0f} min)")
    _check_sudo()
    enter_monitor_mode(iface)
    cap = WifiCapture(iface, bg)
    out = CsvLog(bg / "survey.csv", ["pass", "wall_time", "t_start", "t_end",
                                     "wifi_channel", "airtime_pct", "frames",
                                     "frames_no_duration", "beaconing_networks"])
    per_channel = {}
    try:
        cap.start()
        time.sleep(1)
        for n in range(1, passes + 1):
            for ch in channels:
                w = dwell(iface, ch, dwell_s)
                if w is None:
                    log.warning(f"  channel {ch}: could not tune, skipped")
                    continue
                time.sleep(PIPELINE_LAG_S)
                s = cap.buf.summarize(*w, ch)
                s.update(t_start=round(w[0], 3), t_end=round(w[1], 3))
                out.write({"pass": n, "wall_time": _wall(s["t_start"]), "t_start": s["t_start"],
                           "t_end": s["t_end"], "wifi_channel": ch,
                           "airtime_pct": s["airtime_pct"], "frames": s["frames"],
                           "frames_no_duration": s["frames_no_duration"],
                           "beaconing_networks": s["beaconing_networks"]})
                per_channel.setdefault(ch, []).append(s["airtime_pct"])
                log.info(f"  pass {n} ch {ch:2d}: airtime {s['airtime_pct']:5.1f}%  "
                         f"frames {s['frames']}  networks {s['beaconing_networks']}")
    finally:
        cap.stop()
        out.close()
        leave_monitor_mode(iface)
    if not per_channel:
        sys.exit("Survey measured nothing -- is the adapter plugged in and monitor-capable?")
    means = {ch: round(statistics.mean(v), 2) for ch, v in per_channel.items()}
    busiest = max(means, key=means.get)
    chosen = args.channel or busiest
    record = {
        "wifi_channel": chosen,
        "method": "manual" if args.channel else "survey_busiest_airtime",
        "survey_busiest_channel": busiest,
        "survey_mean_airtime_pct": means,
        "chosen_at": _wall(time.time()),
    }
    (bg / FORCED_CHANNEL_FILE).write_text(json.dumps(record, indent=2) + "\n")
    log.info(f"Busiest channel by airtime: {busiest} ({means[busiest]}%); "
             f"forced channel for this session: {chosen}")
    print(f"Session: {session_dir}")


def cmd_run(cfg, args):
    mcfg = _mcfg(cfg)
    session_dir = _resolve_session_dir(cfg, args.session, create=False)
    bg = background_dir(session_dir)
    _setup_logging(bg, console=not args.quiet)
    fc_path = bg / FORCED_CHANNEL_FILE
    if args.channel:
        channel = args.channel
        if not fc_path.exists():
            fc_path.write_text(json.dumps({"wifi_channel": channel, "method": "manual",
                                           "chosen_at": _wall(time.time())}, indent=2) + "\n")
    elif fc_path.exists():
        channel = json.loads(fc_path.read_text())["wifi_channel"]
    else:
        sys.exit(f"No {fc_path} -- run `survey` first or pass --channel")
    iface = mcfg["wifi_iface"]
    report_s = mcfg.get("airtime_report_s", 15)
    sweep_every = mcfg.get("sweep_interval_s", 900)
    sweep_dwell = mcfg.get("sweep_dwell_s", 2.0)
    ch24 = mcfg.get("sweep_channels_24", list(range(1, 14)))
    ch5 = mcfg.get("sweep_channels_5", [])
    log.info(f"Background monitor: session {session_dir.name}, airtime on WiFi channel "
             f"{channel} every {report_s}s, sweep every {sweep_every}s")
    _check_sudo()
    _write_meta(bg, mcfg, iface, {"wifi_channel": channel})

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    airtime_out = CsvLog(bg / "airtime.csv", [
        "wall_time", "t_start", "t_end", "wifi_channel", "airtime_pct", "frames",
        "frames_no_duration", "mgmt_frames", "ctrl_frames", "data_frames", "beaconing_networks",
        "fault"])
    driver_errors_min = mcfg.get("driver_errors_min", 3)
    collapse = CollapseCheck(mcfg.get("collapse_networks_frac", 0.3),
                             mcfg.get("collapse_airtime_pct", 50),
                             seed=_survey_networks(bg, channel))
    watch = DriverErrorWatch()

    def driver_faulty(t0, t1):
        return watch.alive() and watch.count(t0, t1) >= driver_errors_min
    sweep_out = CsvLog(bg / "sweep.csv", SWEEP_FIELDS)
    net_out = CsvLog(bg / "networks.csv", ["sweep_id", "wall_time", "t_start", "t_end",
                                           "networks_2_4ghz", "networks_5ghz"])
    ble_out = CsvLog(bg / "ble_advertisers.csv", BLE_FIELDS)

    enter_monitor_mode(iface)
    cap = WifiCapture(iface, bg)
    ble = BleCounter(mcfg.get("ble_sniffer_port", "/dev/ttyACM0"), ble_out,
                     window_s=mcfg.get("ble_window_s", 60),
                     nearby_rssi_dbm=mcfg.get("ble_nearby_rssi_dbm", -80),
                     min_packets=mcfg.get("ble_min_packets", 2))
    sweep_id = 0
    try:
        cap.start()
        watch.start()
        ble_on = ble.start()
        time.sleep(1)
        next_sweep = 0  # sweep once at start so network counts exist from t=0
        heard_beacons = False  # this channel has had beacons this session
        stalled = 0  # consecutive stalled windows
        faulted = 0  # consecutive faulty windows (driver errors / collapse)
        while not stop.is_set():
            if time.time() >= next_sweep:
                sweep_id += 1
                t0 = time.time()
                totals = sweep(cap, iface, ch24, ch5, sweep_dwell, sweep_id, sweep_out,
                               faulty=driver_faulty)
                net_out.write({"sweep_id": sweep_id, "wall_time": _wall(t0),
                               "t_start": round(t0, 3), "t_end": round(time.time(), 3),
                               "networks_2_4ghz": totals.get("2.4GHz", ""),
                               "networks_5ghz": totals.get("5GHz", "")})
                log.info(f"sweep {sweep_id}: networks 2.4 GHz={totals.get('2.4GHz')} "
                         f"5 GHz={totals.get('5GHz', 'n/a')} ({time.time() - t0:.0f}s)")
                next_sweep = time.time() + sweep_every
                if not tune(iface, channel):
                    raise RuntimeError(f"could not tune back to channel {channel}")
                time.sleep(RETUNE_SETTLE_S)
            t0 = time.time()
            stop.wait(report_s)
            t1 = time.time()
            time.sleep(PIPELINE_LAG_S)
            s = cap.buf.summarize(t0, t1, channel)
            # A window with no frames at all on a channel that has had
            # beacons is the receiver stalling, not an idle channel: beacons
            # come every ~100 ms. Seen 2026-10-12 (gym_20261012_165947):
            # after sweep 5 ended on 5 GHz ch 140 and retuned to ch 11, the
            # adapter delivered nothing for 15 min, until the next sweep
            # retuned it -- and those windows were written as 0.0 %.
            # A stalled window's airtime is left blank, so it can't pass
            # for a measurement; then retune, then bounce the interface.
            heard_beacons = heard_beacons or s["beaconing_networks"] > 0
            s["fault"] = ""
            if heard_beacons and s["frames"] == 0:
                stalled += 1
                faulted = 0  # the stall handling below restarts the adapter
                s["airtime_pct"], s["fault"] = "", "stalled"
            else:
                stalled = 0
            # A receiver decoding garbage (see DRIVER_FAULT_RE): blank the
            # window like a stall, then reset the adapter.
            n_err = watch.count(t0, t1 + PIPELINE_LAG_S) if watch.alive() else 0
            if not s["fault"]:
                if n_err >= driver_errors_min:
                    s["fault"] = f"driver_errors={n_err}"
                elif collapse.collapsed(s):
                    s["fault"] = (f"collapse: networks={s['beaconing_networks']} "
                                  f"airtime={s['airtime_pct']}")
                if s["fault"]:
                    s["airtime_pct"] = ""
                    faulted += 1
                else:
                    faulted = 0
                    collapse.healthy(s)
            airtime_out.write({"wall_time": _wall(t0), "t_start": round(t0, 3),
                               "t_end": round(t1, 3), "wifi_channel": channel, **s})
            log.debug(f"ch {channel}: airtime {s['airtime_pct']}% frames {s['frames']}")
            if stalled == 1:
                log.warning(f"no frames at all on channel {channel} for {report_s}s -- "
                            f"receiver stalled? retuning")
                tune(iface, channel)
            elif stalled >= 2:
                log.error(f"still no frames on channel {channel} -- restarting the "
                          f"monitor interface and capture")
                cap = restart_wifi(cap, iface, bg, channel)
                stalled = 0
            if faulted:
                # 1st and 2nd faulty window in a row: bounce the interface;
                # 3rd: the bounce didn't help, reload the driver module.
                reload_module = faulted >= 3
                log.error(f"WiFi receiver fault on channel {channel} ({s['fault']}) -- "
                          f"airtime blanked; "
                          + ("reloading the driver" if reload_module else
                             "restarting the monitor interface"))
                cap = restart_wifi(cap, iface, bg, channel, reload_module=reload_module)
                if reload_module:
                    faulted = 0
            if not cap.alive():
                log.error("WiFi capture died -- restarting it")
                cap.stop()
                cap = WifiCapture(iface, bg)
                cap.start()
            if ble_on and not ble.alive():
                log.error("BLE sniffer/tshark died -- restarting it")
                ble.stop()
                ble_on = ble.start()
    finally:
        cap.stop()
        watch.stop()
        ble.stop()
        leave_monitor_mode(iface)
        for o in (airtime_out, sweep_out, net_out, ble_out):
            o.close()
        log.info("Background monitor stopped")


def log_people(session_dir, count, note=""):
    """Append one people count to background/people.csv; returns its time."""
    out = CsvLog(background_dir(session_dir) / "people.csv",
                 ["wall_time", "t", "people", "note"])
    t = time.time()
    out.write({"wall_time": _wall(t), "t": round(t, 3), "people": count, "note": note})
    out.close()
    return t


def cmd_people(cfg, args):
    session_dir = _resolve_session_dir(cfg, args.session, create=False)
    t = log_people(session_dir, args.count, args.note or "")
    print(f"{session_dir.name}: {args.count} people at {_wall(t)}")


def cmd_check(cfg, args):
    """Hardware self-test: 10 s of airtime on one channel and 10 s of BLE
    counting, printed only (nothing kept)."""
    mcfg = _mcfg(cfg)
    iface = mcfg["wifi_iface"]
    tmp = Path(tempfile.mkdtemp(prefix="bgcheck_"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _check_sudo()
    enter_monitor_mode(iface)
    cap = WifiCapture(iface, tmp)
    try:
        cap.start()
        time.sleep(1)
        w = dwell(iface, args.channel, 10)
        time.sleep(PIPELINE_LAG_S)
        s = cap.buf.summarize(*w, args.channel) if w else None
        print(f"WiFi ch {args.channel}: {s}")
        if s and s["frames"] and s["frames_no_duration"] == s["frames"]:
            print("  !! no frame had a computable duration -- the driver's radiotap "
                  "lacks rate info, airtime can't be measured with this adapter")
    finally:
        cap.stop()
        leave_monitor_mode(iface)

    class _Print:
        def write(self, row):
            print(f"BLE: {row}")
    ble = BleCounter(mcfg.get("ble_sniffer_port", "/dev/ttyACM0"), _Print(), window_s=10)
    if ble.start():
        time.sleep(12)
        ble.stop()
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("survey", help="pick the busiest WiFi channel (creates a session)")
    p.add_argument("--session", help="attach to an existing session instead of creating one")
    p.add_argument("--channel", type=int, help="record the survey but force this channel")
    p = sub.add_parser("run", help="continuous background monitoring until Ctrl+C")
    p.add_argument("--session", help="session folder name (default: most recent gym_ session)")
    p.add_argument("--channel", type=int, help="WiFi channel to watch (overrides the survey)")
    p.add_argument("--quiet", action="store_true", help="log to background/monitor.log only")
    p = sub.add_parser("people", help="log a people count")
    p.add_argument("count", type=int)
    p.add_argument("--note")
    p.add_argument("--session", help="session folder name (default: most recent gym_ session)")
    p = sub.add_parser("check", help="10 s hardware self-test, prints only")
    p.add_argument("--channel", type=int, default=6)
    args = parser.parse_args()
    cfg = load_config(args.config)
    {"survey": cmd_survey, "run": cmd_run, "people": cmd_people, "check": cmd_check}[
        args.command](cfg, args)


if __name__ == "__main__":
    main()
