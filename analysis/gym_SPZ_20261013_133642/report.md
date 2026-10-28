# Session gym_SPZ_20261013_133642

Input: `captures/gym_SPZ_20261013_133642`  
Analyzed: 2026-10-21T12:12:09 (analyze_session.py 1.4.3)  
Runs: 24 analyzed, 0 excluded

No-load window starts 40 s after driver launch (default, no loaded runs).  
ECG clock drift removed from every run: -61.8 ppm (median of 3 reference runs (no-load; gym: AFH-free), range -62.3---52.0 ppm).

## Sanity checks

| Check | Status | Detail |
|---|---|---|
| captures present | PASS | all manifest captures found |
| no constrained map in AFH-free runs | PASS | ok |
| constrained maps within requested mask | PASS | ok |
| AFH channel map logged | PASS | ok |
| QBSS airtime present for loaded runs | PASS | ok |
| QBSS consistent within load level | PASS | ok |
| iperf load recorded | PASS | 0 of 0 loaded runs have iperf throughput |
| single driver invocation per device | PASS | ok |
| final connection interval consistent per device | PASS | ok |
| no startup disconnects | INFO | 1 runs with a disconnect in the first 15s (pre-existing link): gym_free_Corsano_free_channel_rep1.btsnoop |
| no link loss without load | PASS | ok |
| link recovered after loss | PASS | ok |
| no BlueZ stale-connection wedge | PASS | ok |
| no-load data rate stable | PASS | n/a |
| scanner adapter unmanaged | PASS | 'unmanaged' set 0 times |
| results.csv agrees with HCI (intervals) | PASS | ok |

**Gym session:** load = airtime measured on the forced WiFi channel by background_monitor.py (`airtime_range`; per run in per_run.csv with the other `bg_*` covariates). There are no no-load runs, so Corsano delivery is relative to this session's free-channel median. Forced masks: see `forced_channels_n` and the extras' airtime (`forced_extra_airtime_*`) in per_run.csv, not only k.

## Link parameters

| device    |   final_ci_ms |   initial_ci_ms |   sup_timeout_ms |   latency |   dle_bytes | phy   |   noload_Bps |   noload_notif_per_s |   requests_per_s | driver                                                                                                                                                                            |
|:----------|--------------:|----------------:|-----------------:|----------:|------------:|:------|-------------:|---------------------:|-----------------:|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Corsano   |            60 |              60 |             5000 |         0 |         251 | 2M    |          nan |                  nan |              nan | ros2 run corsano_ros corsano_ros --mac-address-or-name D9:D9:DF:CB:74:79 --publish-bioz --publish-accel --publish-hr --publish-stress --plan HOSPITAL_RAW --test-republish-latest |
| Polar_H10 |            45 |              45 |             6000 |         0 |         236 | 2M    |          nan |                  nan |              nan | ros2 run polar_ros2 polar_connector --mac-address-or-name A0:9E:1A:DF:7F:6C --no-publish-battery                                                                                  |

## Corsano

| condition    |   n |   airtime_range |   runs_with_loss | t_loss_range_s   | delivered_pct_range   | ecg_completeness_range   |   delay_p50_ms |   delay_p99_ms |   delay_max_ms |   miss_250ms_pct |   miss_1000ms_pct |   miss_age_250ms_pct |   runaways |   backlog_time_pct_median |   requests_per_s |   notif_per_request |   p_hat |
|:-------------|----:|----------------:|-----------------:|:-----------------|:----------------------|:-------------------------|---------------:|---------------:|---------------:|-----------------:|------------------:|---------------------:|-----------:|--------------------------:|-----------------:|--------------------:|--------:|
| free_channel |   3 |             100 |                0 | --               | 100--103              | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.31 |                2.68 |     nan |
| forced_k0    |   3 |             100 |                0 | --               | 96--103               | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.28 |                2.7  |     nan |
| forced_k2    |   3 |             100 |                0 | --               | 101--103              | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.32 |                2.7  |     nan |
| forced_k8    |   3 |             100 |                0 | --               | 101--103              | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.32 |                2.69 |     nan |

## Polar_H10

| condition    |   n | airtime_range   |   runs_with_loss | t_loss_range_s   | delivered_pct_range   | ecg_completeness_range   |   delay_p50_ms |   delay_p99_ms |   delay_max_ms |   miss_250ms_pct |   miss_1000ms_pct |   miss_age_250ms_pct |   runaways |   backlog_time_pct_median |   requests_per_s |   notif_per_request |   p_hat |
|:-------------|----:|:----------------|-----------------:|:-----------------|:----------------------|:-------------------------|---------------:|---------------:|---------------:|-----------------:|------------------:|---------------------:|-----------:|--------------------------:|-----------------:|--------------------:|--------:|
| free_channel |   3 | 79.1--100.0     |                0 | --               | 100--100              | 100--100                 |          43.86 |         109.86 |         137.59 |             0.12 |              0.12 |                62.91 |          0 |                         0 |                0 |                 nan |     nan |
| forced_k0    |   3 | 11.7--100.0     |                0 | --               | 100--100              | 100                      |          43.32 |         119.67 |         162.51 |             0    |              0    |                63    |          0 |                         0 |                0 |                 nan |     nan |
| forced_k2    |   3 | 12.7--100.0     |                0 | --               | 99--100               | 99--100                  |          44.64 |         114.78 |         152.46 |             0.45 |              0.45 |                63.26 |          0 |                         0 |                0 |                 nan |     nan |
| forced_k8    |   3 | 10.2--99.9      |                0 | --               | 100--100              | 100                      |          43.03 |         110.65 |         175.44 |             0    |              0    |                62.78 |          0 |                         0 |                0 |                 nan |     nan |

`miss_*` count added delay only; `miss_age_*` is end-to-end sample age, which adds up to 554 ms of sensor framing (oldest sample of a frame). Runaway: added delay reached 2 s and never fell below 0.5 s again in the window.

## Link recovery after loss

No run lost the link.

## Figures

- `figures/gym_SPZ_20261013_133642_delivery.pdf`
- `figures/gym_SPZ_20261013_133642_request_response.pdf`
