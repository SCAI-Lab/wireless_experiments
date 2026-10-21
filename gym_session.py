#!/usr/bin/env python3
"""
Gym session automation -- sensor tests in a real rehab gym
===========================================================

NOT part of the chamber day, and it never imports or changes
chamber_session.py. The gym's own WiFi is the load: no AP, no Pi, no iperf3.

One command, three parts (pick with --only; default all three):
  survey   background_monitor.py survey: hop WiFi channels 1-13, pick the
           busiest by airtime as the forced channel (skipped if the session
           already has one)
  monitor  background_monitor.py run, as a SEPARATE child process on its own
           radios (USB T3U Plus in monitor mode, nRF sniffer): WiFi airtime
           on the forced channel, network counts, Bluetooth crowding. Never
           touches hci0; logs to background/monitor.log. Restarted if it
           dies; stopped when this script ends.
  sensors  the sensor tests (hci0, btmon, drivers). After the initial
           checks it runs unattended: no prompts between runs (people
           counts are logged by hand with `people N`).
The monitor's time series is joined onto each run by wall-clock time at
parse.
So the per-run nRF sniffer capture and per-run QBSS poll the chamber uses
are switched off here: the sniffer belongs to the monitor, and the T3U is
in monitor mode.

Design (one session = one gym visit; plan for >= 2 busy days + 1 quiet):
  - Conditions, every one once per block:
      free_channel  (row gym_free)    AFH free; the AFH map every 15 s is
                                      the measured headroom
      forced_k0     (row gym_forced)  BLE data channels fully inside the
                                      forced WiFi channel (12-19 for ch 6,
                                      exactly the chamber's mask)
      forced_k2/k8  (row gym_forced)  that band + k extra channels (below)
  - Randomised blocks: block b holds every condition once per device, in a
    seeded shuffled order; replicate number = block number. With
    interleave_devices, devices alternate order block by block
    (Polar, Corsano | Corsano, Polar | ...) so device isn't confounded with
    time of day. Devices still never stream at the same time.
  - 300 s per run; 4 conditions x 3 blocks x 2 devices = 24 runs (~2.5 h).
  - Mounting: same stand, height and distance as the chamber.

Extra channels for k > 0 (gym_session.extra_channels in config.yaml):
  survey  (default) -- the k BLE data channels outside the forced WiFi
          channel with the LOWEST airtime in the monitor's survey, spread
          out (not adjacent when avoidable). Same principle the chamber
          used (channels the ambient scan showed clear), applied to the
          gym's own spectrum. Masks are nested (k2 is part of k8).
  chamber -- the chamber's channel_masks verbatim. Only valid when the
          forced channel is 6. In a gym where ch 1/11 are busy too, the
          chamber's extras (0-4, 28-36) aren't clean, so k stops meaning
          "k clean channels".
Masks are frozen into gym_masks.json on the first run, so a resumed or
second-day session can't drift.

Usage:
    python3 gym_session.py run                        # everything, new captures/gym_<date>_<time>/
    python3 gym_session.py run --resume               # continue the latest gym_ session
    python3 gym_session.py run --resume --rerun-failed
    python3 gym_session.py run --only survey          # just pick the channel (new session)
    python3 gym_session.py run --resume --only sensors        # sensors, monitor run elsewhere
    python3 gym_session.py run --session <chamber TS> --only monitor --wifi-channel 6
                                                      # chamber airtime check, until Ctrl+C
    python3 gym_session.py people 14                  # people count from another terminal
    python3 gym_session.py masks                      # show the masks, no hardware
    python3 gym_session.py parse                      # results.csv, results_with_background.csv,
                                                      # and analysis/<session>/ (the table to analyse:
                                                      # analysis/<session>/tables/per_run.csv)
    python3 gym_session.py covariates --session <TS>  # join only, any session

`run` starts a NEW gym_ session unless --resume (latest gym_ session) or
--session NAME (gym_ prefix optional). The other commands default to the
latest gym_ session.

--test-mode: leaves the internal WiFi on and doesn't insist on airtime
data (desk dry run). Never use it for real gym data.

Requires: sudo for btmon (via capabilities), ros2 environment sourced.
"""

import argparse
import copy
import csv
import json
import random
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from session_lib import (
    DeviceCheckError,
    InfrastructureCheckError,
    RunAbortedError,
    StrayProcessError,
    _kernel_has_link,
    check_bt_controller_ready,
    check_devices_ready,
    check_devices_unbonded,
    check_ros_packages,
    disable_internal_wifi_for_session,
    disconnect_ble_device,
    do_parse,
    do_run,
    kill_stray_driver_processes,
    load_completed_keys,
    load_config,
    log,
    log_wifi_rfkill_state,
    prepare_rerun_failed,
    restore_internal_wifi,
    run_key,
    setup_logging,
    snapshot_environment,
    start_pairing_agent,
    stop_all_drivers,
    stop_pairing_agent,
)
from session_lib import run_checks
from privacy_scrub import scrub_session
from background_monitor import log_people, new_gym_session_dir, resolve_session_dir

MONITOR_SCRIPT = str(Path(__file__).resolve().parent / "background_monitor.py")
PARTS = ("survey", "monitor", "sensors")

ROW_FREE = "gym_free"
ROW_FORCED = "gym_forced"
FREE_CONDITION = "free_channel"
BACKGROUND_DIR = "background"
MASKS_FILE = "gym_masks.json"
PRE_RUN_CHECKS = "pre_run_checks.csv"
RESULTS_WITH_BACKGROUND = "results_with_background.csv"


def forced_condition(k):
    return f"forced_k{k}"


