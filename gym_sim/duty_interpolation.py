"""Predict p_e at any WiFi duty level, reusing wlan_ble_modeling's existing
closed-form model (coexistence/model.py) rather than reimplementing it --
per the manuscript's own wording (sec:sim-design, "Link process" paragraph):
"p_e ... when [AFH] does not [retain clean channels] follows the constrained
measurements across load, interpolated by the analytical model."

Key simplification specific to the forced-channel condition: FORCED_CHANNEL_
RANGE (BLE data channels 12-19) was deliberately chosen to be exactly the
set WiFi channel 6's occupied bandwidth overlaps (config_lab.yaml's
wifi_bandwidth_mhz=16.6 around 2437 MHz) -- so ALL forced channels are
WiFi-overlapped, i.e. afh_overlap_fraction == 1.0 for every run in this
condition. That collapses combined_collision_probability's frequency term
to 1, leaving p_e driven almost entirely by time_overlap_probability(t_ble,
wifi_sources) -- there's no "effective interference bandwidth" to calibrate
here (that parameter matters for the AFH-free / ideal_effective conditions,
not this one).

What genuinely needs calibrating is a DIFFERENT axis mismatch: chamber_
session.py's duty_levels are a THROUGHPUT fraction (% of measured max
Mbit/s), not the analytical model's `duty_cycle` (an airtime/busy-fraction
input to time_overlap_probability). The manuscript states this explicitly
as unconfirmed (sec:results-model): "consistent... IF the saturated UDP
load corresponds to roughly 75-90% airtime". This module fits exactly that
one free scale factor k (airtime_duty = k * throughput_pct/100) from the
one measured anchor point that exists today (100% throughput), then
evaluates the SAME closed-form formula at any other throughput level --
including the held-out 25/50/75% once real data lands. If k comes out
inside [0.75, 0.90], that's a consistency check against the manuscript's
own already-published plausibility range, not a new assumption.
"""

import math
import sys
from pathlib import Path
from typing import Optional

import yaml

_WLAN_BLE_MODELING = Path(__file__).resolve().parents[2] / "wlan_ble_modeling"
if str(_WLAN_BLE_MODELING) not in sys.path:
    sys.path.insert(0, str(_WLAN_BLE_MODELING))

from coexistence.model import (  # noqa: E402
    ble_packet_duration_s,
    combined_collision_probability,
    time_overlap_probability,
)

_CONFIG_LAB_PATH = _WLAN_BLE_MODELING / "config_lab.yaml"


def _load_lab_config() -> dict:
    with open(_CONFIG_LAB_PATH) as f:
        return yaml.safe_load(f)


def _load_device(name: str) -> dict:
    devices_path = _WLAN_BLE_MODELING / "devices.yaml"
    with open(devices_path) as f:
        devices = yaml.safe_load(f)["devices"]
    return next(d for d in devices if d["name"] == name)


def _t_ble_s(device: dict, ble_overhead_bytes: int) -> float:
    payload_bps = device.get("payload_bytes_per_s")
    if payload_bps is None:
        payload_bps = device["sample_rate_hz"] * device["bits_per_sample"] / 8.0
    return ble_packet_duration_s(
        payload_bytes_per_s=payload_bps,
        connection_interval_ms=device["connection_interval_ms"],
        overhead_bytes=ble_overhead_bytes,
        phy=device.get("phy", "1M"),
    )


def _p_e_at_airtime(device_name: str, airtime_duty: float,
                     lab_cfg: Optional[dict] = None) -> float:
    """p_e for the forced-channel condition (freq_overlap_frac == 1.0, see
    module docstring) at a given WiFi airtime duty cycle, using
    institutional_wifi's packet_duration_s from config_lab.yaml plus the
    device's own measured connection interval / payload rate."""
    lab_cfg = lab_cfg or _load_lab_config()
    device = _load_device(_DEVICES_YAML_NAME.get(device_name, device_name))
    overhead = lab_cfg["ble_overhead_bytes"]
    t_ble = _t_ble_s(device, overhead)

    wifi = lab_cfg["wifi_interferers"][0]  # institutional_wifi (the controlled source)
    sources = [{"duty_cycle": airtime_duty, "packet_duration_s": wifi["packet_duration_s"]}]
    ambient = lab_cfg["wifi_interferers"][1] if len(lab_cfg["wifi_interferers"]) > 1 else None
    if ambient and ambient.get("enabled"):
        sources.append({"duty_cycle": ambient["duty_cycle"], "packet_duration_s": ambient["packet_duration_s"]})

    p_time = time_overlap_probability(t_ble, sources)
    # freq_overlap_frac = 1.0 for the forced-channel condition (see module
    # docstring); ble_ble_contention is disabled in config_lab.yaml for
    # these single-device runs, so p_ble_ble = 0.
    return combined_collision_probability(freq_overlap_frac=1.0, p_time=p_time, p_ble_ble=0.0)


# devices.yaml names its Corsano entry "Corsano_CardioWatch"; the chamber's
# own config.yaml / results.csv use "Corsano" -- map between them here
# rather than renaming either file's convention.
_DEVICES_YAML_NAME = {"Corsano": "Corsano_CardioWatch", "Polar_H10": "Polar_H10"}


def calibrate_airtime_scale(device_name: str, measured_pe_by_throughput_pct: dict,
                             tol: float = 1e-6) -> float:
    """Fit k (airtime_duty = k * throughput_pct/100) from whichever
    throughput levels are in measured_pe_by_throughput_pct (today: just
    {100: 0.949} for Corsano / {100: 0.922} for Polar_H10). Bisection: p_e
    is monotonically increasing in airtime duty, so this is well-posed for
    a single calibration point (and least-squares-able later once more
    points exist, but a single point has an exact solution, not a fit)."""
    lab_cfg = _load_lab_config()
    # Use the highest available throughput level as the calibration anchor
    # -- it's the only one that exists today, and also the most informative
    # (closest to the model's steep, sensitive region near saturation).
    throughput_pct = max(measured_pe_by_throughput_pct)
    target_pe = measured_pe_by_throughput_pct[throughput_pct]

    def pe_at_k(k):
        airtime = min(max(k * throughput_pct / 100.0, 0.0), 1.0)
        return _p_e_at_airtime(device_name, airtime, lab_cfg)

    lo, hi = 0.0, 1.0
    if target_pe <= pe_at_k(lo):
        return lo
    if target_pe >= pe_at_k(hi):
        return hi
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if pe_at_k(mid) < target_pe:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def predict_pe(device_name: str, throughput_pct: float, airtime_scale_k: float) -> float:
    """p_e at any throughput level (including held-out 25/50/75%), given a
    previously calibrated airtime_scale_k."""
    airtime = min(max(airtime_scale_k * throughput_pct / 100.0, 0.0), 1.0)
    return _p_e_at_airtime(device_name, airtime)


if __name__ == "__main__":
    from . import measured_stats as ms

    for device in ("Corsano", "Polar_H10"):
        p_e_100 = ms.mean_p_event_fail(device)
        k = calibrate_airtime_scale(device, {100: p_e_100})
        print(f"{device}: measured p_e(100%)={p_e_100:.3f} -> "
              f"calibrated airtime_scale_k={k:.3f} "
              f"({'inside' if 0.75 <= k <= 0.90 else 'OUTSIDE'} the manuscript's own "
              f"guessed 0.75-0.90 plausibility range)")
        for pct in (0, 25, 50, 75, 90, 100):
            print(f"    throughput={pct:3d}% -> predicted p_e={predict_pe(device, pct, k):.3f}")
