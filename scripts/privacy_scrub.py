#!/usr/bin/env python3
"""
Remove bystanders' Bluetooth addresses and names from a session's files
========================================================================

While a driver looks for its sensor, BlueZ scans, and btmon records every
advertiser the laptop hears (HCI LE advertising reports, BR/EDR inquiry
results, MGMT Device Found events with names). The Polar driver also
prints every scanned device, with names, to its driver.log every 5 s
while it can't find the sensor. In a gym those are patients' and staff's
phones and wearables (GDPR / Swiss FADP).

For every *.btsnoop in a session directory:
  1. write a filtered copy that drops every record carrying a Bluetooth
     address that isn't ours (our sensors from config.yaml, this laptop's
     controller);
  2. check the filtered copy gives exactly the same parse_capture_file()
     metrics as the raw file, and that `btmon -r` on it shows no foreign
     address;
  3. only then replace the raw file with it (the raw file is gone).
A file that fails a check is left untouched and reported.

For every *_driver.log: scanned-device lists become a count, and any other
foreign address is replaced by <other device>.

Idempotent: an already-clean file is left as it is.

Usage:
    python3 scripts/privacy_scrub.py captures/gym_20261012_165947 [more dirs...]
    python3 scripts/privacy_scrub.py --dry-run captures/...      # report only

gym_session.py runs this after every run automatically.
"""

import argparse
import csv
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from session_lib import load_config
from session_lib.btmon_parse import parse_capture_file

BTSNOOP_MAGIC = b"btsnoop\0"
DATALINK_MONITOR = 2001
HEADER_LEN = 16
RECORD_HEADER = struct.Struct(">IIIIq")  # orig len, incl len, flags, drops, timestamp

# btmon monitor-format opcodes (BlueZ src/shared/btsnoop.h)
OP_COMMAND, OP_EVENT, OP_CTRL_COMMAND, OP_CTRL_EVENT = 2, 3, 16, 17
MGMT_DEVICE_FOUND = 0x0012
MGMT_COMMAND_COMPLETE = 0x0001

# BlueZ management (MGMT) commands whose parameters -- and whose Command
# Complete return parameters -- start with a device address (mgmt-api.txt).
# 2026-10-13 (gym_20261013_093610): during BR/EDR discovery bluetoothd sent
# Confirm Name (0x0025) for a bystander's phone; only Device Found was
# covered before.
MGMT_ADDR_COMMANDS = {
    0x0014,  # Disconnect
    0x0016, 0x0017,  # PIN Code Reply / Negative Reply
    0x0019, 0x001A, 0x001B,  # Pair Device / Cancel Pair / Unpair Device
    0x001C, 0x001D, 0x001E, 0x001F, 0x0020,  # User Confirm/Passkey replies
    0x0025,  # Confirm Name
    0x0026, 0x0027,  # Block / Unblock Device
    0x0031, 0x0032,  # Get Connection / Clock Information
    0x0033, 0x0034,  # Add / Remove Device
}
# MGMT events whose parameters start with a device address.
MGMT_ADDR_EVENTS = {
    0x000B, 0x000C, 0x000D,  # Device Connected / Disconnected / Connect Failed
    0x000E, 0x000F, 0x0010, 0x0011,  # PIN / User Confirm / Passkey requests, Auth Failed
    MGMT_DEVICE_FOUND,
    0x0014, 0x0015, 0x0016,  # Device Blocked / Unblocked / Unpaired
    0x0017,  # Passkey Notify
    0x001A, 0x001B, 0x001C,  # Device Added / Removed / New Connection Parameter
}

MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")

ALWAYS_ALLOWED = {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}


def _addr(b):
    return ":".join(f"{x:02X}" for x in reversed(b))


def controller_addresses():
    """This laptop's own Bluetooth controller address(es)."""
    out = set()
    try:
        text = subprocess.run(["hciconfig"], capture_output=True, text=True, timeout=5).stdout
        out |= {m.upper() for m in re.findall(r"BD Address: ((?:[0-9A-F]{2}:){5}[0-9A-F]{2})", text)}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def allowed_addresses(cfg, extra=()):
    allowed = set(ALWAYS_ALLOWED) | controller_addresses() | {a.upper() for a in extra}
    allowed |= {d["mac_address"].upper() for d in cfg.get("devices", []) if d.get("mac_address")}
    return allowed