def register_gym_conditions(k_values):
    """Teach the shared verdict/contamination rules about the gym rows
    without editing session_lib: the gym rows aren't in its tables, and
    the sets are shared module objects, so adding to them here only affects
    this process. gym_forced runs set a classification on purpose (skip
    the leak check) and, like chamber rows 5/8, count as engaged-is-enough
    -- forced channels under real load may degrade, and that's data.
    gym_free keeps the normal connected-time rule and the leak check."""
    run_checks.FORCED_CHANNEL_ROWS.add(ROW_FORCED)
    for k in k_values:
        run_checks.EXPECTED_INSTABILITY_ROW_CONDITIONS.add((ROW_FORCED, forced_condition(k)))


# =============================================================================
# Channel masks
# =============================================================================

# Same geometry as session_lib/ble_radio.py's FORCED_CHANNEL_RANGE comment:
# BLE data channels 0-10 at 2404 + 2i MHz, 11-36 at 2406 + 2i MHz; a WiFi
# OFDM signal occupies ~16.25 MHz around its centre.
OFDM_HALF_WIDTH_MHZ = 16.25 / 2
WIFI_NOMINAL_HALF_WIDTH_MHZ = 10


def ble_data_channel_mhz(i):
    return 2404 + 2 * i if i <= 10 else 2406 + 2 * i


def wifi_center_mhz(ch):
    return 2484 if ch == 14 else 2407 + 5 * ch


def channels_inside_wifi(wifi_ch):
    """BLE data channels whose 2 MHz fit fully inside the WiFi channel's
    occupied band. Channel 6 -> 12-19 (the chamber's k=0 mask). Odd WiFi
    channels give 7 (their centre falls on a BLE channel's grid offset),
    even ones 8."""
    c = wifi_center_mhz(wifi_ch)
    return [i for i in range(37) if abs(ble_data_channel_mhz(i) - c) + 1 <= OFDM_HALF_WIDTH_MHZ]


def survey_extra_channels(wifi_ch, k, survey_airtime):
    """k BLE data channels outside the forced WiFi channel's nominal 20 MHz,
    lowest surveyed airtime first (a channel's score is the busiest WiFi
    channel overlapping it), ties broken by distance from the forced
    channel. Non-adjacent picks first; adjacency only if k can't be met
    otherwise. Same ordering for every k, so the masks are nested."""
    c = wifi_center_mhz(wifi_ch)
    candidates = [
        i for i in range(37)
        if abs(ble_data_channel_mhz(i) - c) - 1 >= WIFI_NOMINAL_HALF_WIDTH_MHZ
    ]

    def score(i):
        f = ble_data_channel_mhz(i)
        covering = [
            a for w, a in survey_airtime.items()
            if abs(f - wifi_center_mhz(w)) < WIFI_NOMINAL_HALF_WIDTH_MHZ + 1
        ]
        return round(max(covering), 1) if covering else 0.0

    def side(i):
        return ble_data_channel_mhz(i) > c

    # Greedy, one pick at a time; among equal scores prefer the side of the
    # forced channel with fewer picks so far (the chamber's extras are
    # split below/above too), then the farthest channel.
    picked = []
    for spacing in (2, 1):
        while len(picked) < k:
            ok = [i for i in candidates
                  if i not in picked and all(abs(i - j) >= spacing for j in picked)]
            if not ok:
                break
            n_side = {s: sum(side(j) == s for j in picked) for s in (False, True)}
            picked.append(min(ok, key=lambda i: (
                score(i), n_side[side(i)], -abs(ble_data_channel_mhz(i) - c), i)))
    return sorted(picked), {i: score(i) for i in picked}


def read_forced_channel(session_dir):
    path = Path(session_dir) / BACKGROUND_DIR / "forced_channel.json"
    return json.loads(path.read_text()) if path.exists() else None


def build_masks(cfg, gcfg, wifi_ch, survey_airtime):
    band = channels_inside_wifi(wifi_ch)
    rule = gcfg.get("extra_channels", "survey")
    masks, extra_scores = {}, {}
    for k in gcfg["k_values"]:
        if rule == "chamber":
            if wifi_ch != 6:
                raise ValueError(
                    "gym_session.extra_channels: chamber only works for WiFi channel 6 "
                    f"(the chamber's masks are built around it), not {wifi_ch}"
                )
            masks[k] = sorted(cfg["channel_masks"][k])
            continue
        extras, scores = survey_extra_channels(wifi_ch, k, survey_airtime)
        masks[k] = sorted(band + extras)
        extra_scores.update(scores)
    return {
        "wifi_channel": wifi_ch,
        "extra_channels_rule": rule,
        "band_channels": band,
        "survey_airtime_pct": survey_airtime,
        "extra_channel_scores_pct": extra_scores,
        "masks": {str(k): v for k, v in masks.items()},
    }


