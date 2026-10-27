# Pooled: 20261007_105222_chamber_1, 20261008_010249_chamber_2

| device    | family      | condition                      |   n |   sessions |   runs_with_loss |   t_loss_median_s | t_loss_iqr_s   |   survived |
|:----------|:------------|:-------------------------------|----:|-----------:|-----------------:|------------------:|:---------------|-----------:|
| Corsano   | constrained | forced_channel_duty25pct       |   3 |          1 |                3 |           34.6416 | 23--73         |          0 |
| Corsano   | constrained | forced_channel_duty50pct       |   3 |          1 |                3 |           18.82   | 15--61         |          0 |
| Corsano   | constrained | forced_channel_duty75pct       |   3 |          1 |                3 |           52.3545 | 32--71         |          0 |
| Corsano   | constrained | forced_channel_max_load        |   3 |          1 |                3 |           11.1086 | 11--12         |          0 |
| Corsano   | constrained | forced_channel_no_load         |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Corsano   | constrained | forced_channel_no_load_bracket |   1 |          1 |                0 |          nan      | nan--nan       |          1 |
| Corsano   | headroom    | headroom_k0_duty75pct          |   3 |          1 |                3 |           54.7121 | 41--60         |          0 |
| Corsano   | headroom    | headroom_k2_duty100pct         |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Corsano   | headroom    | headroom_k2_duty75pct          |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Corsano   | headroom    | headroom_k2_no_load            |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Corsano   | headroom    | headroom_k4_duty75pct          |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Corsano   | headroom    | headroom_k8_duty100pct         |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Corsano   | headroom    | headroom_k8_duty75pct          |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | constrained | forced_channel_duty25pct       |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | constrained | forced_channel_duty50pct       |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | constrained | forced_channel_duty75pct       |   5 |          2 |                0 |          nan      | nan--nan       |          5 |
| Polar_H10 | constrained | forced_channel_max_load        |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | constrained | forced_channel_no_load         |   5 |          2 |                0 |          nan      | nan--nan       |          5 |
| Polar_H10 | constrained | forced_channel_no_load_bracket |   1 |          1 |                0 |          nan      | nan--nan       |          1 |
| Polar_H10 | headroom    | headroom_k0_duty75pct          |   5 |          2 |                0 |          nan      | nan--nan       |          5 |
| Polar_H10 | headroom    | headroom_k2_duty100pct         |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | headroom    | headroom_k2_duty75pct          |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | headroom    | headroom_k2_no_load            |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | headroom    | headroom_k4_duty75pct          |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | headroom    | headroom_k8_duty100pct         |   3 |          1 |                0 |          nan      | nan--nan       |          3 |
| Polar_H10 | headroom    | headroom_k8_duty75pct          |   3 |          1 |                0 |          nan      | nan--nan       |          3 |

## Polar runaway backlog

Runaway: added delay reached 2 s and never fell below 0.5 s again in the window. p with Wilson 95 % CI; onset rate per hour of exposure (load onset to runaway onset, or the whole window).

| condition                                                          |   headroom_k |   n |   sessions |   runaways |   p |   p_lo95 |   p_hi95 |   onsets_per_h |   onsets_per_h_lo95 |   onsets_per_h_hi95 | onset_s_range   | implied_capacity_pct_range   |   backlog_time_pct_median |
|:-------------------------------------------------------------------|-------------:|----:|-----------:|-----------:|----:|---------:|---------:|---------------:|--------------------:|--------------------:|:----------------|:-----------------------------|--------------------------:|
| forced_channel_duty25pct                                           |          nan |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.314 | --              | --                           |                     0     |
| forced_channel_duty50pct                                           |          nan |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.294 | --              | --                           |                     0     |
| forced_channel_duty75pct                                           |          nan |   5 |          2 |          1 | 0.2 |    0.036 |    0.624 |          3.13  |               0.079 |              17.437 | 63              | 89.1                         |                     1.037 |
| forced_channel_max_load                                            |          nan |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.294 | --              | --                           |                     0     |
| headroom_k0_duty75pct                                              |            0 |   5 |          2 |          3 | 0.6 |    0.231 |    0.882 |         17.456 |               3.6   |              51.014 | 6--56           | 93.9--97.8                   |                    77.66  |
| headroom_k2_duty100pct                                             |            2 |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.274 | --              | --                           |                     0     |
| headroom_k2_duty75pct                                              |            2 |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.294 | --              | --                           |                     0     |
| headroom_k4_duty75pct                                              |            4 |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.294 | --              | --                           |                     0     |
| headroom_k8_duty100pct                                             |            8 |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.314 | --              | --                           |                     0     |
| headroom_k8_duty75pct                                              |            8 |   3 |          1 |          0 | 0   |    0     |    0.562 |          0     |               0     |              16.294 | --              | --                           |                     0     |
| k=0 @75% (pooled: forced_channel_duty75pct, headroom_k0_duty75pct) |            0 |  10 |          2 |          4 | 0.4 |    0.168 |    0.687 |          8.14  |               2.218 |              20.842 | 6--63           | 89.1--97.8                   |                    17.708 |

