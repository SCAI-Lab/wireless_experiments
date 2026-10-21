#!/usr/bin/env python3
"""
analyze_session.py -- analysis of BLE coexistence test sessions
================================================================

Reads one or more session directories (or .zip exports) produced by
chamber_session.py / gym_session.py and writes, per session, into
    <out>/<session_id>/
        report.md               sanity checks + headline numbers
        meta.json               provenance (input path, hashes, script version)
        tables/per_run.csv      one row per capture, all metrics
        tables/per_condition.csv
        tables/link_params.csv
        tables/sanity.csv
        tables/*.tex            booktabs tables for the paper
        figures/<session_id>_*.pdf  (vector, plus a .png preview)

With --pool, additionally writes <out>/pooled_<id1>+<id2>+.../ with
per-condition statistics pooled across sessions (e.g. time to first link loss)
and the paper's fig:deadline (figures/pooled_fig_deadline.pdf; see FIG_DEADLINE_* constants),
fig:headroom (pooled_fig_headroom.pdf, Polar M(D) against headroom k; FIG_HEADROOM_*) and,
when gym sessions are pooled with chamber ones, fig:gymchamber (pooled_fig_gym_chamber.pdf,
Polar M(100 ms) per run, chamber vs gym occupancy bands; FIG_GYMCHAMBER_*, GYM_OCCUPANCY_BANDS).
Each paper figure's plotted per-run values are written next to it as a .csv.
The pooled report also tabulates Polar runaway counts with Wilson CIs and the runs
needed for a useful estimate (pooled_runaway.csv), M(D) for added delay vs end-to-end
sample age (pooled_framing.csv), and the clean BLE data channels left by WiFi on
1/6/11 (channel_arithmetic.csv).

Everything is derived from the HCI captures (btmon .btsnoop), the AFH poller
CSVs, manifest.csv and session.log -- NOT from the collection script's own
results.csv, which is only cross-checked in the sanity section.

Metric definitions (match the paper's Methods):
  - link loss: a Disconnection Complete with reason != 0x16 (local host)
    after the startup window; reasons 0x08 supervision timeout, 0x3E failed
    to establish, 0x13 remote terminated.
  - load onset: the iperf3 start time from session.log (no-load runs: the
    same offset after driver launch as the session's loaded runs, median).
  - planned window: load onset -> driver stop (or end of capture).
  - Polar ECG completeness: samples received in the planned window /
    (130 Hz x window length). Data never delivered counts as missing.
  - Polar added delay: host arrival - sensor frame timestamp, relative to the
    run's minimum, with the session's clock drift removed (median lower-
    envelope slope of its no-load runs). Delay percentiles use frames that
    arrived in the planned window before the first link loss.
  - deadline miss(D): 1 - (ECG samples delivered with delay <= D) /
    (samples expected in the planned window). Undelivered data are misses.
    miss_age(D): the same with end-to-end sample age, i.e. added delay plus the
    sensor's framing (sample j of an n-sample frame is (n-1-j)/130 s older).
  - runaway (Polar): added delay reaches RUNAWAY_DELAY_S and never falls below
    RUNAWAY_RECOVER_S again in the window; implied capacity = 1 - ramp slope.
  - sniff_* (runs with an nRF-sniffer pcap): link layer in the planned
    window, shares over what the sniffer heard; see sniffer_metrics().
  - Corsano delivery: payload bytes/s in the planned window relative to the
    session's no-load median; plus host request rate (ATT Write Command) and
    notifications per request.
  - p_hat (Polar only): (n_0x3E / n_connection_attempts) ** (1/6).

Usage:
  python3 analyze_session.py SESSION [SESSION ...] [--out analysis] [--pool]
  SESSION = session directory (contains manifest.csv) or a .zip export.
"""

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SCRIPT_VERSION = "1.4.0"   # 1.4.0: a notification arrives at the host with its LAST ACL fragment (was: first)
# previous: SCRIPT_VERSION = "1.3.0"   # 1.3.0: end-to-end age M(D), runaway/backlog, nRF-sniffer link layer, pooled runaway + framing + channel tables

# ----------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------
HCI_CMD, HCI_EVT, ACL_TX, ACL_RX = 2, 3, 4, 5            # btmon monitor opcodes
CTRL_CMD, CTRL_EVT = 16, 17                               # MGMT (bluetoothd <-> kernel)
MGMT_DISCONNECT = 0x0014
WEDGE_MIN_TAIL_S = 30.0         # silence after a failed MGMT Disconnect that counts as a wedge
REASONS = {0x08: "sup_timeout", 0x13: "remote_term", 0x16: "local_host",
           0x3E: "failed_establish", 0x22: "ll_response_timeout"}
WIFI_CH6_OVERLAP = set(range(12, 20))                     # BLE data ch 12-19
ECG_FS = 130.0
STARTUP_WINDOW_S = 15.0         # disconnects earlier than this after capture start
                                # are startup artifacts (pre-existing link timing out)
DEFAULT_SETTLE_S = 40.0         # driver launch -> traffic when the session has no iperf line
                                # (chamber median: settle_delay_s 30 after a ~10 s connect)
DEADLINES_MS = [100, 250, 500, 1000, 5000, 10000]
DRIFT_MIN_RUN_S = 200.0         # no-load runs at least this long feed the session drift estimate
BTSNOOP_EPOCH_S = 62168256000.0 # btsnoop timestamps count from 0000-01-01; minus this = unix time
# runaway backlog (Polar): added delay reaches RUNAWAY_DELAY_S and never falls back
# below RUNAWAY_RECOVER_S before the window ends; onset = last time below RECOVER
RUNAWAY_DELAY_S = 2.0
RUNAWAY_RECOVER_S = 0.5
BACKLOG_DELAY_S = 1.0           # backlog_time_pct: share of the window with added delay above this
# nRF sniffer (manifest sniffer_pcap_file): the next packet in a connection event
# starts 4 us/byte * PDU length + this after a PDU starts (2M PHY, measured)
SNIFF_IFS_US, SNIFF_IFS_TOL_US = 194.0, 15.0
# BLE data channel centres (MHz) and the WiFi 1/6/11 band half-widths for the
# clean-channel count (pooled report): occupied OFDM, nominal, spectral-mask -20 dBr
BLE_DATA_MHZ = {i: (2404 + 2 * i if i <= 10 else 2406 + 2 * i) for i in range(37)}
WIFI_NONOVERLAP_MHZ = {1: 2412, 6: 2437, 11: 2462}
WIFI_HALF_WIDTHS_MHZ = [("OFDM occupied (52 subcarriers)", 8.125), ("nominal 20 MHz", 10.0),
                        ("spectral mask -20 dBr", 11.0)]

# paper figures (pooled output only): IEEE Transactions layout -- 3.5 in single /
# 7.16 in double column, Times-family text at 8 pt, fonts embedded as TrueType
# (no Type 3, for PDF eXpress), colours that stay distinct in grayscale print
IEEE_SINGLE_COL_IN, IEEE_DOUBLE_COL_IN = 3.5, 7.16
IEEE_RC = {
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Liberation Serif", "DejaVu Serif"],   # TrueType only (Type 42)
    "mathtext.fontset": "stix",
    "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8, "legend.fontsize": 7,
    "xtick.labelsize": 8, "ytick.labelsize": 8,
    "axes.linewidth": .5, "xtick.major.width": .5, "ytick.major.width": .5,
    "xtick.minor.width": .4, "ytick.minor.width": .4, "xtick.direction": "in", "ytick.direction": "in",
    "axes.grid": True, "grid.linewidth": .4, "grid.alpha": .3,
    "legend.frameon": False, "legend.handlelength": 1.6,
    "pdf.fonttype": 42, "ps.fonttype": 42,
}
TTL_STYLE = [("#eb6834", "s"), ("#0d366b", "o")]   # per device (sorted): Corsano, Polar_H10

# paper fig:deadline (deadline-miss curves + delay timelines)
FIG_DEADLINE_LOAD_PCT = 75.0                      # headroom conditions shown in panel (a)
FIG_DEADLINE_REFERENCE = "duty0pct"               # no-load reference curve (AFH free, no WiFi)
FIG_DEADLINE_DEADLINES_MS = [250, 1000, 10000]    # marked on panel (a)
FIG_DEADLINE_K_COLORS = {0: "#0d366b", 2: "#1c5cab", 4: "#3987e5", 8: "#86b6ef"}   # ordinal, dark = no headroom
# panel (b): every run in these conditions (all data ch 12-19, at FIG_DEADLINE_LOAD_PCT) in grey
FIG_DEADLINE_TIMELINE_CONDITIONS = ["headroom_k0_duty75pct", "forced_channel_duty75pct"]
# plus two highlighted runs under the same condition (data ch 12-19, 75 % load),
# one bounded and one runaway: (session, condition, replicate, label, color)
FIG_DEADLINE_TIMELINE_RUNS = [
    ("20261008_010249_chamber_2", "headroom_k0_duty75pct", 1, "$k$ = 0, rep. 1", "#0d366b"),
    ("20261008_010249_chamber_2", "forced_channel_duty75pct", 1, "ch 12\u201319, rep. 1", "#eb6834"),
]

# paper fig:headroom (Polar M(D) against headroom k; medians over runs, as Table tab:headroom)
FIG_HEADROOM_DEADLINES_MS = [100, 250, 1000]
FIG_HEADROOM_D_STYLE = {100: ("#0d366b", "o"), 250: ("#3987e5", "s"), 1000: ("#86b6ef", "^")}
FIG_HEADROOM_LOAD_STYLE = {75.0: "-", 100.0: "--"}           # one line per load
# conditions identical to headroom k = 0 at a load (pooled with it, Table footnote a)
FIG_HEADROOM_K0_ALIASES = {75.0: ["forced_channel_duty75pct"]}
FIG_HEADROOM_REFERENCE = "duty75pct"                         # AFH free, drawn right of the k axis
FIG_M_FLOOR_PCT = 0.05                                       # log axis: smaller values drawn here

# paper fig:gymchamber (Polar M(100 ms) per run: chamber without load, chamber headroom
# levels at FIG_GYMCHAMBER_LOAD_PCT, gym runs by occupancy band)
FIG_GYMCHAMBER_DEADLINE_MS = 100
FIG_GYMCHAMBER_LOAD_PCT = 75.0
# persons in the gym (most recent hand count before the run started), [lo, hi)
GYM_OCCUPANCY_BANDS = [("Low", 0, 10), ("Med.", 10, 20), ("High", 20, np.inf)]


# ----------------------------------------------------------------------------
# input handling
# ----------------------------------------------------------------------------
def resolve_session(path, tmpdirs):
    """Return a directory containing manifest.csv for a dir or .zip input."""
    if os.path.isdir(path):
        if os.path.exists(os.path.join(path, "manifest.csv")):
            return path
        subs = [os.path.join(path, d) for d in os.listdir(path)
                if os.path.exists(os.path.join(path, d, "manifest.csv"))]
        if len(subs) == 1:
            return subs[0]
        raise SystemExit(f"{path}: no manifest.csv found")
    if zipfile.is_zipfile(path):
        tmp = tempfile.mkdtemp(prefix="sess_")
        tmpdirs.append(tmp)
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp, [m for m in z.namelist()
                               if not m.endswith("/") and "_rosbag/" not in m])
        return resolve_session(tmp, tmpdirs)
    raise SystemExit(f"{path}: not a directory or zip")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# btsnoop / HCI parsing