def load_or_freeze_masks(cfg, gcfg, session_dir, wifi_channel_arg):
    """The session's masks: frozen gym_masks.json if it exists (a resume
    must use the same masks), else built from the monitor's survey and
    frozen now."""
    path = Path(session_dir) / MASKS_FILE
    if path.exists():
        frozen = json.loads(path.read_text())
        if wifi_channel_arg and wifi_channel_arg != frozen["wifi_channel"]:
            raise ValueError(
                f"--wifi-channel {wifi_channel_arg} but this session's masks are frozen "
                f"for channel {frozen['wifi_channel']} ({path.name})"
            )
        return frozen
    fc = read_forced_channel(session_dir)
    wifi_ch = wifi_channel_arg or (fc or {}).get("wifi_channel")
    if not wifi_ch:
        raise ValueError(
            "no forced WiFi channel: run `background_monitor.py survey` for this "
            "session first, or pass --wifi-channel"
        )
    survey = {int(ch): a for ch, a in ((fc or {}).get("survey_mean_airtime_pct") or {}).items()}
    if not survey and gcfg.get("extra_channels", "survey") == "survey":
        log.warning(
            "  -> no survey airtime for this session: extra channels fall back to "
            "'farthest from the forced channel', not 'quietest measured'"
        )
    masks = build_masks(cfg, gcfg, int(wifi_ch), survey)
    masks["frozen_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(masks, indent=2) + "\n")
    return masks


# =============================================================================
# Schedule
# =============================================================================


def gym_schedule(session_id, device_names, k_values, blocks, interleave):
    """[(block, device, condition)] in run order. Each (device, block) is
    every condition once, shuffled with a seed from the session id, so a
    resume replays the same order."""
    conditions = [FREE_CONDITION] + [forced_condition(k) for k in k_values]

    def block_order(device, b):
        order = list(conditions)
        random.Random(f"{session_id}:{device}:gym:block{b}").shuffle(order)
        return [(b, device, c) for c in order]

    schedule = []
    if interleave:
        for b in range(1, blocks + 1):
            devices = device_names if b % 2 else device_names[::-1]
            for d in devices:
                schedule += block_order(d, b)
    else:
        for d in device_names:
            for b in range(1, blocks + 1):
                schedule += block_order(d, b)
    return schedule


def runtime_cfg(cfg, masks):
    """The config this process runs and parses with: the shared config with
    the gym's own needs layered on, in memory only."""
    rcfg = copy.deepcopy(cfg)
    # The sniffer belongs to background_monitor.py (BLE crowding counts).
    rcfg["ble_sniffer"] = {"enabled": False}
    # No per-run QBSS scan: the T3U is in monitor mode under the monitor,
    # and there's no AP of ours. False = skip, never auto-detect.
    rcfg["row2_duty_cycle"] = {"iperf": {"qbss_iface": False}}
    # Lets do_parse's leak check recognise the gym masks in AFH-free runs.
    rcfg["channel_masks"] = dict(rcfg.get("channel_masks") or {})
    for k, chans in ((masks or {}).get("masks") or {}).items():
        rcfg["channel_masks"][f"gym_k{k}"] = chans
    return rcfg


def check_monitor_running(session_dir, wifi_ch, max_age_s):
    """The monitor is a separate process, but its airtime is this study's
    main covariate: refuse to start without it."""
    path = Path(session_dir) / BACKGROUND_DIR / "airtime.csv"
    rows = _read_csv(path)
    if not rows:
        raise InfrastructureCheckError(
            f"no background airtime in {path} -- start `python3 background_monitor.py "
            f"run --session {Path(session_dir).name}` in another terminal first "
            f"(or --allow-no-monitor to run without the main covariate)"
        )
    last = rows[-1]
    age = time.time() - float(last["t_end"])
    if age > max_age_s:
        raise InfrastructureCheckError(
            f"background monitor's last airtime row is {age:.0f}s old -- is it still running?"
        )
    if int(last["wifi_channel"]) != int(wifi_ch):
        raise InfrastructureCheckError(
            f"background monitor watches WiFi channel {last['wifi_channel']} but this "
            f"session forces channel {wifi_ch}"
        )
    log.info(f"  -> background monitor alive (ch {wifi_ch}, last airtime "
             f"{last['airtime_pct']}%, {age:.0f}s ago)")


# =============================================================================
# Survey and background monitor (separate processes)
# =============================================================================


def ensure_sudo():
    """One password prompt up front, before any child needs sudo -- a prompt
    from a child would land in the middle of this script's output."""
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        raise InfrastructureCheckError("sudo is needed for the WiFi monitor mode")


def run_survey(config_path, session_dir, wifi_channel=None):
    fc = read_forced_channel(session_dir)
    if fc:
        log.info(f"  -> survey: already done for this session (WiFi channel "
                 f"{fc['wifi_channel']}, {fc.get('method')}), skipping")
        return
    log.info("=== Survey: picking the busiest WiFi channel (background_monitor.py) ===")
    cmd = [sys.executable, MONITOR_SCRIPT, "--config", config_path, "survey",
           "--session", Path(session_dir).name]
    if wifi_channel:
        cmd += ["--channel", str(wifi_channel)]
    if subprocess.run(cmd).returncode != 0:
        raise InfrastructureCheckError("survey failed -- see background/monitor.log")


class MonitorProcess:
    """background_monitor.py run, as a child process. Its own radios and
    files; this script only starts, checks and stops it."""

    def __init__(self, config_path, session_dir, wifi_channel):
        self.cmd = [sys.executable, MONITOR_SCRIPT, "--config", config_path, "run",
                    "--session", Path(session_dir).name, "--channel", str(wifi_channel),
                    "--quiet"]
        self.session_dir = Path(session_dir)
        self.wifi_channel = wifi_channel
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL)
        log.info(f"  -> background monitor started (pid {self.proc.pid}, WiFi channel "
                 f"{self.wifi_channel}) -> {BACKGROUND_DIR}/monitor.log")

    def ensure_alive(self):
        if self.proc.poll() is not None:
            log.error(f"  !! background monitor exited (code {self.proc.returncode}) "
                      f"-- restarting it; see {BACKGROUND_DIR}/monitor.log")
            self.start()

    def wait_ready(self, max_age_s, timeout_s=240):
        """Until the monitor writes fresh airtime for our channel (its first
        network sweep comes before the first airtime row)."""
        log.info("  -> waiting for the background monitor's first airtime reading...")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise InfrastructureCheckError(
                    f"background monitor exited (code {self.proc.returncode}) -- "
                    f"see {BACKGROUND_DIR}/monitor.log")
            try:
                check_monitor_running(self.session_dir, self.wifi_channel, max_age_s)
                return
            except InfrastructureCheckError:
                time.sleep(5)
        raise InfrastructureCheckError(
            f"no airtime from the background monitor after {timeout_s}s -- "
            f"see {BACKGROUND_DIR}/monitor.log")

    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        log.info("  -> stopping the background monitor (WiFi adapter back to managed mode)")
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def prompt_people(session_dir, label):
    """Ask for a people count and BLOCK until a number is entered -- the
    count is a covariate, so the session waits rather than skipping it.
    Called at the session start, at every block boundary and at the end
    (with 4 conditions x 2 devices per block: every ~45 min, 4-5 per
    session). Without a terminal there's no one to ask: warn and go on."""
    if not sys.stdin.isatty():
        log.warning(f"  -> no terminal: people count ({label}) not asked -- "
                    f"log it with `gym_session.py people N`")
        return
    while True:
        answer = input(f"\n>>> People count ({label}) -- how many people are in "
                       f"the gym right now? ").strip()
        if answer.isdigit():
            log_people(session_dir, int(answer), label)
            log.info(f"  -> people count ({label}): {answer}")
            return
        print("    Please enter a whole number (0 or more).")


