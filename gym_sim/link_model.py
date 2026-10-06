"""The per-device two-regime link process (manuscript sec:sim-design,
"Link process" paragraph).

In the connected state, each connection event fails or succeeds. Treating
these as i.i.d. Bernoulli(p_e) (as coexistence/model.py's
link_layer_consequences does) badly under-predicts the measured
supervision-timeout rate (sec:results-model: ~40/hr predicted vs. 140-300/hr
measured for Corsano at p_e=0.95; ~0 predicted vs. ~340/hr measured for
Polar at p_e=0.90) -- interference is bursty, so long runs of consecutive
failures (which is what trips a supervision timeout) are far more likely
than independence predicts.

This module keeps the same marginal per-event failure probability p_e (so
it doesn't contradict the already-published p_hat_e estimates) but makes
the per-event outcome sequence a 2-state discrete-time Markov chain
(states OK / FAIL) instead of i.i.d. Bernoulli -- a Gilbert-Elliott-style
burst overlay on the existing model, not a new physical mechanism. The one
new free parameter is the FAIL-state self-transition (persistence) r =
P(FAIL -> FAIL); the OK->FAIL transition q is then pinned by stationarity
so the chain's long-run failure fraction still equals p_e exactly. r is fit
by matching the chain's expected time-to-first-N-consecutive-failures
(N = ceil(T_sup / CI), the same N coexistence/model.py uses) to the
MEASURED supervision-timeout rate from measured_stats.py.

After N consecutive connected-state failures the link enters a
reconnecting state, where each connection *attempt* fails w.p. p_e**6
(reusing coexistence/model.py's own establishment-failure formula directly,
per the manuscript's own wording -- no burst overlay needed there, since
the manuscript doesn't claim one).
"""

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_WLAN_BLE_MODELING = Path(__file__).resolve().parents[2] / "wlan_ble_modeling"
if str(_WLAN_BLE_MODELING) not in sys.path:
    sys.path.insert(0, str(_WLAN_BLE_MODELING))

from coexistence.model import link_layer_consequences  # noqa: E402


def _expected_events_to_n_run(r: float, q: float, n_sup: int) -> float:
    """Expected number of events, starting fresh (state OK, run=0), until
    the first run of n_sup consecutive FAIL events, under a 2-state Markov
    chain with P(FAIL|FAIL)=r, P(FAIL|OK)=q.

    Solved via h_k = A_k + B_k*h_0 (h_k = expected additional events to
    absorption given a current FAIL-run of length k), recursed backward
    from the absorbing state h_{n_sup}=0 -- see link_model.py's module
    docstring / the design notes for the derivation. O(n_sup), exact (no
    simulation/Monte Carlo needed).
    """
    if n_sup <= 0:
        return 0.0
    a, b = 0.0, 0.0  # A_{n_sup}, B_{n_sup}
    for _ in range(n_sup - 1, 0, -1):
        a, b = 1.0 + r * a, r * b + (1.0 - r)
    # a, b are now A_1, B_1
    # h_1 = a + b*h_0 ; h_0 = 1/q + h_1  =>  h_0 (1 - b) = 1/q + a
    denom = 1.0 - b
    if denom <= 0:
        return math.inf
    return (1.0 / q + a) / denom


def _q_from_stationarity(p_e: float, r: float) -> float:
    """OK->FAIL transition rate that keeps the chain's stationary FAIL
    probability equal to p_e, given FAIL->FAIL persistence r. Detailed
    balance for a 2-state chain: pi_FAIL*(1-r) = pi_OK*q."""
    if p_e <= 0.0:
        return 0.0
    if p_e >= 1.0:
        return 1.0
    q = p_e * (1.0 - r) / (1.0 - p_e)
    return min(max(q, 0.0), 1.0)


def fit_burst_persistence(p_e: float, conn_interval_ms: float,
                           supervision_timeout_ms: float,
                           target_sup_timeouts_per_hour: float,
                           tol: float = 1e-6) -> float:
    """Find the FAIL-state persistence r whose implied supervision-timeout
    rate matches the measured one.

    NOT monotonic in r over its whole valid range [r_min, 1), where
    r_min = max(0, (2*p_e-1)/p_e) is the smallest r for which the OK->FAIL
    rate q (pinned by stationarity to keep the marginal failure probability
    at p_e) is still <= 1. Verified numerically: the timeout rate rises
    from r_min to a peak (very short, frequent OK excursions interrupted by
    just-long-enough FAIL runs) and then FALLS back toward 0 as r -> 1
    (excursions get longer but the chain almost never leaves OK to start
    one). A target rate below the peak therefore has two solutions; this
    picks the one on the RISING branch (closer to r_min) as the physically
    preferred "moderately bursty" regime, rather than the falling branch's
    near-degenerate "vanishingly rare, extremely long bursts" regime.

    If the target exceeds the achievable peak (shouldn't happen for the
    measured Corsano/Polar rates -- checked: peaks are ~513/hr and ~390/hr
    vs. targets ~218/hr and ~342/hr -- but a future device could differ),
    clamps to the peak and warns rather than returning a wrong answer.
    """
    import warnings

    n_sup = math.ceil(supervision_timeout_ms / conn_interval_ms)
    ci_s = conn_interval_ms / 1000.0
    r_min = max(0.0, (2.0 * p_e - 1.0) / p_e) if p_e > 0 else 0.0

    def rate_at(r):
        q = _q_from_stationarity(p_e, r)
        events = _expected_events_to_n_run(r, q, n_sup)
        seconds = events * ci_s
        return 3600.0 / seconds if math.isfinite(seconds) and seconds > 0 else 0.0

    # Coarse scan (geometric in 1-r, since the interesting structure is
    # squeezed close to r=1) to bracket the peak.
    xs = [r_min + (1.0 - r_min) * (1.0 - 10.0 ** (-0.01 * t)) for t in range(1, 700)]
    rates = [rate_at(r) for r in xs]
    peak_i = max(range(len(rates)), key=lambda i: rates[i])
    r_peak, rate_peak = xs[peak_i], rates[peak_i]

    if target_sup_timeouts_per_hour >= rate_peak:
        warnings.warn(
            f"target supervision-timeout rate {target_sup_timeouts_per_hour:.1f}/hr "
            f"exceeds this 2-state model's achievable peak ({rate_peak:.1f}/hr at "
            f"r={r_peak:.6f}, p_e={p_e:.3f}) -- clamping to the peak rather than "
            "returning a fabricated fit; the burst model underestimates the "
            "measured burstiness for this device."
        )
        return r_peak

    # Bisect on the rising branch [r_min, r_peak].
    lo, hi = r_min, r_peak
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if rate_at(mid) < target_sup_timeouts_per_hour:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


