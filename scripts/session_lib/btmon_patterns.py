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
# HCI handle numbers are assigned dynamically by the controller and are NOT
# fixed per device -- confirmed on real hardware, the same MAC got handle
# 2048 in one session and 2049 in another. Only the peer's MAC address
# reliably identifies which device a connect/disconnect event belongs to.
# Without this, a stray connection from a DIFFERENT device captured on the
# same shared radio (e.g. one that didn't disconnect cleanly from a prior
# test) gets silently attributed to whatever device this capture is
# nominally for -- confirmed as the actual cause of repeated
# "unexpected disconnect" failures previously misattributed to Polar_H10.
PEER_ADDRESS_RE = re.compile(r"Peer address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
HANDLE_ADDRESS_RE = re.compile(
    r"Handle:\s*\d+\s*Address:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
)
# The Disconnect Complete event's own reason line, e.g.
# "Reason: Connection Timeout (0x08)" -- NOT extracted before 2026-09-25
# (confirmed by grep: no such regex existed), so results.csv had no
# breakdown by HCI reason even though the manuscript's own Table tab:forced
# reports exactly this (0x3E connection-failed-to-establish vs. 0x08
# supervision-timeout counts). Added after manually cross-checking a real
# session's captures found a mix of both reasons within single forced-
# channel runs -- 0x08 for genuine drops of an established link, 0x3e for
# a subsequent failed reconnection attempt (the driver's own retry, not a
# second drop of the same connection).
REASON_RE = re.compile(r"Reason:\s+(\S.*?)\s*\(0x([0-9A-Fa-f]+)\)")
# A bond the sensor no longer honours is silently fatal AND mis-attributed:
# BlueZ re-uses its stored LTK on every connection ("LE Start Encryption"),
# the sensor never answers, and the link dies on its own supervision timeout,
# so Disconnect Complete reports reason 0x08 and the run is indistinguishable
# from the interference-induced loss these tests measure. Confirmed
# 2026-09-29 after a Polar H10 battery change cleared the bond on the strap
# while the host kept its key: every connection died 6.3s in (the negotiated
# 6000ms timeout), zero samples received, and the only honest signal was the
# Encryption Change event's own non-Success status. A bond itself is NOT the
# problem -- the Corsano watch is legitimately bonded and encrypts fine
# ("Encryption Change: Status Success, Enabled with AES-CCM") -- so detect the
# failed handshake, never the mere presence of a bond.
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
# [hci0] 25.429800". This is validated against real hardware captures in
# captures/*.btsnoop, not just a guess at the format.
ACL_RX_RE = re.compile(r"^> ACL Data RX: Handle (\d+)")
ACL_TX_RE = re.compile(r"^< ACL Data TX: Handle (\d+)")

# Polar H10 sample-completeness (deferred TODO from parse_capture_file,
# implemented once the actual wire format was confirmed against both the
# polar_ros2 driver source ($ROS_WS/src/ros4healthcare/ros2_hc_drv/
# polar_ros2/polar_ros2/submodules/PolarH10.py) and a real capture
# (captures/20260918_152906/row1_Polar_H10_baseline_rep1.btsnoop) -- e.g. a
# real ACC frame's Data: 026a8ac782b54e5208013ffcebffd6ff... decodes as
# byte0=0x02 (ACC), bytes1-8=6a8ac782b54e5208 (LE ns timestamp of the last
# sample in this frame), byte9=01 (frame_type -> 16 bit/axis), remainder =
# packed XYZ samples. This is real, physically-embedded data from the
# sensor's OWN internal clock -- unlike RX inter-arrival jitter (which only
# measures OUR reception timing), comparing consecutive frames' embedded
# timestamps against the known fixed sample rate reveals samples the
# sensor actually produced that never reached us at all: "no delivered
# samples lost" vs. the weaker "no gaps observed" RX jitter alone can
# support.
#
# Standard Heart Rate Measurement notifications (0x2A37) are also
# recoverable this way, though only as an approximate cross-check: Polar's
# firmware buffers RR-intervals and backfills multiple into the next
# notification if an earlier one didn't get sent, so the SUM of all
# received RR-intervals approximates real elapsed on-body time -- but the
# buffer isn't unlimited, so this can still underestimate loss under
# extreme/sustained delay. See compute_hr_completeness's docstring.
NOTIFICATION_RE = re.compile(r"ATT: Handle Value Notification")
NOTIFICATION_TYPE_RE = re.compile(r"Type:\s*(.+?)\s*$", re.MULTILINE)
NOTIFICATION_DATA_RE = re.compile(r"Data:\s*([0-9A-Fa-f]+)")
