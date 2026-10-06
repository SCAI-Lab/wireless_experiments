"""Session-level parsing: manifest.csv -> results.csv (do_parse)."""

import csv
import time

from .logs import log
from .session_files import (
    manifest_path,
    results_path,
)
from .capture import parse_sniffer_pcap
from .run_checks import (
    FORCED_CHANNEL_ROWS,
    _known_forced_channel_hex_signatures,
    aggregate_connection_rssi,
    check_channel_map_contamination,
    check_data_published,
    driver_git_version,
    evaluate_run_result,
    extract_battery_pct_from_rosbag,
)
from .btmon_parse import parse_capture_file



def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _qbss_ambient_split(manifest_row):
    """qbss_ambient_pct / qbss_induced_pct for one run. The AP's QBSS counts
    all airtime on its channel, ours included, so the split needs readings
    without our traffic: for loaded runs, the mean of the before/after
    readings is the ambient and during-traffic minus that is our own share;
    a no-load run's whole-run QBSS is all ambient. Blank when the readings
    it needs are missing (e.g. sessions before 2026-10-06)."""
    during = _float_or_none(manifest_row.get("qbss_channel_utilization_pct"))
    loaded = _float_or_none(manifest_row.get("induced_load_mbps")) is not None
    if not loaded:
        return {"qbss_ambient_pct": "" if during is None else during,
                "qbss_induced_pct": ""}
    brackets = [
        v for v in (
            _float_or_none(manifest_row.get("qbss_before_traffic_pct")),
            _float_or_none(manifest_row.get("qbss_after_traffic_pct")),
        ) if v is not None
    ]
    if not brackets:
        return {"qbss_ambient_pct": "", "qbss_induced_pct": ""}
    ambient = round(sum(brackets) / len(brackets), 1)
    induced = "" if during is None else round(during - ambient, 1)
    return {"qbss_ambient_pct": ambient, "qbss_induced_pct": induced}

