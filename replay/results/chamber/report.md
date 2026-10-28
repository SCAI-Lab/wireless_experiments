# End to end through MEDWEAR: measured values

## Full replay

- Recordings: 120, 11.9 h, 15.09 million samples committed (88280 batches, 88280 requests incl. retries)
- Rejections by code: {'SCHEMA_VALIDATION_FAILURE': 1}
- Duplicates answered by the API: 0; checksum mismatches: 0; overlap warnings: 88279
- ECG plausibility rejections: 1
  - chamber_2/row3_Polar_H10_forced_channel_no_load_rep1: 146 samples, 2.8 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=4.17 mV exceeds 30% of ma
- Messages in bags vs committed, per topic:
  - /polar_ecg: 2627270 in bags, 2627124 committed, 146 missing
  - /polar_hr: 0 in bags, 0 committed, 0 missing
  - /polar_acc: 4117140 in bags, 4117140 committed, 0 missing
  - /corsano/acceleration: 3720 in bags, 3720 committed, 0 missing
  - /corsano/hr: 472 in bags, 472 committed, 0 missing
  - /corsano/bioz: 3808 in bags, 3808 committed, 0 missing

## Live path (original chamber runs, HCI arrival → ROS 2 subscriber)

- All Polar ECG samples: median 3.0 ms, p99 10.7 ms (n = 2627270)
- Largest per-run |M_live − M_host|: {'0.1s': 2.730627306273064, '0.25s': 0.7126696832579142, '1s': 0.05390660474139253, '10s': 0.0} percentage points

| Condition | runs | live p50/p99 (ms) | M_host(250ms) | M_live(250ms) | M_host(1s) | M_live(1s) |
|---|---|---|---|---|---|---|
| $k=0$, 75% load | 10 | 3.0/6.7 | 60.11 | 60.33 | 39.38 | 39.39 |
| $k=2$, 75% load | 3 | 3.0/7.5 | 13.02 | 13.40 | 0.12 | 0.12 |
| $k=4$, 75% load | 3 | 2.9/6.3 | 4.92 | 5.02 | 0.16 | 0.16 |
| $k=8$, 75% load | 3 | 3.0/6.8 | 0.85 | 0.92 | 0.09 | 0.09 |
| AFH free, 100% load | 3 | 3.0/6.3 | 0.08 | 0.08 | 0.08 | 0.08 |
| calibration | 1 | 3.1/5.8 | 0.00 | 0.00 | 0.00 | 0.00 |
| duty0pct | 3 | 3.1/6.5 | 0.12 | 0.12 | 0.12 | 0.12 |
| duty0pct_bracket | 1 | 3.1/6.2 | 0.00 | 0.00 | 0.00 | 0.00 |
| duty0pct_long | 1 | 3.2/42.1 | 0.03 | 0.03 | 0.03 | 0.03 |
| duty100pct_long | 1 | 3.0/60.1 | 0.05 | 0.05 | 0.05 | 0.05 |
| duty25pct | 3 | 3.0/6.1 | 0.14 | 0.14 | 0.14 | 0.14 |
| duty50pct | 3 | 2.9/6.8 | 0.08 | 0.08 | 0.08 | 0.08 |
| duty75pct | 3 | 3.0/6.2 | 0.08 | 0.08 | 0.08 | 0.08 |
| forced_channel_duty25pct | 3 | 3.0/6.3 | 5.36 | 5.49 | 0.11 | 0.11 |
| forced_channel_duty50pct | 3 | 3.0/6.5 | 8.70 | 8.96 | 0.16 | 0.16 |
| forced_channel_max_load | 3 | 3.1/6.5 | 14.56 | 14.94 | 0.23 | 0.23 |
| forced_channel_no_load | 5 | 3.1/6.4 | 0.07 | 0.07 | 0.07 | 0.07 |
| forced_channel_no_load_bracket | 1 | 3.1/6.8 | 0.00 | 0.00 | 0.00 | 0.00 |
| headroom_k2_duty100pct | 3 | 2.9/8.5 | 8.54 | 8.94 | 0.08 | 0.08 |
| headroom_k2_no_load | 3 | 3.1/6.6 | 0.06 | 0.06 | 0.06 | 0.06 |
| headroom_k8_duty100pct | 3 | 2.8/6.1 | 1.21 | 1.34 | 0.14 | 0.14 |
| renegotiation_watch | 1 | 3.1/11.9 | 0.09 | 0.09 | 0.09 | 0.09 |

