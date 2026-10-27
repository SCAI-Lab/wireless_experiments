"""WiFi side: QBSS channel-utilization polling, internal-WiFi rfkill handling and
per-channel (1-13) occupancy polling."""

import csv
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from .logs import log

# nl80211 allows one scan in flight per radio, and the QBSS and channel-
# occupancy pollers scan the same USB adapter from separate threads. Unlocked,
# they collided 100 times in 20261006_000828 (57 occupancy + 43 QBSS scans
# lost to "Device or resource busy").
_scan_locks = {}
_scan_locks_guard = threading.Lock()


def _iw_scan(iface):
    """`iw dev <iface> scan`, serialized per interface across this process's
    pollers. Returns the CompletedProcess; raises like subprocess.run."""
    with _scan_locks_guard:
        lock = _scan_locks.setdefault(iface, threading.Lock())
    with lock:
        return subprocess.run(
            ["iw", "dev", iface, "scan"], capture_output=True, text=True, timeout=15
        )


def _qbss_iface_is_safe(iface):
    """Refuse to scan from an interface that's likely sharing a physical
    radio/antenna with the Bluetooth controller under test. Confirmed by
    direct testing on 2026-09-22: on this machine, wlp0s20f3 (iwlwifi
    driver, Intel Meteor Lake PCH CNVi WiFi) and hci0 (Intel AX211
    Bluetooth) are the SAME physical combo module despite enumerating on
    different buses (PCIe vs USB) -- scanning from wlp0s20f3 mid-run would
    force WiFi/BT antenna time-sharing and perturb the very BLE link being
    measured, which would be worse than collecting no QBSS data at all.
    Heuristic: refuses any iface using the iwlwifi driver, since virtually
    all modern Intel WiFi ships as a CNVi combo card paired with Bluetooth
    on the same module. A genuinely separate USB WiFi dongle (ath9k_htc,
    rt2800usb, rtl8xxxu, etc.) is safe."""
    driver_path = Path(f"/sys/class/net/{iface}/device/driver")
    if not driver_path.exists():
        return False  # can't verify -- fail closed, not "assume safe"
    try:
        driver = driver_path.resolve(strict=True).name
    except OSError:
        return False
    return driver != "iwlwifi"


