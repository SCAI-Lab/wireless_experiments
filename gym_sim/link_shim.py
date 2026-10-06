"""The delay/drop shim in front of MEDWEAR's real ROS 2 path (manuscript
sec:sim-design: "A per-device shim applies the link process before messages
enter MEDWEAR's ROS2 path, so MEDWEAR's own processing is real rather than
modeled").

Subscribes to an emulated device's raw topic (/gym_sim/<patient>/<device>/
hr_raw) and republishes onto the REAL topic name the unmodified
ros2_to_medwear/medwear_subscriber.py subscribes to (/corsano/hr,
/polar_hr, ...) -- but only when the fitted LinkModel says this connection
event got through.

The LinkModel is stepped on its OWN timer at the device's measured
connection interval (the real BLE connection-event rate), independent of
the emulator's notification rate (which is slower) -- this matches the
manuscript's model, where failures happen at the connection-event level,
not the application-notification level. Each tick: if the link is
CONNECTED and this event succeeds, forward the most recently received raw
message (if any arrived since the last successful forward) onto the real
topic; if this event fails, hold it (BLE retransmits the same payload until
acknowledged -- "failed events delay delivery by whole connection
intervals"); if RECONNECTING, drop entirely (no data reaches the
controller, matching Table tab:forced's Polar rows).
"""

import argparse
import sys
from pathlib import Path

import rclpy
from rclpy.node import Node

_WIRELESS_EXPERIMENTS = Path(__file__).resolve().parents[1]
if str(_WIRELESS_EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(_WIRELESS_EXPERIMENTS))

from gym_sim import link_model as lm, measured_stats as ms  # noqa: E402

_REAL_TOPIC_BY_DEVICE = {"Corsano": "/corsano/hr", "Polar_H10": "/polar_hr"}
_CI_MS_BY_DEVICE = {"Corsano": 60.0, "Polar_H10": 45.0}
_TSUP_MS_BY_DEVICE = {"Corsano": 5000.0, "Polar_H10": 6000.0}


def _fit_params_for(device_name: str, forced_channel: bool) -> lm.LinkModelParams:
    """AFH-free devices (forced_channel=False) get p_e~0 (Row 2's real
    result: zero unexpected disconnects at every duty level up to
    saturation) -- no meaningful burst dynamics to fit, so a trivial
    always-good model is used. forced_channel=True fits the real burst
    model from measured_stats.py."""
    ci_ms = _CI_MS_BY_DEVICE[device_name]
    tsup_ms = _TSUP_MS_BY_DEVICE[device_name]
    if not forced_channel:
        return lm.LinkModelParams(p_event_fail=1e-6, conn_interval_ms=ci_ms,
                                   supervision_timeout_ms=tsup_ms, burst_persistence_r=0.0)
    p_e = ms.mean_p_event_fail(device_name)
    target = ms.mean_sup_timeouts_per_hour(device_name)
    return lm.fit(device_name, p_e, ci_ms, tsup_ms, target)


class LinkShimNode(Node):
    def __init__(self, patient_id: str, device_name: str, forced_channel: bool, rng_seed: int = None):
        super().__init__(f"gym_sim_shim_{patient_id}_{device_name}".replace("-", "_"))
        from healthcare_msgs.msg import HeartRate

        self.device_name = device_name
        params = _fit_params_for(device_name, forced_channel)
        import random
        self.link = lm.LinkModel(params, rng=random.Random(rng_seed))

        raw_topic = f"/gym_sim/{patient_id}/{device_name}/hr_raw"
        real_topic = _REAL_TOPIC_BY_DEVICE[device_name]

        self._pending_msg = None
        self._forwarded_count = 0
        self._dropped_count = 0
        self._held_count = 0

        self.pub = self.create_publisher(HeartRate, real_topic, 10)
        self.sub = self.create_subscription(HeartRate, raw_topic, self._on_raw, 10)
        self.timer = self.create_timer(params.conn_interval_ms / 1000.0, self._on_connection_event)

        self.get_logger().info(
            f"shim {device_name} ({patient_id}): {raw_topic} -> {real_topic}, "
            f"p_e={params.p_event_fail:.3f} r={params.burst_persistence_r:.4f} "
            f"CI={params.conn_interval_ms}ms forced_channel={forced_channel}"
        )

    def _on_raw(self, msg):
        self._pending_msg = msg

    def _on_connection_event(self):
        outcome = self.link.step()
        if outcome["state"] == lm.LinkModel.RECONNECTING:
            self._dropped_count += 1
            return
        if not outcome["delivered"]:
            self._held_count += 1
            return
        if self._pending_msg is not None:
            self.pub.publish(self._pending_msg)
            self._pending_msg = None
            self._forwarded_count += 1

    def stats(self) -> dict:
        return {"forwarded": self._forwarded_count, "held": self._held_count,
                "dropped_while_reconnecting": self._dropped_count,
                "link_state": self.link.state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patient", required=True)
    parser.add_argument("--device", required=True, choices=["Corsano", "Polar_H10"])
    parser.add_argument("--forced-channel", action="store_true",
                         help="Use the fitted no-AFH-headroom burst model instead of the AFH-free trivial model")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    rclpy.init()
    node = LinkShimNode(args.patient, args.device, args.forced_channel, rng_seed=args.seed)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info(f"final stats: {node.stats()}")
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
