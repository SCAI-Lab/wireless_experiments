"""Snapshot of the software and firmware a session ran on, written to
<session_dir>/environment.json at session start: OS/kernel, BlueZ and
other packages, Bluetooth controller and WiFi firmware, Python packages,
nRF sniffer tooling, git state of this repo and of each device driver, and
(over SSH) the iperf3 load client.

Best-effort throughout: every probe has a short timeout and records an
"error" string instead of raising, so a missing tool never blocks a run.

Standalone: python3 scripts/log_environment.py [OUT_DIR] [--config PATH]
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

from .logs import log

ENVIRONMENT_FILE = "environment.json"

DEB_PACKAGES = (
    "bluez",
    "linux-firmware",
    "iw",
    "iperf3",
    "rfkill",
    "wireless-regdb",
    "ros-jazzy-ros-base",
    "ros-jazzy-rosbag2",
)
PY_PACKAGES = ("bleak", "dbus-fast", "numpy", "PyYAML", "scipy", "pandas")


def _run(cmd, timeout=5):
    """stdout of cmd (stripped), or {"error": ...}. Never raises."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=isinstance(cmd, str),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if r.returncode != 0 and not r.stdout.strip():
        return {"error": (r.stderr.strip() or f"exit {r.returncode}")[:300]}
    return r.stdout.strip()


def _git_state(path):
    path = Path(path).expanduser()
    if not path.exists():
        return {"error": f"{path} not found"}
    out = {"path": str(path)}
    for key, args in (
        ("commit", ["rev-parse", "HEAD"]),
        ("branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
        ("describe", ["describe", "--always", "--dirty"]),
    ):
        out[key] = _run(["git", "-C", str(path), *args])
    status = _run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"])
    out["dirty"] = bool(status) if isinstance(status, str) else status
    return out


def _bt_controller(controller):
    info = {"hciconfig": _run(["hciconfig", "-a", controller])}
    uevent = Path(f"/sys/class/bluetooth/{controller}/device/uevent")
    if uevent.exists():
        info["usb"] = dict(
            line.split("=", 1) for line in uevent.read_text().splitlines() if "=" in line
        )
    # The firmware version is only reported in the kernel log when the
    # controller loads it (boot or resume), so search the current boot.
    klog = _run(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"], timeout=10)
    if isinstance(klog, str):
        pat = re.compile(rf"Bluetooth: {controller}: (Firmware|Found|Bootloader|Device revision|Secure boot|Minimum)")
        lines = [l for l in klog.splitlines() if pat.search(l)]
        info["firmware_kernel_log"] = lines[-12:] or "no firmware lines in this boot's kernel log"
    else:
        info["firmware_kernel_log"] = klog
    return info


def _wifi_ifaces():
    """ethtool -i (driver, firmware) for every wireless interface."""
    out = {}
    for dev in sorted(Path("/sys/class/net").iterdir()):
        if (dev / "wireless").exists() or (dev / "phy80211").exists():
            out[dev.name] = _run(["ethtool", "-i", dev.name])
    return out


def _sniffer():
    nrfutil = shutil.which("nrfutil") or str(Path.home() / ".nrfutil/bin/nrfutil")
    sniffer = shutil.which("nrfutil-ble-sniffer") or str(
        Path.home() / ".nrfutil/bin/nrfutil-ble-sniffer"
    )
    return {
        "nrfutil": _run([nrfutil, "--version"]),
        "nrfutil-ble-sniffer": _run([sniffer, "--version"]),
        "usb_serial_devices": _run("ls -l /dev/serial/by-id/ 2>/dev/null"),
    }


def _load_client(iperf_cfg):
    """The iperf3 load client (the Pi), over SSH: its WiFi link rate sets
    the 100% load ceiling."""
    if not iperf_cfg or not iperf_cfg.get("pi_host"):
        return {"error": "no row2_duty_cycle.iperf.pi_host configured"}
    target = f"{iperf_cfg.get('pi_user', 'pi')}@{iperf_cfg['pi_host']}"
    remote = (
        "echo model: $(tr -d '\\0' </proc/device-tree/model 2>/dev/null); "
        "echo os: $(. /etc/os-release && echo $PRETTY_NAME); "
        "echo kernel: $(uname -r); "
        "echo iperf3: $(iperf3 --version 2>&1 | head -1); "
        "echo wlan0_driver: $(ethtool -i wlan0 2>/dev/null | tr '\\n' ' '); "
        "echo --- iw dev wlan0 link; iw dev wlan0 link 2>&1; "
        "echo --- iw dev wlan0 info; iw dev wlan0 info 2>&1"
    )
    return {
        "target": target,
        "report": _run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, remote],
            timeout=15,
        ),
    }