## Durable path and batch window W (Polar ECG, table conditions)

| W (s) | runs | req/s | med-api CPU (%) | commit added p50/p99 (s) | met 10 s (% samples) | met 250 ms (% samples) | M_durable(10 s) | M_durable(250 ms) | AoI mean (s) |
|---|---|---|---|---|---|---|---|---|---|
| 0.1 | 22 | 1.54 | 2.17 | 0.14/0.17 | 100.00 | 99.98 | 0.66 | 51.06 | 1.41 |
| 0.5 | 22 | 1.22 | 1.80 | 0.53/0.57 | 100.00 | 19.77 | 0.78 | 88.07 | 1.71 |
| 1 | 22 | 0.71 | 1.15 | 0.69/1.07 | 100.00 | 8.57 | 1.03 | 96.64 | 1.98 |
| 5 | 22 | 0.18 | 0.48 | 2.75/5.08 | 100.00 | 2.72 | 4.46 | 98.32 | 3.94 |

- Message-rate reduction W=0.1→5 s: 8.4-fold
- W=1 s vs 0.1 s in M(10 s): +0.37 percentage points

## Time per step, sensor to durable commit (Polar ECG, committed samples)

Batch-level steps count once per sample of the batch. The BLE step is the added delay relative to each run's minimum (as for M(D)); all other steps are absolute. Means add up to the total; percentiles do not. PostgreSQL = commit time minus med-api's logged InfluxDB write duration (1 ms resolution). stages.csv has the same numbers.

### full_W1_sessions

| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| BLE link + host stack (added delay, t_h - t_s) | 330.4 | 55.9 | 1053.4 | 7590.2 |
| Driver to standardized ROS 2 message (t_r - t_h) | 3.4 | 3.0 | 5.3 | 10.7 |
| Bridge batch window (sample receipt -> batch closed) | 719.6 | 639.5 | 1040.6 | 1045.9 |
| Upload queue (batch closed -> request sent) | 0.5 | 0.2 | 0.4 | 7.6 |
| HTTPS to the API: TLS, nginx, auth (sent -> received) | 3.8 | 3.4 | 8.4 | 10.9 |
| Schema and plausibility validation | 0.6 | 0.5 | 1.2 | 2.2 |
| PostgreSQL commit (batch metadata) | 11.3 | 9.8 | 23.7 | 52.2 |
| InfluxDB write (samples) | 6.1 | 6.0 | 9.0 | 17.0 |
| **Total, sensor to durable commit** | 1075.7 | 1048.6 | 1853.9 | 8261.6 |
| Acknowledgment back to the bridge (not in total) | 12.5 | 12.0 | 17.4 | 19.9 |

### sweep_W0.1_sessions

| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| BLE link + host stack (added delay, t_h - t_s) | 963.4 | 106.6 | 6812.2 | 8744.6 |
| Driver to standardized ROS 2 message (t_r - t_h) | 3.1 | 3.0 | 5.2 | 6.7 |
| Bridge batch window (sample receipt -> batch closed) | 121.8 | 122.0 | 144.7 | 147.6 |
| Upload queue (batch closed -> request sent) | 0.3 | 0.1 | 0.2 | 0.2 |
| HTTPS to the API: TLS, nginx, auth (sent -> received) | 3.4 | 3.3 | 3.6 | 3.9 |
| Schema and plausibility validation | 0.5 | 0.5 | 0.5 | 0.6 |
| PostgreSQL commit (batch metadata) | 9.6 | 9.5 | 10.3 | 11.4 |
| InfluxDB write (samples) | 4.8 | 5.0 | 6.0 | 6.0 |
| **Total, sensor to durable commit** | 1107.0 | 251.8 | 6961.1 | 8901.5 |
| Acknowledgment back to the bridge (not in total) | 12.2 | 12.2 | 13.2 | 13.6 |

