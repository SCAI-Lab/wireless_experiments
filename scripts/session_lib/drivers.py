"""Device driver process control, BlueZ connection healing and the pairing agent."""

import os
import shlex
import signal
import subprocess
import sys
import time

from .errors import (
    DriverExitedError,
    InfrastructureCheckError,
)
from .logs import log


# =============================================================================
# Device driver control
# =============================================================================


def start_driver(launch_cmd, log_path=None):
    """Launch a device driver. With log_path, keep the driver's own
    stdout/stderr instead of discarding it: the HCI capture shows what
    reached the air, the driver log what the driver tried to do.
    PYTHONUNBUFFERED so a driver that has to be SIGKILLed still leaves its
    final lines on disk."""
    log.info(f"  -> launching driver: {launch_cmd}")
    out, env, handle = subprocess.DEVNULL, None, None
    if log_path:
        try:
            handle = open(log_path, "ab", buffering=0)
            handle.write(
                f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"launching: {launch_cmd}\n".encode()
            )
            out = handle
            env = dict(os.environ, PYTHONUNBUFFERED="1")
        except OSError as e:
            log.warning(f"  -> could not open driver log {log_path}: {e}")
            out, handle = subprocess.DEVNULL, None
    proc = subprocess.Popen(
        shlex.split(launch_cmd),
        stdout=out,
        stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
        preexec_fn=os.setsid,
        env=env,
    )
    if handle is not None:
        # The child holds its own duplicate of the fd; drop ours so the
        # parent doesn't accumulate one open file per run.
        handle.close()
        log.info(f"     driver log -> {log_path}")
    log.info(f"     driver running (pid {proc.pid})")
    _live_drivers.add(proc)
    return proc


# Drivers started by start_driver and not yet stopped. They run in their own
# session (os.setsid, so stop_driver can signal the whole process group),
# so Ctrl+C in the terminal never reaches them.
_live_drivers = set()


def stop_all_drivers():
    """Stop every driver this process started that is still running -- for a
    session's final cleanup, however it ends (Ctrl+C, crash, abort)."""
    for proc in list(_live_drivers):
        if proc.poll() is None:
            log.warning(f"  -> driver (pid {proc.pid}) still running at session exit")
            stop_driver(proc)
        _live_drivers.discard(proc)


