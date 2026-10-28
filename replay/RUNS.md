# Replay runs and versions (paper section "End to End through MEDWEAR")

## Frozen platform

**2026-10-23: new platform version.** The `*_sessions` reruns use MedWear
`49be39f` (branch `feature/atomic-ingest-sessions`, on top of `a8a666b`, so
the nginx proxy change is now kept), deployed with
`MEDWEAR_COMMIT=49be39f62da5f0d212b29c894054d6b8ceb5ba29 deploy/deploy.sh`.
Its med-api logs `COMMIT OK` once both stores hold a batch; `analyze_e2e.py`
prefers that line over `InfluxDB OK` when `X-Stored-At` is missing.

Every run the paper reports so far uses:

- MedWear `73738cc` (branch `feature/ros2-bridge-batching`; bridge and med-api;
  production `/data/batch` limit 300/minute per user)
- healthcare_msgs `c9de02f`
- Infrastructure images shipped from the laptop (MinIO) or pulled by digest
  (`~/.cache/medwear_e2e/infra_pull.txt`)

`deploy/deploy.sh` exports exactly these commits (`MEDWEAR_COMMIT`,
`HCMSGS_COMMIT`), so later local commits never reach the node. Not adopted:
MedWear `a8a666b` "Nginx proxy" (2026-10-19), which changes nginx, i.e. the
"HTTPS to the API" step.

**Any change to the platform means rerunning everything below** (chamber
~18 h + gym ~6 h on the exclusive node, plus any concurrency runs).

## Runs

| Tag(s) | Job | Node | Recordings | Status | Used for |
|---|---|---|---|---|---|
| `full_W1`, `sweep_W0.1/0.5/1/5` | 13619 (2026-10-16/17) | exclusive (AMD EPYC 9555P) | chamber: 120 at W=1 s; 17 Table III Polar runs per W | done | paper Sec. V-D, Table III (`results_slurm_v14/`; k=0 row pooled: `results_slurm_stages_k0pooled/`) |
| `gym_W1_shared` | 13653 (2026-10-19) | shared, 16 CPUs, 8 GB | gym: 72 at W=1 s, patient IDs `...__gym_W1_shared` | done | commit accounting, live path (`results_slurm_gym_shared/`); platform timings only with a caveat (PostgreSQL 45 vs 20 ms) |
| `gym_W1` | 13647 -> 13648 | exclusive | gym: 72 at W=1 s | disturbed 18:52-21:11 (26/72) by aardvark-dns in user.slice | not used; logs `logs_slurm/gym_W1` |
| `sweep_W0.1` (+5 confined) | 13746 (2026-10-21) | exclusive, in-stack DNS | chamber: 5 `row3_Polar_H10_forced_channel_duty75pct` at W=0.1 s (`run_confined_W01.sh`) | done, 0 failed, 0 duplicates | k=0 row of Table IV pooled with the confined runs (10 runs, as Table II): deadline misses only; platform timings stay on 13619's 17 runs (`results_slurm_stages_k0pooled/`) |
| `gym_W1` (rerun) | 13745 (2026-10-21) | exclusive, in-stack DNS | gym: 72 at W=1 s | INVALID: same patient IDs as 13648, MedWear answered 93-100 % of batches `duplicate` | not used; logs `logs_slurm/gym_W1_dup_13745` |
| `gym_W1_dns` | 13747 (2026-10-21 08:22) | exclusive, in-stack DNS | gym: 72 at W=1 s, patient IDs `...__gym_W1_dns` | done 2026-10-21 14:34; 0 duplicates, 23 ECG plausibility rejections | gym platform timings (`results_slurm_gym_W1_dns/`) |
| `full_W1_dns`, `sweep_W{0.1,0.5,1,5}_dns` | 13748 setup -> 13749 (`./run_e2e.sh dns`), after 13747 | exclusive, in-stack DNS | chamber: 120 at W=1 s (patient IDs `...__full_W1_dns`); 22 Polar runs per W (Table III conditions + 5 confined; `...__W<W>_dns`) | done 2026-10-22 10:11; 0 duplicates, 1 rejection (the known 146-sample baseline wander) | paper source until 2026-10-25 (`results_slurm_dns/`); superseded by `*_sessions` |
| `smoke_sessions` | 13810 setup -> 13811 (`./run_smoke_sessions.sh`) | exclusive, in-stack DNS | chamber: 1 run (`row2_Polar_H10_duty100pct_rep1`) | done 2026-10-23: 1018/1018 session batches ingested, 0 failed, 0 duplicates, 1018 COMMIT OK (same counts as `full_W1_dns`) | check of MedWear `49be39f` before the reruns below |
| `full_W1_sessions`, `sweep_W{0.1,0.5,1,5}_sessions`, `gym_W1_sessions` | 13832 (`./run_e2e.sh sessions`) -> 13833 (`./run_gym.sh gym_W1_sessions`), submitted 2026-10-23 | exclusive, in-stack DNS | as `*_dns` (patient IDs `...__full_W1_sessions`, `...__W<W>_sessions`, `...__gym_W1_sessions`) | done 2026-10-25 (chamber 13832: 19.6 h, gym 13833: 6.2 h); same accounting as `*_dns` (chamber 1 rejection, gym 23, 0 duplicates, 0 resumes, 0 server errors); Table IV rows identical; PostgreSQL step +0.6 ms (W=0.1 s) / +1.0 ms (gym) mean (now insert + completion update) | **paper source** (MedWear `49be39f`): `results/chamber`, `results/gym`; two-phase ingest (acknowledged only when PostgreSQL and InfluxDB both hold a batch; a retry resumes a half-written batch instead of answering `duplicate`), stream metadata sent once per session |

