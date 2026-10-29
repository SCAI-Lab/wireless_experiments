# Replay of the chamber and gym recordings through MEDWEAR

Trace-driven replay for the MEDWEAR paper (Sec. "Trace-Driven Replay through
MEDWEAR", Table IV, supplementary section on the replay). The rosbags
recorded in the chamber and gym sessions (`../captures`) are
played back at their original timing (`ros2 bag play`, rate 1.0) into the
MEDWEAR ROS 2 bridge, which uploads them to the platform. The data takes the
same path as in real use: driver topics → DDS → bridge → `POST /data/batch`.

## Layout

| Path | Contents |
|---|---|
| `replay_chamber.py` | Replays sessions (chamber or gym) through the bridge into MEDWEAR and logs every batch. |
| `analyze_e2e.py` | Joins the logs with the original captures: commit accounting, live and durable path, per-step timing, W sweep, Table IV. |
| `run_e2e.sh`, `run_gym.sh`, `run_confined_W01.sh`, `run_smoke_sessions.sh` | The replay protocols (chamber: full replay + W sweep; gym; extra runs). |
| `sweep_ecg_config.yaml` | Bridge config of the W sweep (Polar H10 ECG only). |
| `deploy/` | Running everything in SLURM jobs on one exclusive node with rootless Podman: copy and submit (`deploy.sh`), compose file, container image, job scripts. |
| `driver_ab.py` | Harness that feeds recorded ECG notifications to the Polar driver's conversion code (original vs. `--ecg-per-frame`). |
| `export_logs.py` | Builds the Zenodo archive of the replay logs (see "Data"). |
| `results/chamber`, `results/gym` | Analysis of the runs the paper reports. |
| `RUNS.md` | Every replay run, its platform version, and what it was used for. |

## Requirements

The replay lives in `wireless_experiments/replay/` and expects MEDWEAR
next to `wireless_experiments`:

```
<workspace>/
  medwear/                         MEDWEAR (bridge in ros2_to_medwear/)
  wireless_experiments/
    scripts/analyze_session.py     session analysis
    captures/<session>/            the recorded sessions
    replay/                        this folder
```

- ROS 2 Jazzy and a workspace with
  [healthcare_msgs](https://github.com/SCAI-Lab/healthcare_msgs) built
  (commit `c9de02f`):
  ```bash
  export ROS_WS=~/ros_ws                   # your workspace
  source /opt/ros/jazzy/setup.bash && source $ROS_WS/install/setup.bash
  ```
- Python: numpy, pandas, PyYAML, requests.
- For a replay: a running MEDWEAR stack and a user with write access to the
  target project (`setup_replay_user.sh` creates one and writes
  `.replay_credentials`, which is git-ignored).

## Versions used in the paper

| Component | Version |
|---|---|
| MEDWEAR (bridge and platform) | `49be39f`
| healthcare_msgs | `c9de02f` |
| Session analysis | `scripts/analyze_session.py` 1.4.3 |
| Replay runs | tags `full_W1_sessions`, `sweep_W{0.1,0.5,1,5}_sessions` (SLURM job 13832), `gym_W1_sessions` (13833), see `RUNS.md` |

`deploy/deploy.sh` exports exactly these commits from git, so local changes
never reach the node.

## Running a replay

Locally, against a MEDWEAR stack:

```bash
C=../captures
python3 replay_chamber.py --project <project> --dry-run $C/20261007_105222_chamber_1
python3 replay_chamber.py --project <project> --tag full_W1 --batch-window 1 \
    $C/20261007_105222_chamber_1 $C/20261008_010249_chamber_2
```

Options: `--api URL` (default `$API_BASE_URL`, else `https://localhost/v1`),
`--only REGEX` (subset of runs), `--rate R` (playback speed), `--batch-window W`,
`--bridge-config`, `--patient-suffix`, `--force` (replay runs already marked
done), `--domain-id N` (isolated ROS domain, default 87; discovery is
restricted to localhost).

The full protocol is `./run_e2e.sh [name]` (chamber, ~20 h) and
`./run_gym.sh [tag]` (gym, ~6 h). Both resume after an interruption; stop
cleanly between runs with `touch logs/STOP`. A repeat must use a new name or
tag: MEDWEAR keeps what it stored and answers already-stored batches as
duplicates.

On a SLURM cluster (what the paper used):

```bash
TARGET=user@login-node deploy/deploy.sh                       # copy, build, initialise (job_setup.sbatch)
ssh user@login-node 'E2E_CMD="./run_e2e.sh sessions" sbatch ~/medwear_e2e/job_e2e.sbatch'
ssh user@login-node 'E2E_CMD="./run_gym.sh gym_W1_sessions" sbatch ~/medwear_e2e/job_e2e.sbatch'
```

Then copy `~/medwear_e2e/replay/logs/` back and analyse locally.

## What gets uploaded

| Topic | Device | MEDWEAR signal | Notes |
|---|---|---|---|
| `/polar_ecg` | Polar_H10 | ECG (mV, 130 Hz) | batch closed when its oldest sample has waited W, or at gaps > 1 s |
| `/polar_acc` | Polar_H10 | IMU (g, 200 Hz) | one batch per axis (`ros2_imu_x_v1`, ...) |
| `/corsano/hr` | Corsano | HR (bpm) | |
| `/corsano/bioz` | Corsano | EDA (raw) | BioZ; the platform has no bioimpedance type |
| `/corsano/acceleration` | Corsano | IMU (raw) | one batch per axis |

Not uploaded: `/polar_rr`, `/corsano/rr`, `/corsano/stress` (no matching
signal type). IDs: `patient_id = <session>__<run>[__<tag suffix>]`,
`device_id = Polar_H10 | Corsano`. Timestamps are the original sensor
stamps, so a gap in a recording stays a gap in MEDWEAR.

## Measurements

All times are on the one clock of the replay node:

- t_h (host arrival): HCI time of the frame's last ACL fragment, from the
  btsnoop capture (`analyze_session.py`).
- t_r (live path): receive time at the rosbag recorder during the original
  session, a local ROS 2 subscriber of the standardized message.
- t_c (durable path): commit time of the sample's batch in the replay (both
  stores hold it: `X-Stored-At` header, else med-api's `COMMIT OK` /
  `InfluxDB OK` log line), placed on the original timeline as
  t_c = t_r + (commit − bridge receipt).

Per run, `logs/<tag>/<session>/` holds `<run>.metrics.jsonl` (one line per
batch: samples, receipt, queue, send, response, verdict), `<run>.api.log`
(med-api lines of this run), `<run>.bridge.log`, `<run>.play.log` and
`<run>.stats.csv` (container CPU and memory at 1 Hz); `logs/<tag>/replay_state.json`
lists every run's status and counts.

## Reproducing the paper's numbers

1. Get the captures (Zenodo) into `../captures/` and the replay logs (Zenodo,
   `replay_logs_*.zip`) into e.g. `logs_zenodo/`.
2. Run the analysis (minutes):

```bash
C=../captures
python3 analyze_e2e.py --logs logs_zenodo/replay_logs_chamber --full-tag full_W1_sessions \
    --sweep-tags sweep_W0.1_sessions sweep_W0.5_sessions sweep_W1_sessions sweep_W5_sessions \
    --table-tag sweep_W0.1_sessions --out results/chamber \
    $C/20261007_105222_chamber_1 $C/20261008_010249_chamber_2
python3 analyze_e2e.py --logs logs_zenodo/replay_logs_gym --full-tag gym_W1_sessions --out results/gym \
    $C/gym_SPZ_20261013_133642 $C/gym_SPZ_20261013_160606 $C/gym_SPZ_20261016_103145
```

`results/chamber/table3_hostcens.tex` is Table IV; `report.md`,
`stages.csv` and `w_sweep.csv` hold the accounting, per-step timing and
W sweep of the supplementary tables.

## Data

The replay logs are archived on Zenodo, one zip for the chamber and one for
the gym runs, with the SLURM job outputs. They hold no personal data: the
sensors were not worn, and the logs contain only batch timing, counts and
the platform's responses. The SLURM user name and node hostname were replaced
(`export_logs.py`).
