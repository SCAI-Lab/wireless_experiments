"""Emulated devices publishing MEDWEAR-standard (healthcare_msgs) payloads
at each real device's measured rate and frame size (manuscript sec:sim-
design, "Emulated devices and MEDWEAR" paragraph; rates from devices.yaml:
Corsano 5.6 notif/s, Polar 7.7 notif/s -- HR figures used here since HR is
the one signal both devices' real bridge config already handles, see
ros2_to_medwear/config.yaml).

Publishes onto RAW topics (/gym_sim/<patient>/<device>/hr_raw), not the
real /corsano/hr or /polar_hr -- link_shim.py sits in between, applying the
fitted link process before republishing onto the real topic names that the
unmodified ros2_to_medwear/medwear_subscriber.py subscribes to. This is
what keeps "MEDWEAR's own processing real rather than modeled" (manuscript,
same paragraph): only the transport between emulator and bridge is
simulated, not the bridge itself.

Payload values are synthetic placeholders (a slowly varying plausible HR),
clearly not real patient data -- this emulates TRAFFIC (rate, framing,
timestamps), not physiology.
"""

import argparse
import math
import sys
import time
from pathlib import Path

import rclpy
from rclpy.node import Node

_WIRELESS_EXPERIMENTS = Path(__file__).resolve().parents[1]
if str(_WIRELESS_EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(_WIRELESS_EXPERIMENTS))

import yaml  # noqa: E402

_WLAN_BLE_MODELING = Path(__file__).resolve().parents[2] / "wlan_ble_modeling"


def _load_device_profile(device_name: str) -> dict:
    devices_yaml_name = {"Corsano": "Corsano_CardioWatch", "Polar_H10": "Polar_H10"}
    with open(_WLAN_BLE_MODELING / "devices.yaml") as f:
        devices = yaml.safe_load(f)["devices"]
    return next(d for d in devices if d["name"] == devices_yaml_name.get(device_name, device_name))


class EmulatedDeviceNode(Node):
    """One node per (patient, device) pair. Publishes an HR stream on a raw
    topic at the device's measured notification rate. Sequence numbers are
    carried in session_id (packed as "<patient_id>:<device>:<seq>") since
    healthcare_msgs/msg/HeartRate has no dedicated sequence field --
    link_shim.py and medwear_subscriber.py's staleness/gap instrumentation
    both parse it back out. A cleaner long-term fix would add a real
    sequence_number field to the schema; not done here to avoid touching
    the shared healthcare_msgs message definitions for a simulation-only
    need (see medwear_subscriber.py's instrumentation comment for the same
    trade-off, decided the same way there)."""

    def __init__(self, patient_id: str, device_name: str):
        super().__init__(f"gym_sim_emulated_{patient_id}_{device_name}".replace("-", "_"))
        from healthcare_msgs.msg import HeartRate

        self._HeartRate = HeartRate
        self.patient_id = patient_id
        self.device_name = device_name
        self.profile = _load_device_profile(device_name)
        self._seq = 0
        self._t0 = time.monotonic()

        topic = f"/gym_sim/{patient_id}/{device_name}/hr_raw"
        self.pub = self.create_publisher(HeartRate, topic, 10)

        rate_hz = float(self.profile.get("notifications_per_s", 1.0))
        self.timer = self.create_timer(1.0 / rate_hz, self._publish_once)
        self.get_logger().info(
            f"emulated {device_name} for patient {patient_id}: publishing {topic} "
            f"at {rate_hz:.2f} Hz (measured rate from devices.yaml)"
        )

    def _publish_once(self):
        msg = self._HeartRate()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.session_id = f"{self.patient_id}:{self.device_name}:{self._seq}"
        msg.sample_size = 1
        elapsed = time.monotonic() - self._t0
        # Slowly varying plausible HR (60-90 bpm) -- synthetic traffic, not physiology.
        msg.heart_rate = [75.0 + 10.0 * math.sin(elapsed / 30.0)]
        msg.quality = [0.95]
        self.pub.publish(msg)
        self._seq += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patient", required=True, help="Patient ID, e.g. patient-01")
    parser.add_argument("--device", required=True, choices=["Corsano", "Polar_H10"])
    args = parser.parse_args()

    rclpy.init()
    node = EmulatedDeviceNode(args.patient, args.device)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