In-stack DNS (from 13745 on): dnsmasq container `medwear-dns` on a network
without Podman DNS (`make_compose.py`), so no aardvark-dns runs in
user.slice; tested in 13664. MedWear unchanged. Decision (a): only the gym is
rerun with it (noted in the Methods). 2026-10-21: we chose (b), rerun the
chamber too, so chamber and gym come from one deployment.

A rerun under an existing tag must use new patient IDs (another tag/suffix):
MedWear keeps its volumes between jobs and answers already stored batches as
duplicates without committing them.

Paper updated from these runs on 2026-10-22 (backup `main_before_dns_rerun.tex`).
13619 (`results_slurm_v14/`) is superseded; its numbers agree within rounding
except the platform tail (PostgreSQL p99 55 -> 11 ms, InfluxDB p99 93 -> 6 ms).

Earlier runs, not reported: laptop runs (`logs/`, `results/`, 2026-10-08 to 14)
and the laptop container run (`logs_ctr/`, `results_ctr/`, 2026-10-15).
Logs of the server runs: `logs_slurm/` (copied from `~/medwear_e2e/replay/logs`).
Clock check of the chamber recordings and their replay: `logs_slurm/CLOCK_CHECK.md`.

## What a change requires

| Change | Rerun |
|---|---|
| `analyze_e2e.py` (analysis) | analysis only, on the laptop (minutes; see "Re-running the analysis") |
| `replay_chamber.py` progress-log line (patient suffix shown) | none (cosmetic; not deployed) |
| replay tooling that changes what is uploaded or measured | the affected replays |
| MedWear, healthcare_msgs, infrastructure images, compose settings | everything |
| Polar driver (bags were recorded with the original driver) | see plan A / B |

Analysis changes since the runs, all re-applied to both server runs:
session drift/background/settle as in `analyze_session.py`; misses on sample
age; medians of runs; 100 ms deadline; `/polar_hr`; per-step tables;
`table3.tex` on the paper's definitions.

## Driver fix: one ECG message per BLE frame

The Polar driver publishes each ECG sample as its own message (73 per frame),
so frames arriving in bursts queue behind one another: the live path's
99th percentile reaches 340 ms at k=0 in the chamber.

**Plan A (now):** fix the driver (one `ECG` message per frame,
`sample_size` = samples in the frame) and measure old vs fixed with a harness
that feeds each recorded run's ECG notifications (raw bytes from the btsnoop
capture) to the driver's conversion code at their original HCI times, on the
gateway laptop, with a separate ROS 2 subscriber recording receipt. Reports
the fixed live path; the platform replays stay as they are, and the paper
states that the durable numbers include the original driver's queueing
(it matters only for the k=0 tail). No replay rerun.