def stop_driver(proc):
    _live_drivers.discard(proc)
    log.info(f"  -> stopping driver (pid {proc.pid})")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=8)
    except ProcessLookupError:
        pass  # already exited on its own
    except subprocess.TimeoutExpired:
        log.warning(f"     driver (pid {proc.pid}) didn't exit cleanly, killing")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def disconnect_ble_device(mac_address, timeout_s=5):
    """Explicitly tell BlueZ to disconnect this device once we're done with
    it. Stopping the driver is not enough: BlueZ auto-reconnects a bonded,
    trusted device in the background, and that connection would share the
    radio with the next test's device."""
    if not mac_address:
        return
    try:
        result = subprocess.run(
            ["bluetoothctl", "disconnect", mac_address],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        log.warning("  -> bluetoothctl not found, skipping explicit BLE disconnect")
        return
    except subprocess.TimeoutExpired:
        log.warning(f"  -> `bluetoothctl disconnect {mac_address}` timed out")
        return
    if result.returncode == 0:
        log.info(
            f"  -> disconnected {mac_address} from BlueZ "
            f"(preventing background auto-reconnect)"
        )
    else:
        log.warning(
            f"  -> `bluetoothctl disconnect {mac_address}` returned "
            f"{result.returncode}: {(result.stdout or result.stderr).strip()}"
        )


def check_ros_packages(cfg):
    """Preflight: every `ros2 run <pkg> ...` driver must resolve (a shell that
    only sourced /opt/ros/<distro> can't find the driver workspace)."""
    missing = []
    for d in cfg["devices"]:
        parts = shlex.split(d.get("launch_cmd") or "")
        if len(parts) >= 3 and parts[:2] == ["ros2", "run"]:
            r = subprocess.run(["ros2", "pkg", "prefix", parts[2]],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                missing.append(parts[2])
    if missing:
        raise InfrastructureCheckError(
            f"ROS package(s) not found: {missing} -- source the driver workspace "
            f"first, e.g. `source $ROS_WS/install/setup.bash`"
        )


def _bluez_says_connected(mac_address):
    try:
        out = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return "Connected: yes" in out


def _kernel_has_link(mac_address):
    try:
        out = subprocess.run(
            ["hcitool", "con"], capture_output=True, text=True, timeout=5
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return mac_address.upper() in out.upper()


def heal_stale_bluez_connection(mac_address):
    """Detect and clear bluetoothd's stale "Connected: yes" for a device the
    kernel no longer has a link to. Returns True if the state was stale.

    When a host disconnect races a supervision timeout, the kernel can fail
    the MGMT Disconnect (status 0x0e) and bluetoothd never gets a Device
    Disconnected event: it keeps the device marked connected, and bleak
    never issues Connect() again. Only an adapter power cycle (or
    restarting bluetoothd) clears it."""
    if not mac_address:
        return False
    if not _bluez_says_connected(mac_address) or _kernel_has_link(mac_address) is not False:
        return False
    log.warning(
        f"  -> BlueZ still reports {mac_address} connected but the kernel has no "
        f"link (stale state after a failed MGMT Disconnect) -- power-cycling "
        f"the adapter so the next run doesn't inherit it"
    )
    for cmd in (["bluetoothctl", "power", "off"], ["bluetoothctl", "power", "on"]):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        time.sleep(1.0)
    if _bluez_says_connected(mac_address):
        log.error(
            f"  !! {mac_address} is STILL marked connected after an adapter power "
            f"cycle -- run `sudo systemctl restart bluetooth` before the next run, "
            f"or it will record no connection attempts at all"
        )
    else:
        log.info("  -> stale BlueZ connection cleared")
    return True


def start_pairing_agent():
    """Register a non-interactive BlueZ pairing agent (capability
    NoInputNoOutput) as the system default for the life of this process, so
    pairing with a known sensor doesn't wait on a confirmation in the
    desktop's own Bluetooth agent ("Just Works" pairing). Returns the Popen
    handle (or None
    if bluetoothctl isn't available) -- pass it to stop_pairing_agent() when
    the session ends so the desktop's own agent can take back over."""
    try:
        proc = subprocess.Popen(
            ["bluetoothctl", "--agent", "NoInputNoOutput"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        log.warning(
            "  -> bluetoothctl not found -- pairing prompts will need manual "
            "approval"
        )
        return None
    log.info(
        "  -> registered a NoInputNoOutput pairing agent (auto-approves "
        "pairing with chamber sensors, no manual 'Allow' needed)"
    )
    return proc


def stop_pairing_agent(proc):
    """Release the pairing agent started by start_pairing_agent(), so the
    desktop's own Bluetooth agent resumes handling pairing prompts normally
    once the session ends."""
    if proc is None:
        return
    try:
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.wait(timeout=5)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.info("  -> released the pairing agent")


def _render_progress_bar(elapsed, duration_s, suffix="", width=30):
    frac = min(elapsed / duration_s, 1.0) if duration_s else 1.0
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    line = f"  [{bar}] {elapsed:.0f}/{duration_s:.0f}s {suffix}"
    sys.stdout.write("\r" + line.ljust(78))
    sys.stdout.flush()


def _clear_progress_bar():
    sys.stdout.write("\n")
    sys.stdout.flush()


def sleep_and_watch_driver(
    duration_s, driver_proc, launch_cmd, poll_interval=1.0, heartbeat_s=30.0
):
    """Sleep for duration_s, polling driver_proc so a crash/exit is caught
    quickly. On an interactive terminal, shows a live progress bar; the
    periodic heartbeat goes to the session log at DEBUG level."""
    elapsed = 0.0
    next_heartbeat = heartbeat_s
    show_bar = sys.stdout.isatty()
    try:
        while elapsed < duration_s:
            step = min(poll_interval, duration_s - elapsed)
            time.sleep(step)
            elapsed += step
            returncode = driver_proc.poll()
            if returncode is not None:
                raise DriverExitedError(launch_cmd, returncode)
            if show_bar:
                _render_progress_bar(elapsed, duration_s, f"(pid {driver_proc.pid})")
            if next_heartbeat <= elapsed < duration_s:
                log.debug(
                    f"     ...{elapsed:.0f}s / {duration_s:.0f}s elapsed, "
                    f"driver still running (pid {driver_proc.pid})"
                )
                next_heartbeat += heartbeat_s
    finally:
        if show_bar:
            _clear_progress_bar()


def start_driver_with_retry(launch_cmd, grace_s=5.0, max_attempts=3, retry_delay_s=3.0,
                            log_path=None):
    """Launch the driver and confirm it survives an initial grace period
    before trusting it, retrying the launch a few times (a failed initial
    connection makes the driver exit within a second or two). Returns the
    live driver
    process, or raises DriverExitedError after the final failed attempt."""
    for attempt in range(1, max_attempts + 1):
        driver_proc = start_driver(launch_cmd, log_path=log_path)
        try:
            sleep_and_watch_driver(
                grace_s,
                driver_proc,
                launch_cmd,
                poll_interval=1.0,
                heartbeat_s=grace_s + 1,
            )
            return driver_proc
        except DriverExitedError as e:
            stop_driver(driver_proc)
            if attempt < max_attempts:
                log.warning(
                    f"  -> driver launch attempt {attempt}/{max_attempts} failed "
                    f"({e}), retrying in {retry_delay_s}s..."
                )
                time.sleep(retry_delay_s)
            else:
                log.error(
                    f"  -> driver launch failed after {max_attempts} attempts: {e}"
                )
                raise