**Runs needed** (from k=0 @75% (pooled: forced_channel_duty75pct, headroom_k0_duty75pct): 4/10 = 40%, 95 % CI 17%--69%): to estimate p = 40% within ±20/15/10 points, 24/41/93 runs per condition; to tell 40% from 10% (alpha 0.05, power 0.8), 32 per group, and from 20%, 82. The k >= 2 conditions had no runaway (15 runs together), which bounds p below 18% (one-sided 95 %); below 10 % needs 29 runaway-free runs and below 5 % needs 59. Runaway within the window depends on its length: onsets occurred at 6--63 s after load onset, 8.1/h of exposure (95 % 2.2--20.8/h), so report the window length with p, or the share of time with backlog > 1 s, and spread runs over sessions.

## Polar deadline miss: added delay vs end-to-end sample age

`M_added` is the paper's M(D) (radio share only); `M_age` adds the sensor's framing (sample j of an n-sample frame is (n-1-j)/130 s older than the frame timestamp). Both exclude the fixed radio + host floor, so ages are lower bounds.

| family       | condition                      |   n |   framing_ms |   M_added_100ms |   M_age_100ms |   M_added_250ms |   M_age_250ms |   M_added_1000ms |   M_age_1000ms |
|:-------------|:-------------------------------|----:|-------------:|----------------:|--------------:|----------------:|--------------:|-----------------:|---------------:|
| calibration  | calibration                    |   1 |        553.8 |             1   |          89.1 |             0   |          62.3 |              0   |            0   |
| calibration  | renegotiation_watch            |   1 |        553.8 |             2.2 |          89.6 |             0.1 |          62.9 |              0.1 |            0.1 |
| constrained  | forced_channel_duty25pct       |   3 |        553.8 |            36.7 |          94.4 |             5.4 |          71.6 |              0.1 |            0.4 |
| constrained  | forced_channel_duty50pct       |   3 |        553.8 |            46.7 |          95.5 |             8.7 |          74.7 |              0.2 |            0.3 |
| constrained  | forced_channel_duty75pct       |   5 |        553.8 |            71.7 |          97.5 |            44.5 |          85.5 |             23.4 |           27.8 |
| constrained  | forced_channel_max_load        |   3 |        553.8 |            57.7 |          96.4 |            14.6 |          77.9 |              0.2 |            0.6 |
| constrained  | forced_channel_no_load         |   5 |        553.8 |             2.4 |          89.5 |             0.1 |          62.8 |              0.1 |            0.1 |
| constrained  | forced_channel_no_load_bracket |   1 |        553.8 |             2.5 |          89.5 |             0   |          62.8 |              0   |            0   |
| free         | duty0pct                       |   3 |        553.8 |             2.6 |          89.6 |             0.1 |          63   |              0.1 |            0.1 |
| free         | duty100pct                     |   3 |        553.8 |             3   |          89.6 |             0.1 |          62.9 |              0.1 |            0.1 |
| free         | duty25pct                      |   3 |        553.8 |             3   |          89.7 |             0.1 |          63.1 |              0.1 |            0.1 |
| free         | duty50pct                      |   3 |        553.8 |             2.2 |          89.7 |             0.1 |          63   |              0.1 |            0.1 |
| free         | duty75pct                      |   3 |        553.8 |             2.4 |          89.4 |             0.1 |          62.7 |              0.1 |            0.1 |
| free_bracket | duty0pct_bracket               |   1 |        553.8 |             3.4 |          89.6 |             0   |          63   |              0   |            0   |
| free_long    | duty0pct_long                  |   1 |        553.8 |             2.7 |          89.5 |             0   |          62.9 |              0   |            0   |
| free_long    | duty100pct_long                |   1 |        553.8 |             3.6 |          90.1 |             0.1 |          63.5 |              0.1 |            0.1 |
| headroom     | headroom_k0_duty75pct          |   5 |        553.8 |            90   |          99.1 |            75.8 |          94.3 |             55.3 |           58.7 |
| headroom     | headroom_k2_duty100pct         |   3 |        553.8 |            44.7 |          95   |             8.5 |          74   |              0.1 |            0.1 |
| headroom     | headroom_k2_duty75pct          |   3 |        553.8 |            50.4 |          95.4 |            13.1 |          75.7 |              0.2 |            0.7 |
| headroom     | headroom_k2_no_load            |   3 |        553.8 |             2.9 |          89.6 |             0.1 |          62.9 |              0.1 |            0.1 |
| headroom     | headroom_k4_duty75pct          |   3 |        553.8 |            38.6 |          94.6 |             4.9 |          72.2 |              0.2 |            0.2 |
| headroom     | headroom_k8_duty100pct         |   3 |        553.8 |            22.9 |          92.6 |             1.2 |          67.6 |              0.1 |            0.1 |
| headroom     | headroom_k8_duty75pct          |   3 |        553.8 |            22.3 |          92.7 |             0.9 |          67.7 |              0.1 |            0.1 |

## Clean BLE data channels with WiFi on 1, 6 and 11

Data channels 0--10 at 2404 + 2i MHz, 11--36 at 2406 + 2i MHz; clean = the whole 2 MHz channel outside every WiFi band.

| wifi_band                                   |   n_clean | clean_channels            |
|:--------------------------------------------|----------:|:--------------------------|
| OFDM occupied (52 subcarriers) (±8.125 MHz) |         9 | 9 10 21 22 23 33 34 35 36 |
| nominal 20 MHz (±10 MHz)                    |         6 | 10 21 22 34 35 36         |
| spectral mask -20 dBr (±11 MHz)             |         5 | 10 22 34 35 36            |