**Plan B (if time):** the harness also writes new bags with the fixed driver's
messages and timing; rerun chamber (`full_W1` + sweep) and gym (`gym_W1`) on
the exclusive node with the frozen platform (~24 h), re-analyse, update
Sec. V-D. Needs `analyze_e2e.py` to match multi-sample messages to frames
(it currently assumes one sample per message).

## Re-running the analysis

The paper's numbers come from the `*_sessions` runs; their analysis is in
`results/chamber` and `results/gym` (Table IV = `results/chamber/table3_hostcens.tex`).
With ROS 2 and healthcare_msgs sourced, from this directory:

```bash
C=../captures
python3 analyze_e2e.py --logs logs_slurm --full-tag full_W1_sessions \
    --sweep-tags sweep_W0.1_sessions sweep_W0.5_sessions sweep_W1_sessions sweep_W5_sessions \
    --table-tag sweep_W0.1_sessions --out results/chamber \
    $C/20261007_105222_chamber_1 $C/20261008_010249_chamber_2
python3 analyze_e2e.py --logs logs_slurm --full-tag gym_W1_sessions --out results/gym \
    $C/gym_SPZ_20261013_133642 $C/gym_SPZ_20261013_160606 $C/gym_SPZ_20261016_103145
```

Earlier result folders named in this file (`results_slurm_*`) are not in the
repository.

## Plan A result (2026-10-19): the live-path tail is not the driver

Driver versions, committed 2026-10-19 on branch `wireless-sessions-2026-10`
of `$ROS_WS/src/ros4healthcare`, pushed 2026-10-19 to github.com/ros-healthcare/ros4healthcare; each verified
byte-identical to the patch archived with the sessions:

- `1170757` drivers as run in the chamber sessions (a73582c + chamber patch,
  sha256 2e47e46d42f2...)
- `fe2ed7d` drivers as run in the gym sessions (+ polar_ros2.py formatting and
  one disabled debug print; sha256 60defbb5828a...)
- `d1bb88b` `polar_ros2 --ecg-per-frame` (opt-in; default unchanged)

The checked-out working tree (`update/corsano_driver`, still with uncommitted
changes) equals `d1bb88b`. PolarH10.py is the same in the chamber and gym
versions, so the harness's original variant is the driver of both.
Harness: `driver_ab.py` (real PolarH10 class fed the captured PMD frames at
their HCI times, `ros2 bag record -a` as subscriber, isolated ROS domain).

- k=0 chamber run (row5_..._k0_duty75pct_rep1), full 300 s, on the gateway
  laptop: original driver code p50/p99/max 3.1/6.0/7.5 ms; per-frame
  1.5/4.0/4.8 ms. The original recording shows 3.6/364.7 ms for this run.
- The tail is link-layer reassembly, not the host: an ECG notification
  (232-byte ATT PDU) reaches the host as several HCI ACL fragments, and
  under interference the last can arrive hundreds of ms after the first
  (k=0 run: 159/480 notifications fragmented, last fragment up to 449 ms
  late; the 33 frames with live latency > 100 ms are those). t_h is taken
  at the FIRST fragment (analyze_session.parse_capture), so this radio time
  was counted on the live path, and host sample ages are slightly optimistic.
- Table III runs, t_h from the LAST fragment: live p50/p99 2.9-3.0 / 6.3-7.5 ms
  in every condition (was 3.1-3.6 / 46-340 ms); host M on age changes by
  +0.0 to +0.5 (100 ms), +0.0 to +1.7 (250 ms; k=2 73.6 -> 75.3), +0.0 to +0.4
  (1 s) percentage points. Durable sample age is unchanged (t_c - t_h shrinks
  by what the host age gains).
- Within a frame, per-sample publishing spreads the samples by ~3 ms
  (late and on-time frames alike); per-frame publishing removes that.

Consequence: fixing t_h (last fragment) is an analysis change in
analyze_session.py (all host metrics) and analyze_e2e.py (live path), no
replay rerun. Plan B (re-recording bags with the fixed driver) would change
only the ~3 ms within-frame spread.
