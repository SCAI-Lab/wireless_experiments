"""Run execution: one recorded run (do_run) with its retry/abort handling."""

import os
import subprocess
import sys
import time

from .errors import (
    ChannelClassificationError,
    DriverExitedError,
    RunAbortedError,
)
from .logs import log
from .session_files import (
    _set_manifest_verdict,
    append_manifest,
    set_aside_existing_run_outputs,
)
from .capture import (
    parse_sniffer_pcap,
    start_ble_sniffer,
    start_btmon,
    start_rosbag_recording,
    stop_ble_sniffer,
    stop_btmon,
    stop_rosbag_recording,
)
from .wifi import (
    sample_qbss_once,
    start_qbss_poller,
    stop_qbss_poller,
)
from .ble_radio import (
    reset_host_channel_classification,
    set_host_channel_classification,
    start_afh_poller,
    stop_afh_poller,
)
from .drivers import (
    disconnect_ble_device,
    heal_stale_bluez_connection,
    sleep_and_watch_driver,
    start_driver_with_retry,
    stop_driver,
)
from .run_checks import (
    MIN_INDUCED_LOAD_FRACTION,
    aggregate_connection_rssi,
    check_data_published,
    evaluate_run_result,
    rosbag_topic_counts,
)
from .btmon_parse import parse_capture_file


# =============================================================================
# Run execution
# =============================================================================