@dataclass
class LinkModelParams:
    p_event_fail: float
    conn_interval_ms: float
    supervision_timeout_ms: float
    burst_persistence_r: float
    establishment_events: int = 6

    @property
    def n_sup_events(self) -> int:
        return math.ceil(self.supervision_timeout_ms / self.conn_interval_ms)

    @property
    def q_ok_to_fail(self) -> float:
        return _q_from_stationarity(self.p_event_fail, self.burst_persistence_r)

    def predicted_sup_timeouts_per_hour(self) -> float:
        events = _expected_events_to_n_run(self.burst_persistence_r, self.q_ok_to_fail, self.n_sup_events)
        seconds = events * (self.conn_interval_ms / 1000.0)
        return 3600.0 / seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def fit(device: str, p_event_fail: float, conn_interval_ms: float,
        supervision_timeout_ms: float, measured_sup_timeouts_per_hour: float) -> LinkModelParams:
    r = fit_burst_persistence(p_event_fail, conn_interval_ms, supervision_timeout_ms,
                               measured_sup_timeouts_per_hour)
    return LinkModelParams(p_event_fail, conn_interval_ms, supervision_timeout_ms, r)


class LinkModel:
    """A live, steppable instance of the fitted link process. `.step()` is
    called once per connection event (connected state) or once per
    connection attempt (reconnecting state) and returns whether data got
    through this step -- this is the pull-based shape link_shim.py needs to
    drive a real-time ROS 2 timer."""

    CONNECTED = "connected"
    RECONNECTING = "reconnecting"

    def __init__(self, params: LinkModelParams, rng: Optional[random.Random] = None):
        self.params = params
        self.rng = rng or random.Random()
        self.state = self.CONNECTED
        self._run_length = 0  # consecutive connected-state failures

    def step(self) -> dict:
        p = self.params
        if self.state == self.CONNECTED:
            fail_prob = p.burst_persistence_r if self._run_length > 0 else p.q_ok_to_fail
            failed = self.rng.random() < fail_prob
            if failed:
                self._run_length += 1
                if self._run_length >= p.n_sup_events:
                    self.state = self.RECONNECTING
                    self._run_length = 0
                    return {"state": self.CONNECTED, "delivered": False,
                            "supervision_timeout": True}
                return {"state": self.CONNECTED, "delivered": False, "supervision_timeout": False}
            self._run_length = 0
            return {"state": self.CONNECTED, "delivered": True, "supervision_timeout": False}

        # RECONNECTING: each attempt fails w.p. p_e**6 (establishment-failure
        # formula), reused directly rather than re-derived.
        p_attempt_fail = p.p_event_fail ** p.establishment_events
        if self.rng.random() < p_attempt_fail:
            return {"state": self.RECONNECTING, "delivered": False, "reconnected": False}
        self.state = self.CONNECTED
        self._run_length = 0
        return {"state": self.RECONNECTING, "delivered": False, "reconnected": True}


if __name__ == "__main__":
    from . import measured_stats as ms

    for device, ci_ms, tsup_ms in (("Corsano", 60.0, 5000.0), ("Polar_H10", 45.0, 6000.0)):
        p_e = ms.mean_p_event_fail(device)
        target = ms.mean_sup_timeouts_per_hour(device)
        params = fit(device, p_e, ci_ms, tsup_ms, target)
        print(f"{device}: p_e={p_e:.3f} r={params.burst_persistence_r:.5f} "
              f"predicted={params.predicted_sup_timeouts_per_hour():.1f}/hr "
              f"(target={target:.1f}/hr) "
              f"independent-model-would-predict="
              f"{link_layer_consequences(p_e, ci_ms, tsup_ms, retransmit_budget_events=3)['sup_timeouts_per_hour']:.1f}/hr")