### sweep_W0.5_sessions

| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| BLE link + host stack (added delay, t_h - t_s) | 963.4 | 106.6 | 6812.2 | 8744.6 |
| Driver to standardized ROS 2 message (t_r - t_h) | 3.1 | 3.0 | 5.2 | 6.7 |
| Bridge batch window (sample receipt -> batch closed) | 424.4 | 511.9 | 542.8 | 547.0 |
| Upload queue (batch closed -> request sent) | 0.6 | 0.2 | 2.0 | 5.6 |
| HTTPS to the API: TLS, nginx, auth (sent -> received) | 3.6 | 3.4 | 3.8 | 8.5 |
| Schema and plausibility validation | 0.5 | 0.5 | 0.6 | 0.6 |
| PostgreSQL commit (batch metadata) | 9.9 | 9.8 | 10.8 | 12.0 |
| InfluxDB write (samples) | 5.2 | 5.0 | 6.0 | 7.0 |
| **Total, sensor to durable commit** | 1410.8 | 629.1 | 7263.7 | 9172.4 |
| Acknowledgment back to the bridge (not in total) | 12.3 | 12.2 | 13.3 | 15.4 |

### sweep_W1_sessions

| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| BLE link + host stack (added delay, t_h - t_s) | 963.4 | 106.6 | 6812.2 | 8744.6 |
| Driver to standardized ROS 2 message (t_r - t_h) | 3.1 | 3.0 | 5.2 | 6.7 |
| Bridge batch window (sample receipt -> batch closed) | 698.2 | 665.1 | 1041.5 | 1046.4 |
| Upload queue (batch closed -> request sent) | 0.6 | 0.2 | 0.3 | 5.5 |
| HTTPS to the API: TLS, nginx, auth (sent -> received) | 3.8 | 3.6 | 4.0 | 16.2 |
| Schema and plausibility validation | 0.6 | 0.5 | 0.6 | 0.7 |
| PostgreSQL commit (batch metadata) | 10.3 | 10.2 | 11.4 | 12.4 |
| InfluxDB write (samples) | 6.1 | 6.0 | 7.0 | 8.0 |
| **Total, sensor to durable commit** | 1686.1 | 1085.3 | 7538.3 | 9467.1 |
| Acknowledgment back to the bridge (not in total) | 12.3 | 12.3 | 13.5 | 14.8 |

### sweep_W5_sessions

| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| BLE link + host stack (added delay, t_h - t_s) | 963.4 | 106.6 | 6812.2 | 8744.6 |
| Driver to standardized ROS 2 message (t_r - t_h) | 3.1 | 3.0 | 5.2 | 6.7 |
| Bridge batch window (sample receipt -> batch closed) | 2665.3 | 2712.2 | 5017.3 | 5040.7 |
| Upload queue (batch closed -> request sent) | 2.3 | 0.2 | 5.5 | 79.6 |
| HTTPS to the API: TLS, nginx, auth (sent -> received) | 5.4 | 4.6 | 16.1 | 18.0 |
| Schema and plausibility validation | 0.9 | 0.7 | 1.4 | 6.4 |
| PostgreSQL commit (batch metadata) | 13.7 | 13.0 | 15.0 | 55.3 |
| InfluxDB write (samples) | 11.3 | 11.0 | 13.0 | 15.0 |
| **Total, sensor to durable commit** | 3665.5 | 3408.0 | 9369.6 | 12270.6 |
| Acknowledgment back to the bridge (not in total) | 12.7 | 12.6 | 14.1 | 15.9 |

Table III durable column from W = 0.1 s; LaTeX rows in table3.tex.
