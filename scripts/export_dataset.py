#!/usr/bin/env python3
"""
Export sessions for publication (Zenodo): a cleaned copy, one zip per session
==============================================================================

The sessions in captures/ are never modified. Each one is copied to
<out>/staging/<session>/ and the copy is cleaned:

  - session.log: libraries' DEBUG lines are dropped. Before session_lib's
    _OwnDebugOnly filter, bleak/dbus-fast logged every D-Bus signal there,
    with each nearby device's address and name (bystanders in the gym).
    Any other address that isn't ours becomes <other device>. Checked:
    analyze_session.parse_log reads exactly the same from the copy.
  - Researcher identifiers in text files: /home/<user> -> ~, the laptop's
    hostname -> host, its Bluetooth address -> 02:00:00:00:00:01, the nRF
    sniffer's USB serial -> SERIAL.
  - *.btsnoop: the laptop's Bluetooth address (same-length byte swap; the
    format has no checksums). Checked: parse_capture_file gives the same
    metrics.
  - rosbags: /rosout's `file` field (driver source paths under /home/<user>).
    Checked: every message keeps its topic, receive time and bytes, except
    those /rosout fields.

Every check must pass, otherwise the export stops. The zips, README.md and
SHA256SUMS.txt are written to <out>/.

Usage (needs ROS 2 and the driver workspace sourced: rewritten rosbags embed
the healthcare_msgs message definitions):
    source /opt/ros/jazzy/setup.bash && source $ROS_WS/install/setup.bash
    python3 scripts/export_dataset.py --out ~/zenodo captures/20261007_105222_chamber_1 ...
"""

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from analyze_session import parse_log
from privacy_scrub import MAC_RE
from session_lib import load_config
from session_lib.btmon_parse import parse_capture_file

TEXT_SUFFIXES = {".log", ".json", ".txt", ".csv", ".patch", ".yaml", ".md"}
LAPTOP_PLACEHOLDER = "02:00:00:00:00:01"
HOME_RE = re.compile(r"/home/[A-Za-z0-9._-]+")
SNIFFER_SERIAL_RE = re.compile(r"(nRF_Sniffer_for_Bluetooth_LE_)([0-9A-F]{8,})")
DEV_PATH_RE = re.compile(r"\bdev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})\b")  # BlueZ object paths


def _mac_bytes_le(mac):
    return bytes(int(x, 16) for x in reversed(mac.split(":")))


def session_identifiers(sdir):
    """(hostname, laptop Bluetooth address) from the session's environment.json."""
    env = json.loads((sdir / "environment.json").read_text())
    host = env["host"]["hostname"]
    m = re.search(r"BD Address: ((?:[0-9A-F]{2}:){5}[0-9A-F]{2})", env["bluetooth_controller"]["hciconfig"])
    return host, m.group(1)


def replace_identifiers(text, host, laptop):
    text = HOME_RE.sub("~", text)
    text = re.sub(rf"\b{re.escape(host)}\b", "host", text)
    text = re.sub(re.escape(laptop), LAPTOP_PLACEHOLDER, text, flags=re.I)
    text = text.replace(laptop.replace(":", "_"), LAPTOP_PLACEHOLDER.replace(":", "_"))
    return SNIFFER_SERIAL_RE.sub(r"\1SERIAL", text)


def clean_session_log(text, allowed):
    """Drop libraries' DEBUG lines (session_lib's own DEBUG output is always
    indented); any address that isn't ours -> <other device>."""
    out = []
    for line in text.splitlines(keepends=True):
        _, sep, msg = line.partition(" [DEBUG] ")
        if sep and not msg[:1].isspace():
            continue
        line = MAC_RE.sub(lambda m: m.group(0) if m.group(0).upper() in allowed else "<other device>", line)
        out.append(DEV_PATH_RE.sub(
            lambda m: m.group(0) if m.group(1).replace("_", ":").upper() in allowed else "dev_<other device>", line))
    return "".join(out)


def foreign_addresses(text, allowed):
    return ({m.upper() for m in MAC_RE.findall(text)}
            | {m.replace("_", ":").upper() for m in DEV_PATH_RE.findall(text)}) - allowed