def scrub_run_outputs(cfg, session_dir):
    """Strip bystanders' Bluetooth addresses and names from the session's
    btsnoops and driver logs (see privacy_scrub.py), right after each run.
    Whole-directory and idempotent, so it also covers captures set aside
    by --rerun-failed. A file that can't be cleaned without changing its
    metrics is left as it is and logged loudly."""
    for line in scrub_session(session_dir, cfg, quiet=True):
        if line.startswith(("KEPT RAW", "ERROR")):
            log.error(f"  !! privacy scrub: {line}")
        elif not line.startswith("clean"):
            log.info(f"  -> privacy scrub: {line}")


# =============================================================================
# Pre-run signal check
# =============================================================================

RSSI_REFERENCE_FILE = "pre_run_rssi_reference.json"
PRE_RUN_FIELDS = ["wall_time", "t", "device", "condition", "block", "rssi_dbm", "n_adv",
                  "reference_dbm", "delta_db", "within_tolerance", "attempts", "note"]


def measure_adv_rssi(mac, scan_s):
    """Median advertising RSSI of our sensor on hci0 over scan_s seconds
    (None if it isn't advertising). Runs between runs only, when btmon
    isn't recording; every other device is dropped in the callback, so no
    bystander address is kept or written."""
    import asyncio
    from bleak import BleakScanner
    vals = []

    def seen(dev, adv):
        if dev.address.upper() == mac.upper():
            vals.append(adv.rssi)

    async def scan():
        async with BleakScanner(detection_callback=seen):
            await asyncio.sleep(scan_s)

    try:
        asyncio.run(scan())
    except Exception as e:  # noqa: BLE001 -- a failed scan is reported, not fatal
        log.warning(f"  -> pre-run RSSI scan failed: {e}")
        return None, 0
    return (statistics.median(vals) if vals else None), len(vals)


def check_devices_advertising(devices, scan_s, attempts=2):
    """Initial check: every sensor with a MAC must be advertising before the
    session starts. check_devices_ready alone doesn't catch a missing sensor
    (a driver that keeps scanning still looks "up"), and the pre-run checks
    never block once the runs have started."""
    missing = []
    for device in devices:
        name, mac = device["name"], device.get("mac_address")
        if not mac:
            continue
        for _ in range(attempts):
            rssi, n = measure_adv_rssi(mac, scan_s)
            if rssi is not None:
                log.info(f"  -> {name} ({mac}) advertising: {rssi:.0f} dBm ({n} adverts)")
                break
        else:
            log.error(f"  -> {name} ({mac}): not advertising "
                      f"({attempts} x {scan_s}s scan)")
            missing.append(name)
    if missing:
        raise DeviceCheckError(
            f"not advertising: {', '.join(missing)} -- check the sensor is on, "
            f"worn{' (strap moist)' if any('olar' in m for m in missing) else ''} "
            f"and in range")


def pre_run_rssi_check(session_dir, gcfg, device, condition, block):
    """Before each run: the sensor's advertising RSSI on hci0 vs its
    reference (gym_session.rssi_reference_dbm, else the first reading of
    the session, taken automatically). Never asks: out of tolerance or not
    advertising is logged as a warning and in pre_run_checks.csv, and the
    run starts anyway -- no prompts during a recording. 2026-10-12: Corsano
    free rep1 swung -64 to -96 dBm and lost the link twice on a free channel --
    geometry or someone in between, not interference."""
    name, mac = device["name"], device.get("mac_address")
    if not mac:
        return
    tol = gcfg.get("rssi_tolerance_db", 8)
    scan_s = gcfg.get("rssi_scan_s", 6)
    ref_path = Path(session_dir) / RSSI_REFERENCE_FILE
    saved = json.loads(ref_path.read_text()) if ref_path.exists() else {}
    ref = (gcfg.get("rssi_reference_dbm") or {}).get(name)
    ref = ref if ref is not None else saved.get(name)
    attempts, note = 1, ""
    rssi, n = measure_adv_rssi(mac, scan_s)
    if ref is None and rssi is not None:
        # The session's first reading is the reference.
        log.info(f"  -> {name}: {rssi:.0f} dBm taken as this session's reference")
        ref = rssi
        saved[name] = rssi
        ref_path.write_text(json.dumps(saved, indent=2) + "\n")
    delta = None if rssi is None or ref is None else rssi - ref
    ok = delta is not None and abs(delta) <= tol
    state = ("not advertising" if rssi is None else
             f"{rssi:.0f} dBm ({delta:+.0f} dB vs reference {ref:.0f})")
    if ok:
        log.info(f"  -> pre-run signal check {name}: {state}, within +/-{tol} dB")
    else:
        log.warning(f"  -> pre-run signal check {name}: {state} -- outside +/-{tol} dB, "
                    f"running anyway")
    out = Path(session_dir) / PRE_RUN_CHECKS
    new = not out.exists()
    with open(out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PRE_RUN_FIELDS)
        if new:
            w.writeheader()
        t = time.time()
        w.writerow({"wall_time": time.strftime("%Y-%m-%d %H:%M:%S"), "t": round(t, 3),
                    "device": name, "condition": condition, "block": block,
                    "rssi_dbm": "" if rssi is None else round(rssi, 1), "n_adv": n,
                    "reference_dbm": "" if ref is None else round(ref, 1),
                    "delta_db": "" if delta is None else round(delta, 1),
                    "within_tolerance": ok, "attempts": attempts, "note": note})