def _event_addresses(data):
    """Bluetooth addresses of OTHER devices an HCI event can carry, or
    None if this event type never carries one."""
    if len(data) < 2:
        return None
    code, params = data[0], data[2:]
    try:
        if code == 0x3E and params:  # LE Meta
            sub, p = params[0], params[1:]
            if sub == 0x02:  # LE Advertising Report
                n, i, addrs = p[0], 1, []
                for _ in range(n):
                    addrs.append(_addr(p[i + 2:i + 8]))
                    i += 8 + 1 + p[i + 8] + 1
                return addrs
            if sub == 0x0D:  # LE Extended Advertising Report
                n, i, addrs = p[0], 1, []
                for _ in range(n):
                    addrs.append(_addr(p[i + 3:i + 9]))
                    addrs.append(_addr(p[i + 17:i + 23]))  # direct address
                    i += 23 + 1 + p[i + 23]
                return addrs
            if sub == 0x0B:  # LE Directed Advertising Report
                n, addrs = p[0], []
                for k in range(n):
                    o = 1 + k * 16
                    addrs += [_addr(p[o + 2:o + 8]), _addr(p[o + 9:o + 15])]
                return addrs
            return None
        if code in (0x02, 0x22, 0x2F):  # BR/EDR inquiry results
            n = params[0]
            step = {0x02: 14, 0x22: 14, 0x2F: 254}[code]
            return [_addr(params[1 + k * step:7 + k * step]) for k in range(n)]
        if code == 0x07:  # Remote Name Request Complete (carries a name)
            return [_addr(params[1:7])]
    except IndexError:
        return ["<malformed>"]
    return None


def foreign_in_record(opcode, data, allowed):
    """True if this btsnoop monitor record names a device that isn't ours."""
    if opcode == OP_EVENT:
        addrs = _event_addresses(data)
    elif opcode == OP_CTRL_EVENT and len(data) >= 12:
        # cookie(4) + mgmt event code(2) + params
        code = int.from_bytes(data[4:6], "little")
        if code in MGMT_ADDR_EVENTS:
            addrs = [_addr(data[6:12])]
        elif code == MGMT_COMMAND_COMPLETE and len(data) >= 15:
            # params: command opcode(2) + status(1) + return params
            cmd = int.from_bytes(data[6:8], "little")
            addrs = [_addr(data[9:15])] if cmd in MGMT_ADDR_COMMANDS else None
        else:
            addrs = None
    elif opcode == OP_CTRL_COMMAND and len(data) >= 12:
        # cookie(4) + mgmt command opcode(2) + params
        code = int.from_bytes(data[4:6], "little")
        addrs = [_addr(data[6:12])] if code in MGMT_ADDR_COMMANDS else None
    elif opcode == OP_COMMAND and len(data) >= 9:
        ogf_ocf = int.from_bytes(data[0:2], "little")
        addrs = [_addr(data[3:9])] if ogf_ocf == 0x0419 else None  # Remote Name Request
    else:
        addrs = None
    return bool(addrs) and any(a not in allowed for a in addrs)


def own_random_addresses(raw):
    """Addresses this laptop set for itself with LE Set Random Address
    (BlueZ uses a fresh non-resolvable one for each scan): ours, not a
    bystander's."""
    own, i = set(), HEADER_LEN
    while i + RECORD_HEADER.size <= len(raw):
        _, incl, flags, _, _ = RECORD_HEADER.unpack_from(raw, i)
        data = raw[i + RECORD_HEADER.size:i + RECORD_HEADER.size + incl]
        if flags & 0xFFFF == OP_COMMAND and len(data) >= 9 and data[0:2] == b"\x05\x20":
            own.add(_addr(data[3:9]))
        i += RECORD_HEADER.size + incl
    return own


def filter_btsnoop(src, dst, allowed):
    """Copy src to dst without records that name foreign devices.
    Returns (kept, dropped, own_random) -- own_random being this laptop's
    own scan addresses, allowed too."""
    raw = Path(src).read_bytes()
    if raw[:8] != BTSNOOP_MAGIC:
        raise ValueError(f"{src}: not a btsnoop file")
    datalink = struct.unpack(">I", raw[12:16])[0]
    if datalink != DATALINK_MONITOR:
        raise ValueError(f"{src}: datalink {datalink}, expected btmon's monitor format")
    own = own_random_addresses(raw)
    allowed = allowed | own
    out = bytearray(raw[:HEADER_LEN])
    i, kept, dropped = HEADER_LEN, 0, 0
    while i + RECORD_HEADER.size <= len(raw):
        _, incl, flags, _, _ = RECORD_HEADER.unpack_from(raw, i)
        end = i + RECORD_HEADER.size + incl
        data = raw[i + RECORD_HEADER.size:end]
        if foreign_in_record(flags & 0xFFFF, data, allowed):
            dropped += 1
        else:
            out += raw[i:end]
            kept += 1
        i = end
    Path(dst).write_bytes(bytes(out))
    return kept, dropped, own


