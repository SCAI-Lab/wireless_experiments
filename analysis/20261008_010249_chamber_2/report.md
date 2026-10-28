# Session 20261008_010249_chamber_2

Input: `captures/20261008_010249_chamber_2`  
Analyzed: 2026-10-21T12:12:08 (analyze_session.py 1.4.3)  
Runs: 12 analyzed, 0 excluded

No-load window starts 40 s after driver launch (median of loaded runs).  
ECG clock drift removed from every run: -7.9 ppm (median of 2 reference runs (no-load; gym: AFH-free), range -9.4---6.3 ppm).

## Sanity checks

| Check | Status | Detail |
|---|---|---|
| captures present | PASS | all manifest captures found |
| no constrained map in AFH-free runs | PASS | ok |
| constrained maps within requested mask | PASS | ok |
| AFH channel map logged | PASS | ok |
| QBSS airtime present for loaded runs | PASS | ok |
| QBSS consistent within load level | WARN | outliers (likely sampling glitches): row8_Polar_H10_headroom_k2_duty100pct_rep3.btsnoop (52.8% vs median 92.2%) |
| iperf load recorded | PASS | 10 of 10 loaded runs have iperf throughput |
| single driver invocation per device | PASS | ok |
| final connection interval consistent per device | PASS | ok |
| no startup disconnects | PASS | ok |
| no link loss without load | PASS | ok |
| link recovered after loss | PASS | ok |
| no BlueZ stale-connection wedge | PASS | ok |
| no-load data rate stable | PASS | n/a |
| scanner adapter unmanaged | PASS | 'unmanaged' set 22 times |
| results.csv agrees with HCI (intervals) | PASS | ok |

## Link parameters

| device    |   final_ci_ms | initial_ci_ms   |   sup_timeout_ms |   latency |   dle_bytes | phy   |   noload_Bps |   noload_notif_per_s |   requests_per_s | driver                                                                                           |
|:----------|--------------:|:----------------|-----------------:|----------:|------------:|:------|-------------:|---------------------:|-----------------:|:-------------------------------------------------------------------------------------------------|
| Polar_H10 |            45 | 45.00--60.00    |             6000 |         0 |         236 | 2M    |          nan |                  nan |              nan | ros2 run polar_ros2 polar_connector --mac-address-or-name A0:9E:1A:DF:7F:6C --no-publish-battery |

## Polar_H10

| condition                |   n | airtime_range   |   runs_with_loss | t_loss_range_s   | delivered_pct_range   | ecg_completeness_range   |   delay_p50_ms |   delay_p99_ms |   delay_max_ms |   miss_250ms_pct |   miss_1000ms_pct |   miss_age_250ms_pct |   runaways |   backlog_time_pct_median |   requests_per_s |   notif_per_request |   p_hat |
|:-------------------------|----:|:----------------|-----------------:|:-----------------|:----------------------|:-------------------------|---------------:|---------------:|---------------:|-----------------:|------------------:|---------------------:|-----------:|--------------------------:|-----------------:|--------------------:|--------:|
| forced_channel_no_load   |   2 | 0.0             |                0 | --               | --                    | 100                      |          43.66 |         112.4  |         131.03 |             0    |              0    |                62.92 |          0 |                      0    |                0 |                 nan |     nan |
| forced_channel_duty75pct |   2 | 59.4--71.3      |                0 | --               | --                    | 98--100                  |        1279.1  |        8944.09 |        9449    |            86.47 |             57.96 |                96.79 |          1 |                     57.69 |                0 |                 nan |     nan |
| headroom_k0_duty75pct    |   2 | 61.2--71.3      |                0 | --               | --                    | 97--100                  |         482.66 |        8075.86 |        8975.25 |            66.96 |             39.85 |                92.27 |          1 |                     39.45 |                0 |                 nan |     nan |
| headroom_k2_duty100pct   |   3 | 52.8--92.5      |                0 | --               | --                    | 100                      |          88.58 |         408.04 |         712.01 |             8.54 |              0.08 |                73.98 |          0 |                      0    |                0 |                 nan |     nan |
| headroom_k8_duty100pct   |   3 | 92.1--92.5      |                0 | --               | --                    | 100--100                 |          59.2  |         251.04 |         383.51 |             1.21 |              0.14 |                67.58 |          0 |                      0    |                0 |                 nan |     nan |

