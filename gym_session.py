#!/usr/bin/env python3
"""
Gym session automation — Row 6: passive real-gym observation
==============================================================

NOT part of the chamber day. This is a separate, later visit to a real
rehab gym — no AP, no iperf3, no generic BLE peripherals. Just one sensor
+ btmon at a time, listening to whatever ambient RF/BLE conditions the
real gym has that day.

Why this exists: the chamber rows (see chamber_session.py) use synthetic
interference (iperf3 traffic, generic BLE dongles) to approximate real-world
congestion, but can't replicate genuine multi-patient RF conditions. This
block is the ground-truth check — it validates whether the simulation's
duty-cycle range and fading/multipath assumptions match a real gym.

BEFORE RUNNING THIS IN A REAL CLINICAL SPACE (checked once, not automated):
  - Get the facility's permission and let staff know you're there.
  - No ethics review should be needed -- the sensors are on a mannequin
    and no patient data is collected -- but confirm that with the facility
    for their specific space/policy rather than assuming it.
  - Privacy (GDPR / Swiss FADP): both the per-channel WiFi poller and the
    nearby-BLE-density poller below are designed to log ONLY aggregate
    counts (channel occupancy percentages, a device-count integer) and
    NEVER individual MAC addresses -- a clinic's ambient scan otherwise
    picks up bystanders' (patients', staff's) phones and wearables, which
    can count as personal data. Don't add per-device logging to either
    poller without re-checking this.

What this script does NOT automate (still manual, by design):
  - physically connecting/charging devices
  - logging ambient conditions (time of day, approximate occupancy) — do
    this alongside the capture, per the field guide; no privacy concern
    here since it's just a human headcount estimate, not device data

Each run gets its own timestamped session directory under
config.yaml's session.output_dir (e.g. <output_dir>/20260918_121724/),
holding that session's log, btsnoop captures, manifest.csv, and
results.csv together as a self-contained unit.

Usage:
    python3 gym_session.py run                # run the passive gym observation
    python3 gym_session.py parse              # re-parse the most recent session
    python3 gym_session.py parse --session 20260918_121724  # re-parse a specific one
    python3 gym_session.py run --resume       # resume the most recent interrupted session
    python3 gym_session.py run --resume 20260918_121724  # resume a specific one

--resume re-uses the session's own directory and skips any (device,
replicate) combo already recorded as a real PASSED (BLE link included, not
just the driver process) in its results.csv.

Requires: sudo access for btmon, ros2 environment sourced.
"""

import argparse
import sys
import time

from session_lib import (
    stop_all_drivers,
    DeviceCheckError,
    InfrastructureCheckError,
    RunAbortedError,
    check_bt_controller_ready,
    check_devices_ready,
    do_parse,
    do_run,
    find_latest_session_ts,
    get_output_dir,
    get_session_dir,
    load_completed_keys,
    load_config,
    log,
    prepare_rerun_failed,
    run_key,
    setup_logging,
    start_ble_density_poller,
    start_channel_occupancy_poller,
    start_pairing_agent,
    stop_ble_density_poller,
    stop_channel_occupancy_poller,
    stop_pairing_agent,
)


