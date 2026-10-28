"""Regexes and constants for matching btmon text output."""

import re


CONN_COMPLETE_RE = re.compile(r"LE (?:Enhanced )?Connection Complete")
CONN_UPDATE_RE = re.compile(r"LE Connection Update Complete")
DISCONNECT_RE = re.compile(r"Disconnect Complete")
STATUS_RE = re.compile(r"Status:\s+(\S.*?)\s*\(0x[0-9A-Fa-f]+\)")
INTERVAL_RE = re.compile(r"Connection [Ii]nterval:\s+([\d.]+)\s*msec")
LATENCY_RE = re.compile(r"Connection [Ll]atency:\s+([\d.]+)\s*msec")
TIMEOUT_RE = re.compile(r"Supervision [Tt]imeout:\s+(\d+)\s*msec")
MTU_RE = re.compile(r"(?:Client RX MTU|MTU):\s+(\d+)")
HANDLE_RE = re.compile(r"Handle:\s*(\d+)")
# HCI handles are assigned dynamically by the controller, so only the peer's
# MAC address identifies which device a connect/disconnect event belongs to
# (another device's connection on the same radio must not be attributed to
# this capture's device).
PEER_ADDRESS_RE = re.compile(r"Peer address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
HANDLE_ADDRESS_RE = re.compile(
    r"Handle:\s*\d+\s*Address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
)
# The Disconnect Complete event's reason line, e.g.
# "Reason: Connection Timeout (0x08)": 0x08 is a drop of an established link,
# 0x3e a failed (re)connection attempt.
REASON_RE = re.compile(r"Reason:\s+(\S.*?)\s*\(0x([0-9A-Fa-f]+)\)")
# A bond the sensor no longer honours: BlueZ re-uses its stored LTK ("LE
# Start Encryption"), the sensor never answers, and the link dies on its
# supervision timeout (reason 0x08), indistinguishable from interference.
# The only signal is the Encryption Change event's non-Success status, so
# detect the failed handshake, not the presence of a bond (the Corsano is
# bonded and encrypts fine).
START_ENCRYPTION_RE = re.compile(r"^<.*\(0x08\|0x0019\)")
ENCRYPTION_STATE_RE = re.compile(r"^\s+Encryption:\s+(Enabled|Disabled)")
# btmon prints a timestamp at the end of each top-level packet header line,
# e.g. "> HCI Event: LE Meta Event (0x3e) plen 31   #325 [hci0] 11.661860".
# Sub-event detail lines (LE Enhanced Connection Complete, Status, Disconnect
# reason, etc.) that follow don't carry their own timestamp, so we track the
# most recently seen header timestamp and attach it to whatever comes after.
HEADER_TS_RE = re.compile(r"\[hci\d+\]\s+([\d.]+)\s*$")
# Every incoming ACL Data RX packet on the connection carries the sensor's
# GATT notifications (its actual data samples), each with its own timestamp
# on the same line, e.g. "> ACL Data RX: Handle 2048 flags 0x02 dlen 7  ...
# [hci0] 25.429800".
ACL_RX_RE = re.compile(r"^> ACL Data RX: Handle (\d+)")
ACL_TX_RE = re.compile(r"^< ACL Data TX: Handle (\d+)")

# Polar H10 PMD frames (format as in the polar_ros2 driver's PolarH10.py),
# e.g. ACC Data: 026a8ac782b54e5208013ffcebffd6ff... = byte0 0x02 (ACC),
# bytes1-8 LE ns timestamp of the frame's last sample, byte9 frame type
# (01: 16 bit/axis), then packed XYZ samples. Comparing consecutive frames'
# sensor timestamps with the fixed sample rate reveals samples the sensor
# produced that never reached the host.
#
# Heart Rate Measurement notifications (0x2A37): the sum of received
# RR-intervals approximates elapsed time (the firmware backfills buffered
# RR-intervals), an approximate cross-check; see compute_hr_completeness.
NOTIFICATION_RE = re.compile(r"ATT: Handle Value Notification")
NOTIFICATION_TYPE_RE = re.compile(r"Type:\s*(.+?)\s*$", re.MULTILINE)
NOTIFICATION_DATA_RE = re.compile(r"Data:\s*([0-9A-Fa-f]+)")
