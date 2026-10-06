# Wireless coexistence experiments: Polar H10 and Corsano 287-2B

Automated BLE-under-WiFi-interference experiments for two wearable sensors,
run in an anechoic chamber (`chamber_session.py`) and, separately, in a real
rehab gym (`gym_session.py`). Both scripts share one `config.yaml` and the
`session_lib/` package.

*Updated 2026-10-06. Replaces `wireless_tests_guide.docx` (last updated
2026-09-25).*

## Contents

1. [Overview](#1-overview)
2. [Chamber layout](#2-chamber-layout)
3. [Before the chamber day](#3-before-the-chamber-day)
4. [Devices under test](#4-devices-under-test)
5. [Rows (chamber_session.py)](#5-rows-chamber_sessionpy)
6. [Running the pipeline](#6-running-the-pipeline)
7. [Output files](#7-output-files)
8. [results.csv fields](#8-resultscsv-fields)
9. [Pass / fail rules](#9-pass--fail-rules)
10. [Known limitations and caveats](#10-known-limitations-and-caveats)
11. [Gym visit (gym_session.py)](#11-gym-visit-gym_sessionpy)
12. [Other tools](#12-other-tools)

---

## 1. Overview

| Script | Occasion | What it does |
|---|---|---|
| `chamber_session.py` | Controlled anechoic-chamber day (the main experiment) | Rows 1–5 by default, plus the opt-in row 8: connection-interval calibration, WiFi offered-load sweep, forced-channel AFH test, endurance runs, headroom sweep. |
| `gym_session.py` | A separate, later visit to a real rehab gym | Passive observation only: no AP, no generated traffic. One sensor at a time, listening to whatever the gym's RF environment is that day. |

> **"Row 6" naming:** the gym script calls its block "Row 6". This is unrelated
> to chamber row numbers. The chamber script's old row 6 (BLE-BLE contention)
> and row 7 (QBSS scan interference) were **removed on 2026-10-06**, but their
> data in older sessions still parses. The new headroom-at-100% row is
> numbered **8**, so its label never collides with that older data.

**One device at a time.** Each device runs every enabled row before the next
device starts; devices are not interleaved row by row. This matches real use
(one sensor active per patient). It also avoids a confirmed bug: BlueZ
auto-reconnects a bonded device in the background after its driver is
killed, and that stray connection competes for the shared radio. Every run
therefore explicitly disconnects its device from BlueZ afterwards.

**Captured automatically for every run:**

- a btmon HCI capture (`.btsnoop`, plus `.pcap` from the nRF sniffer when present)
- an AFH channel-map and connection-RSSI poll every 15 s (`_afh.csv`)
- a rosbag of all ROS 2 topics (`_rosbag/`), one bag per run
- the driver's own log (`_driver.log`)

**Captured once per session:**

- a WiFi occupancy scan of all 13 channels (`channel_occupancy.csv`), from the
  separate USB WiFi adapter
- controller and radio state (`controller_info.txt`, `rfkill_state.txt`)
- the measured max WiFi throughput (`max_throughput.json`)

Every run is logged to `manifest.csv`. When the session finishes or stops,
the captures are parsed into `results.csv`. You can also re-parse later
without re-running.

## 2. Chamber layout

```
 Platform A ("patient + gateway")                     Platform B ("infrastructure")
 ┌─────────────────────────────────┐                  ┌──────────────────────────────┐
 │ Sensors (Polar H10, Corsano)    │                  │ AP (TP-Link, WiFi ch 6)      │
 │      │ BLE, 1–2 m,              │                  │      ▲                       │
 │      │ line of sight            │                  │      │ UDP load, ~1 m        │
 │ Laptop = BLE central (hci0)     │ ◄─── ~3–5 m ───► │ Raspberry Pi                 │
 │  └ USB: nRF52840 sniffer        │                  │ = WiFi load client (STA)     │
 └────────────────┬────────────────┘                  └──────────────────────────────┘
                  │ USB extension
                  └──────────► USB WiFi adapter (QBSS + occupancy scan),
                               placed toward Platform B, away from the BLE link
```

- **WiFi load:** UDP from the load client (STA) to the AP on channel 6, at a
  fraction of the link's measured max throughput (see [row 2](#5-rows-chamber_sessionpy)).
- **Record the actual distances and transmit powers on the day.** WiFi load
  as a fraction of max is not comparable across setups. The geometry is what
  lets others estimate the intended-to-unintended (I/U) signal ratio, as in
  ANSI C63.27 work (Al Kalaa et al. 2020).
- **The laptop is the BLE central.** The drivers, btmon, AFH polling and
  channel forcing all run on its built-in Bluetooth (`btmon_controller: hci0`,
  an Intel AX211). It must sit with the sensors; extension cables can move only
  the USB devices. Results describe this controller's AFH: in BLE, the central
  decides the channel map.
- **The Raspberry Pi is only the WiFi load client,** reached over SSH.
- **The USB WiFi adapter actively scans** (probe requests on every channel,
  including 6). Keep it away from the BLE link; it only needs to hear the
  AP's beacons.
- **Distances to record:** sensor–laptop, Pi–AP, Pi–laptop, Pi–sensors, and
  WiFi adapter–laptop.

## 3. Before the chamber day

1. **nRF sniffer:** flash the sniffer firmware on the nRF52840 dongle and
   check `ble_sniffer.serial_port` (default `/dev/ttyACM0`). It covers the
   advertising channels 37/38/39 only, so treat it as a spot-check.
2. **Drivers:** dry-run each device's driver (`devices[].launch_cmd`) and
   confirm data streams. Then dry-run btmon alongside it and confirm
   `LE Connection Complete` events appear.
3. **AP (TP-Link):** fixed channel 6, 20 MHz width, band steering off, QoS off.
4. **WiFi load client (Raspberry Pi):** enable SSH with key-based (passwordless)
   access from the laptop, and force 2.4 GHz.
   - Check `iw dev wlan0 link`: `tx bitrate 72.2 MBit/s MCS 7 short GI`
     means a theoretical UDP ceiling of about 66 Mbit/s; `65.0 MBit/s MCS 7`
     means about 59 Mbit/s (single stream, 20 MHz, with frame aggregation).
5. **Max-throughput baseline:**
   - `row2_duty_cycle.iperf.expected_max_mbps` (60.6) was measured in the
     **lab** in test mode, not in the chamber.
   - On the first chamber day, measure the chamber value (one `--test-mode`
     start only warns), then update `expected_max_mbps` and its comment.
   - Real runs (without `--test-mode`) stop if the measured max is outside
     ±`max_mbps_tolerance_pct` (10%).
6. **USB WiFi adapter** (`qbss_iface`, `ambient_scan.iface`): it **must** be a
   radio physically separate from the Bluetooth controller.
   - The laptop's built-in WiFi shares one antenna with its Bluetooth chip, and
     the code refuses to scan from it.
   - One-time setup: `sudo ip link set <iface> up` and
     `sudo setcap cap_net_admin+eip $(which iw)`.
7. **Internal WiFi:** it is switched off (rfkill) automatically for real runs
   and restored afterwards. `--test-mode` leaves it on, so in lab dry runs the
   laptop's own WiFi traffic shares the Bluetooth antenna.
8. **Corsano measurement plan:** check that the watch records accelerometer
   data (see [caveats](#10-known-limitations-and-caveats)). In the driver log,
   look for `Plan read back` and the `ACC: no new data` warning.
9. **Wear the sensors:**
   - The Polar baseline fails without skin contact (`no messages on /polar_hr`).
   - The Corsano baseline fails if the watch records nothing new during the run.
10. **Charge** all sensors, the laptop and the Pi.
11. **Config:** device MAC addresses, Pi IP (`iperf.pi_host`), and
    `bitrate_by_duty` only if you want to pin rates instead of measuring.
12. **Channel masks:** check `channel_masks` (rows 5 and 8) against a fresh
    ambient scan. The extra "clean" channels should really be clear of
    nearby networks.

## 4. Devices under test

| Device | MAC address | Driver | Baseline data check (`expected_topics`) |
|---|---|---|---|
| Polar_H10 | A0:9E:1A:DF:7F:6C | `polar_ros2 polar_connector`: ECG 130 Hz and ACC 200 Hz over one PMD characteristic; HR/RR separately | `polar_acc`, `polar_ecg`, `polar_hr` |
| Corsano | F1:3B:40:28:CB:EC | `corsano_ros corsano_ros --publish-bioz --publish-accel --publish-hr --publish-stress --test-republish-latest` | `corsano/acceleration`, `corsano/bioz`, and the watch's own files must grow |

Both drivers live in `~/test_ws/src/ros4healthcare/ros2_hc_drv/`.
`results.csv` records their git commit as `driver_version`.

`--test-republish-latest` (Corsano, **test only**) republishes the latest
record on every poll, so the topics carry a steady load whether or not the
watch has new data. Its messages can be stale, which is why the Corsano
baseline check looks at the watch's own file growth instead.

## 5. Rows (chamber_session.py)

| Row | Name | What it does | Each run | Runs per device | Min per device | In `--row all`? |
|---|---|---|---|---|---|---|
| 1 | Connection interval | No interference. Records the negotiated interval, latency and supervision timeout, and watches for a renegotiation mid-session. A failed run is retried; repeated failure stops the session. | 90 s + 600 s | 2 | 11.5 | Yes |
| 2 | Offered-load sweep ("duty cycle") | WiFi UDP load at 0/25/50/75/100% of the measured max, with AFH free to use any channel. (Order shuffled; final `duty0pct_bracket` drift check.) | 300 s | 5 levels × 3 + 1 | 80 | Yes |
| 3 | Forced channel | Same loads as row 2, but AFH restricted to channels 12–19 ("nowhere to escape"). (Order shuffled; final `forced_channel_no_load_bracket` drift check, added 2026-10-06.) | 300 s | 5 × 3 + 1 | 80 | Yes |
| 4 | Endurance | One 0% and one 100% run, AFH free (**not** forced), sized for a disconnect-rate bound. | 1800 s | 2 | 60 | Yes |
| 5 | Headroom sweep | Channels 12–19 plus k = 0/2/4/8 clean channels, at 75% load, plus a no-load control at k = 2. (Order shuffled.) | 300 s | 5 × 3 | 75 | Yes |
| 8 | Headroom at 100% | Row 5 at 100% load for `k_values` (default k = 2 and 8). (Order shuffled.) | 300 s | 2 × 3 | 30 | No, use `--row 8` |

`--row all` takes about **306 min (≈ 5 h) per device**, about 10 h for both,
plus connect and setup time.

### Load levels (rows 2–5, 8)

**How 100% is measured.** At session start, `iperf3 -u -b 0 -l 1200` runs
from the Pi to the laptop through the AP. This is repeated 3 × 30 s
(`max_throughput_runs`, `max_throughput_duration_s`), and the median becomes
100%. The value is saved in `max_throughput.json` and **reused on every
`--resume`**, so a session never mixes load scales. (Sessions from before
2026-10-06 use the first value in their `session.log`.)

**What the levels mean.** Each level is UDP at that fraction of the max, sent
at a steady rate. That makes it an **offered load**, not on/off duty cycling,
despite the `duty` names in config and conditions.

- The AP's own airtime reports (QBSS) track the levels almost linearly:
  about 26 / 50 / 71 / 91% for 25 / 50 / 75 / 100%.
- 100% is "≈ saturation" (about 91% airtime), not full-buffer saturation.

**What to use in analysis.** Each run records its achieved
`induced_load_mbps` and `qbss_channel_utilization_pct`. Use those rather than
the nominal percentages.

### Forced channels (rows 3, 5, 8)

**Which channels.** BLE data channels 0–10 sit at 2404 + 2i MHz and 11–36 at
2406 + 2i MHz (advertising channel 38 takes 2426 MHz in between). So channels
12–19 cover 2430–2444 MHz. These are exactly the data channels that fit
**fully inside** WiFi channel 6's occupied band (about 2428.9–2445.1 MHz);
channels 11 and 20 only touch its edges.

**What the design is.** The link keeps hopping, but only over channels that
are all interfered. This is our own design:

- Pang et al. pin one channel at a time; Spörk et al. cite work that disables
  blacklisting entirely.
- It does **not** come from Al Kalaa et al. 2020: that paper never restricts
  the channel map.

**How it's applied.** The restriction is set with
`HCI_LE_Set_Host_Channel_Classification` right after connecting, and always
reset afterwards (per run, and again at the end of each row).

**Verified in the logs:**

- Row 3 never went below 8 channels.
- Row 5 at k = 8 always narrowed (to 12–15 channels), dropping the
  WiFi-overlapped channels 14–19. This is AFH avoiding interference when it
  has clean channels to go to.

### Why 75% for row 5

This is our own choice, based on our data, not on the literature. With AFH
free, the link at 75% is clean (Polar 100% delivered, 0/30 drops). At k = 0
it collapses (drops in 21/24 Polar and 11/12 Corsano runs), which gives the
widest range in which to measure recovery as clean channels are added.

- **Results so far:** Polar recovers fully from k = 2 (a cliff), while
  Corsano improves gradually up to k = 8.
- **Row 8** checks whether this holds at 100% load.

## 6. Running the pipeline

### chamber_session.py

| Command | Effect |
|---|---|
| `python3 chamber_session.py run` | Rows 1–5, both devices (default `--row all`). |
| `… run --row 3` | Only row 3. `--row` accepts 1–5, 8 or `all`. |
| `… run --row 8` | Opt-in headroom-at-100% row. |
| `… run --device Polar_H10` | Only one device. |
| `… run --row 3 --condition forced_channel_max_load` | Only that condition (rows 3, 5, 8; repeatable). |
| `… run --resume [SESSION]` | Resume the latest (or a given) session, skipping runs already recorded. |
| `… run --resume --rerun-failed` | Also re-attempt every run whose **latest** attempt FAILED. |
| `… run --test-mode` | Lab dry run: the throughput baseline check only warns, and the internal WiFi stays on. Everything else is enforced. |
| `python3 chamber_session.py parse [--session SESSION]` | Re-parse captures into `results.csv` without re-running. |

**Data is never overwritten** (since 2026-10-06). Before a run writes
anything, existing files at its names are moved aside:

- `…_prerun_failed_<start time>…`: archived by `--rerun-failed`
- `…_superseded_<time>…`: anything else, e.g. a run interrupted with Ctrl-C

A name collision adds `_2`, `_3`, …, and all of one run's files share the
same suffix.

**Careful with `--rerun-failed` on rows 2–5 and 8.** A FAILED result there is
often the intended data point (interference causing a real disconnect), not
an error to retry away.

**End-of-session summary.** It shows **one line per run with its final
status** (green PASSED, red FAILED), noting `(final of N attempts)` where a
run was retried. `results.csv` still keeps every attempt.

### gym_session.py

| Command | Effect |
|---|---|
| `python3 gym_session.py run` | Passive gym observation, both devices one after the other. |
| `… run --resume` / `--resume --rerun-failed` | Same as for the chamber script. |
| `python3 gym_session.py parse [--session …]` | Re-parse a gym session. |

**Requirements:** sudo access for btmon, the ROS 2 environment sourced, and
(for the chamber script's load rows) passwordless SSH to the Pi.

## 7. Output files

Each session gets its own directory under `session.output_dir` (default
`captures/`), named by timestamp, e.g. `captures/20261005_110316/`.

**Per run** (`<row>_<device>_<condition>_rep<N>[_attempt<M>]`):

| File | Contents |
|---|---|
| `.btsnoop` | Raw HCI capture (btmon) |
| `.pcap` | nRF sniffer capture, advertising channels only (when the dongle is present) |
| `_afh.csv` | AFH channel map and connection RSSI, every 15 s |
| `_rosbag/` | All ROS 2 topics for that run |
| `_driver.log` | The driver's stdout/stderr |

**Per session:**

| File | Contents |
|---|---|
| `session.log` | Full log |
| `manifest.csv` | One row per attempt; `--resume` reads it |
| `results.csv` | Parsed results, one row per attempt (see §8) |
| `max_throughput.json` | The session's 100% load: median, the individual runs, duration, time |
| `channel_occupancy.csv` | Per-channel WiFi occupancy (ambient scan) for the whole session |
| `controller_info.txt` | Bluetooth controller model and firmware, to cite in write-ups |
| `rfkill_state.txt` | Radio states at session start |

## 8. results.csv fields

Parsing works directly from the raw HCI bytes, including Polar ACC/ECG
completeness decoded from the PMD notification payloads. **There is one row
per attempt.** A run that failed and was rerun has several rows, and the last
one is the final attempt.

| Group | Fields |
|---|---|
| Identity | `session_id, row, device, condition, replicate, capture_file, status, error, generated_at, duration_s, capture_duration_s, retry_on_fail, driver_version, battery_pct` |
| Connection | `initial_interval_ms, initial_latency_ms, initial_timeout_ms, num_updates, update_intervals, mtu, connection_established, connected_duration_s, n_encryption_attempts/successes/failures` |
| Disconnects | `disconnect_count, unexpected_disconnect_count, n_reason_supervision_timeout, n_reason_establishment_failed, n_reason_local_host_term, n_reason_other` (from the HCI reason codes) |
| Timing | `rx_sample_count, jitter_p50_ms, jitter_p95_ms, jitter_p99_ms, jitter_p999_ms` (gap between received packets), `ecg_stream_start_delay_s` |
| Polar | `acc_* / ecg_*`: `pmd_frame_count, samples_received, samples_lost, completeness_pct` (share of the planned run), `samples_skipped, stream_completeness_pct` (loss within the received stream); `hr_rr_total_s, hr_rr_completeness_pct` |
| Corsano | `corsano_file_polls`, `corsano_acc/bioz/stress_file_growth_bytes`, `corsano_new_sensor_data`, plus the `corsano_acc_*`, `_activity_*`, `_stress_*`, `_bioz_*` completeness fields |
| Load | `induced_load_mbps, packet_loss_pct, iperf_jitter_ms, qbss_channel_utilization_pct` |
| RSSI | `connection_rssi_sample_count/avg/min/max_dbm` (from `_afh.csv`), `sniffer_adv_*` (advertising channels only) |

**Reading the completeness fields:**

- `*_stream_completeness_pct` measures loss **within** the received stream.
  A stream that stops cleanly can still read 100%; for the whole-run picture,
  check `*_completeness_pct` and `connected_duration_s`.
- **Corsano:** with the watch recording no new data, "delivery" is best
  measured by `corsano_file_polls` per second, a link-capacity measure.
  `corsano_bioz_completeness_pct` reads 100% regardless.

## 9. Pass / fail rules

`evaluate_run_result` (`session_lib/run_checks.py`) applies different rules
depending on the condition:

- **Baseline conditions are strict.** These are row 1, `duty0pct` and its
  bracket, `forced_channel_no_load` and its bracket, `duty0pct_long` and
  `headroom_k2_no_load`.
  - Any unexpected disconnect is a FAIL.
  - So is missing sensor data: no messages on an `expected_topics` topic,
    or, for the Corsano, watch files that didn't grow.
- **Expected-instability conditions** (all loaded conditions in rows 2–5 and
  8): disconnects are data. These fail only if the device never connected at
  all.
- **Channel-map leak check:** any run that doesn't force channels fails if
  its AFH map shows a forced-channel pattern left behind by an earlier run.

## 10. Known limitations and caveats

**About what is measured:**

- **You measure the central's AFH, not the sensors'.** In BLE the central
  decides the channel map, so results describe that controller's AFH (the
  laptop's Intel AX211), not Polar's or Corsano's. A phone or hospital
  gateway may behave differently.
- **Present the forced condition as a worst-case stress test.** It
  approximates dense WiFi: with APs on channels 1, 6 and 11 (C63.27 Tier 1),
  only a few gap channels stay clean, which row 5's small k values resemble.
- **One AP, one WiFi channel, one layout, steady-rate load.** Real traffic is
  bursty.
- **Few replicates.** Three per condition is thin for comparisons. Report
  intervals, and use row 4 for rate bounds.

**About the hardware and drivers:**

- **The built-in WiFi shares the Bluetooth antenna** (Intel AX211 combo).
  Scanning must use the separate USB adapter, which the code enforces. In
  `--test-mode` the internal WiFi stays on and connected, so all lab sessions
  to date had the laptop's own WiFi traffic (channel 1) sharing the
  Bluetooth antenna.
- **Corsano measurement plan.** The driver defaults to `HOSPITAL_MULTICOLOR`,
  under which raw ACC is not recorded, and the watch has reported
  `POWER_SAVING_MODE`. In the 2026-10-05 sessions ACC and stress never grew
  and only BioZ did. Raw ACC needs `--plan HOSPITAL_RAW` added to the
  `launch_cmd` (not yet done).
- **Polar ECG/RR in the lab sessions is mains hum, not physiology,** when the
  strap isn't on skin. Only link-level metrics are valid there.
- **The nRF sniffer covers advertising channels only;** it can't follow the
  data connection.
- **On reconnect, BlueZ uses its cached GATT table,** so btmon shows no UUIDs.
  Polar parsing falls back to fixed ATT handles (0x0032 PMD, 0x0010 HR).

**About older data:**

- **`_afh.csv` before 2026-10-02 can contain wrong maps and RSSI.** The
  channel-map reader sometimes logged an unrelated event as the reply. This
  explains all 149 "wider than forced" samples in sessions 09-24 to 10-01;
  the controller's real reply was always the forced set or "link down". For
  those sessions, take maps and RSSI from the `.btsnoop`.
- **Run order before 2026-10-06:** row 3 ran in fixed order, and each
  `--resume` re-measured the 100% load (so one session can mix load scales).
  Use `induced_load_mbps`.

## 11. Gym visit (gym_session.py)

A separate, later occasion. It is **not** part of the chamber day and not a
controlled experiment, just observation. It is a ground-truth check on
whether the chamber's load range and fading assumptions match a real gym.

**Before running it in a clinical space:**

- Get the facility's permission and let staff know you're there.
- Sensors are on a mannequin and no patient data is collected, so no ethics
  review should be needed, but confirm with the facility.
- **Privacy (GDPR / Swiss FADP):** both pollers log only aggregate counts,
  never individual MAC addresses. Don't add per-device logging.

**What it captures:**

- Per device, one after the other: the same btmon, AFH and rosbag capture as
  in the chamber.
- `channel_occupancy.csv`: WiFi occupancy per channel (1–13), every
  `channel_occupancy_poll_interval_s` (60 s).
- `ble_density.csv`: a count of nearby BLE devices only. It is **off by
  default**, because it scans on the same radio as the sensor under test.

Log ambient conditions (time of day, approximate occupancy) by hand
alongside the capture.

## 12. Other tools

| Tool | Purpose |
|---|---|
| `reconnect_regression.py` | Tests reconnects outside the chamber, with no WiFi load. It forces link drops (`--drop block`, the default, via `bluetoothctl block`) and measures how long until the link and the **data** come back. Avoid `--drop rfkill` on this laptop: the AX211 hung once and needed a reboot. |
| `analyze_session.py` | Analyzes one or more session folders (or .zip exports): a `report.md` with sanity checks and headline numbers, per-run and per-condition tables, and provenance in `meta.json`. |
| `gym_sim/` | BLE link simulation (link model, emulated devices, scenarios). `validate_held_out.py` fits the load model on some forced-channel conditions and checks its predictions on the rest. |