def log_wifi_rfkill_state(session_dir):
    """Best-effort: dump `rfkill list` (every Bluetooth/WiFi radio's soft/
    hard block state) into a session-level file, same pattern as
    _log_controller_info's controller_info.txt -- so a write-up can state
    whether the laptop's own WiFi was active during a session. The internal
    radio shares a physical antenna with the Bluetooth controller under
    test (see _qbss_iface_is_safe), so its on/off state is itself part of
    the RF conditions the results characterize. Read-only: always runs,
    regardless of test_mode or disable_internal_wifi.enabled."""
    try:
        result = subprocess.run(
            ["rfkill", "list"], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("  -> could not run `rfkill list` to log WiFi/BT radio state")
        return
    info = result.stdout.strip()
    if not info:
        return
    (session_dir / "rfkill_state.txt").write_text(info + "\n")
    log.info("  -> WiFi/BT radio state saved to rfkill_state.txt")
    for line in info.splitlines():
        log.debug(f"     {line}")


def _find_internal_wifi_iface():
    """Find the WiFi interface that shares a physical radio/antenna with
    the Bluetooth controller under test -- the same iwlwifi-driven CNVi
    combo card _qbss_iface_is_safe() already refuses to scan from. Returns
    the interface name, or None if no such interface exists on this
    machine."""
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        return None
    for p in net_dir.iterdir():
        if (p / "wireless").exists() and not _qbss_iface_is_safe(p.name):
            return p.name
    return None


def _rfkill_id_for_iface(iface):
    """Find iface's OWN rfkill device id via its phy80211 name, matched
    against `rfkill --json`'s per-device "device" field -- deliberately
    NOT `rfkill block wifi`/wlan-type, which would also block a separate,
    safe USB WiFi adapter (used for qbss_iface/ambient_scan.iface) sharing
    the same "wlan" rfkill type. Returns None on any failure (missing
    tooling, no match) -- never raises."""
    try:
        phy_name = Path(f"/sys/class/net/{iface}/phy80211/name").read_text().strip()
    except OSError:
        return None
    try:
        result = subprocess.run(
            ["rfkill", "--json"], capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    for dev in data.get("rfkilldevices", []):
        if dev.get("type") == "wlan" and dev.get("device") == phy_name:
            return dev.get("id")
    return None


def disable_internal_wifi_for_session(test_mode):
    """Soft-block ONLY the internal iwlwifi-driven WiFi radio (see
    _find_internal_wifi_iface/_rfkill_id_for_iface) for the duration of a
    real chamber session -- it shares a physical antenna with the
    Bluetooth controller under test, so leaving it associated/idle-
    scanning during a BLE run is an uncontrolled interference source the
    results would otherwise silently absorb. A separate, safe USB WiFi
    adapter is never touched -- this targets one specific rfkill id, never
    `rfkill block wifi`.

    Skipped entirely under --test-mode: that mode is for trying the
    pipeline out at a desk, not in the chamber, where killing the
    operator's own internet is actively unhelpful and there's no
    controlled RF environment to protect anyway.

    Returns the rfkill id that was blocked (pass to restore_internal_wifi
    when the session ends), or None if nothing was blocked -- test_mode,
    no internal adapter found, or rfkill tooling unavailable. Never
    raises: this is best-effort safety, not a hard requirement."""
    if test_mode:
        log.info("  -> --test-mode: leaving the internal WiFi radio alone")
        return None
    iface = _find_internal_wifi_iface()
    if not iface:
        log.debug("  -> no internal (iwlwifi) WiFi interface found, nothing to block")
        return None
    rfkill_id = _rfkill_id_for_iface(iface)
    if rfkill_id is None:
        log.warning(
            f"  -> could not find {iface}'s own rfkill id, leaving it alone"
        )
        return None
    try:
        result = subprocess.run(
            ["rfkill", "block", str(rfkill_id)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning(f"  -> could not run rfkill to block {iface}")
        return None
    if result.returncode != 0:
        log.warning(
            f"  -> `rfkill block {rfkill_id}` ({iface}) failed: "
            f"{result.stderr.strip()[:200]}"
        )
        return None
    log.info(
        f"  -> soft-blocked {iface} (rfkill id {rfkill_id}) for this session -- "
        f"it shares an antenna with the Bluetooth controller under test. "
        f"Restored automatically when this session ends."
    )
    return rfkill_id


def restore_internal_wifi(rfkill_id):
    """Undo disable_internal_wifi_for_session -- unblocks the same rfkill
    id. Call from a finally block so the operator's WiFi comes back
    regardless of how the session ends (clean finish, abort, or crash).
    No-op if rfkill_id is None (nothing was blocked in the first place)."""
    if rfkill_id is None:
        return
    try:
        subprocess.run(
            ["rfkill", "unblock", str(rfkill_id)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning(
            f"  -> could not run rfkill to restore internal WiFi (id "
            f"{rfkill_id}) -- you may need to run `rfkill unblock "
            f"{rfkill_id}` manually"
        )
        return
    log.info(f"  -> restored internal WiFi (rfkill id {rfkill_id})")


def _autodetect_wifi_iface():
    """Find the one WiFi interface that's safe to use for QBSS/channel-
    occupancy polling, instead of requiring its name to be hardcoded in
    config.yaml -- a USB adapter's interface name (e.g. wlxc03a55a6e1bf,
    the "wlx"+MAC predictable-naming scheme) is normally stable across
    replugs/reboots on the SAME machine, but hardcoding it still means
    config.yaml silently breaks on a different machine, after swapping
    adapters, or if the naming scheme differs. Enumerates every interface
    with a /sys/class/net/<iface>/wireless directory (i.e. an actual WiFi
    interface, not a wired NIC or virtual device) and filters through the
    same _qbss_iface_is_safe() driver check already used to reject the
    laptop's built-in combo chip. Returns the interface name only if
    EXACTLY one safe candidate exists -- 0 or 2+ is ambiguous, so this logs
    why and returns None rather than guessing (same as the old "not
    configured" path's behavior)."""
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        return None
    candidates = sorted(
        p.name
        for p in net_dir.iterdir()
        if (p / "wireless").exists() and _qbss_iface_is_safe(p.name)
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        log.debug(
            "  -> no safe WiFi interface auto-detected (no separate USB "
            "WiFi adapter plugged in?) -- QBSS/channel-occupancy polling "
            "will be skipped"
        )
    else:
        log.warning(
            f"  -> multiple safe WiFi interfaces found ({candidates}) -- "
            f"can't auto-detect which one to use for QBSS/channel-occupancy "
            f"polling, set qbss_iface/channel_occupancy_iface explicitly in "
            f"config.yaml to disambiguate"
        )
    return None


def _ensure_iface_unmanaged(iface):
    """Best-effort: tell NetworkManager to stop managing iface, so ITS OWN
    periodic background scans (fired even while the device is "managed,
    disconnected") stop racing our own scheduled `iw scan` calls on the same
    physical radio. nl80211 only allows one scan in flight per radio, so a
    collision between NetworkManager's autoscan and ours fails with "Device
    or resource busy" and silently drops that poll -- this is the QBSS
    "third failure mode" documented in poll_qbss_channel_utilization below,
    confirmed 2026-09-25 to recur session after session because NM re-scans
    this adapter on its own, not just from one-off manual `iw scan` testing.
    Confirmed live: back-to-back iw scans on this NM-managed, idle adapter
    alternately failed with exactly this error; after `nmcli device set
    <iface> managed no`, the same back-to-back test ran clean (5/5).

    Runtime-only -- reverts to managed on reboot/NM restart -- so this is
    called at the start of every session that uses iface, rather than
    depending on a one-time host config change someone has to remember to
    reapply. Never raises: if nmcli isn't installed or the call fails,
    QBSS/occupancy polling just falls back to its pre-existing flakiness,
    it doesn't get any worse."""
    try:
        result = subprocess.run(
            ["nmcli", "device", "set", iface, "managed", "no"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.debug(f"  -> could not run nmcli to unmanage {iface} (not installed?)")
        return
    if result.returncode == 0:
        log.info(
            f"  -> {iface} set unmanaged in NetworkManager, to stop its "
            f"background autoscan from colliding with our QBSS/occupancy polls"
        )
    else:
        log.debug(
            f"  -> nmcli device set {iface} managed no failed "
            f"(returncode={result.returncode}): {result.stderr.strip()[:200]}"
        )


def poll_qbss_channel_utilization(iface, bssid):
    """Best-effort attempt at a real airtime signal: scan and read the
    target bssid's advertised QBSS Load IE (802.11e/k channel utilization,
    0-255 scale, 255 == 100% busy), if the AP broadcasts one. This is the
    only real-airtime cross-check available without a dedicated monitor-
    mode radio -- see the ble_sniffer/row2_duty_cycle comments in
    config.yaml for why a proper per-second airtime capture isn't
    implemented. Returns a dict with a 0-100 percent value, or None if the
    interface, AP, or IE isn't available -- never raises, since this must
    never fail an actual test. One sample; see start_qbss_poller for
    repeated sampling during a run.

    Two real bugs fixed here (confirmed live, 2026-09-23, against the real
    WirelessTests/TL-MR6400 AP once a genuinely separate USB WiFi adapter
    was available to test against for the first time -- qbss_iface had
    been null since this was written, so neither bug had ever been
    exercised before):
    1. `iw dev <iface> scan bssid <bssid>` -- "bssid" is not a valid scan
       filter on this iw version (6.7; usage: scan [-u] [freq ...]
       [duration ...] [ies ...] [meshid ...] [...] [ssid ...|passive], no
       bssid option at all) -- the command always failed with a usage
       error and non-zero exit, so this function could never have returned
       real data. Fixed by scanning broadly (no filter) instead.
    2. A broad scan returns EVERY visible AP's own "BSS Load" block, each
       with its own "channel utilisation: N/255" line -- searching the
       whole scan output with one global regex (the original approach)
       would silently match whichever AP's line happens to come first,
       not necessarily the target bssid's. Fixed by first locating the
       target's own "BSS <bssid>(...)" section and searching only within
       it (up to the next "BSS " line or end of output).

    A THIRD real (non-code) failure mode, found diagnosing why an entire
    real session's row3 (forced-channel) block came back with every QBSS
    sample blank while row2 on the same session had real values throughout
    (2026-09-24): nl80211 only allows ONE scan in flight per physical
    radio -- a second concurrent `iw scan` on the same iface fails with
    "Device or resource busy" (confirmed by firing two at once: one
    returncode=0, the other returncode=240). That session's row3 window
    overlapped with unrelated manual `iw scan` testing on this same
    adapter (developing poll_channel_occupancy_snapshot/count_nearby_ble_
    devices below), so the two competed and the session's own polls lost
    that race every time. The busy/AP-not-found/no-IE cases were
    previously indistinguishable (all just silently returned None) --
    logged at DEBUG now so a repeat doesn't need this same forensic
    reconstruction.
    """
    try:
        result = _iw_scan(iface)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        log.debug(
            f"  -> QBSS scan on {iface} failed (returncode={result.returncode}): "
            f"{result.stderr.strip()[:200]}"
        )
        return None
    pct = _qbss_from_scan_output(result.stdout, bssid)
    if pct is None:
        return None
    return {"qbss_channel_utilization_pct": pct}


def _qbss_from_scan_output(scan_stdout, bssid):
    """bssid's QBSS channel utilization (0-100 %) from `iw dev <if> scan`
    output, searching only within bssid's own "BSS <bssid>" section (see
    poll_qbss_channel_utilization, bug 2). Returns None if the AP isn't in
    the scan or doesn't advertise a BSS Load IE."""
    section = _bss_section(scan_stdout, bssid)
    if section is None:
        return None
    m = re.search(r"channel utilisation:\s*(\d+)/255", section, re.IGNORECASE)
    if not m:
        return None
    return round(100 * int(m.group(1)) / 255, 1)


def _bss_section(scan_stdout, bssid):
    """The body of bssid's own "BSS <bssid>" block in `iw dev <if> scan`
    output (up to the next "BSS " line), or None if bssid isn't in it."""
    section_re = re.compile(
        rf"^BSS {re.escape(bssid)}\b[^\n]*\n(.*?)(?=^BSS |\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    section_m = section_re.search(scan_stdout)
    return section_m.group(1) if section_m else None


def _ap_channel_from_scan_output(scan_stdout, bssid):
    """bssid's 2.4GHz channel number from its scan "freq:" line, or None."""
    section = _bss_section(scan_stdout, bssid)
    if section is None:
        return None
    m = re.search(r"^\s*freq:\s*(\d+)", section, re.MULTILINE)
    return _freq_mhz_to_24ghz_channel(int(m.group(1))) if m else None


def _qbss_poll_loop(iface, bssid, samples, poll_interval_s, stop_event):
    while not stop_event.wait(poll_interval_s):
        result = poll_qbss_channel_utilization(iface, bssid)
        if result:
            samples.append(result["qbss_channel_utilization_pct"])


def _resolve_qbss_iface(iface, bssid):
    """The interface to scan for QBSS from, or None to skip QBSS (see
    start_qbss_poller for iface=None/False). Unmanages it in NetworkManager."""
    if iface is False or not bssid:
        return None
    if not iface:
        iface = _autodetect_wifi_iface()
        if not iface:
            return None
        log.info(f"  -> auto-detected {iface} as the QBSS polling interface")
    if not _qbss_iface_is_safe(iface):
        log.warning(
            f"  -> QBSS poll skipped: {iface} looks like it may share a "
            f"physical radio/antenna with the Bluetooth controller "
            f"(iwlwifi driver) -- scanning from it mid-run would perturb "
            f"the BLE link being measured. Use a separate USB WiFi adapter "
            f"instead (see config.yaml's qbss_iface comment)."
        )
        return None
    _ensure_iface_unmanaged(iface)
    return iface


def sample_qbss_once(iface, bssid, attempts=2):
    """One QBSS channel-utilization reading (0-100 %), or None. Used to
    bracket a run's WiFi traffic with ambient readings just before it starts
    and after it stops."""
    iface = _resolve_qbss_iface(iface, bssid)
    if iface is None:
        return None
    for _ in range(attempts):
        result = poll_qbss_channel_utilization(iface, bssid)
        if result:
            return result["qbss_channel_utilization_pct"]
        time.sleep(1)
    return None


def start_qbss_poller(iface, bssid, poll_interval_s=30):
    """Start a background thread sampling QBSS channel utilization every
    poll_interval_s WHILE the run is in progress -- polling only once after
    the run ends (an earlier version of this function) measures an idle
    channel, defeating the whole point. Refuses to start (returns None, logs
    once) if iface fails _qbss_iface_is_safe -- actively perturbing the BLE
    measurement it's trying to explain would be worse than no data.

    iface=None (config.yaml's qbss_iface left unset/null) auto-detects via
    _autodetect_wifi_iface() instead of requiring the interface name to be
    hardcoded -- an explicit iface is still honored as-is (and still passes
    through the same safety check below) for the rare case of 2+ qualifying
    adapters, where auto-detection can't disambiguate. iface=False (config.yaml
    explicitly set to `false`, not left blank) skips polling entirely without
    attempting auto-detection, for opting out even when a safe adapter is
    plugged in."""
    iface = _resolve_qbss_iface(iface, bssid)
    if iface is None:
        return None
    samples = []
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_qbss_poll_loop,
        args=(iface, bssid, samples, poll_interval_s, stop_event),
        daemon=True,
    )
    thread.start()
    log.info(
        f"  -> QBSS channel-utilization poller running (every "
        f"{poll_interval_s}s) on {iface}"
    )
    return thread, stop_event, samples


def stop_qbss_poller(poller):
    """Stop the poller started by start_qbss_poller() and return
    {"qbss_channel_utilization_pct": avg-over-the-run} from whatever samples
    were collected, or None if none were (e.g. the run was shorter than one
    poll_interval_s, or every scan failed)."""
    if poller is None:
        return None
    thread, stop_event, samples = poller
    stop_event.set()
    thread.join(timeout=5)
    if not samples:
        return None
    return {"qbss_channel_utilization_pct": round(sum(samples) / len(samples), 1)}


# =============================================================================
# Per-channel (1-13) WiFi occupancy -- for the passive real-gym visit
# (gym_session.py), not the chamber rows. Where poll_qbss_channel_utilization
# above tracks ONE known AP's own advertised load, this surveys every
# visible 2.4GHz channel via a broad scan, so a gym with several APs on
# different channels doesn't get summarized by only its busiest one --
# needed to answer "how much AFH headroom does a real gym leave" (manuscript
# Table tab:gym-observation) for channels the config doesn't already know
# about. Same _qbss_iface_is_safe() gate and separate-adapter requirement.
# =============================================================================

WIFI_24GHZ_CHANNELS = list(range(1, 14))


def _freq_mhz_to_24ghz_channel(freq_mhz):
    """2412 MHz (ch1) .. 2472 MHz (ch13), 5 MHz spacing (802.11 channel
    numbering). Returns None for anything outside that band (5/6GHz APs,
    which aren't part of the BLE-relevant 2.4GHz picture here)."""
    channel = round((freq_mhz - 2407) / 5)
    return channel if 1 <= channel <= 13 else None


def _parse_channel_occupancy(scan_stdout):
    """Bucket every BSS in a broad `iw scan` by its 2.4GHz channel. Returns
    {channel: {"ap_count": N, "mean_channel_utilization_pct": X or None}}
    for channels 1-13 that had at least one visible AP -- channels with no
    APs are left out (the caller/CSV writer fills the full 1-13 range with
    zeros so "channel had nothing on it" is distinguishable from "poll
    failed"). Per-AP channel utilization only averages over APs that
    actually advertise a BSS Load IE (many consumer routers don't), so a
    channel's count can be nonzero while its utilization is None."""
    channels = {}
    for bss_block in re.split(r"^BSS ", scan_stdout, flags=re.MULTILINE)[1:]:
        freq_m = re.search(r"freq:\s*([\d.]+)", bss_block)
        if not freq_m:
            continue
        channel = _freq_mhz_to_24ghz_channel(float(freq_m.group(1)))
        if channel is None:
            continue
        entry = channels.setdefault(channel, {"ap_count": 0, "utilizations": []})
        entry["ap_count"] += 1
        util_m = re.search(r"channel utilisation:\s*(\d+)/255", bss_block, re.IGNORECASE)
        if util_m:
            entry["utilizations"].append(100.0 * int(util_m.group(1)) / 255.0)
    return {
        ch: {
            "ap_count": v["ap_count"],
            "mean_channel_utilization_pct": (
                round(sum(v["utilizations"]) / len(v["utilizations"]), 1)
                if v["utilizations"] else None
            ),
        }
        for ch, v in channels.items()
    }


def poll_channel_occupancy_snapshot(iface):
    """One broad scan, bucketed per 2.4GHz channel. Returns None on any
    scan failure (interface, permissions, driver) -- never raises. Same
    concurrent-scan caveat as poll_qbss_channel_utilization: nl80211 only
    allows one scan in flight per radio, so a second scan on this same
    iface (e.g. manual `iw scan` testing, or another poller on the same
    adapter) fails with "Device or resource busy" -- logged at DEBUG so a
    string of blank polls is diagnosable rather than just silent."""
    try:
        result = _iw_scan(iface)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        log.debug(
            f"  -> Channel-occupancy scan on {iface} failed "
            f"(returncode={result.returncode}): {result.stderr.strip()[:200]}"
        )
        return None
    return _parse_channel_occupancy(result.stdout)


def _wall_time():
    """Local wall-clock timestamp for session-wide poller CSVs."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _open_session_csv(path, header):
    """Open a session-wide poller CSV for writing WITHOUT losing what an
    earlier run of the same session already recorded: `run --resume`
    restarts these pollers in the same session directory, and opening with
    "w" wiped the file -- confirmed 2026-09-30, when resuming session
    20260930_105034 for a single rerun erased ~5 h of channel occupancy.

    Appends when the existing file has the same header. A file with a
    different header (e.g. written before the wall_time column existed) is
    renamed aside to <stem>_before_<mtime><suffix> and a fresh file started,
    so nothing is ever overwritten. timestamp_s restarts at 0 in each
    resumed segment; wall_time is what orders rows across segments."""
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        with open(path, newline="") as f:
            existing_header = next(csv.reader(f), None)
        if existing_header == header:
            log.info(f"  -> appending to existing {path.name} (resumed session)")
            return open(path, "a", newline="")
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(path.stat().st_mtime))
        aside = path.with_name(f"{path.stem}_before_{stamp}{path.suffix}")
        path.rename(aside)
        log.info(
            f"  -> {path.name} had a different header -- kept it as "
            f"{aside.name} and started a new file"
        )
    f = open(path, "w", newline="")
    csv.writer(f).writerow(header)
    f.flush()
    return f


def _channel_occupancy_poll_loop(iface, out_csv_path, poll_interval_s, stop_event):
    start = time.time()
    header = ["timestamp_s", "channel", "ap_count", "mean_channel_utilization_pct", "wall_time"]
    with _open_session_csv(out_csv_path, header) as f:
        writer = csv.writer(f)
        while not stop_event.wait(poll_interval_s):
            snapshot = poll_channel_occupancy_snapshot(iface)
            ts = round(time.time() - start, 1)
            if snapshot is None:
                continue  # scan failed this cycle -- skip, don't write fabricated zeros
            wall = _wall_time()
            for channel in WIFI_24GHZ_CHANNELS:
                entry = snapshot.get(channel, {"ap_count": 0, "mean_channel_utilization_pct": None})
                writer.writerow([ts, channel, entry["ap_count"], entry["mean_channel_utilization_pct"], wall])
            f.flush()


def start_channel_occupancy_poller(iface, out_csv_path, poll_interval_s=60):
    """Start a background thread scanning all 13 2.4GHz channels every
    poll_interval_s for the WHOLE gym session (unlike the per-run AFH/QBSS
    pollers, this is meant to be started once and run across every device
    in the visit -- background WiFi occupancy doesn't belong to any one
    device's run). Refuses to start (returns None, logs once) if iface
    fails _qbss_iface_is_safe(), same reasoning as start_qbss_poller.

    iface=None (config.yaml's channel_occupancy_iface left unset/null)
    auto-detects via _autodetect_wifi_iface(), same as start_qbss_poller.
    iface=False explicitly opts out without attempting auto-detection."""
    if iface is False:
        return None
    if not iface:
        iface = _autodetect_wifi_iface()
        if not iface:
            return None
        log.info(f"  -> auto-detected {iface} as the channel-occupancy polling interface")
    if not _qbss_iface_is_safe(iface):
        log.warning(
            f"  -> Channel-occupancy poll skipped: {iface} looks like it may "
            f"share a physical radio/antenna with the Bluetooth controller "
            f"(iwlwifi driver) -- scanning from it would perturb the BLE "
            f"link being measured. Use a separate USB WiFi adapter instead."
        )
        return None
    _ensure_iface_unmanaged(iface)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_channel_occupancy_poll_loop,
        args=(iface, out_csv_path, poll_interval_s, stop_event),
        daemon=True,
    )
    thread.start()
    log.info(
        f"  -> Per-channel WiFi occupancy poller running (every "
        f"{poll_interval_s}s, channels 1-13) on {iface} -> {out_csv_path}"
    )
    return thread, stop_event


def stop_channel_occupancy_poller(poller):
    if poller is None:
        return
    thread, stop_event = poller
    stop_event.set()
    thread.join(timeout=5)
