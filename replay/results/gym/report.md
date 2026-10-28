# End to end through MEDWEAR: measured values

## Full replay

- Recordings: 72, 6.1 h, 7.20 million samples committed (49033 batches, 49033 requests incl. retries)
- Rejections by code: {'SCHEMA_VALIDATION_FAILURE': 23}
- Duplicates answered by the API: 0; checksum mismatches: 0; overlap warnings: 49010
- ECG plausibility rejections: 23
  - gym_SPZ_20261013_133642/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 144.4 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 4 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 130.8 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 48 samples 
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 131.9 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=5.14 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 133.1 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=5.49 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 135.3 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 3 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 144.3 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=6.24 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 145.4 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 6 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 146.5 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=-3.03 mV exceeds 30% of m
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 161.1 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 56 samples 
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 162.2 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=5.83 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 163.4 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=3.75 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 164.5 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 2 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 175.7 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 4 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k0_rep1: 146 samples, 181.3 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=3.75 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 160.6 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 23 samples 
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 161.7 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 30 samples 
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 162.8 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=6.57 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 164.0 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG baseline wander detected: signal mean=3.49 mV exceeds 30% of ma
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 253.8 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 6 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k2_rep1: 146 samples, 261.7 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 2 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k8_rep1: 146 samples, 208.3 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 3 samples e
  - gym_SPZ_20261016_103145/gym_forced_Polar_H10_forced_k8_rep2: 146 samples, 206.5 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 2 samples e
  - gym_SPZ_20261016_103145/gym_free_Polar_H10_free_channel_rep2: 146 samples, 128.6 s after last connection — {"error": "SCHEMA_VALIDATION_FAILURE", "detail": "ECG body validation failed: : Value error, ECG amplitude outside physiological range [-10, 10] mV: 2 samples e
- Messages in bags vs committed, per topic:
  - /polar_ecg: 1247808 in bags, 1244450 committed, 3358 missing
  - /polar_hr: 3172 in bags, 3172 committed, 0 missing
  - /polar_acc: 1961388 in bags, 1961388 committed, 0 missing
  - /corsano/acceleration: 2542 in bags, 2542 committed, 0 missing
  - /corsano/hr: 154 in bags, 154 committed, 0 missing
  - /corsano/bioz: 2559 in bags, 2559 committed, 0 missing

## Live path (original chamber runs, HCI arrival → ROS 2 subscriber)

- All Polar ECG samples: median 3.3 ms, p99 7.2 ms (n = 1178439)
- Largest per-run |M_live − M_host|: {'0.1s': 2.4441219158200305, '0.25s': 0.0, '1s': 0.0, '10s': 0.0} percentage points

| Condition | runs | live p50/p99 (ms) | M_host(250ms) | M_live(250ms) | M_host(1s) | M_live(1s) |
|---|---|---|---|---|---|---|
| forced_k0 | 8 | 3.3/7.4 | 1.29 | 1.29 | 0.52 | 0.52 |
| forced_k2 | 9 | 3.3/6.9 | 0.22 | 0.22 | 0.15 | 0.15 |
| forced_k8 | 8 | 3.3/7.2 | 0.17 | 0.17 | 0.17 | 0.17 |
| free_channel | 9 | 3.3/7.4 | 0.08 | 0.08 | 0.08 | 0.08 |

## Time per step, sensor to durable commit (Polar ECG, committed samples)

Batch-level steps count once per sample of the batch. The BLE step is the added delay relative to each run's minimum (as for M(D)); all other steps are absolute. Means add up to the total; percentiles do not. PostgreSQL = commit time minus med-api's logged InfluxDB write duration (1 ms resolution). stages.csv has the same numbers.

### gym_W1_sessions

| Step | mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| BLE link + host stack (added delay, t_h - t_s) | 50.1 | 44.8 | 94.3 | 145.6 |
| Driver to standardized ROS 2 message (t_r - t_h) | 3.5 | 3.3 | 5.6 | 7.2 |
| Bridge batch window (sample receipt -> batch closed) | 741.9 | 627.4 | 1041.2 | 1046.2 |
| Upload queue (batch closed -> request sent) | 0.5 | 0.2 | 0.4 | 4.0 |
| HTTPS to the API: TLS, nginx, auth (sent -> received) | 3.6 | 3.4 | 7.1 | 10.3 |
| Schema and plausibility validation | 0.6 | 0.5 | 1.1 | 2.1 |
| PostgreSQL commit (batch metadata) | 11.2 | 9.7 | 21.7 | 53.6 |
| InfluxDB write (samples) | 5.9 | 6.0 | 7.0 | 16.0 |
| **Total, sensor to durable commit** | 817.2 | 863.6 | 1135.8 | 1180.5 |
| Acknowledgment back to the bridge (not in total) | 12.4 | 12.0 | 16.7 | 20.0 |
