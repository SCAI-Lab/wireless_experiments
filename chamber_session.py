#!/usr/bin/env python3
"""
Chamber session automation — Rows 1-3
=======================================

Automates the repetitive parts of the chamber procedure:
  - starts/stops a btmon capture around each run
  - launches each device's ros4hc driver for the run duration
  - triggers iperf3 traffic on the Pi for Row 2's non-zero duty levels
    (25%, 75%), each at its own tuned bitrate
  - logs a manifest of every run (device, condition, replicate, capture file)
  - automatically parses all captures once a run finishes (or aborts) to
    extract connection interval, any mid-session renegotiation, and MTU,
    into a single results CSV ('parse' is also available standalone to
    re-parse without re-running)

What this script does NOT automate (still manual, by design):
  - physically connecting/charging devices
  - verifying the TP-Link/Pi are on the right 2.4 GHz channel (do this in
    your pre-day dry run, per the checklist)

Devices run one at a time, fully (all of rows 1-3 for device A, then all of
rows 1-3 for device B), rather than interleaved row-by-row. This matches how
a single patient's sensor is actually used in the field (one device active
at a time) and, together with the explicit BlueZ disconnect after every run
(see disconnect_ble_device in session_lib/drivers.py), avoids a confirmed bug: a
"finished" device's BLE connection can silently persist in the background
(BlueZ auto-reconnects bonded devices) and contend with whatever device is
tested next on the single shared radio -- this was traced to real ~45s
periodic disconnects on a device that wasn't even under test. The trade-off
is that Row 2's AP/Pi setup and Row 3's generic-BLE-peripheral pause each
happen once per device instead of once total.

Each run gets its own timestamped session directory under
config.yaml's session.output_dir (e.g. <output_dir>/20260918_121724/),
holding that session's log, btsnoop captures, manifest.csv, and
results.csv together as a self-contained unit.

Usage:
    python3 chamber_session.py run                # run rows 1-5 back-to-back (default)
    python3 chamber_session.py run --row 1        # run only Row 1
    python3 chamber_session.py parse              # re-parse the most recent session
    python3 chamber_session.py parse --session 20260918_121724  # re-parse a specific one
    python3 chamber_session.py run --resume       # resume the most recent interrupted session
    python3 chamber_session.py run --resume 20260918_121724  # resume a specific one
    python3 chamber_session.py run --test-mode    # dry run outside the chamber (see below)

--resume re-uses the session's own directory and skips any (row, device,
condition, replicate) combo already recorded as a real PASSED (BLE link
included, not just the driver process) in its results.csv.

--test-mode is for trying the pipeline out somewhere other than the chamber
(e.g. your desk) before the real session: it only relaxes the measured-
throughput-vs-expected_max_mbps baseline check, since that comparison is
only meaningful inside the actual anechoic chamber. Every other check
(devices, Pi, BT controller) and the BLE-link-health pass/fail criteria
still apply, so a run's results.csv is still trustworthy in test mode --
it just won't abort over ambient RF that doesn't match the chamber baseline.

Requires: sudo access for btmon, ros2 environment sourced, SSH key access
to the Pi already set up (passwordless, or you'll be prompted per iperf3 call).
"""

import argparse
import functools
import json
import random
import re
import subprocess
import sys
import time

from session_lib import (
    stop_all_drivers,
    disconnect_ble_device,
    _kernel_has_link as _link_is_up,
    FORCED_CHANNEL_RANGE,
    STARTUP_GRACE_S,
    DeviceCheckError,
    InfrastructureCheckError,
    RunAbortedError,
    StrayProcessError,
    check_ble_sniffer_ready,
    check_bt_controller_ready,
    check_devices_ready,
    check_devices_unbonded,
    kill_stray_driver_processes,
    check_ros_packages,
    check_pi_reachable,
    check_pi_wifi_link,
    check_wifi_scan_ready,
    disable_internal_wifi_for_session,
    do_parse,
    do_run,
    find_latest_session_ts,
    get_output_dir,
    get_session_dir,
    load_completed_keys,
    load_config,
    log,
    log_wifi_rfkill_state,
    prepare_rerun_failed,
    reset_host_channel_classification,
    resolve_duty_bitrates,
    restore_internal_wifi,
    run_key,
    setup_logging,
    start_channel_occupancy_poller,
    start_pairing_agent,
    stop_channel_occupancy_poller,
    stop_pairing_agent,
)

# =============================================================================
# iperf3 traffic control (Row 2, non-zero duty conditions)
# =============================================================================


