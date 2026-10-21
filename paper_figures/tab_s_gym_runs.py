#!/usr/bin/env python3
"""Supplementary Table tab:s-gym-runs: every gym run, Polar H10 and Corsano side by side.

Per-run metrics come from analyze_session.run_metrics (read-only on the captures).
M'(100 ms) and Age>1 s are the censoring-aware columns (miss_cens_100ms_pct,
miss_age_cens_1000ms_pct). Airtime p95 is the inclusive (linear) 95th percentile of the
15 s monitor-mode windows whose midpoint falls in the run (gym_session.py's
statistics.quantiles extrapolates past the maximum with few windows).

Usage:
    python3 paper_figures/tab_s_gym_runs.py <chamber_1 per_run.csv> <template.tex> <out_dir>

<chamber_1 per_run.csv>: analyze_session output of 20261007_105222_chamber_1 (Corsano
AFH-free no-load reference). <template.tex>: current tab_s_gym_runs.tex; its header and
caption are kept, the rows and the "Analysis v..." note are replaced. Writes
<out_dir>/gym_per_run.csv and <out_dir>/tab_s_gym_runs.tex.
"""
import csv
import os
import re
import sys
import time

import numpy as np
import pandas as pd

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, REPO)
import analyze_session as A  # noqa: E402

CAP = os.path.join(REPO, "captures")
GYM = {"gym_1013a": ("gym_SPZ_20261013_133642", "13a"), "gym_1013b": ("gym_SPZ_20261013_160606", "13b"),
       "gym_1016": ("gym_SPZ_20261016_103145", "16")}
# rtw_8822bu monitor-adapter fault: airtime reads are invalid from here on
INVALID_FROM = {"gym_1013a": pd.Timestamp("2026-10-13 14:02:00")}


def load(sdir):
    with open(os.path.join(sdir, "manifest.csv"), newline="") as f:
        manifest = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    loginfo, _ = A.parse_log(os.path.join(sdir, "session.log"))
    offs = [(li["iperf_start"] - li["driver_launch"]).total_seconds() for li in loginfo.values()
            if li.get("iperf_start") and li.get("driver_launch")]
    return manifest, loginfo, (float(np.median(offs)) if offs else A.DEFAULT_SETTLE_S), A.gym_background(sdir)


def airtime_p95(sdir, m):
    """Inclusive p95 of the 15 s airtime windows whose midpoint is inside the run (NaN if < 2)."""
    a = pd.read_csv(os.path.join(sdir, "background", "airtime.csv"))
    a = a[a.airtime_pct.notna()]
    if "fault" in a:
        a = a[a.fault.fillna("") == ""]
    t0 = time.mktime(time.strptime(m["start_time"], "%Y-%m-%d %H:%M:%S"))
    mid = (a.t_start + a.t_end) / 2
    w = a[(mid >= t0) & (mid < t0 + float(m["duration_s"]))].airtime_pct
    return float(np.percentile(w, 95)) if len(w) >= 2 else np.nan


def inrun_rssi(sdir, cap):
    f = os.path.join(sdir, cap.replace(".btsnoop", "_afh.csv"))
    v = pd.read_csv(f).rssi_dbm.dropna() if os.path.exists(f) else []
    return float(np.median(v)) if len(v) else np.nan