def run_gym(rcfg, gcfg, session_dir, session_id, masks, device_names, completed,
            between_runs=None, people_prompt=None, pre_run=None):
    """between_runs() before every run; people_prompt(label) at the start,
    when a new block begins, and at the end. Block boundaries are only
    counted among runs actually executed, so a resume doesn't ask twice in
    a row for a block it skips through."""
    k_values = gcfg["k_values"]
    duration_s = gcfg.get("duration_s", 300)
    blocks = gcfg.get("blocks", 3)
    schedule = gym_schedule(session_id, device_names, k_values, blocks,
                            gcfg.get("interleave_devices", True))
    (Path(session_dir) / "gym_schedule.json").write_text(
        json.dumps([{"block": b, "device": d, "condition": c} for b, d, c in schedule],
                   indent=2) + "\n"
    )
    log.info(f"=== Gym: WiFi channel {masks['wifi_channel']}, k={k_values}, "
             f"{len(schedule)} runs x {duration_s}s ===")
    for k in k_values:
        log.info(f"  mask k={k}: {masks['masks'][str(k)]}")
    devices = {d["name"]: d for d in rcfg["devices"]}
    interleave = gcfg.get("interleave_devices", True)
    if people_prompt:
        people_prompt("session resumed" if completed else "session start")
    current = None
    for n, (block, device_name, condition) in enumerate(schedule, 1):
        row = ROW_FREE if condition == FREE_CONDITION else ROW_FORCED
        if run_key(row, device_name, condition, block) in completed:
            log.info(f"  -> [{n}/{len(schedule)}] skipping {row} | {device_name} | "
                     f"{condition} | block {block} (already recorded)")
            continue
        # Without interleaving, each device has its own blocks 1..N.
        block_key = block if interleave else (device_name, block)
        if people_prompt and current is not None and block_key != current:
            people_prompt(f"start of block {block}"
                          + ("" if interleave else f", {device_name}"))
        current = block_key
        if between_runs:
            between_runs()
        log.info(f"--- [{n}/{len(schedule)}] block {block} ---")
        if pre_run:
            pre_run(devices[device_name], condition, block)
        extra = None
        if row == ROW_FORCED:
            k = condition.removeprefix("forced_k")
            extra = {"forced_channels": masks["masks"][k]}
        device = devices[device_name]
        do_run(
            rcfg, session_dir, row, device_name, device["launch_cmd"], condition,
            block, duration_s, session_id,
            mac_address=device.get("mac_address"), total_replicates=blocks,
            extra=extra,
            # Real-world interference: a failed run is the data point.
            retry_on_fail=False,
        )
        scrub_run_outputs(rcfg, session_dir)
    if people_prompt:
        people_prompt("session end")
    log.info("Gym runs complete")


# =============================================================================
# Joining the background monitor's time series onto runs
# =============================================================================


def _read_csv(path):
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _in_window(rows, t0, t1):
    """Rows whose [t_start, t_end] midpoint falls inside the run."""
    out = []
    for r in rows:
        a, b = _f(r.get("t_start")), _f(r.get("t_end"))
        if a is not None and b is not None and t0 <= (a + b) / 2 < t1:
            out.append(r)
    return out


def _latest_before(rows, t, key="t_start"):
    best = None
    for r in rows:
        rt = _f(r.get(key))
        if rt is not None and rt <= t and (best is None or rt > _f(best[key])):
            best = r
    return best


def _r(v, nd=2):
    return "" if v is None else round(v, nd)