def collect_environment(cfg, load_client=True):
    pkgs = {}
    for name in DEB_PACKAGES:
        v = _run(["dpkg-query", "-W", "-f=${Version}", name])
        pkgs[name] = v if isinstance(v, str) and v else "not installed"
    py = {}
    for name in PY_PACKAGES:
        try:
            py[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            py[name] = "not installed"
    devices = {
        d["name"]: {
            "mac_address": d.get("mac_address"),
            "launch_cmd": d.get("launch_cmd"),
            "driver_git": _git_state(d["driver_source_dir"]) if d.get("driver_source_dir") else None,
        }
        for d in cfg.get("devices") or []
    }
    controller = cfg.get("btmon_controller", "hci0")
    return {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "host": {
            "hostname": platform.node(),
            "os": _run(["lsb_release", "-ds"]),
            "kernel": platform.release(),
            "machine": _run("cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name /sys/class/dmi/id/product_version 2>/dev/null | paste -sd' '"),
            "bios": _run("cat /sys/class/dmi/id/bios_version /sys/class/dmi/id/bios_date 2>/dev/null | paste -sd' '"),
        },
        "tools": {
            "bluetoothd": _run(["bluetoothd", "--version"]),
            "btmon": _run(["btmon", "--version"]),
            "iw": _run(["iw", "--version"]),
            "iperf3": _run("iperf3 --version | head -1"),
            "python": sys.version.split()[0],
            "ros_distro": os.environ.get("ROS_DISTRO", "not sourced"),
        },
        "packages_deb": pkgs,
        "packages_python": py,
        "bluetooth_controller": _bt_controller(controller),
        "wifi_interfaces": _wifi_ifaces(),
        "rfkill": _run(["rfkill", "list"]),
        "nrf_sniffer": _sniffer(),
        "experiment_repo": _git_state(Path(__file__).resolve().parents[2]),
        "devices": devices,
        "load_client": (
            _load_client((cfg.get("row2_duty_cycle") or {}).get("iperf"))
            if load_client else "not probed (no load client in this session type)"
        ),
    }


def _save_uncommitted_diffs(env, session_dir, stem):
    """A "-dirty" commit hash doesn't identify the code that ran, so for every
    repo with uncommitted changes (this one, each driver's), save the diff
    against HEAD next to the snapshot: <stem>_<repo>.patch."""
    repos = {env["experiment_repo"].get("path")}
    for dev in env["devices"].values():
        git = dev.get("driver_git") or {}
        if git.get("dirty") is True:
            top = _run(["git", "-C", git["path"], "rev-parse", "--show-toplevel"])
            if isinstance(top, str):
                repos.add(top)
    if env["experiment_repo"].get("dirty") is not True:
        repos.discard(env["experiment_repo"].get("path"))
    for repo in sorted(r for r in repos if r):
        diff = _run(["git", "-C", repo, "diff", "HEAD"], timeout=15)
        if isinstance(diff, str) and diff:
            out = session_dir / f"{stem}_{Path(repo).name}.patch"
            out.write_text(diff + "\n")
            log.info(f"  -> uncommitted changes in {repo} saved to {out.name}")


def snapshot_environment(cfg, session_dir, load_client=True):
    """Write environment.json into session_dir. Never raises: a failure here
    only costs the snapshot, not the session. Keeps any earlier snapshot of
    a resumed session by numbering the new one (environment_2.json, ...)."""
    try:
        path = Path(session_dir) / ENVIRONMENT_FILE
        n = 2
        while path.exists():
            path = Path(session_dir) / f"environment_{n}.json"
            n += 1
        env = collect_environment(cfg, load_client)
        path.write_text(json.dumps(env, indent=2) + "\n")
        log.info(f"  -> software/firmware snapshot saved to {path.name}")
        _save_uncommitted_diffs(env, Path(session_dir), path.stem)
    except Exception as e:  # noqa: BLE001 -- best-effort by design
        log.warning(f"  -> could not save the environment snapshot: {e}")