def do_parse(cfg, session_dir, session_ts):
    """Parse every capture recorded in this session's own manifest.csv into
    this session's own results.csv. Each session directory is self-contained,
    so every manifest row here belongs to this one session -- no need to
    filter by session_id."""
    manifest = manifest_path(session_dir)
    if not manifest.exists():
        log.warning(
            f"No manifest.csv found in {session_dir} — run some sessions first."
        )
        return

    out_path = results_path(cfg, session_dir)
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"=== Parsing session {session_ts} captures into {out_path} ===")
    forced_channel_signatures = _known_forced_channel_hex_signatures(cfg)

    with open(manifest) as f:
        rows = list(csv.DictReader(f))

    # Fallback for manifests recorded before the mac_address column existed
    # -- look it up from the current config by device name instead, so an
    # old session can still be re-parsed with correct per-device filtering.
    mac_by_device_name = {d["name"]: d.get("mac_address") for d in cfg["devices"]}
    battery_topic_by_device_name = {
        d["name"]: d.get("battery_topic") for d in cfg["devices"]
    }
    # driver_git_version shells out to git -- cache per device name instead
    # of re-running it for every row of a session (the driver source tree
    # doesn't change mid-session).
    driver_version_by_device_name = {
        d["name"]: driver_git_version(d.get("driver_source_dir"))
        for d in cfg["devices"]
    }

    fieldnames = [
        "session_id",
        "row",
        "device",
        "condition",
        "replicate",
        "capture_file",
        # status=PASSED means "ran as expected for this row/condition," NOT
        # "the BLE link stayed up." For rows in EXPECTED_INSTABILITY_ROW_
        # CONDITIONS (forced-channel + load, where degradation/collapse IS
        # the expected result being measured), PASSED only requires that the
        # link engaged at all (some connected time or a recorded 0x3E
        # establishment-failed attempt) -- a run with 30 disconnects can
        # legitimately be PASSED there. See evaluate_run_result's docstring
        # for the full reasoning.
        "status",
        "error",
        "generated_at",
        "duration_s",
        "initial_interval_ms",
        "initial_latency_ms",
        "initial_timeout_ms",
        "num_updates",
        "update_intervals",
        "mtu",
        "disconnect_count",
        "unexpected_disconnect_count",
        "n_reason_supervision_timeout",
        "n_reason_establishment_failed",
        "n_reason_local_host_term",
        "n_reason_other",
        "connected_duration_s",
        "capture_duration_s",
        "ecg_stream_start_delay_s",
        "n_encryption_attempts",
        "n_encryption_successes",
        "n_encryption_failures",
        "induced_load_mbps",
        "rx_sample_count",
        "jitter_p50_ms",
        "jitter_p95_ms",
        "jitter_p99_ms",
        "jitter_p999_ms",
        "sniffer_adv_packet_count",
        "sniffer_adv_crc_error_count",
        "sniffer_adv_crc_error_rate",
        "sniffer_adv_rssi_avg_dbm",
        "sniffer_adv_rssi_min_dbm",
        "sniffer_adv_rssi_max_dbm",
        # connection_rssi_* is the CENTRAL's own RSSI on the actual live
        # connection (HCI Read RSSI via the AFH poller, see
        # aggregate_connection_rssi) -- distinct from sniffer_adv_rssi_*
        # above, which is the Nordic sniffer's OTA view of advertising
        # packets only.
        "connection_rssi_sample_count",
        "connection_rssi_avg_dbm",
        "connection_rssi_min_dbm",
        "connection_rssi_max_dbm",
        "packet_loss_pct",
        "iperf_jitter_ms",
        "qbss_channel_utilization_pct",
        # Ambient vs own-load split of the test AP's QBSS (see
        # _qbss_ambient_split): single readings just before our iperf traffic
        # starts and after it stops, their mean as the ambient estimate, and
        # during-traffic minus ambient as our own load's share.
        "qbss_before_traffic_pct",
        "qbss_after_traffic_pct",
        "qbss_ambient_pct",
        "qbss_induced_pct",
        "connection_established",
        "retry_on_fail",
        # driver_version: this device's driver package's own git commit
        # hash (see driver_git_version) -- config.yaml's devices[].
        # driver_source_dir must point at that package's source tree.
        # battery_pct: last sensor_msgs/BatteryState reading from this
        # run's own rosbag (see extract_battery_pct_from_rosbag) -- blank
        # whenever that device's driver wasn't run with its own
        # --publish-battery enabled (see devices[].battery_topic comments).
        "driver_version",
        "battery_pct",
        "acc_pmd_frame_count",
        "acc_samples_received",
        "acc_samples_lost",
        "acc_completeness_pct",
        "acc_samples_skipped",
        "acc_stream_completeness_pct",
        "hr_rr_total_s",
        "hr_rr_completeness_pct",
        "ecg_pmd_frame_count",
        "ecg_samples_received",
        "ecg_samples_lost",
        "ecg_completeness_pct",
        "ecg_samples_skipped",
        "ecg_stream_completeness_pct",
        "corsano_acc_packet_count",
        "corsano_acc_samples_lost",
        "corsano_acc_completeness_pct",
        "corsano_activity_record_count",
        "corsano_activity_gap_s_max",
        "corsano_stress_record_count",
        "corsano_stress_gap_s_max",
        "corsano_bioz_bytes_downloaded",
        "corsano_bioz_file_growth_bytes",
        "corsano_bioz_completeness_pct",
        "corsano_file_polls",
        "corsano_acc_file_growth_bytes",
        "corsano_stress_file_growth_bytes",
        "corsano_new_sensor_data",
    ]

    num_passed = 0
    num_failed = 0
    session_results = []  # (label, status, reason) for every row in this session
    # Connection-establishment success rate per (row, device, condition):
    # distinguishes "never connected" from "connected, then dropped later" --
    # see connection_established in _attempt_run. [established_count, total]
    establishment_stats = {}

    def _track_establishment(row):
        key = (row["row"], row["device"], row["condition"])
        stats = establishment_stats.setdefault(key, [0, 0])
        stats[1] += 1
        if str(row.get("connection_established", "")).strip().lower() in ("true", "1"):
            stats[0] += 1

    def _label(row):
        return f"{row['row']} | {row['device']} | {row['condition']} | rep{row['replicate']}"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            status = row.get("status", "PASSED")  # older manifests predate this column
            _track_establishment(row)
            if status == "FAILED":
                num_failed += 1
                writer.writerow(
                    {
                        "session_id": row.get("session_id", ""),
                        "row": row["row"],
                        "device": row["device"],
                        "condition": row["condition"],
                        "replicate": row["replicate"],
                        "capture_file": row["capture_file"],
                        "status": status,
                        "error": row.get("error", ""),
                        "generated_at": generated_at,
                        "duration_s": row.get("duration_s", ""),
                        "connection_established": row.get("connection_established", ""),
                        "retry_on_fail": row.get("retry_on_fail", ""),
                    }
                )
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- "
                    f"{row.get('error', '')}, skipping parse"
                )
                session_results.append((_label(row), "FAILED", row.get("error", "")))
                continue

            capture_path = session_dir / row["capture_file"]
            if not capture_path.exists():
                log.warning(f"  ! missing capture file: {capture_path}, skipping")
                num_failed += 1
                session_results.append((_label(row), "FAILED", "capture file missing"))
                continue
            mac_address = row.get("mac_address") or mac_by_device_name.get(
                row["device"]
            )
            planned_duration_s = row.get("duration_s", "")
            parsed = parse_capture_file(
                capture_path,
                mac_address=mac_address,
                planned_duration_s=planned_duration_s,
            )
            sniffer_pcap_file = row.get("sniffer_pcap_file")
            sniffer_metrics = parse_sniffer_pcap(
                session_dir / sniffer_pcap_file if sniffer_pcap_file else None,
                mac_address,
            )

            # The driver process staying alive (status == "PASSED" here) says
            # nothing about whether the underlying BLE link actually stayed
            # up -- so the real pass/fail verdict is decided here, from the
            # parsed capture, and can downgrade a live-process run to FAILED.
            device_cfg = next(
                (d for d in cfg.get("devices") or [] if d.get("name") == row["device"]), {}
            )
            data_problems = check_data_published(
                device_cfg, capture_path.with_name(capture_path.stem + "_rosbag"), parsed
            )
            final_status, final_error, failure_kind = evaluate_run_result(
                parsed, row["row"], row["condition"], planned_duration_s, data_problems
            )

            # Computed unconditionally (not just for the contamination check
            # below) -- the AFH poller writes this run's own connection-RSSI
            # samples here regardless of row, so aggregate_connection_rssi
            # needs it for every row, not just the ones eligible for the
            # forced-channel-leak check.
            afh_csv_path = capture_path.with_name(capture_path.stem + "_afh.csv")
            connection_rssi = aggregate_connection_rssi(afh_csv_path)
            rosbag_dir = capture_path.with_name(capture_path.stem + "_rosbag")
            battery_pct = extract_battery_pct_from_rosbag(
                rosbag_dir, battery_topic_by_device_name.get(row["device"])
            )
            driver_version = driver_version_by_device_name.get(row["device"])

            # Channel-map contamination check: only for rows that never
            # force a classification themselves (see FORCED_CHANNEL_ROWS) --
            # a leaked forced map here invalidates the run regardless of
            # what evaluate_run_result decided from the link metrics alone.
            if row["row"] not in FORCED_CHANNEL_ROWS:
                contamination = check_channel_map_contamination(
                    afh_csv_path, forced_channel_signatures
                )
                if contamination:
                    final_status, final_error, failure_kind = (
                        "FAILED",
                        contamination,
                        "channel_map_contamination",
                    )
                    log.error(
                        f"  ! {row['capture_file']}: EXCLUDED -- {contamination}"
                    )
            link_ok = final_status == "PASSED"

            out_row = {
                "session_id": row.get("session_id", ""),
                "row": row["row"],
                "device": row["device"],
                "condition": row["condition"],
                "replicate": row["replicate"],
                "capture_file": row["capture_file"],
                "status": final_status,
                "error": final_error,
                "generated_at": generated_at,
                "duration_s": planned_duration_s,
                "induced_load_mbps": row.get("induced_load_mbps", ""),
                "packet_loss_pct": row.get("packet_loss_pct", ""),
                "iperf_jitter_ms": row.get("iperf_jitter_ms", ""),
                "qbss_channel_utilization_pct": row.get(
                    "qbss_channel_utilization_pct", ""
                ),
                "qbss_before_traffic_pct": row.get("qbss_before_traffic_pct", ""),
                "qbss_after_traffic_pct": row.get("qbss_after_traffic_pct", ""),
                **_qbss_ambient_split(row),
                "connection_established": row.get("connection_established", ""),
                "retry_on_fail": row.get("retry_on_fail", ""),
                "driver_version": driver_version,
                "battery_pct": battery_pct,
                **parsed,
                **sniffer_metrics,
                **connection_rssi,
            }
            writer.writerow(out_row)
            if link_ok:
                num_passed += 1
            else:
                num_failed += 1
            log.info(
                f"  parsed {row['capture_file']}: "
                f"interval={parsed['initial_interval_ms']}ms, "
                f"updates={parsed['num_updates']}, mtu={parsed['mtu']}, "
                f"connected={parsed['connected_duration_s']}s, "
                f"disconnects={parsed['disconnect_count']} "
                f"({parsed['unexpected_disconnect_count']} unexpected), "
                f"establishment_failed={parsed['n_reason_establishment_failed']}"
            )
            log.info(
                f"     RX inter-arrival jitter (n={parsed['rx_sample_count']}): "
                f"p50={parsed['jitter_p50_ms']}ms p95={parsed['jitter_p95_ms']}ms "
                f"p99={parsed['jitter_p99_ms']}ms p99.9={parsed['jitter_p999_ms']}ms"
            )
            if parsed["rx_sample_count"] and parsed["rx_sample_count"] < 1000:
                log.warning(
                    f"     only {parsed['rx_sample_count']} RX samples -- p99.9 "
                    f"is not statistically reliable with this few, treat as "
                    f"indicative only"
                )
            if parsed["acc_pmd_frame_count"]:
                log.info(
                    f"     Polar ACC: {parsed['acc_completeness_pct']}% of planned "
                    f"run, {parsed['acc_stream_completeness_pct']}% in-stream "
                    f"({parsed['acc_samples_skipped']} skipped)"
                )
            if parsed["ecg_pmd_frame_count"]:
                log.info(
                    f"     Polar ECG: {parsed['ecg_completeness_pct']}% of planned "
                    f"run, {parsed['ecg_stream_completeness_pct']}% in-stream "
                    f"({parsed['ecg_samples_skipped']} skipped)"
                )
            if parsed["hr_rr_total_s"] is not None:
                log.info(
                    f"     Polar HR/RR completeness (approximate): "
                    f"{parsed['hr_rr_completeness_pct']}%"
                )
            if parsed["corsano_file_polls"] is not None:
                log.info(
                    f"     Corsano: {parsed['corsano_file_polls']} polls, recorded "
                    f"ACC +{parsed['corsano_acc_file_growth_bytes']} B / BioZ "
                    f"+{parsed['corsano_bioz_file_growth_bytes']} B / stress "
                    f"+{parsed['corsano_stress_file_growth_bytes']} B"
                    + (" -- NO new sensor data" if parsed["corsano_new_sensor_data"] is False else "")
                )
            if parsed["corsano_acc_completeness_pct"] is not None:
                log.info(
                    f"     Corsano ACC completeness: "
                    f"{parsed['corsano_acc_completeness_pct']}%"
                )
            if parsed["corsano_bioz_completeness_pct"] is not None:
                log.info(
                    f"     Corsano BioZ completeness: "
                    f"{parsed['corsano_bioz_completeness_pct']}%"
                )
            if failure_kind == "baseline":
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- baseline stability "
                    f"check: {final_error}"
                )
            elif failure_kind == "pairing":
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- stale bond: "
                    f"{final_error}"
                )
            elif failure_kind == "connectivity":
                log.warning(
                    f"  ! {row['capture_file']}: FAILED -- {final_error} -- "
                    f"the BLE link did not stay up for the full run"
                )
            elif failure_kind == "no_data":
                log.warning(f"  ! {row['capture_file']}: FAILED -- {final_error}")
            session_results.append((_label(row), final_status, final_error or None))

    log.info(f"Saved results to {out_path}")
    log.info(
        f"Attempts parsed: {num_passed} passed, {num_failed} failed "
        f"(every attempt, including ones later rerun -- all kept in results.csv)"
    )

    if session_results:
        # One line per run with its FINAL status: manifest rows are in run
        # order, so the last attempt of each label is its latest. Earlier
        # attempts stay in results.csv and in the per-capture log lines above.
        final = {}
        attempts = {}
        for label, status, reason in session_results:
            final[label] = (status, reason)
            attempts[label] = attempts.get(label, 0) + 1
        passed = [l for l, (s, _) in final.items() if s == "PASSED"]
        failed = [l for l, (s, _) in final.items() if s != "PASSED"]

        def _tries(label):
            n = attempts[label]
            return f" (final of {n} attempts)" if n > 1 else ""

        log.info(f"=== Session {session_ts} results (final status per run) ===")
        for label in passed:
            log.info(f"  RESULT: PASSED -- {label}{_tries(label)}")
        for label in failed:
            reason = final[label][1]
            log.info(f"  RESULT: FAILED -- {label}{_tries(label)}" + (f" -- {reason}" if reason else ""))
        log.info(
            f"=== Session summary: {len(final)} runs -- {len(passed)} passed, "
            f"{len(failed)} failed ==="
        )
        if establishment_stats:
            log.info("=== Connection-establishment success rate ===")
            for (row_label, device, condition), (established, total) in sorted(
                establishment_stats.items()
            ):
                pct = 100 * established / total if total else 0.0
                log.info(
                    f"  {row_label} | {device} | {condition}: "
                    f"{established}/{total} established ({pct:.0f}%)"
                )