# ----------------------------------------------------------------------------
def read_btsnoop(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"btsnoop\0":
        raise ValueError(f"{path}: not a btsnoop file")
    off, out = 16, []
    while off + 24 <= len(data):
        _ol, il, flags, _dr, ts = struct.unpack(">IIIIq", data[off:off + 24])
        off += 24
        out.append((ts / 1e6, flags & 0xFFFF, data[off:off + il]))
        off += il
    return out


def parse_capture(path, device):
    """Extract link events and application data from one capture."""
    recs = read_btsnoop(path)
    if not recs:
        return None
    t0, tend = recs[0][0], recs[-1][0]
    ev = dict(t0=t0, tend=tend, conns=[], updates=[], discs=[], dle=[], phy=[],
              notif=[], writes=[], reads=[], ecg=[], enc=0,
              advs=[], conn_cmds=[], scan_cmds=[], mgmt_disc_fail=[])
    rx_pending = {}            # ACL connection handle -> notification awaiting continuation fragments

    def _rx_complete(pend, t):
        ev["notif"].append((t - t0, pend["h"], pend["L"]))
        if pend["ts_ns"] is not None:
            ev["ecg"].append((t - t0, pend["ts_ns"] / 1e9, (pend["L"] - 10) // 3, pend["h"]))

    for t, op, p in recs:
        if op == CTRL_EVT and len(p) >= 9:
            # cookie(4) evt(2) | Command Complete: opcode(2) status(1)
            evt = int.from_bytes(p[4:6], "little")
            if evt == 0x0001 and int.from_bytes(p[6:8], "little") == MGMT_DISCONNECT and p[8]:
                ev["mgmt_disc_fail"].append((t - t0, p[8]))
        elif op == HCI_EVT and len(p) >= 2:
            code = p[0]
            if code == 0x05 and len(p) >= 6:                       # Disconnection Complete
                ev["discs"].append((t - t0, p[5]))
            elif code == 0x08:
                ev["enc"] += 1
            elif code == 0x3E and len(p) >= 4:                     # LE meta
                sub = p[2]
                if sub in (0x01, 0x0A, 0x03) and p[3] == 0:
                    base = {0x01: 14, 0x0A: 26, 0x03: 6}[sub]
                    if len(p) >= base + 6:
                        ci = int.from_bytes(p[base:base + 2], "little") * 1.25
                        lat = int.from_bytes(p[base + 2:base + 4], "little")
                        to = int.from_bytes(p[base + 4:base + 6], "little") * 10
                        (ev["updates"] if sub == 0x03 else ev["conns"]).append((t - t0, ci, lat, to))
                elif sub == 0x07 and len(p) >= 13:
                    ev["dle"].append(int.from_bytes(p[5:7], "little"))
                elif sub == 0x0C and len(p) >= 8 and p[3] == 0:
                    ev["phy"].append((p[6], p[7]))
                elif sub == 0x0D and len(p) >= 13:      # LE Extended Adv Report
                    et = int.from_bytes(p[4:6], "little")
                    ev["advs"].append((t - t0, p[7:13][::-1].hex(":"),
                                       bool(et & 0x01)))
                elif sub == 0x02 and len(p) >= 12:      # LE Adv Report (legacy)
                    ev["advs"].append((t - t0, p[6:12][::-1].hex(":"),
                                       p[4] in (0x00, 0x01)))
        elif op == HCI_CMD and len(p) >= 3:
            oc = int.from_bytes(p[0:2], "little")
            if oc in (0x200D, 0x2043):                  # (Ext) Create Connection
                ev["conn_cmds"].append(t - t0)
            elif oc in (0x200C, 0x2042) and len(p) >= 5:            # Scan Enable
                ev["scan_cmds"].append((t - t0, bool(p[3]), bool(p[4])))
        elif op in (ACL_TX, ACL_RX) and len(p) >= 4:
            conn = int.from_bytes(p[0:2], "little") & 0x0FFF
            if (int.from_bytes(p[0:2], "little") >> 12) & 3 == 1:  # continuation fragment: no L2CAP header
                # 1.4.0: an ATT PDU reaches the host only with its last ACL
                # fragment; under interference that can be hundreds of ms
                # after the first (link-layer retransmissions), so the
                # notification is stamped when it is complete.
                pend = rx_pending.get(conn) if op == ACL_RX else None
                if pend is not None:
                    pend["got"] += int.from_bytes(p[2:4], "little")
                    if pend["got"] >= pend["need"]:
                        rx_pending.pop(conn)
                        _rx_complete(pend, t)
                continue
            if len(p) < 9:
                continue
            if op == ACL_RX:
                rx_pending.pop(conn, None)          # a new start fragment ends any stale one
            if int.from_bytes(p[6:8], "little") != 4:              # ATT only
                continue
            att = p[8]
            L = int.from_bytes(p[4:6], "little") - 3               # ATT value length (L2CAP len)
            if op == ACL_RX and att == 0x1B and len(p) >= 11:
                pend = dict(h=int.from_bytes(p[9:11], "little"), L=L, ts_ns=None,
                            need=int.from_bytes(p[4:6], "little") + 4,
                            got=int.from_bytes(p[2:4], "little"))
                if "polar" in device.lower() and len(p) >= 21 and p[11] == 0:
                    pend["ts_ns"] = int.from_bytes(p[12:20], "little")
                if pend["got"] >= pend["need"]:
                    _rx_complete(pend, t)
                else:
                    rx_pending[conn] = pend
            elif op == ACL_TX and att == 0x52:
                ev["writes"].append(t - t0)
            elif op == ACL_TX and att == 0x0A:
                ev["reads"].append(t - t0)
    # keep ECG only from the handle carrying most ECG-like frames
    if ev["ecg"]:
        hc = collections.Counter(e[3] for e in ev["ecg"])
        h = hc.most_common(1)[0][0]
        ev["ecg"] = [e[:3] for e in ev["ecg"] if e[3] == h]
    return ev


def channel_set(hexmap):
    b = bytes.fromhex(hexmap)
    return {i for i in range(37) if (b[i // 8] >> (i % 8)) & 1}


def read_afh(path):
    maps = []
    if not os.path.exists(path):
        return maps
    with open(path) as f:
        for row in csv.DictReader(f):
            h = (row.get("channel_map_hex") or "").strip()
            if h:
                try:
                    maps.append(channel_set(h))
                except ValueError:
                    pass
    return maps


# ----------------------------------------------------------------------------
# session.log parsing
# ----------------------------------------------------------------------------
TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def parse_log(path):
    """Per-capture timing and settings from session.log."""
    info = collections.defaultdict(dict)
    session = dict(saturation=[], unmanaged=0, driver_cmds=collections.Counter())
    if not os.path.exists(path):
        return info, session
    cur = None
    with open(path, errors="replace") as f:
        for line in f:
            m = TS_RE.match(line)
            ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None
            if "-> capture file:" in line:
                cur = os.path.basename(line.split("capture file:")[1].strip())
                info[cur]["log_start"] = ts
            if cur is None:
                if re.search(r"max(imum)? (UDP )?throughput", line, re.I):
                    session["saturation"].append(line.strip()[20:])
                continue
            d = info[cur]
            if "starting btmon" in line:
                d["btmon_start"] = ts
            elif "launching driver:" in line:
                cmd = line.split("launching driver:")[1].strip()
                d["driver_cmd"] = cmd
                d["driver_launch"] = ts
                session["driver_cmds"][cmd] += 1
            elif "forcing BLE channel classification to" in line:
                d["forced_mask"] = [int(x) for x in re.findall(r"\d+", line.split("to", 1)[1].split("(")[0])]
                d["force_time"] = ts
            elif "starting iperf3" in line:
                d["iperf_start"] = ts
                mb = re.search(r"-b\s+([\d.]+)M", line)
                if mb:
                    d["iperf_target_mbps"] = float(mb.group(1))
            elif "set unmanaged in NetworkManager" in line:
                d["nm_unmanaged"] = True
                session["unmanaged"] += 1
            elif "QBSS channel-utilization poller running" in line:
                d["qbss_poller"] = True
            elif "stopping driver" in line:
                d["driver_stop"] = ts
            elif "stopping btmon" in line:
                d["btmon_stop"] = ts
                # the capture is over: a resumed session's pre-flight driver
                # launch/stop comes before the next "capture file:" line and
                # must not overwrite this capture's timing
                cur = None
            elif re.search(r"max(imum)? (UDP )?throughput", line, re.I):
                session["saturation"].append(line.strip()[20:])
    return info, session


# ----------------------------------------------------------------------------
# per-run metrics
# ----------------------------------------------------------------------------
def classify(cond):
    c = cond.lower()
    # gym_session.py's conditions: the load is the gym's own WiFi, measured
    # as airtime by background_monitor.py (no nominal duty), so
    # nominal_load_pct is NaN and these never count as no-load references.
    if c == "free_channel":
        return "gym_free", np.nan, None
    gk = re.fullmatch(r"forced_k(\d+)", c)
    if gk:
        return "gym_forced", np.nan, int(gk.group(1))
    if "headroom" in c:
        fam = "headroom"
    elif "forced" in c:
        fam = "constrained"
    elif "long" in c:
        fam = "free_long"
    elif "calibration" in c or "renegotiation" in c:
        fam = "calibration"
    elif "duty" in c:
        fam = "free"
    else:
        fam = "other"
    m = re.search(r"duty(\d+)pct", c)
    load = float(m.group(1)) if m else (100.0 if "max" in c else 0.0)
    if "bracket" in c:
        fam = "free_bracket" if fam == "free" else fam
    k = re.search(r"headroom_k(\d+)", c)
    return fam, load, int(k.group(1)) if k else None


def envelope_slope(t_host, off):
    """Sensor/host clock drift (s/s): linear fit to per-10s minima of the offset."""
    w = ((t_host - t_host[0]) // 10).astype(int)
    pts = np.array([(t_host[w == k].mean(), off[w == k].min()) for k in np.unique(w)])
    return np.polyfit(pts[:, 0], pts[:, 1], 1)[0]


def lower_envelope_detrend(t_host, off, drift=None):
    """Remove sensor/host clock drift. `drift` is the session's rate from its
    no-load runs; a per-run fit would absorb a growing backlog (k=0 runs fit
    to >10000 ppm), so it is only the fallback for long runs of a session
    without no-load runs."""
    if drift is None:
        if t_host[-1] - t_host[0] < 600:
            return off - off.min()
        drift = envelope_slope(t_host, off)
    d = off - drift * (t_host - t_host[0])
    return d - d.min()


def session_drift(sdir, manifest):
    """Median clock drift over a session's Polar no-load runs (~220 ppm on
    20261007, ~0 on 20261008: constant within a session, not across them).
    Gym sessions have no no-load runs: their AFH-free runs are used instead.
    Returns (drift or None, per-run slopes in ppm)."""
    ref = []
    for m in manifest:
        fam, load, _k = classify(m["condition"])
        if "polar" in m["device"].lower() and fam not in ("calibration", "other") \
                and (load == 0 or fam == "gym_free"):
            ref.append((fam, m))
    if any(f != "gym_free" for f, _ in ref):
        ref = [(f, m) for f, m in ref if f != "gym_free"]
    slopes = []
    for _f, m in ref:
        cap = m.get("capture_file") or ""
        if not os.path.exists(os.path.join(sdir, cap)):
            continue
        ev = parse_capture(os.path.join(sdir, cap), m["device"])
        if ev is None or not ev["ecg"]:
            continue
        a = np.array(ev["ecg"], dtype=float)
        if a[-1, 0] - a[0, 0] >= DRIFT_MIN_RUN_S:
            slopes.append(envelope_slope(a[:, 0], a[:, 0] - a[:, 1]) * 1e6)
    return (float(np.median(slopes)) / 1e6 if slopes else None), slopes


def reconnect_breakdown(ev, device, wend, losses, mac):
    """Separate "the link came back" from "the data came back".

    A reconnect count alone is misleading: row3 max_load rep1 (2026-09-28)
    logged 24 successful Connection Completes and still delivered 1.1% of its
    ECG, all of it from the first connection. Here every connection gets its
    own time-to-first-sample and samples per connected second, and every link
    loss gets the times until the sensor advertised again (any / connectable),
    the host tried to connect, the link was back, and data was back.

    Data = ECG samples for Polar, notifications otherwise. Advertising reports
    are only visible while our own controller is scanning, so the adv times
    are upper bounds unless the host was scanning throughout."""
    polar = "polar" in device.lower()
    data = [(t, n) for t, _ts, n in ev["ecg"]] if polar else [(t, 1) for t, _h, _l in ev["notif"]]
    downs = sorted(ev["discs"])
    segs = []
    for i, (tc, *_rest) in enumerate(ev["conns"]):
        if tc > wend:
            break
        nxt = next(((t, rs) for t, rs in downs if t > tc), None)
        t_end = min(nxt[0], wend) if nxt else wend
        d = [(t, n) for t, n in data if tc < t <= t_end]
        dur = max(t_end - tc, 1e-3)
        segs.append(dict(conn_idx=i, t_up_s=tc, t_down_s=t_end,
                         down_reason=hex(nxt[1]) if nxt and nxt[0] <= wend else "",
                         connected_s=dur, data_units=sum(n for _, n in d),
                         t_first_data_s=(d[0][0] - tc) if d else np.nan,
                         data_per_connected_s=sum(n for _, n in d) / dur,
                         after_first_loss=bool(losses) and tc > losses[0][0]))
    loss_rows = []
    for j, (tl, rs) in enumerate(losses):
        t_next_loss = losses[j + 1][0] if j + 1 < len(losses) else wend
        advs = [a for a in ev["advs"] if tl < a[0] <= t_next_loss and (not mac or a[1] == mac)]
        first = lambda xs: (xs[0] - tl) if xs else np.nan
        loss_rows.append(dict(
            loss_idx=j, t_loss_s=tl, reason=hex(rs),
            t_first_adv_s=first([a[0] for a in advs]),
            t_first_connectable_adv_s=first([a[0] for a in advs if a[2]]),
            n_nonconnectable_advs=sum(1 for a in advs if not a[2]),
            t_first_connect_cmd_s=first([c for c in ev["conn_cmds"] if tl < c <= wend]),
            t_link_back_s=first([c[0] for c in ev["conns"] if tl < c[0] <= wend]),
            t_data_back_s=first([t for t, _ in data if tl < t <= wend])))
    post = [s for s in segs if s["after_first_loss"]]
    post_s = sum(s["connected_s"] for s in post)
    post_units = sum(s["data_units"] for s in post)
    with_data = [s for s in post if s["data_units"] > 0]
    ttfs = [s["t_first_data_s"] for s in with_data]
    summ = dict(
        n_reconnects=len(post),
        n_reconnects_with_data=len(with_data),
        reconnect_data_yield_pct=100 * len(with_data) / len(post) if post else np.nan,
        ttfs_median_s=float(np.median(ttfs)) if ttfs else np.nan,
        ttfs_max_s=float(np.max(ttfs)) if ttfs else np.nan,
        post_loss_connected_s=post_s,
        post_loss_data_per_connected_s=post_units / post_s if post_s else np.nan,
        t_data_back_after_first_loss_s=loss_rows[0]["t_data_back_s"] if loss_rows else np.nan,
        recovered_data_after_loss=(float(any(t > losses[-1][0] for t, _ in data))
                                   if losses else np.nan))
    if polar:
        summ["post_loss_ecg_pct_of_nominal"] = (100 * summ["post_loss_data_per_connected_s"] / ECG_FS
                                                if post_s else np.nan)
    return segs, loss_rows, summ


def detect_stack_wedge(ev, dur):
    """bluetoothd left holding a stale "connected" device (see
    session_lib.heal_stale_bluez_connection): a failed MGMT Disconnect
    followed by no Create Connection for the rest of the capture. Returns
    (t_wedge or None, inherited) where inherited means the capture never
    issued a single connect, i.e. it started in the wedged state."""
    for t, _st in ev["mgmt_disc_fail"]:
        if dur - t >= WEDGE_MIN_TAIL_S and not any(c > t for c in ev["conn_cmds"]):
            return t, not ev["conn_cmds"]
    if ev["mgmt_disc_fail"] and not ev["conn_cmds"]:
        return 0.0, True
    return None, False


def delay_mask(t, onset, wend, losses):
    """Frames that enter the delay percentiles: arrived in the planned window
    and before the first link loss (losses: [(t, reason)] or [t])."""
    cut = (losses[0][0] if isinstance(losses[0], tuple) else losses[0]) if losses else np.inf
    return (t >= onset) & (t <= wend) & (t < cut)


def backlog_metrics(t, d_s):
    """Backlog of one Polar run from its in-window frames (t: s after load
    onset, d_s: added delay in s). A runaway reaches RUNAWAY_DELAY_S and never
    falls back below RUNAWAY_RECOVER_S; it is not absorbing in general (the
    queue is lossless and wanders near critical load), only within the window.
    The ramp from that onset to 80 % of the later peak gives the growth rate; a FIFO
    growing at g s/s is served at (1 - g) of the sensor's data rate."""
    r = dict(backlog_time_pct=np.nan, runaway=np.nan, runaway_onset_s=np.nan,
             backlog_growth_s_per_s=np.nan, implied_capacity_pct=np.nan)
    if len(d_s) < 20:
        return r
    r["backlog_time_pct"] = 100 * float(np.mean(d_s > BACKLOG_DELAY_S))
    # earlier excursions that recovered don't count: look after the last recovery
    below = np.nonzero(d_s < RUNAWAY_RECOVER_S)[0]
    i0 = below[-1] if len(below) else 0
    run = bool((d_s[i0:] >= RUNAWAY_DELAY_S).any())
    r["runaway"] = float(run)
    if run:
        i80 = i0 + int(np.argmax(d_s[i0:] >= 0.8 * d_s[i0:].max()))
        r["runaway_onset_s"] = float(t[i0])
        if i80 - i0 > 20:
            g = float(np.polyfit(t[i0:i80 + 1], d_s[i0:i80 + 1], 1)[0])
            r["backlog_growth_s_per_s"] = g
            r["implied_capacity_pct"] = 100 * (1 - g)
    return r


SNIFF_FIELDS = ["frame.time_epoch", "nordic_ble.event_counter", "nordic_ble.direction",
                "nordic_ble.crcok", "btle.access_address", "btle.data_header.llid",
                "btle.data_header.sequence_number", "btle.data_header.next_expected_sequence_number",
                "btle.data_header.more_data", "btle.data_header.length", "btatt.handle", "btatt.value"]
SNIFF_SUMMARY_COLS = ("sniff_event_coverage_pct", "sniff_nack_pct", "sniff_periph_md_pct",
                      "sniff_multi_pdu_event_pct", "sniff_after_air_p90_ms")


def sniffer_metrics(pcap, t0_unix, onset, wend, ecg_host=None):
    """Link-layer view of one run from its nRF-sniffer pcap (planned window only).
    The sniffer sits in the same interference and misses packets, so every
    share here is over what it heard (sniff_event_coverage_pct says how much):
      - nack_pct: peripheral payload PDU followed in the same event (T_IFS gap)
        by a central packet whose NESN == the PDU's SN. Losses that end the
        event (missed packets) are not NACKs and are not counted here.
      - periph_md_pct: peripheral payload PDUs with More Data set (its LL queue
        held another PDU).
      - multi_pdu_event_pct: events with >= 2 distinct new peripheral PDUs,
        among events where the sniffer heard >= 1.
      - after_air_*: Polar ECG frames matched by sensor timestamp; host arrival
        minus first time heard on air, relative to the run's 5th percentile
        (the sniffer's pcap clock has its own offset, constant within a run)."""
    nan = np.nan
    out = dict(sniff_event_coverage_pct=nan, sniff_periph_payload_pdus=0, sniff_nack_pairs=0,
               sniff_nack_pct=nan, sniff_periph_md_pct=nan, sniff_multi_pdu_event_pct=nan,
               sniff_ecg_frames_matched=0, sniff_after_air_p50_ms=nan, sniff_after_air_p90_ms=nan)
    cmd = ["tshark", "-r", pcap, "-T", "fields", "-E", "separator=\t", "-E", "occurrence=f"]
    for f in SNIFF_FIELDS:
        cmd += ["-e", f]
    try:
        txt = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return out
    rows = [p for p in (ln.split("\t") for ln in txt.splitlines())
            if len(p) == len(SNIFF_FIELDS) and p[5]]                  # data-channel PDUs
    if not rows:
        return out
    aa = collections.Counter(p[4] for p in rows).most_common(1)[0][0]
    w0, w1 = t0_unix + onset, t0_unix + wend
    tf = lambda s: s.lower() in ("true", "1")
    pk = []                     # (t, event, central, crc_ok, sn, nesn, md, len, handle, value)
    for p in rows:
        t = float(p[0])
        if p[4] != aa or not w0 <= t <= w1 or not p[1]:
            continue
        pk.append((t, int(p[1]), tf(p[2]), tf(p[3]), int(p[6] or 0), int(p[7] or 0), tf(p[8]),
                   int(p[9] or 0), p[10].lower(), p[11]))
    pk.sort()
    if not pk:
        return out
    ok = [p for p in pk if p[3]]
    n_ev = pk[-1][1] - pk[0][1] + 1
    out["sniff_event_coverage_pct"] = 100 * len({p[1] for p in ok if p[2]}) / n_ev
    nack = acks = 0
    pay, md, new_per_ev, prev = [], 0, collections.Counter(), None
    for i, p in enumerate(ok):
        if p[2] or p[7] == 0:
            continue
        pay.append(p)
        md += p[6]
        key = (p[4], p[7], p[9])
        if key != prev:
            new_per_ev[p[1]] += 1
        prev = key
        nxt = next((q for q in ok[i + 1:i + 4] if q[2]), None)
        if nxt and nxt[1] == p[1] and \
                abs((nxt[0] - p[0]) * 1e6 - 4 * p[7] - SNIFF_IFS_US) <= SNIFF_IFS_TOL_US:
            nack, acks = nack + (nxt[5] == p[4]), acks + (nxt[5] != p[4])
    out["sniff_periph_payload_pdus"] = len(pay)
    out["sniff_nack_pairs"] = nack + acks
    if nack + acks:
        out["sniff_nack_pct"] = 100 * nack / (nack + acks)
    if pay:
        out["sniff_periph_md_pct"] = 100 * md / len(pay)
        out["sniff_multi_pdu_event_pct"] = 100 * np.mean([c >= 2 for c in new_per_ev.values()])
    if ecg_host:
        first_air = {}
        for p in pay:
            v = p[9].replace(":", "")
            if len(v) >= 18 and v[:2] == "00":              # PMD ECG frame: type 0, then 8-byte ts
                ts = round(int.from_bytes(bytes.fromhex(v[2:18]), "little") / 1e9, 6)
                first_air.setdefault(ts, p[0])
        x = np.array([(t0_unix + ecg_host[ts] - ta) * 1000 for ts, ta in first_air.items()
                      if ts in ecg_host and onset <= ecg_host[ts] <= wend])
        out["sniff_ecg_frames_matched"] = len(x)
        if len(x) >= 10:
            x = np.clip(x - np.percentile(x, 5), 0, None)
            out["sniff_after_air_p50_ms"] = float(np.percentile(x, 50))
            out["sniff_after_air_p90_ms"] = float(np.percentile(x, 90))
    return out


GYM_BACKGROUND_COLS = (
    "bg_airtime_mean_pct", "bg_airtime_p95_pct", "bg_airtime_coverage_pct",
    "bg_ble_advertisers_mean", "bg_ble_advertisers_nearby_mean", "bg_people",
    "bg_networks_2_4ghz", "bg_networks_5ghz", "forced_wifi_channel",
    "forced_channels_n", "forced_extra_channels", "forced_extra_airtime_survey_pct",
    "forced_extra_airtime_sweep_pct", "pre_run_rssi_dbm", "pre_run_rssi_delta_db",
    "pre_run_note")


def gym_background(sdir):
    """Per-run background covariates written by gym_session.py
    (results_with_background.csv), keyed by capture file; {} for chamber
    sessions."""
    p = os.path.join(sdir, "results_with_background.csv")
    if not os.path.exists(p):
        return {}
    with open(p, newline="") as f:
        return {r["capture_file"]: r for r in csv.DictReader(f)}


def gym_people(sdir):
    """Hand-logged people counts (background/people.csv) as sorted [(unix t, persons)];
    [] for chamber sessions. Read here rather than from results_with_background.csv's
    bg_people, which is empty when the counts were entered after the session."""
    p = os.path.join(sdir, "background", "people.csv")
    if not os.path.exists(p):
        return []
    with open(p, newline="") as f:
        return sorted((float(r["t"]), int(r["people"])) for r in csv.DictReader(f)
                      if r.get("t") and r.get("people"))


def people_at_start(people, start_time):
    """Most recent people count at or before the run's start (manifest start_time,
    local time, as people.csv's t), or NaN."""
    if not people or not start_time:
        return np.nan
    t = dt.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").timestamp()
    before = [n for tp, n in people if tp <= t]
    return before[-1] if before else np.nan


def run_metrics(sdir, mrow, loginfo, background=None, drift=None, settle_s=DEFAULT_SETTLE_S):
    cap = mrow.get("capture_file") or ""
    path = os.path.join(sdir, cap)
    device, cond = mrow["device"], mrow["condition"]
    fam, load, k = classify(cond)
    r = dict(session=os.path.basename(os.path.normpath(sdir)), row=mrow.get("row"),
             device=device, condition=cond, family=fam, nominal_load_pct=load,
             headroom_k=k, replicate=int(mrow.get("replicate") or 0), capture_file=cap,
             start_time=mrow.get("start_time"), manifest_status=mrow.get("status"),
             exclude=False, exclude_reason="", flags="")
    flags = []
    q = mrow.get("qbss_channel_utilization_pct") or ""
    r["airtime_pct"] = float(q) if q.strip() else np.nan
    bg = (background or {}).get(cap)
    if bg is not None:
        # Gym: airtime measured on the forced channel by the background
        # monitor's passive capture (not an AP's QBSS).
        a = bg.get("bg_airtime_mean_pct") or ""
        r["airtime_pct"] = float(a) if a.strip() else np.nan
        for c in GYM_BACKGROUND_COLS:
            r[c] = bg.get(c, "")
    il = mrow.get("induced_load_mbps") or ""
    r["iperf_mbps"] = float(il) if il.strip() else np.nan
    li = loginfo.get(cap, {})
    r["driver_cmd"] = li.get("driver_cmd", "")
    r["forced_mask"] = " ".join(map(str, li["forced_mask"])) if li.get("forced_mask") else ""
    if not os.path.exists(path):
        r["exclude"], r["exclude_reason"] = True, "capture missing"
        return r, None
    ev = parse_capture(path, device)
    if ev is None:
        r["exclude"], r["exclude_reason"] = True, "empty capture"
        return r, None
    dur = ev["tend"] - ev["t0"]
    r["capture_s"] = dur

    # --- timing from the log (relative to btmon start ~ capture start) ---
    bstart = li.get("btmon_start")
    def rel(key):
        return (li[key] - bstart).total_seconds() if bstart and li.get(key) else None
    launch, iperf, stop = rel("driver_launch"), rel("iperf_start"), rel("driver_stop")
    onset = iperf if iperf is not None else ((launch or 0) + settle_s)
    # planned window ends when the driver is stopped (not at the end of the capture)
    wend = min(dur, stop) if stop is not None and stop > onset else dur
    r["driver_launch_s"], r["load_onset_s"], r["window_end_s"] = launch, onset, wend

    # --- connection parameters ---
    first_conn = ev["conns"][0] if ev["conns"] else None
    final = (ev["updates"] or ev["conns"] or [None])[-1]
    r["initial_ci_ms"] = first_conn[1] if first_conn else np.nan
    r["final_ci_ms"] = final[1] if final else np.nan
    r["final_latency"] = final[2] if final else np.nan
    r["final_sup_timeout_ms"] = final[3] if final else np.nan
    r["n_param_updates"] = len(ev["updates"])
    r["dle_tx_bytes"] = max(ev["dle"]) if ev["dle"] else np.nan
    r["phy"] = f"{ev['phy'][-1][0]}M" if ev["phy"] else ""
    r["n_conn_attempts"] = len(ev["conns"])

    # --- disconnects ---
    reasons = collections.Counter(rs for _, rs in ev["discs"])
    for code, name in REASONS.items():
        r[f"n_{name}"] = reasons.get(code, 0)
    r["n_other_reason"] = sum(v for c, v in reasons.items() if c not in REASONS)
    startup = [(t, rs) for t, rs in ev["discs"] if t < STARTUP_WINDOW_S and rs != 0x16]
    if startup:
        flags.append(f"startup disconnect(s) {[hex(rs) for _, rs in startup]}")
    stop_rel = rel("driver_stop")
    losses = [(t, rs) for t, rs in ev["discs"]
              if rs not in (0x16,) and t >= STARTUP_WINDOW_S
              and (stop_rel is None or t <= stop_rel + 1.0)]
    r["n_link_losses"] = len(losses)
    r["first_loss_s"] = losses[0][0] if losses else np.nan
    r["first_loss_reason"] = hex(losses[0][1]) if losses else ""
    r["t_first_loss_after_onset_s"] = (losses[0][0] - onset) if losses else np.nan
    r["survived"] = not losses

    # --- recovery after the last link loss -----------------------------------
    # Did the link ever come back inside the observation window, and if not,
    # what did the host actually do?  Scoped to the sensor's own address so
    # other advertisers in the chamber are not counted.
    mac = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", r["driver_cmd"] or "")
    mac = mac.group(1).lower() if mac else ""
    if losses:
        tl = losses[-1][0]
        back = [c[0] for c in ev["conns"] if c[0] > tl + 1e-3]
        post = [a for a in ev["advs"] if a[0] > tl + 1e-3 and (not mac or a[1] == mac)]
        r["recovered_after_loss"] = float(bool(back))
        r["n_reconnects_after_loss"] = len(back)
        r["t_reconnect_after_loss_s"] = (back[0] - tl) if back else np.nan
        r["t_after_last_loss_s"] = max(0.0, dur - tl)
        r["n_connect_cmds_after_loss"] = sum(1 for c in ev["conn_cmds"] if c > tl + 1e-3)
        r["n_target_advs_after_loss"] = len(post)
        r["n_target_connectable_advs_after_loss"] = sum(1 for a in post if a[2])
        # was duplicate filtering on for the scan the host used to look again?
        sc = [x for x in ev["scan_cmds"] if x[0] > tl + 1e-3 and x[1]]
        r["rescan_dup_filter"] = float(sc[0][2]) if sc else np.nan
        r["t_scan_stop_after_loss_s"] = next(
            (x[0] - tl for x in ev["scan_cmds"] if sc and x[0] > sc[0][0] and not x[1]), np.nan)
    else:
        for c_ in ("recovered_after_loss", "t_reconnect_after_loss_s", "t_after_last_loss_s",
                   "rescan_dup_filter", "t_scan_stop_after_loss_s"):
            r[c_] = np.nan
        for c_ in ("n_reconnects_after_loss", "n_connect_cmds_after_loss",
                   "n_target_advs_after_loss", "n_target_connectable_advs_after_loss"):
            r[c_] = 0

    # connected fraction (conn events / disconnects; link pre-existing if data
    # or a disconnect arrives before any connection event)
    up, conn_s = None, 0.0
    first_evt = min([x[0] for x in ev["conns"][:1]] + [x[0] for x in ev["discs"][:1]]
                    + [x[0] for x in ev["notif"][:1]] + [dur])
    if not ev["conns"] or first_evt < ev["conns"][0][0]:
        up = 0.0
    timeline = sorted([(c[0], "C") for c in ev["conns"]] + [(d[0], "D") for d in ev["discs"]])
    for t, kind in timeline:
        if kind == "C":
            up = t if up is None else up
        elif up is not None:
            conn_s += t - up
            up = None
    if up is not None:
        conn_s += dur - up
    r["connected_pct"] = 100 * conn_s / dur if dur else np.nan

    # --- data in planned window ---
    win = max(1.0, wend - onset)
    r["window_s"] = win
    nb = [(t, L) for t, h, L in ev["notif"] if onset <= t <= wend]
    r["notif_per_s"] = len(nb) / win
    r["payload_Bps"] = sum(L for _, L in nb) / win
    wr = [t for t in ev["writes"] if onset <= t <= wend]
    r["requests_per_s"] = len(wr) / win
    r["notif_per_request"] = len(nb) / len(wr) if wr else np.nan
    r["n_gatt_reads"] = len(ev["reads"])

    # --- Polar ECG ---
    ecg_detail = None
    if ev["ecg"]:
        a = np.array(ev["ecg"], dtype=float)                      # t_host, t_sensor, n
        inwin = a[(a[:, 0] >= onset) & (a[:, 0] <= wend)]
        r["ecg_completeness_pct"] = 100 * inwin[:, 2].sum() / (ECG_FS * win) if len(a) else np.nan
        # delay: delivered frames (before first loss if any, for the stats)
        off = a[:, 0] - a[:, 1]
        d_ms = lower_envelope_detrend(a[:, 0], off, drift) * 1000
        # delay stats: frames delivered in the planned window, before the first loss
        dd = d_ms[delay_mask(a[:, 0], onset, wend, losses)]
        if len(dd) > 10:
            for q_ in (50, 95, 99, 99.9):
                r[f"delay_p{q_}_ms".replace(".", "_")] = float(np.percentile(dd, q_))
            r["delay_max_ms"] = float(dd.max())
        # deadline-miss including undelivered samples (planned window)
        w_ = (a[:, 0] >= onset) & (a[:, 0] <= wend)
        exp_samples = ECG_FS * win
        for D in DEADLINES_MS:
            ok = a[w_ & (d_ms <= D), 2].sum()
            r[f"miss_{D}ms_pct"] = 100 * max(0.0, 1 - ok / exp_samples)
        # end-to-end sample age: sample j of an n-sample frame is (n-1-j)/fs older
        # than the frame timestamp (its last sample), so this M(D) also counts framing
        nw, dw = a[w_, 2], d_ms[w_]
        r["framing_ms"] = float((np.median(a[:, 2]) - 1) * 1000 / ECG_FS)
        for D in DEADLINES_MS:
            ok = np.clip(np.floor((D - dw) * ECG_FS / 1000) + 1, 0, nw).sum()
            r[f"miss_age_{D}ms_pct"] = 100 * max(0.0, 1 - ok / exp_samples)
        r.update(backlog_metrics(a[w_, 0] - onset, dw / 1000))
        ecg_detail = dict(t=a[:, 0], delay_ms=d_ms, n=a[:, 2], onset=onset, wend=wend,
                          losses=[t for t, _ in losses], win=win)

    # --- link layer from the nRF sniffer, when the run has one ---
    sp = mrow.get("sniffer_pcap_file") or ""
    if sp and os.path.exists(os.path.join(sdir, sp)):
        ecg_host = None
        if ev["ecg"]:
            e_ = np.array(ev["ecg"], dtype=float)
            ecg_host = dict(zip(np.round(e_[:, 1], 6), e_[:, 0]))
        r.update(sniffer_metrics(os.path.join(sdir, sp), ev["t0"] - BTSNOOP_EPOCH_S,
                                 onset, wend, ecg_host))

    # --- reconnected vs recovered data ---
    segs, loss_rows, summ = reconnect_breakdown(ev, device, wend, losses, mac)
    r.update(summ)
    r["_segments"], r["_losses"] = segs, loss_rows

    # --- host-stack wedge (bluetoothd stale connection) ---
    t_w, inherited = detect_stack_wedge(ev, dur)
    r["stack_wedge_s"] = t_w if t_w is not None else np.nan
    r["stack_wedge_tail_s"] = (dur - t_w) if t_w is not None else 0.0
    r["n_mgmt_disconnect_failed"] = len(ev["mgmt_disc_fail"])
    if inherited:
        r["exclude"] = True
        r["exclude_reason"] = ("BlueZ stale-connection state inherited from an earlier run: "
                               "no connect attempt was possible (host-stack artefact, not RF)")
    elif t_w is not None:
        flags.append(f"BlueZ wedged at {t_w:.0f}s: last {dur - t_w:.0f}s had no connect "
                     f"attempts (host-stack artefact; delivery figures are floors)")

    # --- AFH maps ---
    maps = read_afh(path.replace(".btsnoop", "_afh.csv"))
    r["afh_samples"] = len(maps)
    if maps:
        r["afh_used_mean"] = float(np.mean([len(m) for m in maps]))
        r["afh_used_min"] = min(len(m) for m in maps)
        r["afh_overlap_used_mean"] = float(np.mean([len(m & WIFI_CH6_OVERLAP) for m in maps]))
        r["afh_overlap_frac_mean"] = float(np.mean([len(m & WIFI_CH6_OVERLAP) / len(m) for m in maps if m]))
    r["_maps"] = maps
    r["flags"] = "; ".join(flags)
    return r, ecg_detail


# ----------------------------------------------------------------------------
# sanity checks
# ----------------------------------------------------------------------------
def sanity_checks(df, sdir, session_log, results_csv):
    checks = []

    def add(name, ok, detail, level="WARN"):
        checks.append(dict(check=name, status="PASS" if ok else level, detail=detail))

    # 1 files
    missing = df[df.exclude_reason.isin(["capture missing", "empty capture"])]
    add("captures present", missing.empty,
        "all manifest captures found" if missing.empty else
        f"{len(missing)} missing/empty: " + ", ".join(missing.capture_file), "FAIL")

    # 2 leaked constrained map in unconstrained runs
    free = df[df.family.isin(["free", "free_long", "free_bracket", "calibration"])]
    leaked = free[(free.afh_used_min <= 8) & free.afh_samples.gt(0)]
    for i in leaked.index:
        df.loc[i, "exclude"] = True
        df.loc[i, "exclude_reason"] = "constrained channel map persisted into unconstrained run"
    add("no constrained map in AFH-free runs", leaked.empty,
        "ok" if leaked.empty else "EXCLUDED: " + ", ".join(leaked.capture_file), "FAIL")

    # 3 constrained/headroom maps match the requested mask
    bad = []
    for i, r in df[df.family.isin(["constrained", "headroom"])].iterrows():
        if r.forced_mask and r._maps:
            mask = set(map(int, r.forced_mask.split()))
            if any(not m <= mask for m in r._maps):
                bad.append(r.capture_file)
    add("constrained maps within requested mask", not bad,
        "ok" if not bad else "maps outside mask: " + ", ".join(bad))

    # 4 AFH poller output
    no_afh = df[(df.afh_samples == 0) & ~df.exclude]
    add("AFH channel map logged", no_afh.empty,
        "ok" if no_afh.empty else f"{len(no_afh)} runs without channel-map samples")

    # 5 airtime present and consistent with load
    loaded = df[(df.nominal_load_pct > 0) & ~df.exclude & df.family.ne("calibration")]
    noq = loaded[loaded.airtime_pct.isna()]
    add("QBSS airtime present for loaded runs", noq.empty,
        "ok" if noq.empty else f"{len(noq)} loaded runs without QBSS: " + ", ".join(noq.capture_file))
    med = loaded.groupby("nominal_load_pct").airtime_pct.median()
    glitch = []
    for i, r in loaded.iterrows():
        m = med.get(r.nominal_load_pct, np.nan)
        if not np.isnan(r.airtime_pct) and not np.isnan(m) and abs(r.airtime_pct - m) > max(10, 0.3 * m):
            glitch.append(f"{r.capture_file} ({r.airtime_pct:.1f}% vs median {m:.1f}%)")
    add("QBSS consistent within load level", not glitch,
        "ok" if not glitch else "outliers (likely sampling glitches): " + "; ".join(glitch))

    # 6 iperf delivered the target
    low = loaded[(loaded.iperf_mbps.notna())]
    add("iperf load recorded", not low.empty or loaded.empty,
        f"{len(low)} of {len(loaded)} loaded runs have iperf throughput")

    # 7 driver consistency per device
    drv = df[~df.exclude].groupby("device").driver_cmd.nunique()
    multi = drv[drv > 1]
    add("single driver invocation per device", multi.empty,
        "ok" if multi.empty else "multiple driver commands for: " + ", ".join(multi.index))

    # 8 connection parameters consistent per device (final CI)
    cis = df[~df.exclude & df.final_ci_ms.notna()].groupby("device").final_ci_ms.unique()
    incons = {d: list(v) for d, v in cis.items() if len(v) > 1}
    add("final connection interval consistent per device", not incons,
        "ok" if not incons else f"varies: {incons}")

    # 9 startup artifacts
    st_ = df[df["flags"].str.contains("startup", na=False)]
    add("no startup disconnects", st_.empty,
        "ok" if st_.empty else f"{len(st_)} runs with a disconnect in the first "
        f"{STARTUP_WINDOW_S:.0f}s (pre-existing link): " + ", ".join(st_.capture_file), "INFO")

    # 10 unexpected link loss in no-interference runs
    quiet = df[~df.exclude & (df.nominal_load_pct == 0)]
    ql = quiet[quiet.n_link_losses > 0]
    add("no link loss without load", ql.empty,
        "ok" if ql.empty else "link loss without load: " + ", ".join(ql.capture_file))

    # 10b link never came back after a loss (observation-window limitation)
    lost = df[~df.exclude & (df.n_link_losses > 0)]
    nr = lost[lost.recovered_after_loss == 0]
    add("link recovered after loss", nr.empty,
        "ok" if nr.empty else
        f"{len(nr)} of {len(lost)} runs with a link loss never reconnected; "
        f"unobserved tail {rng(nr.t_after_last_loss_s)} s "
        f"(by device: " + ", ".join(
            f"{d} {len(gg)}/{len(lost[lost.device == d])}"
            for d, gg in nr.groupby("device")) + ")", "INFO")

    # 10c host stack wedged (bluetoothd stale connection)
    wed = df[df.stack_wedge_s.notna()]
    add("no BlueZ stale-connection wedge", wed.empty,
        "ok" if wed.empty else
        f"{len(wed)} runs: " + ", ".join(
            f"{r.capture_file} ({'whole run, excluded' if r.exclude else f'from {r.stack_wedge_s:.0f}s'})"
            for _, r in wed.iterrows()), "FAIL")

    # 11 no-load throughput stable per device
    msg = []
    for dev, g in df[~df.exclude & (df.family == "free") & (df.nominal_load_pct == 0)].groupby("device"):
        if len(g) > 1 and g.payload_Bps.median() > 0:
            cv = g.payload_Bps.std() / g.payload_Bps.mean()
            msg.append(f"{dev}: {g.payload_Bps.mean():.0f} B/s (CV {100*cv:.1f}%)")
    add("no-load data rate stable", True, "; ".join(msg) or "n/a", "INFO")

    # 12 NetworkManager kept off the scanning adapter
    qb = df[df.nominal_load_pct > 0]
    add("scanner adapter unmanaged", session_log["unmanaged"] > 0 or qb.empty,
        f"'unmanaged' set {session_log['unmanaged']} times")

    # 13 cross-check collection-script results.csv
    if results_csv is not None:
        diffs = []
        for i, r in df.iterrows():
            rr = results_csv.get((r.device, r.condition, str(r.replicate)))
            if not rr:
                continue
            try:
                ci = float(rr.get("initial_interval_ms") or rr.get("connection_interval_ms") or "nan")
            except ValueError:
                ci = np.nan
            if r.n_param_updates and not np.isnan(ci) and ci != r.final_ci_ms \
                    and (rr.get("num_updates") in ("0", "", None)):
                diffs.append(f"{r.capture_file}: results.csv CI {ci} / 0 updates, HCI final {r.final_ci_ms}")
        add("results.csv agrees with HCI (intervals)", not diffs,
            "ok" if not diffs else f"{len(diffs)} disagreements, e.g. " + "; ".join(diffs[:3]), "INFO")
    return checks


# ----------------------------------------------------------------------------
# summaries
# ----------------------------------------------------------------------------
def rng(s, fmt="{:.0f}"):
    s = pd.Series(s).dropna()
    if s.empty:
        return "--"
    a, b = s.min(), s.max()
    return fmt.format(a) if abs(a - b) < 1e-9 else (fmt + "--" + fmt).format(a, b)


def sniffer_section(df):
    """Per-run link-layer view (nRF sniffer) of the Polar constrained/headroom runs."""
    if "sniff_periph_payload_pdus" not in df:
        return ""
    d = df[~df.exclude & df.device.str.contains("olar") & df.family.isin(["constrained", "headroom"])
           & df.sniff_periph_payload_pdus.gt(0)]
    if d.empty:
        return ""
    cols = ["condition", "replicate", "nominal_load_pct", "miss_1000ms_pct", "runaway",
            "implied_capacity_pct", "notif_per_s", "sniff_event_coverage_pct", "sniff_nack_pct",
            "sniff_nack_pairs", "sniff_periph_md_pct", "sniff_multi_pdu_event_pct",
            "sniff_after_air_p90_ms", "sniff_ecg_frames_matched"]
    d = d.sort_values(["family", "headroom_k", "nominal_load_pct", "condition", "replicate"])
    return ("\n## Link layer (nRF sniffer), Polar constrained/headroom runs\n\n"
            "Shares are over what the sniffer heard (`sniff_event_coverage_pct`). NACK: central "
            "NESN == peripheral SN in the same event; missed packets end the event and are not "
            "NACKs. MD: peripheral payload PDUs with More Data. Multi-PDU: events with >= 2 new "
            "peripheral PDUs. After-air p90: host arrival minus first time an ECG frame was heard "
            "on air, relative to the run's 5th percentile. Implied capacity (runaways): 1 - backlog "
            "growth rate, as % of the sensor's data rate.\n\n"
            + d[cols].round(1).to_markdown(index=False) + "\n")


def recovery_section(df):
    """Markdown on what happened after each link loss (and what did not)."""
    lost = df[~df.exclude & (df.n_link_losses > 0)]
    if lost.empty:
        return "\n## Link recovery after loss\n\nNo run lost the link.\n"
    t = lost.assign(outcome=np.where(lost.recovered_after_loss == 1, "reconnected",
                                     "never reconnected"))
    cols = ["condition", "replicate", "n_link_losses", "first_loss_reason",
            "t_after_last_loss_s", "outcome", "n_reconnects_after_loss",
            "n_connect_cmds_after_loss", "n_target_advs_after_loss",
            "n_target_connectable_advs_after_loss", "t_scan_stop_after_loss_s"]
    hdr = ["condition", "rep", "losses", "reason", "tail_s", "outcome", "reconnects",
           "host_connect_cmds", "sensor_advs", "connectable", "scan_off_after_s"]
    out = ["\n## Link recovery after loss\n",
           "Per run that lost the link: the tail is the observation time left after the "
           "last loss; `host_connect_cmds` counts HCI (Extended) Create Connection "
           "commands issued after it; `sensor_advs` / `connectable` count advertising "
           "reports from the sensor's own address in that tail.\n"]
    for dev, g in t.groupby("device"):
        gg = g[cols].sort_values(["condition", "replicate"]).round(1)
        gg.columns = hdr
        out.append(f"\n### {dev}\n\n" + gg.to_markdown(index=False) + "\n")

    # --- limitation text, derived ---
    out.append("\n## Limitations\n")
    for dev, g in t.groupby("device"):
        nr = g[g.recovered_after_loss == 0]
        if nr.empty:
            out.append(f"\n- **{dev}**: every run that lost the link reconnected within "
                       f"the run ({rng(g.t_reconnect_after_loss_s, '{:.1f}')} s after the "
                       f"loss); no non-recovery limitation applies.\n")
            continue
        conds = ", ".join(f"`{c}` ({n}/{len(df[(df.device == dev) & (df.condition == c) & ~df.exclude])})"
                          for c, n in nr.condition.value_counts().sort_index().items())
        no_cmd = int((nr.n_connect_cmds_after_loss == 0).sum())
        adv_any = int((nr.n_target_advs_after_loss > 0).sum())
        adv_conn = int((nr.n_target_connectable_advs_after_loss > 0).sum())
        dup = int((nr.rescan_dup_filter == 1).sum())
        out.append(
            f"\n- **{dev}: once the link dropped it did not come back.** "
            f"{len(nr)} of the {len(g)} runs that lost the link never reconnected for the "
            f"rest of the run, leaving {rng(nr.t_after_last_loss_s)} s of each run "
            f"unobserved. Affected conditions: {conds}. Delivery, completeness and "
            f"deadline-miss figures for these conditions therefore describe a link that "
            f"was down for most of the window, not a degraded-but-live link, and the "
            f"reported miss rates are floors set by the run length rather than steady-state "
            f"values.\n"
            f"  - The captures show the host did look again but never actually tried to "
            f"connect: {no_cmd}/{len(nr)} of these runs contain **zero** Create Connection "
            f"commands after the loss"
            + (f", and the host stopped scanning {rng(nr.t_scan_stop_after_loss_s)} s after "
               f"the loss and never resumed" if nr.t_scan_stop_after_loss_s.notna().any() else "")
            + f". The sensor was still on the air in {adv_any}/{len(nr)} runs, but only "
            f"{adv_conn}/{len(nr)} saw a *connectable* advertisement from it, so the "
            f"kernel's auto-connect path had nothing to act on. "
            + (f"Duplicate filtering was enabled on the re-scan in {dup}/{len(nr)} runs, so "
               f"the low advertising-report counts in the tail are partly a filtering "
               f"artefact and are **not** evidence that the sensor became unreachable.\n"
               if dup else "\n")
            + f"  - Non-recovery is therefore a property of this driver/stack "
            f"configuration as much as of the RF conditions, and these runs do not bound "
            f"how long the outage would have lasted with retries enabled.\n")
    return "".join(out)


def data_recovery_section(df):
    """Markdown: a reconnect only counts if data followed it."""
    lost = df[(df.n_link_losses > 0) & df.capture_file.ne("")]
    if lost.empty:
        return ""
    cols = ["condition", "replicate", "n_link_losses", "n_reconnects", "n_reconnects_with_data",
            "ttfs_median_s", "t_data_back_after_first_loss_s", "post_loss_connected_s",
            "post_loss_data_per_connected_s", "stack_wedge_s"]
    hdr = ["condition", "rep", "losses", "reconnects", "with data", "ttfs p50 (s)",
           "data back (s)", "post-loss conn. (s)", "data/conn. s", "wedged at (s)"]
    if "post_loss_ecg_pct_of_nominal" in lost:
        cols.insert(-1, "post_loss_ecg_pct_of_nominal")
        hdr.insert(-1, "% of 130 Hz")
    out = ["\n## Reconnected vs recovered data\n",
           "A reconnect counts as recovered only if data followed it. `ttfs` = time from "
           "Connection Complete to the first ECG sample (Polar) or notification (others); "
           "`data back` = first data after the first loss; `data/conn. s` = samples (Polar) or "
           "notifications per second of post-loss connected time. Per-connection and per-loss "
           "detail, including when the sensor advertised (connectably) again: "
           "`tables/connections.csv`, `tables/losses.csv`.\n"]
    for dev, g in lost.groupby("device"):
        gg = g[[c for c in cols if c in g]].sort_values(["condition", "replicate"]).round(2)
        gg.columns = [h for c, h in zip(cols, hdr) if c in g]
        out.append(f"\n### {dev}\n\n" + gg.to_markdown(index=False) + "\n")
    return "".join(out)


def per_condition(df, ecg_details):
    rows = []
    base = df[~df.exclude & (df.family == "free") & (df.nominal_load_pct == 0)] \
        .groupby("device").payload_Bps.median().to_dict()
    if not base:
        # Gym session (no no-load runs): delivery relative to the session's
        # own free-channel median instead -- stated in the report.
        base = df[~df.exclude & (df.family == "gym_free")] \
            .groupby("device").payload_Bps.median().to_dict()
    for (dev, fam, cond), g in df[~df.exclude].groupby(["device", "family", "condition"]):
        s = dict(device=dev, family=fam, condition=cond, n=len(g),
                 nominal_load_pct=g.nominal_load_pct.iloc[0], headroom_k=g.headroom_k.iloc[0],
                 airtime_mean=g.airtime_pct.mean(), airtime_range=rng(g.airtime_pct, "{:.1f}"),
                 runs_with_loss=int((g.n_link_losses > 0).sum()),
                 t_loss_median_s=g.t_first_loss_after_onset_s.median(),
                 t_loss_range_s=rng(g.t_first_loss_after_onset_s),
                 runs_recovered=int((g.recovered_after_loss == 1).sum()),
                 runs_not_recovered=int((g.recovered_after_loss == 0).sum()),
                 t_after_last_loss_range_s=rng(g.t_after_last_loss_s),
                 connected_pct_mean=g.connected_pct.mean(),
                 attempts_range=rng(g.n_conn_attempts),
                 n_0x3E=int(g.n_failed_establish.sum()), n_0x08=int(g.n_sup_timeout.sum()),
                 delivered_pct_mean=100 * g.payload_Bps.mean() / base.get(dev, np.nan),
                 delivered_pct_range=rng(100 * g.payload_Bps / base.get(dev, np.nan)),
                 requests_per_s=g.requests_per_s.mean(),
                 notif_per_request=g.notif_per_request.mean())
        if "ecg_completeness_pct" in g:
            s["ecg_completeness_range"] = rng(g.ecg_completeness_pct)
            dd = np.concatenate([e["delay_ms"][delay_mask(e["t"], e["onset"], e["wend"], e["losses"])]
                                 for e in (ecg_details[c] for c in g.capture_file if c in ecg_details)]
                                or [np.array([])])
            if len(dd) > 10:
                for q_ in (50, 95, 99):
                    s[f"delay_p{q_}_ms"] = float(np.percentile(dd, q_))
                s["delay_max_ms"] = float(dd.max())
            for D in DEADLINES_MS:
                for col in (f"miss_{D}ms_pct", f"miss_age_{D}ms_pct"):
                    if col in g:
                        s[col] = g[col].mean()
            if "runaway" in g and g.runaway.notna().any():
                s["runaways"] = int((g.runaway == 1).sum())
                s["backlog_time_pct_median"] = g.backlog_time_pct.median()
            if dev.lower().startswith("polar"):
                att, f3e = g.n_conn_attempts.sum(), g.n_failed_establish.sum()
                s["p_hat"] = (f3e / att) ** (1 / 6) if att and f3e else np.nan
        for col in SNIFF_SUMMARY_COLS:
            if col in g and g[col].notna().any():
                s[f"{col}_median"] = g[col].median()
        rows.append(s)
    out = pd.DataFrame(rows)
    order = {"calibration": 0, "free": 1, "free_bracket": 2, "free_long": 3, "constrained": 4, "headroom": 5,
             "gym_free": 6, "gym_forced": 7}
    out["_o"] = out.family.map(order).fillna(9)
    return out.sort_values(["device", "_o", "headroom_k", "nominal_load_pct", "condition"]).drop(columns="_o")


def link_params(df):
    g = df[~df.exclude].groupby("device")
    return pd.DataFrame(dict(
        final_ci_ms=g.final_ci_ms.agg(lambda s: rng(s, "{:.2f}")),
        initial_ci_ms=g.initial_ci_ms.agg(lambda s: rng(s, "{:.2f}")),
        sup_timeout_ms=g.final_sup_timeout_ms.agg(rng),
        latency=g.final_latency.agg(rng),
        dle_bytes=g.dle_tx_bytes.agg(rng), phy=g.phy.agg(lambda s: ",".join(sorted(set(x for x in s if x)))),
        noload_Bps=df[~df.exclude & (df.nominal_load_pct == 0) & (df.family == "free")]
        .groupby("device").payload_Bps.median().round(0),
        noload_notif_per_s=df[~df.exclude & (df.nominal_load_pct == 0) & (df.family == "free")]
        .groupby("device").notif_per_s.median().round(2),
        requests_per_s=df[~df.exclude & (df.nominal_load_pct == 0) & (df.family == "free")]
        .groupby("device").requests_per_s.median().round(2),
        driver=g.driver_cmd.agg(lambda s: " | ".join(sorted(set(s))))))


def to_latex(dfx, cols, headers, caption, label, path):
    lines = [r"\begin{table}[t]", rf"\caption{{{caption}}}", rf"\label{{{label}}}",
             r"\centering", r"\scriptsize", r"\setlength{\tabcolsep}{3pt}",
             r"\begin{tabular}{" + "l" + "r" * (len(cols) - 1) + "}", r"\toprule",
             " & ".join(rf"\textbf{{{h}}}" for h in headers) + r" \\", r"\midrule"]
    for _, r in dfx.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, float):
                v = "--" if np.isnan(v) else (f"{v:.0f}" if abs(v) >= 10 else f"{v:.2f}")
            cells.append(str(v).replace("_", r"\_").replace("%", r"\%"))
        lines.append(" & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------------
# figures
# ----------------------------------------------------------------------------
def savefig(fig, figdir, sid, name):
    """Write <sid>_<name>.pdf (vector; dense point clouds rasterized) and a .png preview."""
    p = os.path.join(figdir, f"{sid}_{name}")
    fig.savefig(p + ".pdf", dpi=300, bbox_inches="tight")
    fig.savefig(p + ".png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p + ".pdf"


def fig_airtime(df, figdir, sid):
    d = df[~df.exclude & df.airtime_pct.notna() & (df.nominal_load_pct > 0)]
    if d.empty:
        return None
    fig, ax = plt.subplots(figsize=(4.5, 3.5))
    for dev, g in d.groupby("device"):
        ax.scatter(g.nominal_load_pct + (2 if "orsano" in dev else -2), g.airtime_pct, label=dev, s=18, alpha=.8)
    ax.plot([0, 100], [0, 100], "k:", lw=.8)
    ax.set_xlabel("Nominal WiFi load (% of saturation)")
    ax.set_ylabel("Measured airtime, QBSS (%)")
    ax.set_title(f"{sid}: airtime vs load", fontsize=9)
    ax.legend(fontsize=7)
    return savefig(fig, figdir, sid, "airtime_vs_load")


def fig_delay_cdf(df, ecg_details, figdir, sid):
    pol = df[~df.exclude & df.device.str.contains("olar")]
    # headroom by condition, not by k: k2 also has no-load and 100%-load runs
    groups = [("AFH free", pol[pol.family.isin(["free", "free_long"])], None),
              ("Headroom", pol[pol.family == "headroom"], "condition"),
              ("Constrained", pol[pol.family == "constrained"], "nominal_load_pct")]
    groups = [g for g in groups if not g[1].empty]
    if not groups:
        return None
    fig, axes = plt.subplots(1, len(groups), figsize=(4.2 * len(groups), 3.4), squeeze=False)
    for ax, (title, g, key) in zip(axes[0], groups):
        keyf = key or "nominal_load_pct"
        for val, gg in g.groupby(keyf):
            dd = []
            for c in gg.capture_file:
                e = ecg_details.get(c)
                if e is None:
                    continue
                dd.append(e["delay_ms"][delay_mask(e["t"], e["onset"], e["wend"], e["losses"])])
            if not dd:
                continue
            x = np.sort(np.concatenate(dd))
            if key == "condition":
                r0 = gg.iloc[0]
                lab = f"k={int(r0.headroom_k)}, " + ("no load" if r0.nominal_load_pct == 0
                                                     else f"{r0.nominal_load_pct:.0f}%")
            else:
                lab = f"{val:.0f}%"
            ax.plot(np.maximum(x, 1), 1 - np.arange(len(x)) / len(x), label=lab)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_ylim(1e-4, 1.05)
        ax.set_xlabel("Added delay (ms)")
        ax.set_title(title, fontsize=9)
        ax.grid(alpha=.3, which="both")
        ax.legend(fontsize=7)
    axes[0][0].set_ylabel("P(delay > x), delivered ECG frames")
    fig.suptitle(f"{sid}: Polar ECG delay (CCDF)", fontsize=9)
    return savefig(fig, figdir, sid, "polar_delay_ccdf")


def fig_deadline(df, ecg_details, figdir, sid):
    pol = df[~df.exclude & df.device.str.contains("olar") & df.family.isin(["headroom", "constrained", "free"])]
    if pol.empty:
        return None
    Ds = np.logspace(1.5, 5, 60)
    fig, ax = plt.subplots(figsize=(5.5, 3.8))
    for (fam, cond), g in pol.groupby(["family", "condition"]):
        if fam == "free" and cond not in ("duty0pct", "duty100pct"):
            continue
        miss = []
        for c in g.capture_file:
            e = ecg_details.get(c)
            if e is None:
                continue
            w = (e["t"] >= e["onset"]) & (e["t"] <= e["wend"])
            exp = ECG_FS * e["win"]
            miss.append([100 * max(0, 1 - e["n"][w & (e["delay_ms"] <= D)].sum() / exp) for D in Ds])
        if miss:
            ls = "-" if fam == "headroom" else ("--" if fam == "constrained" else ":")
            ax.plot(Ds, np.mean(miss, axis=0), ls, label=f"{cond} (n={len(miss)})")
    ax.set_xscale("log")
    ax.set_xlabel("Deadline D (ms)")
    ax.set_ylabel("ECG samples missing deadline (%)\n(undelivered counted as missed)")
    ax.grid(alpha=.3, which="both")
    ax.legend(fontsize=6, ncol=2)
    ax.set_title(f"{sid}: Polar deadline-miss curves", fontsize=9)
    return savefig(fig, figdir, sid, "polar_deadline_miss")


def fig_time_to_loss(df, figdir, sid):
    d = df[~df.exclude & df.family.isin(["constrained", "headroom"]) & (df.nominal_load_pct > 0)]
    if d.empty:
        return None
    devs = sorted(d.device.unique())
    fig, axes = plt.subplots(1, len(devs), figsize=(5 * len(devs), 3.6), squeeze=False, sharey=True)
    for ax, dev in zip(axes[0], devs):
        g = d[d.device == dev]
        conds = list(dict.fromkeys(g.sort_values(["family", "headroom_k", "nominal_load_pct"]).condition))
        for i, c in enumerate(conds):
            gg = g[g.condition == c]
            for j, (_, r) in enumerate(gg.iterrows()):
                xj = i + (j - (len(gg) - 1) / 2) * 0.12
                if r.survived:
                    ax.scatter(xj, r.window_s, marker="^", facecolors="none", edgecolors="g", s=30)
                else:
                    ax.scatter(xj, r.t_first_loss_after_onset_s, c="r", s=22)
        ax.set_xticks(range(len(conds)))
        ax.set_xticklabels([c.replace("forced_channel_", "forced ").replace("headroom_", "")
                            for c in conds], rotation=45, ha="right", fontsize=7)
        ax.set_title(dev, fontsize=9)
        ax.grid(alpha=.3)
    axes[0][0].set_ylabel("First link loss after load onset (s)\n(open triangle = survived run)")
    fig.suptitle(f"{sid}: time to first link loss", fontsize=9)
    return savefig(fig, figdir, sid, "time_to_link_loss")


def fig_delivery(df, figdir, sid):
    d = df[~df.exclude & df.family.ne("calibration")].copy()
    if d.empty:
        return None
    base = d[(d.family == "free") & (d.nominal_load_pct == 0)].groupby("device").payload_Bps.median()
    d["deliv"] = d.apply(lambda r: 100 * r.payload_Bps / base.get(r.device, np.nan), axis=1)
    devs = sorted(d.device.unique())
    fig, axes = plt.subplots(len(devs), 1, figsize=(9, 2.8 * len(devs)), squeeze=False)
    for ax, dev in zip(axes[:, 0], devs):
        g = d[d.device == dev].sort_values(["family", "headroom_k", "nominal_load_pct"])
        conds = list(dict.fromkeys(g.condition))
        for i, c in enumerate(conds):
            y = g[g.condition == c].deliv
            ax.scatter([i] * len(y), y, s=16)
        ax.set_xticks(range(len(conds)))
        ax.set_xticklabels(conds, rotation=45, ha="right", fontsize=6)
        ax.set_ylabel("Delivered data\n(% of no-load)")
        ax.set_title(dev, fontsize=9)
        ax.axhline(100, color="k", lw=.6, ls=":")
        ax.grid(alpha=.3)
    fig.suptitle(f"{sid}: data delivered in the planned window", fontsize=9)
    fig.tight_layout()
    return savefig(fig, figdir, sid, "delivery")


def fig_channel_maps(df, figdir, sid):
    d = df[~df.exclude & (df.afh_samples > 0)]
    if d.empty:
        return None
    devs = sorted(d.device.unique())
    fig, axes = plt.subplots(len(devs), 1, figsize=(9, 3.2 * len(devs)), squeeze=False)
    for ax, dev in zip(axes[:, 0], devs):
        g = d[d.device == dev].sort_values(["family", "headroom_k", "nominal_load_pct"])
        conds = list(dict.fromkeys(g.condition))
        M = np.zeros((len(conds), 37))
        for i, c in enumerate(conds):
            maps = [m for ms in g[g.condition == c]._maps for m in ms]
            for m in maps:
                for ch in m:
                    M[i, ch] += 1
            M[i] /= max(1, len(maps))
        im = ax.imshow(M, aspect="auto", cmap="viridis", vmin=0, vmax=1)
        ax.set_yticks(range(len(conds)))
        ax.set_yticklabels(conds, fontsize=6)
        ax.axvspan(11.5, 19.5, color="r", alpha=.12)
        ax.set_xlabel("BLE data channel (shaded: overlapped by WiFi ch 6)")
        ax.set_title(f"{dev}: fraction of time each channel was in the hop set", fontsize=9)
        fig.colorbar(im, ax=ax, fraction=.02)
    fig.tight_layout()
    return savefig(fig, figdir, sid, "channel_maps")


def fig_polar_timelines(df, ecg_details, figdir, sid):
    d = df[~df.exclude & df.device.str.contains("olar") & df.family.isin(["constrained", "headroom"])]
    d = d[d.capture_file.isin(ecg_details.keys())]
    if d.empty:
        return None
    d = d.sort_values(["family", "headroom_k", "nominal_load_pct", "replicate"])
    n = len(d)
    cols = 3
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 2.2 * rows), squeeze=False, sharex=True)
    for ax, (_, r) in zip(axes.flat, d.iterrows()):
        e = ecg_details[r.capture_file]
        ax.plot(e["t"], e["delay_ms"] / 1000, ".", ms=1.5, rasterized=True)
        ax.axvline(e["onset"], color="k", lw=.6, ls=":")
        for t in e["losses"]:
            ax.axvline(t, color="r", lw=.8)
        ax.set_title(f"{r.condition} r{r.replicate}", fontsize=7)
        ax.tick_params(labelsize=6)
    for ax in axes.flat[n:]:
        ax.axis("off")
    fig.supxlabel("Time in run (s)   (dotted: load onset, red: link loss)", fontsize=8)
    fig.supylabel("Added delay (s)", fontsize=8)
    fig.suptitle(f"{sid}: Polar ECG delay per run", fontsize=9)
    fig.tight_layout()
    return savefig(fig, figdir, sid, "polar_delay_timelines")


def fig_requests(df, figdir, sid):
    d = df[~df.exclude & df.requests_per_s.gt(0.2)]
    if d.empty:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.3))
    for dev, g in d.groupby("device"):
        g = g.sort_values(["family", "headroom_k", "nominal_load_pct"])
        conds = list(dict.fromkeys(g.condition))
        x = {c: i for i, c in enumerate(conds)}
        axes[0].scatter([x[c] for c in g.condition], g.requests_per_s, s=14, label=dev)
        axes[1].scatter([x[c] for c in g.condition], g.notif_per_request, s=14, label=dev)
        for ax in axes:
            ax.set_xticks(range(len(conds)))
            ax.set_xticklabels(conds, rotation=45, ha="right", fontsize=6)
    axes[0].set_ylabel("Host requests / s")
    axes[1].set_ylabel("Notifications per request")
    for ax in axes:
        ax.grid(alpha=.3)
    fig.suptitle(f"{sid}: request/response behaviour (polled devices)", fontsize=9)
    fig.tight_layout()
    return savefig(fig, figdir, sid, "request_response")


def miss_curve(e, Ds):
    """Deadline miss (%) for each deadline in Ds; undelivered samples count as missed."""
    w = (e["t"] >= e["onset"]) & (e["t"] <= e["wend"])
    exp = ECG_FS * e["win"]
    return np.array([100 * max(0, 1 - e["n"][w & (e["delay_ms"] <= D)].sum() / exp) for D in Ds])


def fig_paper_deadline(df, ecg_details, figdir):
    """Paper fig:deadline, IEEE double-column width, panels side by side.
    (a) deadline-miss curves pooled over sessions: headroom conditions at
    FIG_DEADLINE_LOAD_PCT plus the no-load reference, mean over runs. (b) delay timelines
    of all FIG_DEADLINE_TIMELINE_CONDITIONS runs, FIG_DEADLINE_TIMELINE_RUNS highlighted.
    ecg_details is keyed by (session, capture_file)."""
    pol = df[~df.exclude & df.device.str.contains("olar")]
    pol = pol[[(r.session, r.capture_file) in ecg_details for r in pol.itertuples()]]
    hr = pol[(pol.family == "headroom") & (pol.nominal_load_pct == FIG_DEADLINE_LOAD_PCT)]
    ref = pol[(pol.family == "free") & (pol.condition == FIG_DEADLINE_REFERENCE)]
    if hr.empty:
        return None
    with plt.rc_context(IEEE_RC):
        Ds = np.logspace(1.5, 4.5, 80)
        fig, (axa, axb) = plt.subplots(1, 2, figsize=(IEEE_DOUBLE_COL_IN, 2.4))
        series = [(f"$k$ = {int(k)}", g, FIG_DEADLINE_K_COLORS.get(int(k), "#256abf"), "-")
                  for k, g in hr.groupby("headroom_k")]
        if not ref.empty:
            series.append(("no load", ref, "#555555", "--"))
        for lab, g, col, ls in series:
            curves = [miss_curve(ecg_details[(r.session, r.capture_file)], Ds) for r in g.itertuples()]
            axa.plot(Ds, np.mean(curves, axis=0), ls, color=col, lw=1.2,
                     label=f"{lab} ($n$ = {len(curves)})")
        for D in FIG_DEADLINE_DEADLINES_MS:
            axa.axvline(D, color="#999999", lw=.5, ls=":", zorder=0)
            axa.text(D, 101.5, f"{D / 1000:g} s" if D >= 1000 else f"{D} ms", ha="center", va="bottom")
        axa.set_xscale("log")
        axa.set_xlim(Ds[0], Ds[-1])
        axa.set_ylim(0, 100)
        axa.set_xlabel("Deadline (ms)")
        axa.set_ylabel("ECG samples missing deadline (%)")
        axa.legend(loc="upper right")

        def window(r):
            e = ecg_details[(r.session, r.capture_file)]
            w = (e["t"] >= e["onset"]) & (e["t"] <= e["wend"])
            return e["t"][w] - e["onset"], e["delay_ms"][w] / 1000

        # every run of the same condition (data ch 12-19 at FIG_DEADLINE_LOAD_PCT) in grey,
        # the FIG_DEADLINE_TIMELINE_RUNS pair highlighted on top
        same = pol[pol.condition.isin(FIG_DEADLINE_TIMELINE_CONDITIONS) & (pol.nominal_load_pct == FIG_DEADLINE_LOAD_PCT)]
        hl = {(s_, c_, str(r_)) for s_, c_, r_, _, _ in FIG_DEADLINE_TIMELINE_RUNS}
        for r in same.itertuples():
            if (r.session, r.condition, str(r.replicate)) not in hl:
                axb.plot(*window(r), "-", color="#c4c4c4", lw=.5, zorder=1)
        found = 0
        for sess, cond, rep, lab, col in FIG_DEADLINE_TIMELINE_RUNS:
            m = same[(same.session == sess) & (same.condition == cond) & (same.replicate.astype(str) == str(rep))]
            if m.empty:
                print(f"[pooled] fig:deadline (b): no run {sess} {cond} r{rep}")
                continue
            r = m.iloc[0]
            axb.plot(*window(r), "-", color=col, lw=.9, label=lab, zorder=3)
            e = ecg_details[(r.session, r.capture_file)]
            for t in e["losses"]:
                if e["onset"] <= t <= e["wend"]:
                    axb.axvline(t - e["onset"], color=col, lw=.8, ls="--")
            found += 1
        if len(same) > found:
            axb.plot([], [], "-", color="#c4c4c4", lw=.8, label=f"other runs ($n$ = {len(same) - found})")
        axb.axhline(1.0, color="#999999", lw=.5, ls=":", zorder=0)
        axb.set_xlabel("Time since load onset (s)")
        axb.set_ylabel("Added delay (s)")
        axb.set_ylim(bottom=0)
        axb.legend(loc="upper left")
        # subfigure labels centred below each panel, under its axis label
        fig.tight_layout(w_pad=2.0)
        fig.align_xlabels([axa, axb])
        for ax, lab in ((axa, "(a)"), (axb, "(b)")):
            ax.annotate(lab, xy=(.5, 0), xycoords=ax.xaxis.label, xytext=(0, -4),
                        textcoords="offset points", ha="center", va="top")
        return savefig(fig, figdir, "pooled", "fig_deadline")


def polar_headroom_runs(df):
    """Non-excluded Polar runs with k_eff = headroom k, counting FIG_HEADROOM_K0_ALIASES
    (the identical constrained condition) as k = 0; chamber only."""
    pol = df[~df.exclude & df.device.str.contains("olar") & df.family.isin(["headroom", "constrained"])].copy()
    k = pol.headroom_k.astype(float)
    for load, conds in FIG_HEADROOM_K0_ALIASES.items():
        k[pol.condition.isin(conds) & (pol.nominal_load_pct == load)] = 0.0
    pol["k_eff"] = k
    return pol[pol.k_eff.notna()]


def fig_paper_headroom(df, figdir):
    """Paper fig:headroom, IEEE column width: Polar H10 M(D) for FIG_HEADROOM_DEADLINES_MS
    against headroom k, one line per load in FIG_HEADROOM_LOAD_STYLE (median over runs,
    pooled over sessions, as Table tab:headroom), every run as a small point, and AFH free
    (FIG_HEADROOM_REFERENCE) right of the k axis. Log axis: values below FIG_M_FLOOR_PCT
    are drawn at the floor. Plotted runs: pooled_fig_headroom.csv."""
    hr = polar_headroom_runs(df)
    hr = hr[hr.nominal_load_pct.isin(list(FIG_HEADROOM_LOAD_STYLE)) & (hr.nominal_load_pct > 0)]
    ref = df[~df.exclude & df.device.str.contains("olar") & (df.condition == FIG_HEADROOM_REFERENCE)]
    if hr.empty:
        return None
    ks = sorted(hr.k_eff.unique())
    x_ref = max(ks) + 2.5
    loads = sorted(hr.nominal_load_pct.unique())
    with plt.rc_context(IEEE_RC):
        fig, ax = plt.subplots(figsize=(IEEE_SINGLE_COL_IN, 2.3))
        for j, load in enumerate(loads):
            g = hr[hr.nominal_load_pct == load]
            dx = (j - (len(loads) - 1) / 2) * 0.3
            for D in FIG_HEADROOM_DEADLINES_MS:
                col, mk = FIG_HEADROOM_D_STYLE[D]
                c = f"miss_{D}ms_pct"
                med = g.groupby("k_eff")[c].median()
                ax.scatter(g.k_eff + dx, g[c].clip(lower=FIG_M_FLOOR_PCT), marker=mk, s=5,
                           color=col, alpha=.35, lw=0, zorder=2)
                ax.plot(med.index + dx, med.clip(lower=FIG_M_FLOOR_PCT), FIG_HEADROOM_LOAD_STYLE[load],
                        color=col, marker=mk, ms=3.5, lw=1, zorder=3)
        for D in FIG_HEADROOM_DEADLINES_MS:
            if ref.empty:
                break
            col, mk = FIG_HEADROOM_D_STYLE[D]
            v = ref[f"miss_{D}ms_pct"].clip(lower=FIG_M_FLOOR_PCT)
            ax.scatter(np.full(len(v), x_ref), v, marker=mk, s=5, color=col, alpha=.35, lw=0, zorder=2)
            ax.scatter([x_ref], [v.median()], marker=mk, s=14, facecolors="none", edgecolors=col, lw=.9, zorder=3)
        ax.axvline(max(ks) + 1.25, color="#999999", lw=.5, zorder=0)
        ref_load = f", {ref.nominal_load_pct.iloc[0]:.0f}%" if not ref.empty else ""
        ax.set_xticks(ks + ([x_ref] if not ref.empty else []))
        ax.set_xticklabels([f"{k:.0f}" for k in ks] + ([f"AFH free{ref_load}"] if not ref.empty else []))
        ax.set_xlim(min(ks) - .8, (x_ref if not ref.empty else max(ks)) + .8)
        ax.set_yscale("log")
        ax.set_ylim(FIG_M_FLOOR_PCT * .7, 100)
        ax.set_yticks([.1, 1, 10, 100])
        ax.set_yticklabels(["0.1", "1", "10", "100"])
        ax.grid(axis="x", alpha=0)
        ax.set_xlabel("Clean channels added to data ch 12–19, $k$")
        ax.set_ylabel("ECG samples missing deadline (%)")
        hd = [ax.plot([], [], ls="none", color=FIG_HEADROOM_D_STYLE[D][0], marker=FIG_HEADROOM_D_STYLE[D][1],
                      ms=3.5, label=f"{D / 1000:g} s" if D >= 1000 else f"{D} ms")[0]
              for D in FIG_HEADROOM_DEADLINES_MS]
        hl = [ax.plot([], [], FIG_HEADROOM_LOAD_STYLE[ld], color="#555555", lw=1,
                      label=f"{ld:.0f}% load ($n$ = {len(hr[hr.nominal_load_pct == ld])})")[0] for ld in loads]
        ax.legend(handles=hd + hl, loc="lower center", bbox_to_anchor=(.5, 1.0), ncol=len(hd) + len(hl),
                  columnspacing=.8, handletextpad=.3)
        fig.tight_layout()
        cols = ["session", "condition", "replicate", "nominal_load_pct", "k_eff"] + \
               [f"miss_{D}ms_pct" for D in FIG_HEADROOM_DEADLINES_MS]
        pd.concat([hr, ref.assign(k_eff=np.nan)])[cols].to_csv(
            os.path.join(figdir, "pooled_fig_headroom.csv"), index=False)
        return savefig(fig, figdir, "pooled", "fig_headroom")


def fig_paper_gym_chamber(df, figdir):
    """Paper fig:gymchamber, IEEE column width: Polar H10 M(FIG_GYMCHAMBER_DEADLINE_MS) per
    run for the chamber without load (any channel set), the chamber headroom levels at
    FIG_GYMCHAMBER_LOAD_PCT (k = 0 pooled with its alias), and the gym runs by
    GYM_OCCUPANCY_BANDS (people_at_start; AFH free vs confined marked). Grey band: range of
    the chamber's no-load runs; bar: median. Plotted runs: pooled_fig_gym_chamber.csv."""
    c = f"miss_{FIG_GYMCHAMBER_DEADLINE_MS}ms_pct"
    pol = df[~df.exclude & df.device.str.contains("olar")]
    gym = pol[pol.family.isin(["gym_free", "gym_forced"])]
    if gym.empty or "people_at_start" not in gym or gym.people_at_start.isna().all():
        return None
    noload = pol[(pol.nominal_load_pct == 0) &
                 pol.family.isin(["free", "free_bracket", "free_long", "constrained", "headroom"])]
    hr = polar_headroom_runs(df)
    hr = hr[hr.nominal_load_pct == FIG_GYMCHAMBER_LOAD_PCT]
    groups = [("No\nload", noload, "#555555", "chamber")]
    groups += [(f"$k$ = {k:.0f}", g, FIG_DEADLINE_K_COLORS.get(int(k), "#256abf"), "chamber")
               for k, g in hr.groupby("k_eff")]
    for name, lo, hi in GYM_OCCUPANCY_BANDS:
        g = gym[(gym.people_at_start >= lo) & (gym.people_at_start < hi)]
        if not g.empty:
            lo_n, hi_n = g.people_at_start.min(), g.people_at_start.max()
            persons = f"{lo_n:.0f}" if lo_n == hi_n else f"{lo_n:.0f}–{hi_n:.0f}"
            groups.append((f"{name}\n{persons}", g, "#eb6834", "gym"))
    rows = []
    with plt.rc_context(IEEE_RC):
        fig, ax = plt.subplots(figsize=(IEEE_SINGLE_COL_IN, 2.4))
        if not noload.empty:
            ax.axhspan(max(noload[c].min(), FIG_M_FLOOR_PCT), noload[c].max(), color="#e3e3e3", lw=0, zorder=0)
        for i, (lab, g, col, sec) in enumerate(groups):
            v = g[c].clip(lower=FIG_M_FLOOR_PCT).values
            xs = i + np.linspace(-.22, .22, len(v)) if len(v) > 1 else np.array([float(i)])
            if sec == "gym":
                free = (g.family == "gym_free").values
                ax.scatter(xs[free], v[free], marker="o", s=9, color=col, lw=0, zorder=2)
                ax.scatter(xs[~free], v[~free], marker="s", s=9, facecolors="none", edgecolors=col, lw=.6, zorder=2)
            else:
                ax.scatter(xs, v, marker="o", s=9, color=col, lw=0, zorder=2)
            ax.plot([i - .3, i + .3], [np.median(v)] * 2, color="#222222", lw=1, zorder=3)
            rows += [dict(group=lab.replace("\n", " "), section=sec, session=r.session, condition=r.condition,
                          replicate=r.replicate, people_at_start=getattr(r, "people_at_start", np.nan),
                          **{c: getattr(r, c)}) for r in g.itertuples()]
        n_ch = sum(1 for g in groups if g[3] == "chamber")
        ax.axvline(n_ch - .5, color="#999999", lw=.5, zorder=0)
        ax.set_xticks(range(len(groups)))
        ax.set_xticklabels([g[0] for g in groups])
        ax.set_xlim(-.6, len(groups) - .4)
        ax.set_yscale("log")
        ax.set_ylim(FIG_M_FLOOR_PCT * .7, 100)
        ax.set_yticks([.1, 1, 10, 100])
        ax.set_yticklabels(["0.1", "1", "10", "100"])
        ax.grid(axis="x", alpha=0)
        ax.set_ylabel(f"$M$({FIG_GYMCHAMBER_DEADLINE_MS} ms) per run (%)")
        for x0, x1, txt in ((-.5, n_ch - .5, f"Chamber ($k$: {FIG_GYMCHAMBER_LOAD_PCT:.0f}% load)"),
                            (n_ch - .5, len(groups) - .5, "Gym, persons present")):
            ax.annotate(txt, xy=((x0 + x1) / 2, 0), xycoords=ax.get_xaxis_transform(), xytext=(0, -24),
                        textcoords="offset points", ha="center", va="top")
        ax.scatter([], [], marker="o", s=9, color="#eb6834", lw=0, label="gym, AFH free")
        ax.scatter([], [], marker="s", s=9, facecolors="none", edgecolors="#eb6834", lw=.6,
                   label="gym, confined ($k$ = 0, 2, 8)")
        ax.fill_between([], [], color="#e3e3e3", label="chamber no-load range")
        ax.legend(loc="lower center", bbox_to_anchor=(.5, 1.0), ncol=3, columnspacing=.8, handletextpad=.3)
        fig.tight_layout()
        pd.DataFrame(rows).to_csv(os.path.join(figdir, "pooled_fig_gym_chamber.csv"), index=False)
        return savefig(fig, figdir, "pooled", "fig_gym_chamber")


def cond_label(r):
    """Short paper label for a constrained/headroom condition."""
    load = "no load" if r.nominal_load_pct == 0 else f"{r.nominal_load_pct:.0f}%"
    if r.family == "headroom":
        return f"$k$ = {int(r.headroom_k)}, {load}"
    return f"ch 12–19, {load}"


def fig_paper_time_to_loss(df, figdir, sid):
    """Time to first link loss, both sensors in one IEEE column-width panel:
    one row per condition, marker shape/colour per device, open marker at the
    window end = no loss in the run."""
    d = df[~df.exclude & df.family.isin(["constrained", "headroom"]) & (df.nominal_load_pct > 0)]
    if d.empty:
        return None
    d = d.sort_values(["family", "headroom_k", "nominal_load_pct"])
    conds = list(dict.fromkeys(d.condition))
    labels = [cond_label(d[d.condition == c].iloc[0]) for c in conds]
    devs = sorted(d.device.unique())
    with plt.rc_context(IEEE_RC):
        fig, ax = plt.subplots(figsize=(IEEE_SINGLE_COL_IN, 0.22 * len(conds) + 0.9))
        for k, dev in enumerate(devs):
            col, mk = TTL_STYLE[k % len(TTL_STYLE)]
            g = d[d.device == dev]
            for i, c in enumerate(conds):
                gg = g[g.condition == c]
                ys = i + (k - (len(devs) - 1) / 2) * 0.3 + np.linspace(-.1, .1, max(len(gg), 1))[:len(gg)]
                for y, (_, r) in zip(ys, gg.iterrows()):
                    if r.survived:
                        ax.scatter(r.window_s, y, marker=mk, facecolors="none", edgecolors=col, s=14, lw=.7)
                    else:
                        ax.scatter(r.t_first_loss_after_onset_s, y, marker=mk, color=col, s=14, lw=0)
            ax.scatter([], [], marker=mk, color=col, s=14, label=f"{dev.replace('_', ' ')}: link loss")
            ax.scatter([], [], marker=mk, facecolors="none", edgecolors=col, s=14, lw=.7,
                       label=f"{dev.replace('_', ' ')}: no loss (end of run)")
        ax.set_yticks(range(len(conds)))
        ax.set_yticklabels(labels)
        ax.set_ylim(len(conds) - .5, -.5)
        ax.set_xlim(left=0)
        ax.set_xlabel("Time to first link loss after load onset (s)")
        ax.grid(axis="y", alpha=0)
        ax.legend(loc="lower center", bbox_to_anchor=(.42, 1.0), ncol=2, columnspacing=.8)
        fig.tight_layout()
        return savefig(fig, figdir, sid, "time_to_link_loss")


# ----------------------------------------------------------------------------
# main per-session analysis
# ----------------------------------------------------------------------------
def analyze(sdir, input_path, out_root):
    sid = os.path.basename(os.path.normpath(sdir))
    out = os.path.join(out_root, sid)
    tdir, fdir = os.path.join(out, "tables"), os.path.join(out, "figures")
    os.makedirs(tdir, exist_ok=True)
    os.makedirs(fdir, exist_ok=True)

    with open(os.path.join(sdir, "manifest.csv"), newline="") as f:
        manifest = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    loginfo, slog = parse_log(os.path.join(sdir, "session.log"))
    results_csv = None
    rp = os.path.join(sdir, "results.csv")
    if os.path.exists(rp):
        with open(rp, newline="") as f:
            results_csv = {(r["device"], r["condition"], r["replicate"]): r for r in csv.DictReader(f)}

    background = gym_background(sdir)
    # no-load runs: load onset at the same offset after driver launch as the
    # session's loaded runs (the documented definition), not a fixed constant
    offs = [(li["iperf_start"] - li["driver_launch"]).total_seconds() for li in loginfo.values()
            if li.get("iperf_start") and li.get("driver_launch")]
    settle_s = float(np.median(offs)) if offs else DEFAULT_SETTLE_S
    drift, drift_ppm = session_drift(sdir, manifest)
    runs, ecg_details = [], {}
    for m in manifest:
        if not m.get("capture_file"):
            continue
        r, e = run_metrics(sdir, m, loginfo, background, drift, settle_s)
        runs.append(r)
        if e is not None:
            ecg_details[r["capture_file"]] = e
    df = pd.DataFrame(runs)
    people = gym_people(sdir)
    if people:
        df["people_at_start"] = [people_at_start(people, t) for t in df.start_time]
    for c in ("afh_used_min", "afh_samples", "n_link_losses", "final_ci_ms", "payload_Bps",
              "airtime_pct", "iperf_mbps", "n_failed_establish", "n_sup_timeout", "requests_per_s",
              "notif_per_request", "t_first_loss_after_onset_s", "connected_pct", "headroom_k",
              "recovered_after_loss", "t_after_last_loss_s", "t_reconnect_after_loss_s",
              "n_reconnects_after_loss", "n_connect_cmds_after_loss",
              "n_target_advs_after_loss", "n_target_connectable_advs_after_loss",
              "stack_wedge_s", "n_reconnects", "_segments", "_losses"):
        if c not in df:
            df[c] = np.nan
    df["flags"] = df["flags"].fillna("")

    checks = sanity_checks(df, sdir, slog, results_csv)
    cond = per_condition(df, ecg_details)
    lp = link_params(df)

    # --- tables ---
    keys = ["device", "condition", "replicate", "capture_file"]
    for col, name in (("_segments", "connections.csv"), ("_losses", "losses.csv")):
        rows = [dict({k: r[k] for k in keys}, **x) for _, r in df.iterrows()
                for x in (r[col] if isinstance(r[col], list) else [])]
        pd.DataFrame(rows).round(3).to_csv(os.path.join(tdir, name), index=False)
    df.drop(columns=["_maps", "_segments", "_losses"]).to_csv(os.path.join(tdir, "per_run.csv"),
                                                              index=False)
    cond.to_csv(os.path.join(tdir, "per_condition.csv"), index=False)
    lp.to_csv(os.path.join(tdir, "link_params.csv"))
    pd.DataFrame(checks).to_csv(os.path.join(tdir, "sanity.csv"), index=False)
    lpx = lp.reset_index()
    to_latex(lpx, ["device", "final_ci_ms", "sup_timeout_ms", "dle_bytes", "phy", "noload_notif_per_s",
                   "noload_Bps", "requests_per_s"],
             ["Device", "CI (ms)", "T_sup (ms)", "DLE (B)", "PHY", "Notif./s", "B/s", "Req./s"],
             f"Measured BLE link parameters (session {sid})", "tab:link-params",
             os.path.join(tdir, "tab_link_params.tex"))
    for dev, g in cond.groupby("device"):
        cols = ["condition", "n", "airtime_range", "runs_with_loss", "t_loss_range_s", "delivered_pct_range"]
        hdr = ["Condition", "n", "Airtime (%)", "Link loss", "t loss (s)", "Delivered (%)"]
        if "delay_p50_ms" in g and g["delay_p50_ms"].notna().any():
            cols += ["delay_p50_ms", "delay_p99_ms"]
            hdr += ["p50 (ms)", "p99 (ms)"]
        to_latex(g, cols, hdr, f"{dev}: results per condition (session {sid})",
                 f"tab:cond-{dev.lower().replace('_', '-')}",
                 os.path.join(tdir, f"tab_conditions_{dev}.tex"))

    # --- figures ---
    figs = [f for f in [fig_airtime(df, fdir, sid), fig_delay_cdf(df, ecg_details, fdir, sid),
                        fig_deadline(df, ecg_details, fdir, sid), fig_time_to_loss(df, fdir, sid),
                        fig_delivery(df, fdir, sid), fig_channel_maps(df, fdir, sid),
                        fig_polar_timelines(df, ecg_details, fdir, sid), fig_requests(df, fdir, sid)] if f]

    # --- provenance ---
    meta = dict(session_id=sid, input=os.path.abspath(input_path), script_version=SCRIPT_VERSION,
                analyzed_at=dt.datetime.now().isoformat(timespec="seconds"),
                n_manifest_rows=len(manifest), n_runs_analyzed=int((~df.exclude).sum()),
                n_excluded=int(df.exclude.sum()),
                noload_settle_s=settle_s,
                ecg_drift_ppm=None if drift is None else round(drift * 1e6, 1),
                ecg_drift_ppm_per_run=[round(x, 1) for x in drift_ppm],
                sha256={f: sha256(os.path.join(sdir, f)) for f in ("manifest.csv", "session.log")
                        if os.path.exists(os.path.join(sdir, f))},
                driver_commands=dict(slog["driver_cmds"]))
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # --- report ---
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write(f"# Session {sid}\n\nInput: `{meta['input']}`  \nAnalyzed: {meta['analyzed_at']} "
                f"(analyze_session.py {SCRIPT_VERSION})  \nRuns: {meta['n_runs_analyzed']} analyzed, "
                f"{meta['n_excluded']} excluded\n\n")
        f.write(f"No-load window starts {settle_s:.0f} s after driver launch "
                f"({'median of loaded runs' if offs else 'default, no loaded runs'}).  \n"
                + (f"ECG clock drift removed from every run: {drift * 1e6:.1f} ppm (median of "
                   f"{len(drift_ppm)} reference runs (no-load; gym: AFH-free), range {rng(drift_ppm, '{:.1f}')} ppm).\n"
                   if drift is not None else
                   "ECG clock drift: no reference Polar runs; per-run fit for runs > 600 s only.\n"))
        f.write("\n## Sanity checks\n\n| Check | Status | Detail |\n|---|---|---|\n")
        for c in checks:
            f.write(f"| {c['check']} | {c['status']} | {c['detail']} |\n")
        ex = df[df.exclude]
        if not ex.empty:
            f.write("\n### Excluded runs\n\n")
            for _, r in ex.iterrows():
                f.write(f"- `{r.capture_file}`: {r.exclude_reason}\n")
        if df.family.isin(["gym_free", "gym_forced"]).any():
            f.write("\n**Gym session:** load = airtime measured on the forced WiFi channel by "
                    "background_monitor.py (`airtime_range`; per run in per_run.csv with the other "
                    "`bg_*` covariates). There are no no-load runs, so Corsano delivery is relative "
                    "to this session's free-channel median. Forced masks: see `forced_channels_n` "
                    "and the extras' airtime (`forced_extra_airtime_*`) in per_run.csv, not only k.\n")
        f.write("\n## Link parameters\n\n" + lp.to_markdown() + "\n")
        for dev, g in cond.groupby("device"):
            keep = [c for c in ["condition", "n", "airtime_range", "runs_with_loss", "t_loss_range_s",
                                "delivered_pct_range", "ecg_completeness_range", "delay_p50_ms",
                                "delay_p99_ms", "delay_max_ms", "miss_250ms_pct", "miss_1000ms_pct",
                                "miss_age_250ms_pct", "runaways", "backlog_time_pct_median",
                                "requests_per_s", "notif_per_request", "p_hat"] if c in g]
            f.write(f"\n## {dev}\n\n" + g[keep].round(2).to_markdown(index=False) + "\n")
            if "miss_age_250ms_pct" in g and g.miss_age_250ms_pct.notna().any():
                f.write(f"\n`miss_*` count added delay only; `miss_age_*` is end-to-end sample age, "
                        f"which adds up to {df.framing_ms.median():.0f} ms of sensor framing (oldest "
                        f"sample of a frame). Runaway: added delay reached {RUNAWAY_DELAY_S:g} s and "
                        f"never fell below {RUNAWAY_RECOVER_S:g} s again in the window.\n")
        f.write(sniffer_section(df))
        f.write(recovery_section(df))
        f.write(data_recovery_section(df))
        f.write("\n## Figures\n\n" + "\n".join(f"- `{os.path.relpath(p, out)}`" for p in figs) + "\n")
    print(f"[{sid}] {meta['n_runs_analyzed']} runs -> {out}")
    for c in checks:
        if c["status"] != "PASS":
            print(f"   {c['status']:4s} {c['check']}: {c['detail'][:140]}")
    return df, ecg_details


def wilson(x, n, z=1.96):
    """Wilson 95 % interval for x successes in n trials."""
    if not n:
        return np.nan, np.nan
    p, d = x / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def poisson_rate_ci(x, expo_s):
    """Rate per hour with exact (Garwood) 95 % bounds; bounds NaN without scipy."""
    try:
        from scipy.stats import chi2
        lo = chi2.ppf(.025, 2 * x) / 2 if x else 0.0
        hi = chi2.ppf(.975, 2 * x + 2) / 2
    except ImportError:
        lo = hi = np.nan
    return tuple(3600 * v / expo_s for v in (x, lo, hi))


def pooled_runaway(df):
    """Runaway counts per Polar loaded constrained/headroom condition, with the
    k = 0 aliases (FIG_HEADROOM_K0_ALIASES) pooled into one extra row. Exposure
    for the onset rate: time from load onset to runaway onset, or the whole
    window for runs that did not run away."""
    d = df[df.device.str.contains("olar") & df.family.isin(["constrained", "headroom"])
           & df.nominal_load_pct.gt(0) & df.runaway.notna()].copy()
    if d.empty:
        return pd.DataFrame()
    d["_expo"] = np.where(d.runaway == 1, d.runaway_onset_s, d.window_s)
    groups = [(c, g) for c, g in d.groupby("condition")]
    for load, aliases in FIG_HEADROOM_K0_ALIASES.items():
        g = d[(d.headroom_k == 0) & (d.nominal_load_pct == load) | d.condition.isin(aliases)]
        if g.condition.nunique() > 1:
            groups.append((f"k=0 @{load:g}% (pooled: {', '.join(sorted(g.condition.unique()))})", g))
    rows = []
    for c, g in groups:
        x, n = int((g.runaway == 1).sum()), len(g)
        lo, hi = wilson(x, n)
        rate, rlo, rhi = poisson_rate_ci(x, g._expo.sum())
        rows.append(dict(condition=c, headroom_k=g.headroom_k.max(), n=n, sessions=g.session.nunique(), runaways=x,
                         p=x / n, p_lo95=lo, p_hi95=hi, onsets_per_h=rate, onsets_per_h_lo95=rlo,
                         onsets_per_h_hi95=rhi, onset_s_range=rng(g.runaway_onset_s),
                         implied_capacity_pct_range=rng(g.implied_capacity_pct, "{:.1f}"),
                         backlog_time_pct_median=g.backlog_time_pct.median()))
    return pd.DataFrame(rows)


def runaway_sample_size_text(rw):
    """Runs needed for a useful runaway probability, from the pooled k = 0 row."""
    if rw.empty:
        return ""
    k0 = rw[rw.condition.str.startswith("k=0")]
    r0 = (k0 if not k0.empty else rw.sort_values("runaways")).iloc[-1]
    p = min(max(r0.p, .1), .9)
    nh = {h: math.ceil(1.96 ** 2 * p * (1 - p) / h ** 2) for h in (.2, .15, .1)}
    za, zb = 1.96, 0.8416
    def n2(p1, p2):
        pb = (p1 + p2) / 2
        return math.ceil((za * math.sqrt(2 * pb * (1 - pb)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
                         / (p1 - p2) ** 2)
    zero = rw[(rw.runaways == 0) & rw.headroom_k.ge(2)]
    nz = int(zero.n.sum())
    return (f"\n**Runs needed** (from {r0.condition}: {r0.runaways}/{r0.n} = {r0.p:.0%}, "
            f"95 % CI {r0.p_lo95:.0%}--{r0.p_hi95:.0%}): to estimate p = {p:.0%} within "
            f"±20/15/10 points, {nh[.2]}/{nh[.15]}/{nh[.1]} runs per condition; to tell {p:.0%} "
            f"from {p / 4:.0%} (alpha 0.05, power 0.8), {n2(p, p / 4)} per group, and from "
            f"{p / 2:.0%}, {n2(p, p / 2)}. The k >= 2 conditions had no runaway ({nz} runs together), which bounds "
            f"p below {1 - 0.05 ** (1 / nz):.0%} (one-sided 95 %); below 10 % needs 29 runaway-free "
            f"runs and below 5 % needs 59. Runaway within the window depends on its length: "
            f"onsets occurred at {r0.onset_s_range} s after load onset, "
            f"{r0.onsets_per_h:.1f}/h of exposure (95 % {r0.onsets_per_h_lo95:.1f}--"
            f"{r0.onsets_per_h_hi95:.1f}/h), so report the window length with p, or the share of "
            f"time with backlog > {BACKLOG_DELAY_S:g} s, and spread runs over sessions.\n"
            if nz else "")


def pooled_framing(df):
    """Polar M(D): added delay only vs end-to-end sample age (with framing), means per condition."""
    d = df[df.device.str.contains("olar") & df.framing_ms.notna()] if "framing_ms" in df else df.iloc[:0]
    if d.empty:
        return pd.DataFrame()
    cols = {}
    for D in (100, 250, 1000):
        cols[f"M_added_{D}ms"] = (f"miss_{D}ms_pct", "mean")
        cols[f"M_age_{D}ms"] = (f"miss_age_{D}ms_pct", "mean")
    out = d.groupby(["family", "condition"]).agg(n=("condition", "size"),
                                                 framing_ms=("framing_ms", "median"), **cols)
    return out.reset_index()


def channel_arithmetic():
    """Clean BLE data channels (whole 2 MHz channel outside every band) with WiFi on 1/6/11."""
    rows = []
    for name, hw in WIFI_HALF_WIDTHS_MHZ:
        clean = [i for i, f in BLE_DATA_MHZ.items()
                 if all(abs(f - c) >= hw + 1 for c in WIFI_NONOVERLAP_MHZ.values())]
        rows.append(dict(wifi_band=f"{name} (±{hw:g} MHz)", n_clean=len(clean),
                         clean_channels=" ".join(map(str, clean))))
    return pd.DataFrame(rows)


def pooled(dfs, out_root, ecg_details=None):
    ids = [d.session.iloc[0] for d in dfs]
    out = os.path.join(out_root, "pooled_" + "+".join(ids))
    os.makedirs(os.path.join(out, "figures"), exist_ok=True)
    df = pd.concat(dfs, ignore_index=True)
    df = df[~df.exclude]
    g = df[df.family.isin(["constrained", "headroom"])].groupby(["device", "family", "condition"])
    summ = g.apply(lambda x: pd.Series(dict(
        n=len(x), sessions=x.session.nunique(), runs_with_loss=int((x.n_link_losses > 0).sum()),
        t_loss_median_s=x.t_first_loss_after_onset_s.median(),
        t_loss_iqr_s=f"{x.t_first_loss_after_onset_s.quantile(.25):.0f}--{x.t_first_loss_after_onset_s.quantile(.75):.0f}",
        survived=int(x.survived.sum())))).reset_index()
    summ.to_csv(os.path.join(out, "pooled_constrained.csv"), index=False)
    sid = "pooled"
    fig_paper_time_to_loss(df, os.path.join(out, "figures"), sid)
    fig_paper_headroom(df, os.path.join(out, "figures"))
    fig_paper_gym_chamber(df, os.path.join(out, "figures"))
    if ecg_details:
        fig_paper_deadline(df, ecg_details, os.path.join(out, "figures"))
    rw, fr, ch = pooled_runaway(df), pooled_framing(df), channel_arithmetic()
    rw.to_csv(os.path.join(out, "pooled_runaway.csv"), index=False)
    fr.to_csv(os.path.join(out, "pooled_framing.csv"), index=False)
    ch.to_csv(os.path.join(out, "channel_arithmetic.csv"), index=False)
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write(f"# Pooled: {', '.join(ids)}\n\n" + summ.to_markdown(index=False) + "\n")
        if not rw.empty:
            f.write(f"\n## Polar runaway backlog\n\nRunaway: added delay reached {RUNAWAY_DELAY_S:g} s "
                    f"and never fell below {RUNAWAY_RECOVER_S:g} s again in the window. p with Wilson "
                    f"95 % CI; onset rate per hour of exposure (load onset to runaway onset, or the "
                    f"whole window).\n\n" + rw.round(3).to_markdown(index=False) + "\n"
                    + runaway_sample_size_text(rw))
        if not fr.empty:
            f.write("\n## Polar deadline miss: added delay vs end-to-end sample age\n\n"
                    "`M_added` is the paper's M(D) (radio share only); `M_age` adds the sensor's "
                    "framing (sample j of an n-sample frame is (n-1-j)/130 s older than the frame "
                    "timestamp). Both exclude the fixed radio + host floor, so ages are lower "
                    "bounds.\n\n" + fr.round(1).to_markdown(index=False) + "\n")
        f.write("\n## Clean BLE data channels with WiFi on 1, 6 and 11\n\nData channels 0--10 at "
                "2404 + 2i MHz, 11--36 at 2406 + 2i MHz; clean = the whole 2 MHz channel outside "
                "every WiFi band.\n\n" + ch.to_markdown(index=False) + "\n")
    print(f"[pooled] {len(ids)} sessions -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+", help="session directories or .zip exports")
    ap.add_argument("--out", default="analysis", help="output root (default: ./analysis)")
    ap.add_argument("--pool", action="store_true", help="also write pooled statistics across sessions")
    a = ap.parse_args()
    tmp, dfs, ecg = [], [], {}
    try:
        for s in a.sessions:
            sdir = resolve_session(s, tmp)
            df, e = analyze(sdir, s, a.out)
            dfs.append(df)
            sid = df.session.iloc[0]
            ecg.update({(sid, c): v for c, v in e.items()})
        if a.pool and len(dfs) > 1:
            pooled(dfs, a.out, ecg)
    finally:
        for t in tmp:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    main()