def _btmon_misdecoded(allowed):
    """btmon 5.72 decodes MGMT New Connection Parameter (0x001c) one byte
    off (store hint before the address), so OUR device's address prints
    shifted: A0:9E:1A:DF:7F:6C, type 1 -> 01:A0:9E:1A:DF:7F. Checked
    against the raw bytes 2026-10-12. Only these exact variants of our own
    addresses are accepted, nothing broader."""
    out = set()
    for mac in allowed:
        le = bytes(int(x, 16) for x in reversed(mac.split(":")))
        for addr_type in (0, 1, 2):
            out.add(_addr(le[1:6] + bytes([addr_type])))
    return out


def btmon_foreign_addresses(path, allowed):
    allowed = allowed | _btmon_misdecoded(allowed)
    text = subprocess.run(["btmon", "-r", str(path)], capture_output=True, text=True,
                          errors="replace", timeout=300).stdout
    return {m.upper() for m in MAC_RE.findall(text)} - allowed


def _manifest_info(session_dir):
    info = {}
    try:
        with open(Path(session_dir) / "manifest.csv") as f:
            for r in csv.DictReader(f):
                info[r["capture_file"]] = (r.get("mac_address") or None, r.get("duration_s"))
    except OSError:
        pass
    return info


def scrub_btsnoop(path, allowed, mac=None, duration_s=None, dry_run=False):
    """Returns a one-line status string."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".btsnoop.tmp", delete=False) as t:
        tmp = Path(t.name)
    try:
        kept, dropped, own = filter_btsnoop(path, tmp, allowed)
        if dropped == 0:
            return f"clean        {path.name}"
        if dry_run:
            return f"would drop   {path.name}: {dropped} of {kept + dropped} records"
        before = parse_capture_file(path, mac_address=mac, planned_duration_s=duration_s)
        after = parse_capture_file(tmp, mac_address=mac, planned_duration_s=duration_s)
        diff = {k for k in before if before[k] != after.get(k)}
        if diff:
            return f"KEPT RAW     {path.name}: metrics changed ({sorted(diff)[:5]}) -- review by hand"
        left = btmon_foreign_addresses(tmp, allowed | own)
        if left:
            return f"KEPT RAW     {path.name}: {len(left)} foreign address(es) remain after filtering"
        os.replace(tmp, path)
        tmp = None
        return f"scrubbed     {path.name}: dropped {dropped} of {kept + dropped} records, metrics identical"
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def scrub_text_log(path, allowed, dry_run=False):
    """Any line listing scanned devices (bleak's BLEDevice(...) entries --
    the Polar driver prints them every 5 s while it can't find its sensor)
    becomes a count, whatever the names contain; any other foreign address
    -> <other device>."""
    path = Path(path)
    text = path.read_text(errors="replace")
    out = []
    for line in text.splitlines(keepends=True):
        n = line.count("BLEDevice(")
        if n:
            end = "\n" if line.endswith("\n") else ""
            prefix = line.split("[", 1)[0] if "[" in line else ""
            line = f"{prefix}[{n} nearby devices -- addresses and names removed]{end}"
        out.append(MAC_RE.sub(
            lambda m: m.group(0) if m.group(0).upper() in allowed else "<other device>", line))
    new = "".join(out)
    if new == text:
        return f"clean        {path.name}"
    if not dry_run:
        path.write_text(new)
    return f"{'would scrub' if dry_run else 'scrubbed    '} {path.name}"


def scrub_session(session_dir, cfg, dry_run=False, only=None, quiet=False):
    """Scrub every btsnoop and driver log in session_dir (or only the
    files whose names start with `only`). Returns the status lines."""
    session_dir = Path(session_dir)
    allowed = allowed_addresses(cfg)
    info = _manifest_info(session_dir)
    lines = []
    for p in sorted(session_dir.glob("*.btsnoop")):
        if only and not p.name.startswith(only):
            continue
        mac, dur = info.get(p.name, (None, None))
        try:
            lines.append(scrub_btsnoop(p, allowed, mac, dur, dry_run))
        except Exception as e:  # noqa: BLE001 -- report and carry on with the rest
            lines.append(f"ERROR        {p.name}: {e}")
    for p in sorted(session_dir.glob("*_driver.log")):
        if only and not p.name.startswith(only):
            continue
        lines.append(scrub_text_log(p, allowed, dry_run))
    if not quiet:
        for line in lines:
            print(line)
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dirs", nargs="+")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    bad = 0
    for d in args.session_dirs:
        print(f"== {d}")
        lines = scrub_session(d, cfg, dry_run=args.dry_run)
        bad += sum(l.startswith(("KEPT RAW", "ERROR")) for l in lines)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
