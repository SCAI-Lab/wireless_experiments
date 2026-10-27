# Session gym_SPZ_20261016_103145

Input: `captures/gym_SPZ_20261016_103145`  
Analyzed: 2026-10-21T12:12:10 (analyze_session.py 1.4.3)  
Runs: 24 analyzed, 0 excluded

No-load window starts 40 s after driver launch (default, no loaded runs).  
ECG clock drift removed from every run: -27.1 ppm (median of 3 reference runs (no-load; gym: AFH-free), range -47.7---21.8 ppm).

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
| no startup disconnects | PASS | ok |
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

| condition    |   n | airtime_range   |   runs_with_loss | t_loss_range_s   | delivered_pct_range   | ecg_completeness_range   |   delay_p50_ms |   delay_p99_ms |   delay_max_ms |   miss_250ms_pct |   miss_1000ms_pct |   miss_age_250ms_pct |   runaways |   backlog_time_pct_median |   requests_per_s |   notif_per_request |   p_hat |
|:-------------|----:|:----------------|-----------------:|:-----------------|:----------------------|:-------------------------|---------------:|---------------:|---------------:|-----------------:|------------------:|---------------------:|-----------:|--------------------------:|-----------------:|--------------------:|--------:|
| free_channel |   3 | 12.7--12.7      |                0 | --               | 98--101               | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.31 |                2.72 |     nan |
| forced_k0    |   3 | 12.5--22.7      |                1 | 10               | 6--100                | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             1.74 |                2.16 |     nan |
| forced_k2    |   3 | 11.8--12.2      |                0 | --               | 58--99                | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.08 |                2.59 |     nan |
| forced_k8    |   3 | 11.7--12.1      |                0 | --               | 98--99                | --                       |            nan |            nan |            nan |              nan |               nan |                  nan |        nan |                       nan |             2.31 |                2.7  |     nan |

## Polar_H10

| condition    |   n | airtime_range   |   runs_with_loss | t_loss_range_s   | delivered_pct_range   | ecg_completeness_range   |   delay_p50_ms |   delay_p99_ms |   delay_max_ms |   miss_250ms_pct |   miss_1000ms_pct |   miss_age_250ms_pct |   runaways |   backlog_time_pct_median |   requests_per_s |   notif_per_request |   p_hat |
|:-------------|----:|:----------------|-----------------:|:-----------------|:----------------------|:-------------------------|---------------:|---------------:|---------------:|-----------------:|------------------:|---------------------:|-----------:|--------------------------:|-----------------:|--------------------:|--------:|
| free_channel |   3 | 10.5--12.1      |                0 | --               | 100--100              | 100                      |          43.92 |         110.34 |         144.46 |             0    |              0    |                62.88 |          0 |                         0 |                0 |                 nan |     nan |
| forced_k0    |   3 | 10.3--19.9      |                0 | --               | 99--100               | 99--100                  |          50.1  |        1087.77 |        2626.19 |             3.38 |              1.33 |                66.17 |          0 |                         0 |                0 |                 nan |     nan |
| forced_k2    |   3 | 11.2--19.0      |                0 | --               | 100--100              | 100                      |          47.99 |         188.57 |         361.87 |             0.21 |              0    |                64.69 |          0 |                         0 |                0 |                 nan |     nan |
| forced_k8    |   3 | 10.7--28.1      |                0 | --               | 99--100               | 99--100                  |          47.05 |         165.56 |         251.8  |             0.4  |              0.26 |                64.26 |          0 |                         0 |                0 |                 nan |     nan |

`miss_*` count added delay only; `miss_age_*` is end-to-end sample age, which adds up to 554 ms of sensor framing (oldest sample of a frame). Runaway: added delay reached 2 s and never fell below 0.5 s again in the window.

## Link recovery after loss
Per run that lost the link: the tail is the observation time left after the last loss; `host_connect_cmds` counts HCI (Extended) Create Connection commands issued after it; `sensor_advs` / `connectable` count advertising reports from the sensor's own address in that tail.

### Corsano

| condition   |   rep |   losses | reason   |   tail_s | outcome     |   reconnects |   host_connect_cmds |   sensor_advs |   connectable |   scan_off_after_s |
|:------------|------:|---------:|:---------|---------:|:------------|-------------:|--------------------:|--------------:|--------------:|-------------------:|
| forced_k0   |     3 |        4 | 0x8      |     14.5 | reconnected |            1 |                   1 |             1 |             1 |                5.3 |

## Limitations

- **Corsano**: every run that lost the link reconnected within the run (5.7 s after the loss); no non-recovery limitation applies.

## Reconnected vs recovered data
A reconnect counts as recovered only if data followed it. `ttfs` = time from Connection Complete to the first ECG sample (Polar) or notification (others); `data back` = first data after the first loss; `data/conn. s` = samples (Polar) or notifications per second of post-loss connected time. Per-connection and per-loss detail, including when the sensor advertised (connectably) again: `tables/connections.csv`, `tables/losses.csv`.

### Corsano

| condition   |   rep |   losses |   reconnects |   with data |   ttfs p50 (s) |   data back (s) |   post-loss conn. (s) |   data/conn. s |   % of 130 Hz |   wedged at (s) |
|:------------|------:|---------:|-------------:|------------:|---------------:|----------------:|----------------------:|---------------:|--------------:|----------------:|
| forced_k0   |     3 |        4 |            4 |           3 |          15.21 |           20.94 |                229.18 |           0.75 |           nan |             nan |

## Figures

- `figures/gym_SPZ_20261016_103145_delivery.pdf`
- `figures/gym_SPZ_20261016_103145_request_response.pdf`