def run_passive_gym(cfg, session_dir, session_id, completed=frozenset()):
    """Passive real-gym observation. No AP, no iperf3, no generic peripherals —
    just each listed device streaming normally while btmon captures whatever
    ambient conditions the gym actually has. Purely observational."""
    r = cfg.get("row6_passive_gym")
    if not r or not r.get("enabled"):
        log.info("Passive gym observation: disabled in config, skipping")
        return
    log.info("=== Passive real-gym observation ===")
    log.info(
        "No interference is generated for this block — this is observational only."
    )
    if r.get("notes"):
        log.info(f"Reminder: {r['notes']}")

    # Per-channel (1-13) WiFi occupancy for the WHOLE session, not per
    # device run -- background WiFi doesn't belong to any one device, and
    # this answers "how much AFH headroom does a real gym leave" (Table
    # tab:gym-observation) rather than just the one AP's own load
    # (poll_qbss_channel_utilization, used in the chamber rows). Gracefully
    # skipped (with a warning, not an error) if channel_occupancy_iface
    # isn't configured or fails the same combo-chip safety check the
    # chamber's qbss_iface uses.
    occupancy_poller = start_channel_occupancy_poller(
        r.get("channel_occupancy_iface"),
        session_dir / "channel_occupancy.csv",
        poll_interval_s=r.get("channel_occupancy_poll_interval_s", 60),
    )
    # Nearby BLE device density -- also whole-session, not per-device-run.
    # Shares hci0 with the sensor connection (unlike the WiFi poller above),
    # so short/infrequent by default to limit perturbation -- see the
    # PRIVACY note at the top of this file and in session_lib/ble_radio.py's
    # count_nearby_ble_devices docstring before changing what gets logged.
    # Disabled unless ble_density_scan is explicitly enabled in config.yaml
    # (opt-in, not opt-out -- this is the one poller here that touches the
    # sensor's own radio, so it shouldn't run by accident).
    ble_density_cfg = r.get("ble_density_scan") or {}
    ble_density_poller = start_ble_density_poller(
        cfg["btmon_controller"] if ble_density_cfg.get("enabled") else None,
        session_dir / "ble_density.csv",
        scan_duration_s=ble_density_cfg.get("scan_duration_s", 5),
        poll_interval_s=ble_density_cfg.get("poll_interval_s", 90),
    )
    try:
        for device_name in r["device_names"]:
            device = next(d for d in cfg["devices"] if d["name"] == device_name)
            for rep in range(1, r["replicates"] + 1):
                # row_label must match what do_run() below actually records
                # ("row6") -- was "row4" here, a stale mismatch that meant
                # --resume could never recognize anything as already done
                # (load_completed_keys builds its keys from manifest.csv's
                # real "row6" label), found 2026-09-25 while wiring
                # --rerun-failed through this same check.
                if run_key("row6", device["name"], "passive_ambient", rep) in completed:
                    log.info(
                        f"  -> skipping row6 | {device['name']} | passive_ambient | "
                        f"rep{rep} (already recorded, resuming)"
                    )
                    continue
                do_run(
                    cfg,
                    session_dir,
                    "row6",
                    device["name"],
                    device["launch_cmd"],
                    "passive_ambient",
                    rep,
                    r["duration_s"],
                    session_id,
                    mac_address=device.get("mac_address"),
                    total_replicates=r["replicates"],
                )
    finally:
        stop_channel_occupancy_poller(occupancy_poller)
        stop_ble_density_poller(ble_density_poller)
    log.info("Passive gym observation complete")


# =============================================================================
# Entry point
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the passive gym observation")
    run_p.add_argument("--config", default="config.yaml")
    run_p.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION",
        help="Resume an interrupted run instead of starting a new session: "
        "re-uses that session's directory and skips any (device, replicate) "
        "combo already recorded in its manifest.csv. Give a session "
        "timestamp to resume a specific one, or omit the value to resume "
        "the most recent.",
    )
    run_p.add_argument(
        "--rerun-failed",
        action="store_true",
        help="Only with --resume: also re-attempt any (device, replicate) "
        "already recorded as FAILED, not just what's missing entirely. "
        "The original failed capture is renamed out of the way first, "
        "never overwritten.",
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
    log.info(
        f"config: {args.config}, command: {args.command}, session: {session_ts}"
        + (f" (resuming, {len(completed)} run(s) already recorded)" if resuming else "")
    )

    pairing_agent = None
    try:
        if args.command == "run":
            pairing_agent = start_pairing_agent()
            check_bt_controller_ready(cfg["btmon_controller"])
            check_devices_ready(cfg)
            run_passive_gym(cfg, session_dir, session_ts, completed)
            log.info("=== Run complete, parsing captures ===")
            do_parse(cfg, session_dir, session_ts)
        elif args.command == "parse":
            do_parse(cfg, session_dir, session_ts)
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
        stop_all_drivers()  # drivers don't receive the terminal's Ctrl+C
        stop_pairing_agent(pairing_agent)


if __name__ == "__main__":
    main()