def _airtime_stalled(row, bg):
    """A window the receiver delivered nothing in. Current monitors leave
    its airtime blank; older files wrote 0.0 with 0 frames, which on a
    channel whose windows normally hold hundreds of frames can only be a
    stall (beacons alone are ~10/s per network)."""
    if _f(row.get("airtime_pct")) is None:
        return True
    if row.get("frames") == "0":
        if "_median_frames" not in bg:
            counts = sorted(_f(r.get("frames")) or 0 for r in bg["airtime"])
            bg["_median_frames"] = counts[len(counts) // 2] if counts else 0
        return bg["_median_frames"] >= 100
    return False


def run_background_covariates(session_dir, manifest_row, bg):
    t0 = time.mktime(time.strptime(manifest_row["start_time"], "%Y-%m-%d %H:%M:%S"))
    t1 = t0 + float(manifest_row["duration_s"])
    cov = {}

    # Only windows with a real reading count, for the values and for the
    # coverage: stalled windows have a blank airtime_pct (or, before
    # 2026-10-12's fix, 0.0 with 0 frames -- see _airtime_stalled).
    air = [r for r in _in_window(bg["airtime"], t0, t1) if not _airtime_stalled(r, bg)]
    vals = [v for v in (_f(r["airtime_pct"]) for r in air) if v is not None]
    covered = sum(_f(r["t_end"]) - _f(r["t_start"]) for r in air if _f(r["airtime_pct"]) is not None)
    cov.update(
        bg_wifi_channel=air[0]["wifi_channel"] if air else "",
        bg_airtime_mean_pct=_r(statistics.mean(vals)) if vals else "",
        bg_airtime_p95_pct=_r(statistics.quantiles(vals, n=20)[-1]) if len(vals) >= 2 else "",
        bg_airtime_max_pct=_r(max(vals)) if vals else "",
        bg_airtime_coverage_pct=_r(100 * covered / (t1 - t0), 1),
    )

    ble = _in_window(bg["ble"], t0, t1)
    adv = [v for v in (_f(r["advertisers"]) for r in ble) if v is not None]
    near = [v for v in (_f(r["advertisers_nearby"]) for r in ble) if v is not None]
    cov.update(
        bg_ble_advertisers_mean=_r(statistics.mean(adv), 1) if adv else "",
        bg_ble_advertisers_nearby_mean=_r(statistics.mean(near), 1) if near else "",
    )

    net = _latest_before(bg["networks"], t1)
    cov.update(
        bg_networks_2_4ghz=(net or {}).get("networks_2_4ghz", ""),
        bg_networks_5ghz=(net or {}).get("networks_5ghz", ""),
        bg_networks_age_min=_r((t1 - _f(net["t_start"])) / 60, 1) if net else "",
    )

    ppl = _latest_before(bg["people"], t1, key="t")
    cov.update(
        bg_people=(ppl or {}).get("people", ""),
        bg_people_age_min=_r((t1 - _f(ppl["t"])) / 60, 1) if ppl else "",
    )

    # The controller's own AFH map every 15 s: in free runs, how many
    # channels it kept = the measured headroom.
    afh_path = Path(session_dir) / (Path(manifest_row["capture_file"]).stem + "_afh.csv")
    counts = [v for v in (_f(r.get("enabled_channel_count")) for r in _read_csv(afh_path))
              if v is not None]
    cov.update(
        afh_enabled_channels_mean=_r(statistics.mean(counts), 1) if counts else "",
        afh_enabled_channels_min=_r(min(counts), 0) if counts else "",
    )
    return cov


# results.csv columns that are artefacts for the Corsano: ACC "completeness"
# is ~0.9 % whenever the watch barely records ACC, and BioZ "completeness"
# is bytes downloaded vs file growth (100 % by construction). Delivery is
# analyze_session.py's payload_Bps instead.
ARTEFACT_COLUMNS = ("corsano_acc_completeness_pct", "corsano_acc_samples_lost",
                    "corsano_acc_packet_count", "corsano_bioz_completeness_pct")


def extra_channel_airtime(extras, sweep_rows):
    """Mean over the extra BLE channels of the busiest overlapping 2.4 GHz
    WiFi channel's airtime in one sweep -- how clean the k extras really
    were at that time."""
    air = {int(r["wifi_channel"]): _f(r["airtime_pct"]) for r in sweep_rows
           if r.get("band") == "2.4GHz" and _f(r.get("airtime_pct")) is not None}
    vals = []
    for e in extras:
        f = ble_data_channel_mhz(e)
        cover = [a for w, a in air.items()
                 if abs(f - wifi_center_mhz(w)) < WIFI_NOMINAL_HALF_WIDTH_MHZ + 1]
        if cover:
            vals.append(max(cover))
    return round(statistics.mean(vals), 2) if vals else ""


def mask_columns(masks, condition, sweeps_before):
    """Report a forced run by its real channel count and how busy its extras
    were, not only by k: channel 11 gives a 7-channel band (channel 6 gives
    8), and when channels 1, 6 and 11 are all in use the k=8 extras land
    inside other WiFi channels."""
    k = condition.removeprefix("forced_k")
    mask = (masks.get("masks") or {}).get(k)
    if mask is None:
        return {}
    extras = sorted(set(mask) - set(masks.get("band_channels") or []))
    scores = {int(c): a for c, a in (masks.get("extra_channel_scores_pct") or {}).items()}
    survey = [scores[e] for e in extras if e in scores]
    return {
        "forced_mask": " ".join(map(str, mask)),
        "forced_channels_n": len(mask),
        "forced_extra_channels": " ".join(map(str, extras)),
        "forced_extra_airtime_survey_pct": round(statistics.mean(survey), 2) if survey else "",
        "forced_extra_airtime_sweep_pct": extra_channel_airtime(extras, sweeps_before),
    }


def run_analysis(session_dir, out_root="analysis"):
    """The chamber's analysis (analyze_session.py) on this session: added
    delay, deadline misses, link losses, Corsano delivery, AFH maps -- with
    the background covariates picked up from results_with_background.csv.
    Its tables/per_run.csv is the table to analyse."""
    try:
        import analyze_session
        analyze_session.analyze(str(session_dir), str(session_dir), out_root)
        log.info(f"  -> analysis: {out_root}/{Path(session_dir).name}/ (tables/per_run.csv, report.md)")
    except Exception as e:  # noqa: BLE001 -- the captures are safe; rerun `parse` later
        log.error(f"  !! analyze_session.py failed: {e} -- rerun `gym_session.py parse`")


def write_results_with_background(session_dir):
    """results.csv + one set of background/AFH columns per run, matched by
    capture_file to the manifest's start time. Works for any session with a
    background/ directory (a chamber airtime check too)."""
    session_dir = Path(session_dir)
    results = _read_csv(session_dir / "results.csv")
    manifest = {r["capture_file"]: r for r in _read_csv(session_dir / "manifest.csv")}
    if not results:
        log.warning(f"  -> no results.csv in {session_dir}, nothing to join")
        return
    b = session_dir / BACKGROUND_DIR
    bg = {
        "airtime": _read_csv(b / "airtime.csv"),
        "ble": _read_csv(b / "ble_advertisers.csv"),
        "networks": _read_csv(b / "networks.csv"),
        "people": _read_csv(b / "people.csv"),
    }
    sweeps = {}
    for r in _read_csv(b / "sweep.csv"):
        sweeps.setdefault(r["sweep_id"], []).append(r)
    pre_run = {(r["device"], r["condition"], r["block"]): r
               for r in _read_csv(session_dir / PRE_RUN_CHECKS)}
    if not bg["airtime"]:
        log.warning("  -> no background airtime for this session: bg_* columns will be blank")
    masks = json.loads((session_dir / MASKS_FILE).read_text()) if (session_dir / MASKS_FILE).exists() else {}
    out_rows = []
    for r in results:
        m = manifest.get(r["capture_file"])
        cov = run_background_covariates(session_dir, m, bg) if m else {}
        maskcols = {}
        if r.get("row") == ROW_FORCED and m:
            t_end = time.mktime(time.strptime(m["start_time"], "%Y-%m-%d %H:%M:%S")) + float(m["duration_s"])
            before = [rows for rows in sweeps.values() if _f(rows[0]["t_start"]) <= t_end]
            maskcols = mask_columns(masks, r["condition"], before[-1] if before else [])
        pr = pre_run.get((r["device"], r["condition"], r["replicate"]), {})
        base = {k: v for k, v in r.items() if k not in ARTEFACT_COLUMNS}
        out_rows.append({**base, "forced_wifi_channel": masks.get("wifi_channel", ""),
                         "forced_mask": "", "forced_channels_n": "", "forced_extra_channels": "",
                         "forced_extra_airtime_survey_pct": "", "forced_extra_airtime_sweep_pct": "",
                         **maskcols,
                         "pre_run_rssi_dbm": pr.get("rssi_dbm", ""),
                         "pre_run_rssi_delta_db": pr.get("delta_db", ""),
                         "pre_run_note": pr.get("note", ""), **cov})
    fields = list(out_rows[0].keys())
    for row in out_rows[1:]:
        fields += [k for k in row if k not in fields]
    path = session_dir / RESULTS_WITH_BACKGROUND
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)
    log.info(f"  -> {path.name}: {len(out_rows)} runs with background covariates")