def rewrite_rosbag(src, dst):
    """Copy a rosbag, replacing /home/<user> in /rosout's `file` field.
    Returns the number of /rosout messages changed."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message, serialize_message
    from rcl_interfaces.msg import Log

    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(src), storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
    topics = reader.get_all_topics_and_types()
    shutil.rmtree(dst, ignore_errors=True)
    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(dst), storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
    for t in topics:
        writer.create_topic(t)
    changed = 0
    while reader.has_next():
        topic, data, t = reader.read_next()
        if topic == "/rosout" and HOME_RE.search(data.decode("latin-1")):
            msg = deserialize_message(data, Log)
            msg.file, msg.msg = HOME_RE.sub("~", msg.file), HOME_RE.sub("~", msg.msg)
            data, changed = serialize_message(msg), changed + 1
        writer.write(topic, data, t)
    del writer
    return changed


def check_rosbag(src, dst):
    """Same topics and, message by message, the same topic, receive time and
    bytes -- except /rosout, which may differ only in file/msg paths."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rcl_interfaces.msg import Log

    def messages(uri):
        r = rosbag2_py.SequentialReader()
        r.open(rosbag2_py.StorageOptions(uri=str(uri), storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
        types = sorted((t.name, t.type) for t in r.get_all_topics_and_types())
        out = []
        while r.has_next():
            out.append(r.read_next())
        return types, out

    (ta, a), (tb, b) = messages(src), messages(dst)
    assert ta == tb, f"{dst}: topics differ"
    assert len(a) == len(b), f"{dst}: {len(a)} vs {len(b)} messages"
    for (topic, da, t), (topic2, db, t2) in zip(a, b):
        assert (topic, t) == (topic2, t2), f"{dst}: message order/time differs"
        if da != db:
            assert topic == "/rosout", f"{dst}: {topic} message bytes differ"
            ma, mb = deserialize_message(da, Log), deserialize_message(db, Log)
            ma.file, ma.msg = HOME_RE.sub("~", ma.file), HOME_RE.sub("~", ma.msg)
            assert (ma.stamp, ma.level, ma.name, ma.msg, ma.file, ma.function, ma.line) == \
                   (mb.stamp, mb.level, mb.name, mb.msg, mb.file, mb.function, mb.line), f"{dst}: /rosout differs"


def export_session(src, staging, cfg):
    dst = staging / src.name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.zip", "*.tmp"))
    host, laptop = session_identifiers(src)
    allowed = {d["mac_address"].upper() for d in cfg["devices"] if d.get("mac_address")}
    allowed |= {cfg["row2_duty_cycle"]["iperf"]["qbss_bssid"].upper(), LAPTOP_PLACEHOLDER,
                "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}
    log = []

    for p in sorted(dst.rglob("*")):
        if p.is_file() and p.suffix in TEXT_SUFFIXES:
            text = p.read_text(errors="replace")
            new = replace_identifiers(text, host, laptop)
            if p.name == "session.log":
                new = clean_session_log(new, allowed)
                assert parse_log(str(src / "session.log")) == _parse_text(new), f"{p}: parse_log changed"
                left = foreign_addresses(new, allowed)
                assert not left, f"{p}: {len(left)} foreign addresses remain"
                log.append(f"session.log: {len(text.splitlines()) - len(new.splitlines())} lines dropped")
            if new != text:
                p.write_text(new)

    old, new_b = _mac_bytes_le(laptop), _mac_bytes_le(LAPTOP_PLACEHOLDER)
    manifest = {}
    with open(src / "manifest.csv") as f:
        for r in csv.DictReader(f):
            manifest[r["capture_file"]] = (r.get("mac_address") or None, r.get("duration_s"))
    n_bt = 0
    for p in sorted(dst.glob("*.btsnoop")):
        raw = p.read_bytes()
        if old in raw:
            p.write_bytes(raw.replace(old, new_b))
            mac, dur = manifest.get(p.name, (None, None))
            a = parse_capture_file(src / p.name, mac_address=mac, planned_duration_s=dur)
            b = parse_capture_file(p, mac_address=mac, planned_duration_s=dur)
            assert a == b, f"{p}: parse_capture_file metrics changed"
            n_bt += 1
    log.append(f"btsnoop: laptop address replaced in {n_bt} files, metrics identical")

    n_bags = n_msgs = 0
    for bag in sorted(dst.glob("*_rosbag")):
        raw = b"".join(f.read_bytes() for f in bag.glob("*.mcap"))
        if not HOME_RE.search(raw.decode("latin-1")):
            continue
        with tempfile.TemporaryDirectory(dir=staging) as td:
            tmp = Path(td) / bag.name  # same name, so the .mcap inside keeps its name
            n_msgs += rewrite_rosbag(src / bag.name, tmp)
            check_rosbag(src / bag.name, tmp)
            shutil.rmtree(bag)
            shutil.move(str(tmp), str(bag))
        n_bags += 1
    log.append(f"rosbags: /rosout paths replaced in {n_msgs} messages in {n_bags} bags, all else identical")
    return dst, log


def _parse_text(text):
    """parse_log on text without writing it next to the session."""
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as t:
        t.write(text)
    try:
        return parse_log(t.name)
    finally:
        Path(t.name).unlink()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    try:
        import healthcare_msgs  # noqa: F401
    except ImportError:
        sys.exit("healthcare_msgs not found: source $ROS_WS/install/setup.bash first "
                 "(rewritten rosbags would lose their message definitions)")
    cfg = load_config(args.config)
    staging = args.out / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    for src in args.sessions:
        print(f"== {src.name}")
        dst, log = export_session(src.resolve(), staging, cfg)
        for line in log:
            print("  " + line)
        z = args.out / f"{src.name}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(dst.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(staging))
        print(f"  -> {z} ({z.stat().st_size / 1e6:.0f} MB)")
    with open(args.out / "SHA256SUMS.txt", "w") as f:
        for z in sorted(args.out.glob("*.zip")):
            f.write(f"{sha256(z)}  {z.name}\n")


if __name__ == "__main__":
    sys.exit(main())
