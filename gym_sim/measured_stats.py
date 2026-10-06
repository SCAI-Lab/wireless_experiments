"""Real measured link stats for the forced-channel ("no AFH headroom")
condition -- the fitting input for link_model.py and duty_interpolation.py.

Source: captures/20260923_112431/results.csv (session 20260923_112431, Row 6
at the time it was captured, now Row 3 -- forced_channel_no_load /
forced_channel_max_load). These are the exact runs behind Table tab:forced
in medwear_manuscript_v3.tex. That results.csv has disconnect_count /
unexpected_disconnect_count per run but does NOT break disconnects down by
HCI reason (0x3E connection-failed-to-establish vs 0x08 supervision-timeout)
-- parse_btmon_text (session_lib/btmon_parse.py) never extracts the reason code, only
whether a disconnect happened. The manuscript's Table 3 numbers were
derived by a separate, already-vetted analysis; rather than re-derive them
here (which would require extending parse_btmon_text to pull HCI reason
codes out of the raw .btsnoop captures -- real, but out of scope for this
pass), this module hardcodes those published numbers as the fitting target
and cites exactly where each one came from, so it's a citation, not a
guess.

Once row3_forced_channel's new dose-response conditions (duty25/50/75pct,
added 2026-09-23) are actually run in the chamber, results.csv will have
rows for them, but still without the 0x3E/0x08 breakdown -- extending
parse_btmon_text to extract HCI disconnect reasons is a real prerequisite
for validate_held_out.py to use real intermediate data for anything beyond
connected_duration_s. Flagged there, not solved here.
"""

from dataclasses import dataclass

import sys
from pathlib import Path

_WLAN_BLE_MODELING = Path(__file__).resolve().parents[2] / "wlan_ble_modeling"
if str(_WLAN_BLE_MODELING) not in sys.path:
    sys.path.insert(0, str(_WLAN_BLE_MODELING))

from coexistence.model import implied_p_from_establishment  # noqa: E402


@dataclass(frozen=True)
class ForcedChannelRun:
    device: str
    duty_pct: int          # WiFi load level, 0 or 100 for the data that exists today
    replicate: int
    connected_duration_s: float
    connection_attempts: int
    n_establishment_fail: int   # HCI 0x3E
    n_supervision_timeout: int  # HCI 0x08

    @property
    def p_event_fail(self) -> float:
        """Implied per-event failure probability from the establishment-
        failure ratio: p_establish_fail = p**6 (Core Spec Vol 6, Part B,
        4.5.2), inverted -- same formula the manuscript uses for p_hat_e."""
        return implied_p_from_establishment(self.n_establishment_fail, self.connection_attempts)

    @property
    def sup_timeouts_per_hour(self) -> float:
        if self.connected_duration_s <= 0:
            return float("nan")
        return self.n_supervision_timeout / self.connected_duration_s * 3600.0


# Table tab:forced (medwear_manuscript_v3.tex, "Constrained AFH (Data
# Channels 12-19), Full WiFi Load"). The two devices' no-load reps (0%) are
# not in that table (both were clean/near-100% connected, no meaningful
# failure stats to fit against) -- only the max-load (100%) reps have real
# burst dynamics worth fitting.
FORCED_CHANNEL_MAX_LOAD_RUNS = [
    ForcedChannelRun("Corsano", 100, 1, connected_duration_s=182.5,
                     connection_attempts=30, n_establishment_fail=22, n_supervision_timeout=7),
    ForcedChannelRun("Corsano", 100, 2, connected_duration_s=48.4,
                     connection_attempts=11, n_establishment_fail=8, n_supervision_timeout=4),
    ForcedChannelRun("Polar_H10", 100, 1, connected_duration_s=135.4,
                     connection_attempts=33, n_establishment_fail=17, n_supervision_timeout=13),
    ForcedChannelRun("Polar_H10", 100, 2, connected_duration_s=63.7,
                     connection_attempts=22, n_establishment_fail=16, n_supervision_timeout=6),
]
# Excluded, per the manuscript's own Table 3 footnote and session notes: the
# third replicate of each device's max-load condition never connected at all
# (connected_duration_s=0, 0 attempts recorded) -- a "never connected"
# outcome, not a "connected then degraded" one, so it carries no sojourn-
# time information and would corrupt a burst-persistence fit if included.


def runs_for_device(device: str) -> list:
    return [r for r in FORCED_CHANNEL_MAX_LOAD_RUNS if r.device == device]


def mean_p_event_fail(device: str) -> float:
    runs = runs_for_device(device)
    return sum(r.p_event_fail for r in runs) / len(runs)


def mean_sup_timeouts_per_hour(device: str) -> float:
    runs = runs_for_device(device)
    return sum(r.sup_timeouts_per_hour for r in runs) / len(runs)


if __name__ == "__main__":
    for device in ("Corsano", "Polar_H10"):
        print(f"{device}: p_e={mean_p_event_fail(device):.3f}  "
              f"sup_timeouts/hr={mean_sup_timeouts_per_hour(device):.1f}  "
              f"(n={len(runs_for_device(device))} runs)")