`miss_*` count added delay only; `miss_age_*` is end-to-end sample age, which adds up to 554 ms of sensor framing (oldest sample of a frame). Runaway: added delay reached 2 s and never fell below 0.5 s again in the window.

## Link layer (nRF sniffer), Polar constrained/headroom runs

Shares are over what the sniffer heard (`sniff_event_coverage_pct`). NACK: central NESN == peripheral SN in the same event; missed packets end the event and are not NACKs. MD: peripheral payload PDUs with More Data. Multi-PDU: events with >= 2 new peripheral PDUs. After-air p90: host arrival minus first time an ECG frame was heard on air, relative to the run's 5th percentile. Implied capacity (runaways): 1 - backlog growth rate, as % of the sensor's data rate.

| condition                |   replicate |   nominal_load_pct |   miss_1000ms_pct |   runaway |   implied_capacity_pct |   notif_per_s |   sniff_event_coverage_pct |   sniff_nack_pct |   sniff_nack_pairs |   sniff_periph_md_pct |   sniff_multi_pdu_event_pct |   sniff_after_air_p90_ms |   sniff_ecg_frames_matched |
|:-------------------------|------------:|-------------------:|------------------:|----------:|-----------------------:|--------------:|---------------------------:|-----------------:|-------------------:|----------------------:|----------------------------:|-------------------------:|---------------------------:|
| forced_channel_no_load   |           1 |                  0 |               0   |         0 |                  nan   |           8.4 |                       50   |              0   |                511 |                  34   |                        43.3 |                      4.4 |                        154 |
| forced_channel_duty75pct |           1 |                 75 |              85.9 |         1 |                   89.1 |           8.2 |                       21.3 |              0.7 |                149 |                  80.7 |                        22.1 |                      4.9 |                         34 |
| forced_channel_duty75pct |           2 |                 75 |              30   |         0 |                  nan   |           8.4 |                       22   |              1   |                195 |                  81.2 |                        21.8 |                      5.1 |                         52 |
| headroom_k0_duty75pct    |           1 |                 75 |               1.4 |         0 |                  nan   |           8.4 |                       21.7 |              1.7 |                120 |                  61.9 |                        23.3 |                      4.9 |                         31 |
| headroom_k0_duty75pct    |           2 |                 75 |              78.3 |         1 |                   93.9 |           8.2 |                       21.9 |              2   |                153 |                  78.2 |                        19.5 |                      4.3 |                         38 |
| headroom_k2_duty100pct   |           1 |                100 |               0.1 |         0 |                  nan   |           8.4 |                       22.7 |              0   |                563 |                  46.2 |                        54   |                      4.6 |                        153 |
| headroom_k2_duty100pct   |           2 |                100 |               0.1 |         0 |                  nan   |           8.4 |                       23.6 |              0   |                515 |                  45.5 |                        52.1 |                      4.1 |                        153 |
| headroom_k2_duty100pct   |           3 |                100 |               0.1 |         0 |                  nan   |           8.4 |                       22.6 |              0   |                481 |                  44.4 |                        51.4 |                      4.4 |                        134 |
| headroom_k8_duty100pct   |           1 |                100 |               0   |         0 |                  nan   |           8.4 |                       35.7 |              0   |                683 |                  39.9 |                        46   |                      4.3 |                        207 |
| headroom_k8_duty100pct   |           2 |                100 |               0.1 |         0 |                  nan   |           8.4 |                       36.2 |              0.4 |                689 |                  40.1 |                        48.8 |                      4.4 |                        208 |
| headroom_k8_duty100pct   |           3 |                100 |               0.3 |         0 |                  nan   |           8.4 |                       36.4 |              0   |                655 |                  39.7 |                        46.4 |                      4.9 |                        214 |

## Link recovery after loss

No run lost the link.

## Figures

- `figures/20261008_010249_chamber_2_airtime_vs_load.pdf`
- `figures/20261008_010249_chamber_2_polar_delay_ccdf.pdf`
- `figures/20261008_010249_chamber_2_polar_deadline_miss.pdf`
- `figures/20261008_010249_chamber_2_time_to_link_loss.pdf`
- `figures/20261008_010249_chamber_2_delivery.pdf`
- `figures/20261008_010249_chamber_2_channel_maps.pdf`
- `figures/20261008_010249_chamber_2_polar_delay_timelines.pdf`