def _bluetoothd_instance():
    """(MainPID, start timestamp) of the running bluetooth.service, or None
    if it can't be read. A different value after a run means bluetoothd
    died and was restarted in between -- confirmed 2026-09-30 21:25:51, when
    it segfaulted mid-run (row3 forced_channel_duty25pct rep2) and systemd
    restarted it while the run carried on and was recorded as PASSED.
    The start timestamp guards against PID reuse."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "bluetooth", "-p", "MainPID",
             "-p", "ExecMainStartTimestamp"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pid = props.get("MainPID", "0")
    if pid in ("", "0"):
        return ("not running", "")
    return (pid, props.get("ExecMainStartTimestamp", ""))


def _attempt_run(
    cfg,
    session_dir,
    row_label,
    device_name,
    launch_cmd,
    condition,
    replicate,
    duration_s,
    session_id,
    capture_file,
    mac_address=None,
    total_replicates=None,
    extra=None,
    retry_launch=True,
):
    """Execute exactly one attempt of a run (btmon + driver + optional iperf
    + watch), write its manifest row, and return (status, error) -- the REAL
    verdict, checked immediately via evaluate_run_result() when the driver
    survives, not just whether the process stayed alive. Never raises for a
    failed attempt; do_run decides what to do about that.

    retry_launch=False (Row 2/3) launches the driver exactly once instead of
    retrying a flaky initial connection -- a failed connection attempt under
    interference is itself the KPI, not noise to retry away. See
    connection_established in the manifest row: distinguishes "never
    connected" from "connected, then dropped later"."""
    rep_label = (
        f"{replicate}/{total_replicates}" if total_replicates else str(replicate)
    )
    log.info(
        f"=== {row_label} | {device_name} | {condition} | replicate {rep_label} ==="
    )
    start_time = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"  -> start time: {start_time}, planned duration: {duration_s}s")
    log.info(f"  -> capture file: {capture_file}")
    # Also before the run: covers a wedge left by an interrupted session.
    heal_stale_bluez_connection(mac_address)
    bluetoothd_before = _bluetoothd_instance()

    if not set_aside_existing_run_outputs(capture_file):
        raise RunAbortedError(
            f"earlier output at {capture_file.stem} couldn't be moved aside -- "
            f"refusing to start a run that would write over it"
        )
    btmon_proc = start_btmon(cfg["btmon_controller"], capture_file)
    sniffer_pcap = capture_file.with_suffix(".pcap")
    sniffer_proc = start_ble_sniffer(cfg, mac_address, sniffer_pcap)
    afh_csv = capture_file.with_name(capture_file.stem + "_afh.csv")
    afh_poller = start_afh_poller(cfg["btmon_controller"], mac_address, afh_csv)
    rosbag_proc = None
    if (cfg.get("rosbag") or {}).get("enabled"):
        rosbag_dir = capture_file.with_name(capture_file.stem + "_rosbag")
        rosbag_proc = start_rosbag_recording(rosbag_dir)

    iperf_proc = None
    driver_proc = None
    qbss_poller = None
    qbss = None
    qbss_before = None
    traffic_end = None
    iperf_cfg = (cfg.get("row2_duty_cycle") or {}).get("iperf") or {}
    qbss_iface = iperf_cfg.get("qbss_iface")
    qbss_bssid = iperf_cfg.get("qbss_bssid")
    connection_established = False
    forced_channels_applied = False
    try:
        if not (extra and extra.get("forced_channels")):
            # This run must be AFH-free. The controller's channel
            # classification is persistent hardware state -- NOT tied to
            # any one process -- so a run that itself never calls
            # set_host_channel_classification has no way to know whether a
            # PREVIOUS run (or an interrupted/crashed session) already left
            # one active. Confirmed as a real incident (2026-09-24): a Row 3
            # run was interrupted when a session was stopped and resumed,
            # and its forced classification leaked into two subsequent
            # "AFH-free" Row 2 runs, both showing exactly FORCED_CHANNEL_
            # RANGE's bitmap for their entire duration. Resetting is cheap
            # and idempotent (a no-op if already unrestricted), so this
            # runs unconditionally rather than trying to first detect
            # whether a reset is actually needed -- there's no HCI command
            # to read back "what classification is currently set" without
            # an active connection to query its negotiated map against, so
            # a blind, confirmed reset is the only real verification
            # available before a connection even exists. See do_parse's
            # check_channel_map_contamination for the detection-side
            # backstop, in case this guard is ever bypassed.
            if not reset_host_channel_classification():
                raise ChannelClassificationError(
                    "could not confirm the channel classification was reset "
                    "before this AFH-free run -- refusing to proceed, since "
                    "starting without confirming a clean (unrestricted) map "
                    "risks silently repeating a real past contamination "
                    "incident rather than producing valid AFH-free data"
                )

        driver_proc = start_driver_with_retry(
            launch_cmd,
            max_attempts=3 if retry_launch else 1,
            log_path=os.path.splitext(capture_file)[0] + "_driver.log",
        )
        connection_established = True

        if extra and extra.get("forced_channels"):
            forced = extra["forced_channels"]
            log.info(
                f"  -> forcing BLE channel classification to {forced} "
                f"(no AFH escape route)"
            )
            # Set before sending: a "failed" send may still have reached the
            # controller (2026-10-05 row5 k0 rep2: hcitool missed a Success
            # reply), so the abort path must reset it either way.
            forced_channels_applied = True
            if not set_host_channel_classification(forced):
                raise ChannelClassificationError(
                    f"failed to force channel classification to {forced} -- "
                    f"hcitool likely lacks permission (see the AFH preflight "
                    f"check's setcap guidance) or the controller rejected it"
                )

        settle = 0
        if extra and extra.get("start_iperf"):
            settle = extra["iperf_cfg"].get("settle_delay_s", 0)
            if settle:
                log.info(
                    f"  -> waiting {settle}s for BLE connection to settle before traffic"
                )
                sleep_and_watch_driver(settle, driver_proc, launch_cmd)
            # Ambient reading just before our own traffic starts -- the
            # during-traffic QBSS includes our load, so it can't be split
            # into ambient + induced on its own. The scan takes a few
            # seconds, which comes out of the traffic phase so the run still
            # ends on time.
            scan_start = time.monotonic()
            qbss_before = sample_qbss_once(qbss_iface, qbss_bssid)
            settle += round(time.monotonic() - scan_start)
            iperf_proc = extra["start_iperf_fn"](
                extra["iperf_cfg"], duration_s - settle
            )
        # QBSS during the run: the traffic phase for loaded runs, the whole
        # run (all ambient) for no-load runs. Stopped as soon as the watched
        # duration ends (below) -- an earlier version sampled once, after
        # teardown, which measured an idle channel.
        qbss_poller = start_qbss_poller(qbss_iface, qbss_bssid)

        remaining = duration_s - settle
        log.info(f"  -> running for {remaining}s more ({duration_s}s total)...")
        sleep_and_watch_driver(remaining, driver_proc, launch_cmd)
        # Stop QBSS sampling as soon as the watched duration ends (traffic
        # is still flowing or just finished), not after the driver/btmon/
        # sniffer teardown below -- by then the channel is idle again.
        qbss = stop_qbss_poller(qbss_poller)
        qbss_poller = None  # already stopped; don't stop it again in a later path
    except (DriverExitedError, ChannelClassificationError) as e:
        log.error(f"  !! run aborted: {e}")
        if iperf_proc:
            iperf_proc.terminate()
        if driver_proc is not None:
            stop_driver(driver_proc)
        stop_btmon(btmon_proc)
        stop_ble_sniffer(sniffer_proc)
        stop_afh_poller(afh_poller)
        stop_rosbag_recording(rosbag_proc)
        stop_qbss_poller(qbss_poller)
        if forced_channels_applied:
            reset_host_channel_classification()
        disconnect_ble_device(mac_address)
        heal_stale_bluez_connection(mac_address)
        append_manifest(
            session_dir,
            {
                "session_id": session_id,
                "row": row_label,
                "device": device_name,
                "condition": condition,
                "replicate": replicate,
                "capture_file": capture_file.name,
                "start_time": start_time,
                "duration_s": duration_s,
                "status": "FAILED",
                "error": str(e),
                "mac_address": mac_address or "",
                "sniffer_pcap_file": sniffer_pcap.name,
                "connection_established": connection_established,
                "retry_on_fail": retry_launch,
            },
        )
        log.error(f"  !! RESULT: FAILED -- {e}")
        return "FAILED", str(e)

    induced_load_mbps = None
    udp_stats = None
    # Set when this run doesn't represent its condition, whatever the link
    # did, and is recorded as FAILED: its WiFi load can't be shown to have
    # been applied (was only a warning until 2026-09-30, when row4
    # duty100pct_long passed although its load stopped ~13 min into a 30 min
    # run), or bluetoothd crashed/restarted mid-run (checked after teardown).
    invalid_error = None
    if iperf_proc:
        try:
            stdout_text, _ = iperf_proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            iperf_proc.kill()
            stdout_text, _ = iperf_proc.communicate()
        traffic_end = time.monotonic()
        parse_fn = extra.get("parse_iperf_fn") if extra else None
        if parse_fn:
            induced_load_mbps = parse_fn(stdout_text)
            target_mbps = extra.get("target_mbps") if extra else None
            if induced_load_mbps is None:
                invalid_error = (
                    "could not confirm induced load -- iperf3 output was "
                    "unparseable (ssh/iperf3 probably died mid-run), so the "
                    "interference condition may not have been applied; rerun "
                    "with --rerun-failed"
                )
                log.error(f"  !! {invalid_error}")
            elif target_mbps:
                pct = 100 * induced_load_mbps / target_mbps
                log.info(
                    f"  -> induced load (iperf3 actual): {induced_load_mbps:.1f} "
                    f"Mbit/s ({pct:.0f}% of {target_mbps:.1f} Mbit/s target)"
                )
                if induced_load_mbps < MIN_INDUCED_LOAD_FRACTION * target_mbps:
                    invalid_error = (
                        f"induced load only averaged {pct:.0f}% of the "
                        f"{target_mbps:.1f} Mbit/s target (minimum "
                        f"{MIN_INDUCED_LOAD_FRACTION:.0%}) -- the interference "
                        f"condition wasn't applied as intended; rerun with "
                        f"--rerun-failed"
                    )
                    log.error(f"  !! {invalid_error}")
            else:
                log.info(
                    f"  -> induced load (iperf3 actual): {induced_load_mbps:.1f} Mbit/s"
                )
        udp_parse_fn = extra.get("parse_udp_stats_fn") if extra else None
        if udp_parse_fn:
            udp_stats = udp_parse_fn(stdout_text)
            if udp_stats:
                log.info(
                    f"  -> iperf3 UDP: {udp_stats['packet_loss_pct']:.2f}% packet "
                    f"loss, jitter {udp_stats['iperf_jitter_ms']:.2f}ms (Pi-side, "
                    f"transport-level -- separate from BLE's own metrics)"
                )
    stop_driver(driver_proc)
    stop_btmon(btmon_proc)
    stop_ble_sniffer(sniffer_proc)
    stop_afh_poller(afh_poller)
    stop_rosbag_recording(rosbag_proc)
    if forced_channels_applied:
        if not reset_host_channel_classification():
            log.warning(
                "  -> could not confirm the forced BLE channel classification "
                "was reset -- check before trusting the NEXT run's results"
            )
    disconnect_ble_device(mac_address)
    heal_stale_bluez_connection(mac_address)

    # Ambient reading after our traffic stops, once the AP's QBSS averaging
    # window no longer covers it (802.11's default is 50 beacon intervals,
    # ~5 s; this AP's own window is undocumented). Teardown above usually
    # already used up most of the wait.
    qbss_after = None
    if traffic_end is not None:
        wait_s = iperf_cfg.get("qbss_after_traffic_delay_s", 6) - (
            time.monotonic() - traffic_end
        )
        if wait_s > 0:
            time.sleep(wait_s)
        qbss_after = sample_qbss_once(qbss_iface, qbss_bssid)

    if qbss:
        log.info(
            f"  -> AP channel utilization (QBSS, best-effort, sampled "
            f"{'during traffic' if iperf_proc else 'during the run, no load'}): "
            f"{qbss['qbss_channel_utilization_pct']}%"
        )
    if iperf_proc:
        log.info(
            f"  -> ambient QBSS bracketing the traffic: before "
            f"{'n/a' if qbss_before is None else f'{qbss_before}%'}, after "
            f"{'n/a' if qbss_after is None else f'{qbss_after}%'}"
        )

    bluetoothd_after = _bluetoothd_instance()
    if bluetoothd_before and bluetoothd_after and bluetoothd_before != bluetoothd_after:
        restart_error = (
            f"bluetoothd crashed or restarted during the run (pid "
            f"{bluetoothd_before[0]} started {bluetoothd_before[1]} -> pid "
            f"{bluetoothd_after[0]} started {bluetoothd_after[1]}; see "
            f"`journalctl -u bluetooth`) -- every BlueZ connection was torn "
            f"down mid-run, so this isn't a measurement of the condition; "
            f"rerun with --rerun-failed"
        )
        log.error(f"  !! {restart_error}")
        invalid_error = f"{invalid_error}; {restart_error}" if invalid_error else restart_error

    append_manifest(
        session_dir,
        {
            "session_id": session_id,
            "row": row_label,
            "device": device_name,
            "condition": condition,
            "replicate": replicate,
            "capture_file": capture_file.name,
            "start_time": start_time,
            "duration_s": duration_s,
            "status": "FAILED" if invalid_error else "PASSED",
            "error": invalid_error or "",
            "induced_load_mbps": (
                "" if induced_load_mbps is None else round(induced_load_mbps, 2)
            ),
            "packet_loss_pct": (udp_stats or {}).get("packet_loss_pct", ""),
            "iperf_jitter_ms": (udp_stats or {}).get("iperf_jitter_ms", ""),
            "qbss_channel_utilization_pct": (qbss or {}).get(
                "qbss_channel_utilization_pct", ""
            ),
            "qbss_before_traffic_pct": "" if qbss_before is None else qbss_before,
            "qbss_after_traffic_pct": "" if qbss_after is None else qbss_after,
            "mac_address": mac_address or "",
            "sniffer_pcap_file": sniffer_pcap.name,
            "connection_established": connection_established,
            "retry_on_fail": retry_launch,
        },
    )
    log.info(f"  -> saved {capture_file.name}")
    log.info(
        f"=== test finished: {row_label} | {device_name} | {condition} | "
        f"rep{replicate} -- verifying BLE link ==="
    )

    # The driver surviving doesn't mean the BLE link stayed up -- check that
    # immediately (same criteria do_parse uses at session end) so a real
    # failure surfaces now, while the operator is still at the chamber,
    # instead of only showing up when results.csv is parsed much later.
    if not capture_file.exists():
        return "FAILED", "capture file missing after run"
    parsed = parse_capture_file(
        capture_file, mac_address=mac_address, planned_duration_s=duration_s
    )
    log.info(
        f"  -> metrics: interval={parsed['initial_interval_ms']}ms, "
        f"mtu={parsed['mtu']}, connected={parsed['connected_duration_s']}s, "
        f"disconnects={parsed['disconnect_count']} "
        f"({parsed['unexpected_disconnect_count']} unexpected), "
        f"establishment_failed={parsed['n_reason_establishment_failed']}"
    )
    # "unexpected" lumps real link losses together with failed reconnect
    # attempts (0x3E) -- 2026-09-30 row3 max_load: 37 "unexpected", of which
    # only 6 were the link actually dropping. Report them separately.
    log.info(
        f"     link losses (supervision timeout): "
        f"{parsed['n_reason_supervision_timeout']}, failed (re)connect "
        f"attempts: {parsed['n_reason_establishment_failed']}, other "
        f"remote/unknown reasons: {parsed['n_reason_other']}"
    )
    log.info(
        f"     RX inter-arrival jitter (n={parsed['rx_sample_count']}): "
        f"p50={parsed['jitter_p50_ms']}ms p95={parsed['jitter_p95_ms']}ms "
        f"p99={parsed['jitter_p99_ms']}ms p99.9={parsed['jitter_p999_ms']}ms"
    )
    if parsed["acc_pmd_frame_count"]:
        log.info(
            f"     Polar ACC: {parsed['acc_completeness_pct']}% of the planned "
            f"run delivered ({parsed['acc_samples_received']} received, "
            f"{parsed['acc_samples_lost']} missing); in-stream: "
            f"{parsed['acc_samples_skipped']} samples skipped between "
            f"received frames ({parsed['acc_stream_completeness_pct']}% intact, "
            f"from the sensor's own timestamps)"
        )
    if parsed["ecg_pmd_frame_count"]:
        log.info(
            f"     Polar ECG: {parsed['ecg_completeness_pct']}% of the planned "
            f"run delivered ({parsed['ecg_samples_received']} received, "
            f"{parsed['ecg_samples_lost']} missing); in-stream: "
            f"{parsed['ecg_samples_skipped']} samples skipped between "
            f"received frames ({parsed['ecg_stream_completeness_pct']}% intact, "
            f"from the sensor's own timestamps)"
        )
    if parsed["hr_rr_total_s"] is not None:
        log.info(
            f"     Polar HR/RR completeness (approximate): "
            f"{parsed['hr_rr_total_s']}s of RR-intervals received vs. "
            f"{parsed['connected_duration_s']}s connected "
            f"({parsed['hr_rr_completeness_pct']}%)"
        )
    if parsed["corsano_file_polls"] is not None:
        log.info(
            f"     Corsano: {parsed['corsano_file_polls']} file polls completed; "
            f"recorded during run: ACC +{parsed['corsano_acc_file_growth_bytes']} B, "
            f"BioZ +{parsed['corsano_bioz_file_growth_bytes']} B, "
            f"stress +{parsed['corsano_stress_file_growth_bytes']} B"
        )
        if parsed["corsano_new_sensor_data"] is False:
            log.warning(
                "     ! the watch recorded NO new ACC/BioZ/stress data during this "
                "run (its files didn't grow) -- the driver is republishing old "
                "samples; check the watch is worn and recording"
            )
    if parsed["corsano_acc_completeness_pct"] is not None:
        log.info(
            f"     Corsano ACC completeness: "
            f"{parsed['corsano_acc_packet_count']} chunks, "
            f"{parsed['corsano_acc_samples_lost']} lost "
            f"({parsed['corsano_acc_completeness_pct']}%)"
        )
    if parsed["corsano_bioz_completeness_pct"] is not None:
        log.info(
            f"     Corsano BioZ completeness: "
            f"{parsed['corsano_bioz_bytes_downloaded']}/"
            f"{parsed['corsano_bioz_file_growth_bytes']} bytes "
            f"({parsed['corsano_bioz_completeness_pct']}%)"
        )
    if induced_load_mbps is not None:
        log.info(f"     induced load (iperf3 actual): {induced_load_mbps:.1f} Mbit/s")
    sniffer_metrics = parse_sniffer_pcap(sniffer_pcap, mac_address)
    if sniffer_metrics["sniffer_adv_packet_count"] is not None:
        log.info(
            f"     over-the-air (adv channels): "
            f"n={sniffer_metrics['sniffer_adv_packet_count']}, "
            f"CRC errors={sniffer_metrics['sniffer_adv_crc_error_count']} "
            f"({sniffer_metrics['sniffer_adv_crc_error_rate']:.1%}), "
            f"RSSI avg={sniffer_metrics['sniffer_adv_rssi_avg_dbm']}dBm "
            f"[{sniffer_metrics['sniffer_adv_rssi_min_dbm']}, "
            f"{sniffer_metrics['sniffer_adv_rssi_max_dbm']}]"
        )
    connection_rssi = aggregate_connection_rssi(afh_csv)
    if connection_rssi["connection_rssi_sample_count"]:
        log.info(
            f"     connection RSSI (central, n="
            f"{connection_rssi['connection_rssi_sample_count']}): "
            f"avg={connection_rssi['connection_rssi_avg_dbm']}dBm "
            f"[{connection_rssi['connection_rssi_min_dbm']}, "
            f"{connection_rssi['connection_rssi_max_dbm']}]"
        )
    if invalid_error:
        log.error(f"  !! RESULT: FAILED -- {invalid_error}")
        return "FAILED", invalid_error
    device_cfg = next(
        (d for d in cfg.get("devices") or [] if d.get("name") == device_name), {}
    )
    run_rosbag_dir = capture_file.with_name(capture_file.stem + "_rosbag")
    data_problems = check_data_published(device_cfg, run_rosbag_dir, parsed)
    counts = rosbag_topic_counts(run_rosbag_dir) or {}
    expected = device_cfg.get("expected_topics") or []
    if expected:
        log.info(
            "     published: "
            + ", ".join(
                f"{t if t.startswith('/') else '/' + t}="
                f"{counts.get(t if t.startswith('/') else '/' + t, 0)}"
                for t in expected
            )
        )
    if data_problems is None and expected:
        log.warning("     ! could not check published data (no rosbag metadata)")
    elif data_problems:
        log.warning(f"     ! sensor data problem: {'; '.join(data_problems)}")
    real_status, real_error, _ = evaluate_run_result(
        parsed, row_label, condition, duration_s, data_problems
    )
    if real_status != "PASSED":
        log.error(f"  !! RESULT: FAILED -- {real_error}")
        _set_manifest_verdict(session_dir, capture_file.name, start_time, "FAILED", real_error)
        return "FAILED", real_error
    log.info("  -> RESULT: PASSED")
    return "PASSED", ""