def per_run(ref_bps):
    rows = []
    for key, (dname, short) in GYM.items():
        sdir = os.path.join(CAP, dname)
        manifest, loginfo, settle, bg = load(sdir)
        drift, _ = A.session_drift(sdir, manifest)
        for m in manifest:
            if not m.get("capture_file"):
                continue
            r, e = A.run_metrics(sdir, m, loginfo, bg, drift, settle)
            st = pd.Timestamp(r["start_time"])
            air, pq, note = r["airtime_pct"], airtime_p95(sdir, m), ""
            cov = pd.to_numeric(r.get("bg_airtime_coverage_pct"), errors="coerce")
            if key in INVALID_FROM and st >= INVALID_FROM[key]:
                air, pq, note = np.nan, np.nan, "a"
            elif pd.isna(air):
                note = "b"
            elif cov < 50:
                note = "c"
            row = dict(session=short, start=st.strftime("%H:%M"), device=r["device"],
                       cond={"free_channel": "free"}.get(r["condition"],
                                                         r["condition"].replace("forced_k", "").replace("forced_", "")),
                       airtime_pct=air, airtime_p95_pct=pq, airtime_coverage_pct=cov,
                       rssi_dbm=inrun_rssi(sdir, m["capture_file"]),
                       hop_set=r.get("afh_used_mean"), link_losses=r["n_link_losses"], note=note)
            if e is not None:
                row.update(ecg_delivered_pct=r["ecg_completeness_pct"], m100_cens_pct=r.get("miss_cens_100ms_pct"),
                           delay_p99_ms=r["delay_p99_ms"], delay_max_ms=r["delay_max_ms"],
                           age_gt1s_cens_pct=r.get("miss_age_cens_1000ms_pct"))
            else:
                row.update(corsano_payload_Bps=r["payload_Bps"], corsano_pct_of_chamber=100 * r["payload_Bps"] / ref_bps)
            rows.append(row)
    return pd.DataFrame(rows)


def write_tex(d, template, out):
    f = lambda x, fmt="{:.0f}": "-" if pd.isna(x) else fmt.format(x)  # noqa: E731
    neg = lambda x: "-" if pd.isna(x) else "$-$%.0f" % -x  # noqa: E731

    def air(r):
        return f(r.airtime_pct, "{:.1f}") + ("" if pd.isna(r.note) or r.note == "" else "$^{%s}$" % r.note)
    pol = [r for _, r in d[d.device == "Polar_H10"].iterrows()]
    cor = [r for _, r in d[d.device == "Corsano"].iterrows()]
    assert len(pol) == len(cor)
    rows, prev = [], None
    for p, c in zip(pol, cor):
        assert p.session == c.session
        if prev is not None and p.session != prev:
            rows.append(r"\midrule")
        prev = p.session
        pc = [p.session, p.start, p.cond, air(p), f(p.airtime_p95_pct), neg(p.rssi_dbm), f(p.hop_set),
              f(p.ecg_delivered_pct, "{:.1f}"), f(p.m100_cens_pct, "{:.1f}"), f(p.delay_p99_ms),
              f(p.delay_max_ms / 1000, "{:.1f}"), f(p.age_gt1s_cens_pct, "{:.1f}")]
        cc = [c.session, c.start, c.cond, air(c), f(c.airtime_p95_pct), neg(c.rssi_dbm), f(c.hop_set),
              str(int(c.link_losses)), f(c.corsano_pct_of_chamber)]
        rows.append(" & ".join(pc + cc) + r" \\")
    L = open(template).read().split("\n")
    i0, i1 = L.index(r"\midrule") + 1, L.index(r"\bottomrule")
    tail = "\n".join(L[i1:])
    tail, n = re.subn(r"Analysis v\d+\.\d+\.\d+", f"Analysis v{A.SCRIPT_VERSION}", tail)
    assert n == 1, "template: expected one 'Analysis vX.Y.Z' note"
    with open(out, "w") as fh:
        fh.write("\n".join(L[:i0] + rows) + "\n" + tail)


def main():
    chamber_csv, template, out_dir = sys.argv[1:4]
    ch = pd.read_csv(chamber_csv)
    ref = ch[(ch.device == "Corsano") & (ch.nominal_load_pct == 0) & ~ch.exclude & (ch.family == "free")].payload_Bps.median()
    print(f"Corsano chamber AFH-free no-load reference: {ref:.1f} B/s")
    d = per_run(ref)
    d.to_csv(os.path.join(out_dir, "gym_per_run.csv"), index=False)
    write_tex(d, template, os.path.join(out_dir, "tab_s_gym_runs.tex"))
    for r in d[d.note == "c"].itertuples():
        print(f"{r.session} {r.start} {r.device}: airtime covers {r.airtime_coverage_pct:.0f}% of the run")


if __name__ == "__main__":
    main()
