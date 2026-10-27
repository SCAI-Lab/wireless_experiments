#!/usr/bin/env python3
"""corsano_data_age.py -- Corsano bio-impedance (BioZ) data age from HCI captures.

Companion to analyze_session.py (imports its btsnoop reader); written 2026-10-14
for Table I of the MEDWEAR paper.

Data age(t) = t - (last time BioZ bytes not contained in the previous BioZ transfer
reached the host), sampled every 0.1 s over the planned window
[load_onset_s, window_end_s] from analysis/<session>/tables/per_run.csv.
BioZ = file 0x0e. 0x0012 notifications are attributed to the most recent stream
request (ATT Write Command 0x44 <file> <len> <offset>), because under load data keep
arriving after the driver has moved on to the next command. A lower bound for the
consumer: it ignores driver/ROS processing after the host receives the bytes.

Usage: python3 scripts/corsano_data_age.py CAPTURE_DIR PER_RUN_CSV OUT_CSV
"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_session as A

def att_pdus(path):
    buf = {}
    for t, op, p in A.read_btsnoop(path):
        if op not in (A.ACL_TX, A.ACL_RX) or len(p) < 4:
            continue
        hdr = int.from_bytes(p[0:2], "little"); key = (op, hdr & 0xfff); data = p[4:]
        if (hdr >> 12) & 3 == 1:
            if key in buf: buf[key][1] += data
        else:
            if len(data) < 4: continue
            buf[key] = [t, bytearray(data)]
        if key not in buf: continue
        t0, b = buf[key]; L = int.from_bytes(b[0:2], "little")
        if len(b) - 4 >= L:
            if int.from_bytes(b[2:4], "little") == 4:
                yield t0, op, bytes(b[4:4 + L])
            del buf[key]

def bioz_events(path):
    t0 = A.read_btsnoop(path)[0][0]; cur = None; buf = b""; prev = b""; ev = []
    for t, op, a in att_pdus(path):
        if op == A.ACL_TX and a[0] == 0x52 and len(a) >= 13 and a[3] == 0x44:
            if cur == 0x0e and buf: prev = buf
            cur = a[4]; buf = b""
        elif op == A.ACL_RX and a[0] == 0x1b and int.from_bytes(a[1:3], "little") == 0x12 and cur == 0x0e:
            ch = a[3:]; buf += ch
            if ch not in prev: ev.append(t - t0)
    return np.array(sorted(set(np.round(ev, 3))))

def main(cdir, per_run, out):
    pr = pd.read_csv(per_run); pr = pr[pr.device.str.contains("Corsano") & (pr.exclude != True)]
    rows = []
    for _, r in pr.iterrows():
        f = os.path.join(cdir, r.capture_file)
        if not os.path.exists(f): continue
        ev = bioz_events(f); on, we = r.load_onset_s, r.window_end_s
        ts = np.arange(on, we, 0.1); idx = np.searchsorted(ev, ts, side="right") - 1
        ref = np.where(idx >= 0, ev[np.clip(idx, 0, None)] if len(ev) else np.nan, np.nan)
        start = r.driver_launch_s if pd.notna(r.driver_launch_s) else 0.0
        age = np.where(np.isnan(ref), ts - start, ts - ref)
        rows.append(dict(condition=r.condition, replicate=r.replicate, capture_file=r.capture_file,
                         n_new_chunks=int(((ev >= on) & (ev <= we)).sum()),
                         age_p50_s=np.percentile(age, 50), age_p99_s=np.percentile(age, 99),
                         age_max_s=age.max(), age_gt10s_pct=100 * (age > 10).mean()))
    pd.DataFrame(rows).to_csv(out, index=False); print(f"{len(rows)} runs -> {out}")

if __name__ == "__main__":
    main(*sys.argv[1:4])