def do_run(
    cfg,
    session_dir,
    row_label,
    device_name,
    launch_cmd,
    condition,
    replicate,
    duration_s,
    session_id,
    mac_address=None,
    total_replicates=None,
    extra=None,
    retry_on_fail=True,
):
    """Run one test, retrying automatically-unresolvable failures by asking
    the operator to retry or stop -- on a real terminal. Non-interactively
    (no one to ask), a failure aborts the session immediately, same as
    before this per-test check existed.

    retry_on_fail=False (Row 2/3, where interference-induced instability IS
    the thing being measured) launches exactly once, never prompts, and
    never raises RunAbortedError on failure -- the failed attempt is logged
    and recorded as-is, and the session continues to the next replicate/
    condition. Retrying-until-PASS on these rows would silently discard the
    exact data point interference testing exists to capture."""
    label = f"{row_label} | {device_name} | {condition} | rep{replicate}"
    safe_condition = condition.replace("%", "pct")

    if not retry_on_fail:
        capture_file = (
            session_dir
            / f"{row_label}_{device_name}_{safe_condition}_rep{replicate}.btsnoop"
        )
        status, error = _attempt_run(
            cfg,
            session_dir,
            row_label,
            device_name,
            launch_cmd,
            condition,
            replicate,
            duration_s,
            session_id,
            capture_file,
            mac_address=mac_address,
            total_replicates=total_replicates,
            extra=extra,
            retry_launch=False,
        )
        if status != "PASSED":
            log.warning(
                f"  -> {label} FAILED (no retry -- interference-row data "
                f"point, recording as-is and continuing): {error}"
            )
        return

    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f"_attempt{attempt}"
        capture_file = (
            session_dir
            / f"{row_label}_{device_name}_{safe_condition}_rep{replicate}{suffix}.btsnoop"
        )
        status, error = _attempt_run(
            cfg,
            session_dir,
            row_label,
            device_name,
            launch_cmd,
            condition,
            replicate,
            duration_s,
            session_id,
            capture_file,
            mac_address=mac_address,
            total_replicates=total_replicates,
            extra=extra,
            retry_launch=True,
        )
        if status == "PASSED":
            return

        log.error(f"  !! TEST FAILED (attempt {attempt}): {label} -- {error}")
        if not sys.stdin.isatty():
            log.error(
                "  -> not an interactive terminal, no one to ask -- " "aborting session"
            )
            raise RunAbortedError(f"{label}: {error}")

        while True:
            answer = (
                input(
                    f"     Retry '{label}', or stop the session? [r/s] (default: r): "
                )
                .strip()
                .lower()
            )
            if answer in ("", "r", "retry"):
                attempt += 1
                log.info(f"  -> retrying {label} (attempt {attempt})...")
                break
            if answer in ("s", "stop"):
                log.error(f"  -> stopping session at user request after {label} failed")
                raise RunAbortedError(f"user stopped after {label} failed: {error}")
            print("     Please enter 'r' to retry or 's' to stop.")