def run_iperf_background(iperf_cfg, duration_s, bitrate):
    """SSH into the Pi and run iperf3 at the given bitrate as a background
    subprocess for duration_s. Requests JSON output (-J) with stdout captured
    (not DEVNULL) so the actual achieved throughput can be confirmed after
    the run -- otherwise we're only ever confirming the ssh call was made,
    never that real traffic was actually induced on the link.

    UDP (-u) with a fixed payload size (-l), not TCP: TCP backs off/adapts
    under loss, which is exactly the wrong behavior for an "offered load"
    independent variable -- we want the requested bitrate actually sent
    regardless of loss, with loss itself reported as its own KPI (see
    parse_iperf_udp_stats) rather than silently absorbed by TCP's
    congestion control."""
    ssh_target = f"{iperf_cfg['pi_user']}@{iperf_cfg['pi_host']}"
    payload = iperf_cfg.get("udp_payload_bytes", 1200)
    remote_cmd = (
        f"iperf3 -c {iperf_cfg['server_ip']} -u -b {bitrate} -l {payload} "
        f"-t {duration_s} -J"
    )
    full_cmd = ["ssh", ssh_target, remote_cmd]
    log.info(f"  -> starting iperf3 UDP traffic via {ssh_target}: {remote_cmd}")
    proc = subprocess.Popen(
        full_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    log.info(f"     iperf3 ssh session running (pid {proc.pid})")
    return proc


def parse_iperf_json(stdout_text):
    """Extract the actual achieved bitrate (Mbit/s) from a completed
    `iperf3 -u -J` run's captured stdout. Returns None if it can't be parsed
    -- e.g. the ssh call or iperf3 itself failed silently, which previously
    we had no way to detect at all."""
    if not stdout_text:
        return None
    try:
        data = json.loads(stdout_text)
        end = (
            data["end"].get("sum")
            or data["end"].get("sum_sent")
            or data["end"]["sum_received"]
        )
        return end["bits_per_second"] / 1_000_000
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def parse_iperf_udp_stats(stdout_text):
    """Extract UDP-specific loss/jitter from a completed `iperf3 -u -J` run's
    captured stdout -- iperf3's own transport-level loss and jitter,
    separate from (and a useful cross-check against) BLE's own metrics.
    Returns None if unparseable or the run wasn't UDP."""
    if not stdout_text:
        return None
    try:
        data = json.loads(stdout_text)
        end = data["end"]["sum"]
        return {
            "packet_loss_pct": end["lost_percent"],
            "iperf_jitter_ms": end["jitter_ms"],
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def bitrate_str_to_mbps(bitrate):
    """Convert an iperf3 -b argument (e.g. "41M", "13.6M", "500K") to Mbit/s,
    to compare the actually-achieved load against the intended target."""
    s = str(bitrate).strip()
    if s[-1:].upper() == "M":
        return float(s[:-1])
    if s[-1:].upper() == "K":
        return float(s[:-1]) / 1000
    if s[-1:].upper() == "G":
        return float(s[:-1]) * 1000
    return float(s) / 1_000_000  # assume raw bits/s


def _log_controller_info(controller, session_dir):
    """Log this session's Bluetooth controller model/firmware once, into
    both session.log and a small standalone file -- so the write-up can
    cite what hardware/firmware the results characterize (external review
    point: results partly characterize the central's own controller and
    AFH implementation, not just the wearables)."""
    try:
        result = subprocess.run(
            ["hciconfig", "-a", controller], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("  -> could not run hciconfig -a to log controller info")
        return
    info = result.stdout.strip()
    if not info:
        return
    (session_dir / "controller_info.txt").write_text(info + "\n")
    log.info(f"  -> controller info saved to controller_info.txt")
    for line in info.splitlines():
        log.debug(f"     {line}")


# =============================================================================
# Row execution
# =============================================================================


def run_test1_connection_interval(
    cfg, session_dir, session_id, device, completed=frozenset()
):
    r = cfg["row1_connection_interval"]
    if not r.get("enabled"):
        log.info(f"Row 1 (connection interval) [{device['name']}]: disabled, skipping")
        return
    log.info(f"=== Row 1/3 [{device['name']}]: connection interval calibration ===")
    # Two distinct sub-runs, not N identical replicates: a short one just to
    # record the negotiated interval/latency/timeout, and a long one to
    # watch for a mid-session renegotiation (which existing captures haven't
    # actually shown yet -- see the config.yaml comment on this row).
    sub_runs = [
        ("calibration", r["calibration_duration_s"]),
        ("renegotiation_watch", r["renegotiation_watch_duration_s"]),
    ]
    for condition, duration_s in sub_runs:
        for rep in range(1, r["replicates"] + 1):
            if run_key("row1", device["name"], condition, rep) in completed:
                log.info(
                    f"  -> skipping row1 | {device['name']} | {condition} | rep{rep} "
                    f"(already PASSED, resuming)"
                )
                continue
            do_run(
                cfg,
                session_dir,
                "row1",
                device["name"],
                device["launch_cmd"],
                condition,
                rep,
                duration_s,
                session_id,
                mac_address=device.get("mac_address"),
                total_replicates=r["replicates"],
                retry_on_fail=True,  # Row 1: a failure means the setup is broken
            )
    log.info(f"Row 1/3 [{device['name']}] complete")


def _row2_extra(r, bitrate_by_duty, duty):
    if duty <= 0:
        return None
    extra = {
        "start_iperf": True,
        "start_iperf_fn": functools.partial(
            run_iperf_background, bitrate=bitrate_by_duty[duty]
        ),
        "iperf_cfg": r["iperf"],
        "parse_iperf_fn": parse_iperf_json,
        "parse_udp_stats_fn": parse_iperf_udp_stats,
        "target_mbps": bitrate_str_to_mbps(bitrate_by_duty[duty]),
    }
    return extra


def run_test2_duty_cycle(cfg, session_dir, session_id, device, completed=frozenset()):
    r = cfg["row2_duty_cycle"]
    if not r.get("enabled"):
        log.info(f"Row 2 (duty cycle) [{device['name']}]: disabled, skipping")
        return
    bitrate_by_duty = r["iperf"].get("bitrate_by_duty")
    if not bitrate_by_duty:
        raise ValueError(
            "row2_duty_cycle.iperf.bitrate_by_duty was never resolved -- "
            "this should have been derived/validated in main() before "
            "run_test2_duty_cycle was called"
        )
    # Randomize this device's own (duty level, replicate) order each
    # session, seeded from (session_id, device) so it's reproducible/
    # auditable rather than opaque -- avoids confounding duty level with
    # time/thermal/battery drift (external review point: order was always a
    # fixed 0 -> 25 -> 75). Shuffling the FULL list of (level, rep) pairs,
    # not just the level order with reps still run back-to-back per level --
    # 3 identical-condition reps in a row would still leave each level's
    # exposure clustered in time, only partially addressing the confound.
    # Device order itself (Corsano fully before Polar) stays fixed -- see
    # the comment at the top of this file for why.
    rng = random.Random(f"{session_id}:{device['name']}:row2")
    pairs = [
        (duty, rep)
        for duty in r["duty_levels"]
        for rep in range(1, r["replicates"] + 1)
    ]
    rng.shuffle(pairs)
    log.info(
        f"=== Row 2/3 [{device['name']}]: duty cycle validation -- "
        f"duty levels {r['duty_levels']}, {r['replicates']} replicate(s) each "
        f"(this device's shuffled (level, rep) order: {pairs}) ==="
    )
    for duty, rep in pairs:
        condition = f"duty{duty}pct"
        if run_key("row2", device["name"], condition, rep) in completed:
            log.info(
                f"  -> skipping row2 | {device['name']} | {condition} | "
                f"rep{rep} (already recorded, resuming)"
            )
            continue
        do_run(
            cfg,
            session_dir,
            "row2",
            device["name"],
            device["launch_cmd"],
            condition,
            rep,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=r["replicates"],
            extra=_row2_extra(r, bitrate_by_duty, duty),
            retry_on_fail=False,  # interference-induced instability IS the data
        )
    # Bracket: re-run the 0% condition once more at the end of this device's
    # block, to check for drift across the whole randomized-order sequence.
    bracket_condition = "duty0pct_bracket"
    if run_key("row2", device["name"], bracket_condition, 1) not in completed:
        log.info(f"  -> bracket: re-running duty0pct as {bracket_condition}")
        do_run(
            cfg,
            session_dir,
            "row2",
            device["name"],
            device["launch_cmd"],
            bracket_condition,
            1,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=1,
            extra=_row2_extra(r, bitrate_by_duty, 0),
            retry_on_fail=False,
        )
    log.info(f"Row 2/3 [{device['name']}] complete")


_FORCED_CHANNEL_DUTY_BY_CONDITION = {
    "forced_channel_no_load": 0,
    "forced_channel_max_load": 100,
}


def _forced_channel_duty_pct(condition):
    """Map a row3 condition name to its duty percentage. Covers the
    original binary no_load/max_load names plus dose-response conditions
    named forced_channel_duty<NN>pct (e.g. forced_channel_duty25pct),
    added 2026-09-23 so the held-out validation in gym_sim/ has real
    intermediate-load data to predict against instead of just the two
    endpoints."""
    if condition in _FORCED_CHANNEL_DUTY_BY_CONDITION:
        return _FORCED_CHANNEL_DUTY_BY_CONDITION[condition]
    m = re.fullmatch(r"forced_channel_duty(\d+)pct", condition)
    if not m:
        raise ValueError(
            f"row3_forced_channel: unrecognized condition name {condition!r} -- "
            "expected forced_channel_no_load, forced_channel_max_load, or "
            "forced_channel_duty<NN>pct"
        )
    return int(m.group(1))


def _condition_selected(cfg, condition):
    """--condition filter (rows 3, 5 and 8 only). Lets one condition be rerun
    in a fresh session -- e.g. forced_channel_max_load reps 1-3 -- without
    repeating the whole row, which --resume/--rerun-failed can't do for a
    rep that was recorded as PASSED."""
    selected = cfg.get("_condition_filter")
    return not selected or condition in selected


def run_test3_forced_channel(
    cfg, session_dir, session_id, device, completed=frozenset()
):
    """The 'mechanism' test: forces AFH onto exactly the BLE data channels
    WiFi channel 6 overlaps (no escape route), instead of the AFH-free
    condition every other row uses. Comparing this against Row 2's
    duty100pct (same load, AFH free) is what actually quantifies how much
    AFH is protecting the link, rather than inferring it from the channel
    map alone -- see FORCED_CHANNEL_RANGE in session_lib/ble_radio.py for exactly
    which channels and why."""
    r = cfg.get("row3_forced_channel")
    if not r or not r.get("enabled"):
        log.info(f"Row 3 (forced channel) [{device['name']}]: disabled, skipping")
        return
    row2 = cfg["row2_duty_cycle"]
    bitrate_by_duty = row2["iperf"].get("bitrate_by_duty")
    if not bitrate_by_duty:
        raise ValueError(
            "row2_duty_cycle.iperf.bitrate_by_duty was never resolved -- "
            "needed by row3_forced_channel's forced_channel_max_load condition"
        )
    log.info(
        f"=== Row 3 [{device['name']}]: forced-channel mechanism test -- "
        f"AFH constrained to channels {FORCED_CHANNEL_RANGE} (WiFi ch6's "
        f"overlap), {r['duration_s']}s x {r['replicates']} reps, conditions "
        f"{r['conditions']} -- compare against Row 2's duty100pct (AFH free, "
        f"same load) to quantify AFH's protection =="
    )
    def _extra(duty):
        extra = dict(_row2_extra(row2, bitrate_by_duty, duty) or {})
        extra["forced_channels"] = FORCED_CHANNEL_RANGE
        return extra

    # Shuffled (condition, rep) order, seeded per session/device -- same
    # reasoning as Row 2/5: run in config order, load level was confounded
    # with time/thermal/battery drift across the block (review 2026-10-06).
    # Deterministic, so a --resume reproduces the same order.
    conditions = [c for c in r["conditions"] if _condition_selected(cfg, c)]
    rng = random.Random(f"{session_id}:{device['name']}:row3")
    pairs = [
        (condition, rep)
        for condition in conditions
        for rep in range(1, r["replicates"] + 1)
    ]
    rng.shuffle(pairs)
    log.info(f"  -> shuffled (condition, rep) order: {pairs}")
    for condition, rep in pairs:
        if run_key("row3", device["name"], condition, rep) in completed:
            log.info(
                f"  -> skipping row3 | {device['name']} | {condition} | "
                f"rep{rep} (already recorded, resuming)"
            )
            continue
        do_run(
            cfg,
            session_dir,
            "row3",
            device["name"],
            device["launch_cmd"],
            condition,
            rep,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=r["replicates"],
            extra=_extra(_forced_channel_duty_pct(condition)),
            retry_on_fail=False,
        )
    # Bracket, as in Row 2: one more no-load run at the end of the block, to
    # check for drift across the shuffled sequence.
    bracket_condition = "forced_channel_no_load_bracket"
    if "forced_channel_no_load" in conditions and run_key(
        "row3", device["name"], bracket_condition, 1
    ) not in completed:
        log.info(f"  -> bracket: re-running forced_channel_no_load as {bracket_condition}")
        do_run(
            cfg,
            session_dir,
            "row3",
            device["name"],
            device["launch_cmd"],
            bracket_condition,
            1,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=1,
            extra=_extra(0),
            retry_on_fail=False,
        )
    # Block-level safety net, in addition to _attempt_run's own per-run
    # reset: if the LAST run in this block crashed/was interrupted before
    # its own reset, the classification would otherwise stay forced on the
    # controller (persistent hardware state) until whatever runs next
    # happens to be AFH-free and catches it via its own proactive reset --
    # reset here too so nothing downstream depends on that.
    if not reset_host_channel_classification():
        log.warning(
            "  -> could not confirm the channel classification was reset "
            "at the end of Row 3 -- the next AFH-free run's own proactive "
            "reset should still catch this, but check the AFH CSV of "
            "whatever runs next if unsure"
        )
    log.info(f"Row 3 [{device['name']}] complete")


def run_test5_headroom_sweep(
    cfg, session_dir, session_id, device, completed=frozenset()
):
    """Headroom sweep: unlike Row 3 (one fixed 8-channel mask, WiFi load
    varied), this varies how many CLEAN channels AFH has available to
    escape to, at one fixed high load -- see config.yaml's row5_headroom_
    sweep/channel_masks comments for the full rationale. The logged AFH
    channel map (start_afh_poller, already captured for every run) is the
    key output: whether the controller actually hops onto the added clean
    channels at each k, not just whether the link stays connected.

    Runs as part of --row all."""
    r = cfg.get("row5_headroom_sweep")
    if not r or not r.get("enabled"):
        log.info(f"Row 5 (headroom sweep) [{device['name']}]: disabled, skipping")
        return
    channel_masks = cfg.get("channel_masks")
    if not channel_masks:
        raise ValueError(
            "row5_headroom_sweep is enabled but top-level channel_masks is "
            "missing from config.yaml -- needed to know which channels each "
            "k value forces"
        )
    row2 = cfg["row2_duty_cycle"]
    bitrate_by_duty = row2["iperf"].get("bitrate_by_duty")
    if not bitrate_by_duty:
        raise ValueError(
            "row2_duty_cycle.iperf.bitrate_by_duty was never resolved -- "
            "needed by row5_headroom_sweep's load conditions"
        )
    load_pct = r["load_duty_pct"]
    no_load_k = r["no_load_control_k"]
    if no_load_k not in channel_masks:
        raise ValueError(
            f"row5_headroom_sweep.no_load_control_k ({no_load_k}) isn't a key "
            f"in channel_masks ({sorted(channel_masks)})"
        )

    # One load condition per k, plus one no-load control at no_load_k.
    conditions = {}
    for k in sorted(channel_masks):
        conditions[f"headroom_k{k}_duty{load_pct}pct"] = (channel_masks[k], load_pct)
    conditions[f"headroom_k{no_load_k}_no_load"] = (channel_masks[no_load_k], 0)
    conditions = {c: v for c, v in conditions.items() if _condition_selected(cfg, c)}

    log.info(
        f"=== Row 5 [{device['name']}]: headroom sweep -- k in "
        f"{sorted(channel_masks)} clean channels at {load_pct}% load, plus a "
        f"no-load control at k={no_load_k}, {r['duration_s']}s x "
        f"{r['replicates']} reps == "
    )

    # Shuffle (condition, rep) order per device, same reasoning as Row 2:
    # avoid confounding channel count with time/thermal/battery drift.
    rng = random.Random(f"{session_id}:{device['name']}:row5")
    pairs = [
        (condition, rep)
        for condition in conditions
        for rep in range(1, r["replicates"] + 1)
    ]
    rng.shuffle(pairs)
    log.info(f"  -> shuffled (condition, rep) order: {pairs}")

    for condition, rep in pairs:
        if run_key("row5", device["name"], condition, rep) in completed:
            log.info(
                f"  -> skipping row5 | {device['name']} | {condition} | "
                f"rep{rep} (already recorded, resuming)"
            )
            continue
        channels, duty = conditions[condition]
        extra = dict(_row2_extra(row2, bitrate_by_duty, duty) or {})
        extra["forced_channels"] = channels
        do_run(
            cfg,
            session_dir,
            "row5",
            device["name"],
            device["launch_cmd"],
            condition,
            rep,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=r["replicates"],
            extra=extra,
            retry_on_fail=False,
        )
    # Block-level safety net, same reasoning as the end of Row 3.
    if not reset_host_channel_classification():
        log.warning(
            "  -> could not confirm the channel classification was reset "
            "at the end of Row 5 -- the next AFH-free run's own proactive "
            "reset should still catch this, but check the AFH CSV of "
            "whatever runs next if unsure"
        )
    log.info(f"Row 5 [{device['name']}] complete")


def run_test4_endurance(cfg, session_dir, session_id, device, completed=frozenset()):
    r = cfg.get("row4_endurance")
    if not r or not r.get("enabled"):
        log.info(f"Row 4 (endurance) [{device['name']}]: disabled, skipping")
        return
    row2 = cfg["row2_duty_cycle"]
    bitrate_by_duty = row2["iperf"].get("bitrate_by_duty")
    if not bitrate_by_duty:
        raise ValueError(
            "row2_duty_cycle.iperf.bitrate_by_duty was never resolved -- "
            "needed by row4_endurance's duty100pct_long condition"
        )
    log.info(
        f"=== Row 4 [{device['name']}]: endurance runs -- "
        f"{r['duration_s']}s each, conditions {r['conditions']} -- "
        f"for a real disconnect-rate bound (rule of three), not percentile stats ==="
    )
    for condition in r["conditions"]:
        if run_key("row4", device["name"], condition, 1) in completed:
            log.info(
                f"  -> skipping row4 | {device['name']} | {condition} | rep1 "
                f"(already recorded, resuming)"
            )
            continue
        duty = 100 if condition == "duty100pct_long" else 0
        do_run(
            cfg,
            session_dir,
            "row4",
            device["name"],
            device["launch_cmd"],
            condition,
            1,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=1,
            extra=_row2_extra(row2, bitrate_by_duty, duty),
            retry_on_fail=False,
        )
    log.info(f"Row 4 [{device['name']}] complete")


def run_test8_headroom_saturation(
    cfg, session_dir, session_id, device, completed=frozenset()
):
    """Opt-in (--row 8, not in --row all), added 2026-10-06: Row 5's
    headroom sweep repeated at 100% load for a few k values, to test whether
    Row 5's 75% result holds at the top of the load range -- e.g. whether
    the Polar's "fully recovered from k=2" cliff stays at k=2. Same masks
    (channel_masks) and same 100% bitrate as Row 2's duty100pct. Labelled
    row8 because row6/row7 already name other runs in existing data
    (chamber contention, the gym visit, the scan-interference test)."""
    r = cfg.get("row8_headroom_saturation")
    if not r or not r.get("enabled"):
        log.info(f"Row 8 (headroom at 100%) [{device['name']}]: disabled, skipping")
        return
    channel_masks = cfg.get("channel_masks") or {}
    missing = [k for k in r["k_values"] if k not in channel_masks]
    if missing:
        raise ValueError(
            f"row8_headroom_saturation.k_values {missing} aren't keys in "
            f"channel_masks ({sorted(channel_masks)})"
        )
    row2 = cfg["row2_duty_cycle"]
    bitrate_by_duty = row2["iperf"].get("bitrate_by_duty")
    load_pct = r["load_duty_pct"]
    if not bitrate_by_duty or load_pct not in bitrate_by_duty:
        raise ValueError(
            f"row2_duty_cycle.iperf.bitrate_by_duty has no {load_pct}% level -- "
            f"needed by row8_headroom_saturation"
        )
    conditions = {
        f"headroom_k{k}_duty{load_pct}pct": channel_masks[k] for k in r["k_values"]
    }
    conditions = {c: v for c, v in conditions.items() if _condition_selected(cfg, c)}
    log.info(
        f"=== Row 8 [{device['name']}]: headroom at {load_pct}% load -- k in "
        f"{r['k_values']}, {r['duration_s']}s x {r['replicates']} reps =="
    )
    rng = random.Random(f"{session_id}:{device['name']}:row8")
    pairs = [
        (condition, rep)
        for condition in conditions
        for rep in range(1, r["replicates"] + 1)
    ]
    rng.shuffle(pairs)
    log.info(f"  -> shuffled (condition, rep) order: {pairs}")
    for condition, rep in pairs:
        if run_key("row8", device["name"], condition, rep) in completed:
            log.info(
                f"  -> skipping row8 | {device['name']} | {condition} | "
                f"rep{rep} (already recorded, resuming)"
            )
            continue
        extra = dict(_row2_extra(row2, bitrate_by_duty, load_pct) or {})
        extra["forced_channels"] = conditions[condition]
        do_run(
            cfg,
            session_dir,
            "row8",
            device["name"],
            device["launch_cmd"],
            condition,
            rep,
            r["duration_s"],
            session_id,
            mac_address=device.get("mac_address"),
            total_replicates=r["replicates"],
            extra=extra,
            retry_on_fail=False,
        )
    if not reset_host_channel_classification():
        log.warning(
            "  -> could not confirm the channel classification was reset "
            "at the end of Row 8 -- the next AFH-free run's own proactive "
            "reset should still catch this, but check the AFH CSV of "
            "whatever runs next if unsure"
        )
    log.info(f"Row 8 [{device['name']}] complete")


# =============================================================================
# Entry point
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a block of sessions")
    run_p.add_argument(
        "--row",
        default="all",
        choices=["1", "2", "3", "4", "5", "8", "all"],
        help="Which row to run, or 'all' for rows 1, 2, 3, 4, 5 in that order. "
        "Row 8 (headroom at 100%% load) is opt-in: run it with --row 8. "
        "(Rows 6, BLE-BLE contention, and 7, QBSS scan interference, were "
        "removed 2026-10-06; their past data still parses.)",
    )
    run_p.add_argument("--config", default="config.yaml")
    run_p.add_argument(
        "--device",
        default=None,
        metavar="NAME",
        help="Run only this device (by its config.yaml devices[].name, e.g. "
        "Corsano or Polar_H10) instead of every configured device. Useful "
        "when only one sensor is charged/available, or to isolate one "
        "device's results. Device-readiness checks and stray-process "
        "cleanup are also scoped to just this device.",
    )
    run_p.add_argument(
        "--condition",
        action="append",
        default=None,
        metavar="NAME",
        help="Rows 3, 5 and 8 only: run just this condition (repeatable), e.g. "
        "--row 3 --condition forced_channel_max_load, or --row 5 --condition "
        "headroom_k0_duty75pct. Other rows ignore it.",
    )
    run_p.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION",
        help="Resume an interrupted run instead of starting a new session: "
        "re-uses that session's directory and skips any (row, device, "
        "condition, replicate) combo already recorded in its manifest.csv "
        "-- PASSED always, FAILED too for no-retry rows (2/3/4/5), where "
        "a FAILED result is itself the recorded outcome, not an error to "
        "retry (see --rerun-failed to override that). Give a session "
        "timestamp to resume a specific one, or omit the value to resume "
        "the most recent.",
    )
    run_p.add_argument(
        "--rerun-failed",
        action="store_true",
        help="Only with --resume: also re-attempt every (row, device, "
        "condition, replicate) already recorded as FAILED, not just "
        "what's missing entirely. The original failed capture (and its "
        "pcap/afh.csv/rosbag) is renamed out of the way first, never "
        "overwritten. For no-retry rows (2/3/4/5), a FAILED result is "
        "often ITSELF the intended data point (interference causing a "
        "real disconnect) -- make sure that's not what you're about to "
        "discard before using this on those rows.",
    )
    run_p.add_argument(
        "--test-mode",
        action="store_true",
        help="Dry-run outside the anechoic chamber: treat the measured-"
        "throughput baseline check (row2_duty_cycle.iperf.expected_max_mbps) "
        "as a warning instead of a fatal error, since ambient RF outside the "
        "chamber won't match that baseline by design. Everything else "
        "(device connectivity, Pi reachability, BLE link health, etc.) is "
        "still checked/enforced normally.",
    )

    parse_p = sub.add_parser(
        "parse", help="Re-parse a session's captures into its results.csv"
    )
    parse_p.add_argument("--config", default="config.yaml")
    parse_p.add_argument(
        "--session",
        default=None,
        help="Timestamp of the session to re-parse, as named by its output "
        "directory (e.g. 20260918_121724). Defaults to the most recent session.",
    )

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.command == "run" and not args.resume:
        session_ts = time.strftime("%Y%m%d_%H%M%S")
    else:
        session_ts = args.session if args.command == "parse" else None
        if args.command == "run" and args.resume != "latest":
            session_ts = args.resume
        session_ts = session_ts or find_latest_session_ts(cfg)
        if session_ts is None:
            print(
                f"No sessions found under {get_output_dir(cfg)} -- run a session first.",
                file=sys.stderr,
            )
            sys.exit(1)

    session_dir = get_session_dir(cfg, session_ts)
    setup_logging(session_dir)
    resuming = args.command == "run" and args.resume
    completed = (
        frozenset(load_completed_keys(cfg, session_dir)) if resuming else frozenset()
    )
    if resuming and args.rerun_failed:
        completed = prepare_rerun_failed(session_dir, completed)
    test_mode = args.command == "run" and args.test_mode
    log.info(
        f"config: {args.config}, command: {args.command}, session: {session_ts}"
        + (f" (resuming, {len(completed)} run(s) already recorded)" if resuming else "")
        + (
            " [TEST MODE -- chamber baseline check is advisory only]"
            if test_mode
            else ""
        )
    )

    pairing_agent = None
    rfkill_id = None
    occupancy_poller = None
    try:
        if args.command == "run":
            if args.device:
                all_names = [d["name"] for d in cfg["devices"]]
                matched = [d for d in cfg["devices"] if d["name"] == args.device]
                if not matched:
                    raise ValueError(
                        f"--device {args.device!r} doesn't match any device in "
                        f"config.yaml -- available: {all_names}"
                    )
                cfg["devices"] = matched
                log.info(f"--device given: running only {args.device} this session")
            if args.condition:
                cfg["_condition_filter"] = set(args.condition)
                log.info(f"--condition given: rows 3/5/8 run only {sorted(args.condition)}")
            pairing_agent = start_pairing_agent()
            row2_will_run = args.row in ("2", "3", "4", "5", "8", "all") and cfg[
                "row2_duty_cycle"
            ].get("enabled")
            kill_stray_driver_processes(cfg)
            check_devices_unbonded(cfg)
            check_ros_packages(cfg)
            check_bt_controller_ready(cfg["btmon_controller"])
            check_ble_sniffer_ready(cfg)
            _log_controller_info(cfg["btmon_controller"], session_dir)
            log_wifi_rfkill_state(session_dir)
            if (cfg.get("disable_internal_wifi") or {}).get("enabled", True):
                rfkill_id = disable_internal_wifi_for_session(test_mode)
            # Rows 2-5 poll QBSS during every run.
            check_wifi_scan_ready(cfg, need_qbss=row2_will_run)
            ambient_cfg = cfg.get("ambient_scan") or {}
            if ambient_cfg.get("enabled"):
                occupancy_poller = start_channel_occupancy_poller(
                    ambient_cfg.get("iface"),
                    session_dir / "channel_occupancy.csv",
                    poll_interval_s=ambient_cfg.get("poll_interval_s", 60),
                )
            if row2_will_run:
                check_pi_reachable(cfg["row2_duty_cycle"]["iperf"])
                check_pi_wifi_link(cfg["row2_duty_cycle"]["iperf"])
                cfg["row2_duty_cycle"]["iperf"]["bitrate_by_duty"] = (
                    resolve_duty_bitrates(
                        cfg, strict=not test_mode, session_dir=session_dir
                    )
                )
                settle = cfg["row2_duty_cycle"]["iperf"].get("settle_delay_s", 0)
                if settle < STARTUP_GRACE_S:
                    log.warning(
                        f"  -> row2_duty_cycle.iperf.settle_delay_s ({settle}s) is "
                        f"less than STARTUP_GRACE_S ({STARTUP_GRACE_S}s) -- drops "
                        f"caused by traffic starting could be silently hidden "
                        f"inside the startup grace window"
                    )
            check_devices_ready(cfg)
            for device in cfg["devices"]:
                log.info(
                    f"=== Starting all selected rows for device: {device['name']} ==="
                )
                if args.row == "all":
                    run_test1_connection_interval(
                        cfg, session_dir, session_ts, device, completed
                    )
                    run_test2_duty_cycle(
                        cfg, session_dir, session_ts, device, completed
                    )
                    run_test3_forced_channel(
                        cfg, session_dir, session_ts, device, completed
                    )
                    run_test4_endurance(cfg, session_dir, session_ts, device, completed)
                    run_test5_headroom_sweep(
                        cfg, session_dir, session_ts, device, completed
                    )
                elif args.row == "1":
                    run_test1_connection_interval(
                        cfg, session_dir, session_ts, device, completed
                    )
                elif args.row == "2":
                    run_test2_duty_cycle(
                        cfg, session_dir, session_ts, device, completed
                    )
                elif args.row == "3":
                    run_test3_forced_channel(
                        cfg, session_dir, session_ts, device, completed
                    )
                elif args.row == "4":
                    run_test4_endurance(cfg, session_dir, session_ts, device, completed)
                elif args.row == "5":
                    run_test5_headroom_sweep(
                        cfg, session_dir, session_ts, device, completed
                    )
                elif args.row == "8":
                    run_test8_headroom_saturation(
                        cfg, session_dir, session_ts, device, completed
                    )
            log.info("=== Run complete, parsing captures ===")
            do_parse(cfg, session_dir, session_ts)
        elif args.command == "parse":
            do_parse(cfg, session_dir, session_ts)
    except ValueError as e:
        log.error(f"Config error: {e}")
        sys.exit(1)
    except StrayProcessError as e:
        log.error(f"Stray process check failed: {e}")
        sys.exit(1)
    except InfrastructureCheckError as e:
        log.error(f"Infrastructure check failed: {e}")
        sys.exit(1)
    except DeviceCheckError as e:
        log.error(f"Sanity check failed: {e}")
        log.error("Fix device power/pairing/range before starting the session.")
        sys.exit(1)
    except RunAbortedError as e:
        log.error(f"Session aborted: {e}")
        log.info("=== Parsing captures collected so far ===")
        do_parse(cfg, session_dir, session_ts)
        sys.exit(1)
    finally:
        # Drivers run in their own session, so Ctrl+C doesn't reach them --
        # stop any still running and release their devices, or the next
        # session starts with a stray link on the shared radio.
        stop_all_drivers()
        for device in (cfg.get("devices") or []) if args.command == "run" else []:
            if device.get("mac_address") and _link_is_up(device["mac_address"]):
                disconnect_ble_device(device["mac_address"])
        stop_pairing_agent(pairing_agent)
        stop_channel_occupancy_poller(occupancy_poller)
        restore_internal_wifi(rfkill_id)


if __name__ == "__main__":
    main()