# =============================================================================
# Entry point
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="survey + background monitor + sensor tests")
    run_p.add_argument("--config", default="config.yaml")
    where = run_p.add_mutually_exclusive_group()
    where.add_argument("--resume", action="store_true",
                       help="continue the most recent gym_ session (recorded runs are skipped)")
    where.add_argument("--session",
                       help="continue/attach to this session folder (gym_ prefix optional; "
                       "a chamber session works for --only monitor)")
    run_p.add_argument("--only", action="append", choices=PARTS,
                       help="run only this part (repeatable, e.g. --only monitor --only "
                       "sensors); default: all of survey, monitor, sensors")
    run_p.add_argument("--wifi-channel", type=int,
                       help="force this WiFi channel instead of the survey's busiest")
    run_p.add_argument("--device", action="append",
                       help="run only this device (repeatable); default: gym_session.device_names")
    run_p.add_argument("--rerun-failed", action="store_true",
                       help="also re-attempt runs recorded as FAILED (their captures "
                       "are moved aside first, never overwritten)")
    run_p.add_argument("--allow-no-monitor", action="store_true",
                       help="with --only sensors: start even without fresh airtime data")
    run_p.add_argument("--test-mode", action="store_true",
                       help="desk dry run: internal WiFi left on, no monitor required")

    for name, help_text in (
        ("parse", "re-parse captures into results.csv + results_with_background.csv"),
        ("covariates", "only (re)write results_with_background.csv"),
        ("masks", "show this session's channel masks (no hardware)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", default="config.yaml")
        p.add_argument("--session", help="default: the most recent gym_ session")
        if name == "masks":
            p.add_argument("--wifi-channel", type=int)
    p = sub.add_parser("people", help="log a people count (e.g. from another terminal)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("count", type=int)
    p.add_argument("--note", default="")
    p.add_argument("--session", help="default: the most recent gym_ session")

    args = parser.parse_args()
    cfg = load_config(args.config)
    gcfg = cfg["gym_session"]
    if args.command == "run" and not (args.resume or args.session):
        session_dir = new_gym_session_dir(cfg)
    else:
        session_dir = resolve_session_dir(cfg, args.session)
    session_ts = session_dir.name

    if args.command == "people":
        log_people(session_dir, args.count, args.note)
        print(f"{session_ts}: {args.count} people")
        return

    if args.command == "masks":
        masks_path = session_dir / MASKS_FILE
        if masks_path.exists():
            masks = json.loads(masks_path.read_text())
            print(f"(frozen in {MASKS_FILE})")
        else:
            fc = read_forced_channel(session_dir) or {}
            ch = args.wifi_channel or fc.get("wifi_channel")
            if not ch:
                sys.exit("no forced channel yet: run the survey or pass --wifi-channel")
            survey = {int(c): a for c, a in (fc.get("survey_mean_airtime_pct") or {}).items()}
            masks = build_masks(cfg, gcfg, int(ch), survey)
            print("(not frozen yet -- the first `run` freezes it)")
        print(json.dumps(masks, indent=2))
        return

    setup_logging(session_dir)
    register_gym_conditions(gcfg["k_values"])

    if args.command in ("parse", "covariates"):
        masks_path = session_dir / MASKS_FILE
        masks = json.loads(masks_path.read_text()) if masks_path.exists() else None
        if args.command == "parse":
            do_parse(runtime_cfg(cfg, masks), session_dir, session_ts)
        write_results_with_background(session_dir)
        if args.command == "parse":
            run_analysis(session_dir)
        return

    parts = set(args.only or PARTS)
    log.info(f"session: {session_ts}, parts: {[p for p in PARTS if p in parts]}"
             + (" [TEST MODE -- not valid gym data]" if args.test_mode else ""))
    pairing_agent = None
    rfkill_id = None
    monitor = None
    rcfg = cfg
    finished = False
    device_names = args.device or gcfg["device_names"]
    try:
        unknown = set(device_names) - {d["name"] for d in cfg["devices"]}
        if unknown:
            raise ValueError(f"unknown device(s) {sorted(unknown)} -- not in config.yaml devices")
        if parts & {"survey", "monitor"}:
            ensure_sudo()
        if "survey" in parts:
            run_survey(args.config, session_dir, args.wifi_channel)
        fc = read_forced_channel(session_dir) or {}
        frozen = session_dir / MASKS_FILE
        if frozen.exists():
            wifi_ch = json.loads(frozen.read_text())["wifi_channel"]
            if args.wifi_channel and args.wifi_channel != wifi_ch:
                raise ValueError(
                    f"--wifi-channel {args.wifi_channel}, but this session's masks are "
                    f"frozen for channel {wifi_ch} -- start a new session instead"
                )
        else:
            wifi_ch = args.wifi_channel or fc.get("wifi_channel")
        if not wifi_ch:
            raise ValueError(
                "no forced WiFi channel for this session: include the survey part, "
                "pass --wifi-channel, or --resume a surveyed session"
            )
        if "monitor" in parts:
            monitor = MonitorProcess(args.config, session_dir, wifi_ch)
            monitor.start()

        if "sensors" not in parts:
            if monitor:
                log.info("=== Background monitor only -- Ctrl+C to stop ===")
                try:
                    while True:
                        time.sleep(5)
                        monitor.ensure_alive()
                except KeyboardInterrupt:
                    log.info("  -> Ctrl+C: stopping")
            return

        masks = load_or_freeze_masks(cfg, gcfg, session_dir, wifi_ch)
        rcfg = runtime_cfg(cfg, masks)
        completed = frozenset(load_completed_keys(rcfg, session_dir))
        if args.rerun_failed:
            completed = prepare_rerun_failed(session_dir, completed)
        log.info(f"devices: {device_names}"
                 + (f" ({len(completed)} run(s) already recorded)" if completed else ""))
        # check_devices_ready probes every device in the config it's given.
        probe_cfg = dict(rcfg, devices=[d for d in rcfg["devices"] if d["name"] in device_names])
        pairing_agent = start_pairing_agent()
        kill_stray_driver_processes(probe_cfg)
        check_devices_unbonded(probe_cfg)
        check_ros_packages(probe_cfg)
        check_bt_controller_ready(rcfg["btmon_controller"])
        log_wifi_rfkill_state(session_dir)
        snapshot_environment(rcfg, session_dir, load_client=False)
        # The internal WiFi shares an antenna with hci0 -- off, as in the chamber.
        rfkill_id = disable_internal_wifi_for_session(args.test_mode)
        max_age = gcfg.get("monitor_max_age_s", 90)
        if monitor:
            monitor.wait_ready(max_age)
        elif args.allow_no_monitor or args.test_mode:
            log.warning("  -> not checking for background airtime data")
        else:
            # --only sensors: the monitor must be running elsewhere.
            check_monitor_running(session_dir, wifi_ch, max_age)
        check_devices_ready(probe_cfg)
        # After check_devices_ready, which drops stale links: a connected
        # sensor doesn't advertise.
        check_devices_advertising(probe_cfg["devices"], gcfg.get("rssi_scan_s", 6))

        def between_runs():
            if monitor:
                monitor.ensure_alive()

        def people_prompt(label):
            if gcfg.get("people_count_prompts", True):
                prompt_people(session_dir, label)

        def pre_run(device, condition, block):
            if gcfg.get("pre_run_rssi_check", True):
                pre_run_rssi_check(session_dir, gcfg, device, condition, block)

        run_gym(rcfg, gcfg, session_dir, session_ts, masks, device_names, completed,
                between_runs=between_runs, people_prompt=people_prompt, pre_run=pre_run)
        log.info("=== Run complete, parsing captures ===")
        scrub_run_outputs(rcfg, session_dir)
        do_parse(rcfg, session_dir, session_ts)
        write_results_with_background(session_dir)
        run_analysis(session_dir)
        finished = True
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
        do_parse(rcfg, session_dir, session_ts)
        write_results_with_background(session_dir)
        sys.exit(1)
    finally:
        stop_all_drivers()  # drivers don't receive the terminal's Ctrl+C
        for device in rcfg.get("devices") or []:
            if device.get("mac_address") and _kernel_has_link(device["mac_address"]):
                disconnect_ble_device(device["mac_address"])
        stop_pairing_agent(pairing_agent)
        restore_internal_wifi(rfkill_id)
        if monitor:
            monitor.stop()
        if "sensors" in parts:
            scrub_run_outputs(cfg, session_dir)  # also after an abort or Ctrl+C
        if "sensors" in parts and not finished:
            log.info(f"Continue with: python3 gym_session.py run --session {session_ts}")

if __name__ == "__main__":
    main()
