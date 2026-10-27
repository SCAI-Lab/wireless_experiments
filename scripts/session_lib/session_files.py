"""Config loading, session directories, manifest.csv and resume/rerun bookkeeping."""

import csv
import os
import re
import time
from pathlib import Path

import yaml

from .logs import log

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parent


# =============================================================================
# Config loading
# =============================================================================


def load_config(path="config.yaml"):
    """config.yaml, with ${VAR} and ~ expanded in each device's
    driver_source_dir (it points into the ROS workspace the drivers are
    built in: export ROS_WS=<that workspace>). Left unexpanded if the
    variable isn't set -- driver_version then just reads blank.
    A relative path not found in the working directory is looked up next
    to the scripts (scripts/config.yaml)."""
    path = Path(path)
    if not path.is_absolute() and not path.exists():
        path = SCRIPTS_DIR / path
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for d in cfg.get("devices") or []:
        if d.get("driver_source_dir"):
            d["driver_source_dir"] = os.path.expanduser(os.path.expandvars(d["driver_source_dir"]))
    return cfg


def get_output_dir(cfg):
    """The base directory under which every session gets its own timestamped
    subdirectory (see get_session_dir). A relative output_dir is relative
    to the repo root (captures/), whatever the working directory; it is
    returned relative to the working directory so logged paths stay short."""
    d = Path(os.path.relpath(REPO_ROOT / cfg["session"]["output_dir"]))
    d.mkdir(parents=True, exist_ok=True)
    return d


SESSION_DIR_RE = re.compile(r"^\d{8}_\d{6}$")


def get_session_dir(cfg, session_ts):
    """Each session (a 'run' invocation) gets its own timestamped directory
    under the base output_dir, holding that session's log, captures,
    manifest.csv, and results.csv together as a self-contained unit."""
    d = get_output_dir(cfg) / session_ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_latest_session_ts(cfg):
    """Return the timestamp (directory name) of the most recently created
    session under the base output_dir, or None if no sessions exist yet.
    Used so `parse` can default to the latest session when --session isn't
    given."""
    base = get_output_dir(cfg)
    session_names = sorted(
        p.name for p in base.iterdir() if p.is_dir() and SESSION_DIR_RE.match(p.name)
    )
    return session_names[-1] if session_names else None


def manifest_path(session_dir):
    return session_dir / "manifest.csv"


def results_path(cfg, session_dir):
    name = Path(cfg["session"]["results_csv"]).name
    return session_dir / name


def run_key(row_label, device_name, condition, replicate):
    """Identifies one (row, device, condition, replicate) run, for tracking
    which runs a resumed session can skip."""
    return (row_label, device_name, condition, str(replicate))


def load_completed_keys(cfg, session_dir):
    """Return the run_key()s that `run --resume` should skip.

    For a retryable row (retry_on_fail=True, currently only Row 1), only a
    real PASSED counts as done -- a FAILED run there means the setup was
    broken and should be retried on resume, same as before.

    For a no-retry row (retry_on_fail=False, Rows 2/3/5), ANY recorded
    attempt -- PASSED or FAILED -- counts as done. A FAILED attempt there is
    itself the recorded data point (the interference did its job), not an
    error to retry; without this, --resume would silently re-run it under a
    capture filename with no _attemptN suffix (do_run only adds that suffix
    inside its retry loop, which no-retry rows never enter) and overwrite
    the original failed capture, destroying the evidence.

    Reads manifest.csv, NOT results.csv (originally read results.csv --
    real bug, found 2026-09-24 after a session crashed with an uncaught
    ValueError mid-run: only main()'s RunAbortedError handler calls
    do_parse before exiting, so a ValueError/StrayProcessError/
    InfrastructureCheckError/DeviceCheckError crash -- anything else --
    leaves results.csv missing or stale. With results.csv missing, this
    returned an empty set, so --resume would have treated every already-
    completed run as not done and re-run it, overwriting the ORIGINAL
    completed captures under the same no-suffix filename described above
    -- silently destroying real, already-collected data, not just wasting
    chamber time. manifest.csv is written incrementally by append_manifest
    inside _attempt_run itself, so it survives any crash and already
    carries the same status/retry_on_fail columns this needs."""
    path = manifest_path(session_dir)
    if not path.exists():
        return set()
    with open(path) as f:
        keys = set()
        for row in csv.DictReader(f):
            status = row.get("status")
            retry_on_fail = str(row.get("retry_on_fail", "True")).strip().lower()
            if status == "PASSED" or (status == "FAILED" and retry_on_fail == "false"):
                keys.add(
                    run_key(
                        row["row"], row["device"], row["condition"], row["replicate"]
                    )
                )
        return keys


def load_failed_keys(session_dir):
    """Return the run_key()s whose LATEST manifest.csv row is FAILED,
    regardless of retry_on_fail -- the counterpart to load_completed_keys,
    used by --rerun-failed to find what to re-attempt.

    Latest row only: a rerun appends a new row rather than replacing the
    old one, so a key that failed and was then rerun successfully has both.
    Selecting on ANY FAILED row re-queued those already-passed reruns on
    every later --rerun-failed (2026-10-05, five row5 Polar runs that had
    passed at 21:45 were rerun again at 22:42)."""
    path = manifest_path(session_dir)
    if not path.exists():
        return set()
    with open(path) as f:
        latest = {
            run_key(row["row"], row["device"], row["condition"], row["replicate"]): row
            for row in csv.DictReader(f)
        }
    return {key for key, row in latest.items() if row.get("status") == "FAILED"}



def archive_failed_capture_for_rerun(session_dir, manifest_row):
    """Before re-running a FAILED (row, device, condition, replicate),
    rename its existing capture and associated files (pcap, afh.csv,
    rosbag dir) out of the way first. No-retry rows (2/3/4/5/6) always
    reuse the same capture filename with no attempt suffix (see
    load_completed_keys's docstring) -- without this, do_run's rerun
    would silently overwrite the original failed evidence rather than
    sitting alongside it. Best-effort: a rename failure just logs a
    warning and continues -- that one file's old copy would then be
    overwritten by the rerun (the same risk as not having this safeguard
    at all), but it shouldn't block the rerun itself."""
    # No early return when the capture itself is missing: a leftover
    # rosbag dir alone still makes the rerun's recorder exit immediately.
    capture_file = session_dir / manifest_row["capture_file"]
    ts = re.sub(r"[^0-9]", "", manifest_row.get("start_time", "")) or "unknowntime"
    _move_run_outputs(
        capture_file, f"prerun_failed_{ts}", "preserving the failed attempt before rerun"
    )


# Every file/dir a run writes, as the tail appended to its capture stem --
# see _attempt_run in run.py. Archiving and set_aside_existing_run_outputs
# both go through this list, so a new per-run output only needs adding here.
RUN_OUTPUT_TAILS = (".btsnoop", ".pcap", "_afh.csv", "_rosbag", "_driver.log")


def _move_run_outputs(capture_file, tag, why):
    """Rename every existing output of the run captured at `capture_file` to
    <stem>_<tag><tail>, never replacing an existing file. If any name in the
    group is taken, the whole group gets <tag>_2, _3, ... so its files keep
    matching each other (the parser finds _afh.csv/_rosbag by capture stem).
    Path.rename silently replaces on Linux, and two --rerun-failed passes
    over one run produce the same archive name -- that overwrote original
    failed captures on 2026-10-05. Returns False if any rename failed."""
    base = f"{capture_file.stem}_{tag}"
    stem, n = base, 2
    while any(capture_file.with_name(stem + t).exists() for t in RUN_OUTPUT_TAILS):
        stem, n = f"{base}_{n}", n + 1
    ok = True
    for tail in RUN_OUTPUT_TAILS:
        src = capture_file.with_name(capture_file.stem + tail)
        if not src.exists():
            continue
        dst = src.with_name(stem + tail)
        try:
            src.rename(dst)
            log.info(f"  -> moved {src.name} -> {dst.name} ({why})")
        except OSError as e:
            ok = False
            log.error(f"  -> could not move {src.name} aside: {e}")
    return ok


def set_aside_existing_run_outputs(capture_file):
    """Called right before a run starts writing: moves any output already at
    this run's filenames to <stem>_superseded_<its mtime><tail>, so no run
    ever writes over earlier data. --rerun-failed archives the runs it knows
    are FAILED, but an interrupted run (Ctrl-C mid-run) never gets a manifest
    row, so a plain --resume would otherwise rewrite its files in place.
    Returns False if something couldn't be moved -- the caller must not
    start the run then."""
    existing = [
        capture_file.with_name(capture_file.stem + tail)
        for tail in RUN_OUTPUT_TAILS
        if capture_file.with_name(capture_file.stem + tail).exists()
    ]
    if not existing:
        return True
    stamp = time.strftime(
        "%Y%m%d%H%M%S", time.localtime(max(p.stat().st_mtime for p in existing))
    )
    return _move_run_outputs(
        capture_file, f"superseded_{stamp}", "earlier output at this run's filename"
    )


def prepare_rerun_failed(session_dir, completed):
    """For --rerun-failed: archives every FAILED run's existing captures
    (see archive_failed_capture_for_rerun) and returns a NEW completed-keys
    set with those keys removed, so do_run's skip-if-completed check
    treats them as not yet done and re-attempts them. Returns `completed`
    unchanged if nothing is recorded as FAILED.

    Deliberately does NOT filter by failure_kind or row: for no-retry rows
    (2/3/4/5/6), a FAILED result is often ITSELF the intended data point
    (interference causing a real disconnect, or -- since 2026-09-24's
    evaluate_run_result redesign -- specifically the runs that DIDN'T match
    "ran as expected"), not something to blindly retry away. Re-running one
    of those means deliberately discarding that recorded outcome for a
    fresh attempt -- this function makes that possible and preserves the
    original evidence, but the decision to invoke --rerun-failed on an
    interference row's results is the operator's, not something this
    silently assumes is always correct."""
    failed_keys = load_failed_keys(session_dir)
    if not failed_keys:
        log.info("--rerun-failed: nothing recorded as FAILED, nothing to do")
        return completed
    log.info(
        f"--rerun-failed: re-attempting {len(failed_keys)} run(s) previously "
        f"recorded as FAILED: {sorted(failed_keys)}"
    )
    with open(manifest_path(session_dir)) as f:
        manifest_rows = list(csv.DictReader(f))
    # Newest first: a no-retry row reuses one capture filename across
    # reruns, so the file on disk belongs to the key's latest row and must
    # be archived under that row's start_time, not an older one's.
    for row in reversed(manifest_rows):
        if row.get("status") != "FAILED":
            continue
        key = run_key(row["row"], row["device"], row["condition"], row["replicate"])
        if key in failed_keys:
            archive_failed_capture_for_rerun(session_dir, row)
    return completed - failed_keys


def _set_manifest_verdict(session_dir, capture_file_name, start_time, status, error):
    """Rewrite the status/error of this run's manifest row with the verdict
    reached after the capture was parsed. The row is appended before that
    (so it survives a crash mid-verification) with the provisional status
    from the run itself; without this, a run that failed its link check
    (e.g. a baseline disconnect) stayed PASSED in manifest.csv even though
    results.csv said FAILED -- so `run --resume` skipped it as done and
    `--rerun-failed` never saw it (2026-10-01, row1 Corsano calibration)."""
    path = manifest_path(session_dir)
    if not path.exists():
        return
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames, rows = reader.fieldnames, list(reader)
    changed = False
    for row in rows:
        if row.get("capture_file") == capture_file_name and row.get("start_time") == start_time:
            if row.get("status") != status:
                row["status"], row["error"] = status, error
                changed = True
    if changed:
        tmp = path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(path)


MANIFEST_FIELDS = [
    "session_id",
    "row",
    "device",
    "condition",
    "replicate",
    "capture_file",
    "start_time",
    "duration_s",
    "status",
    "error",
    "induced_load_mbps",
    "packet_loss_pct",
    "iperf_jitter_ms",
    "qbss_channel_utilization_pct",
    "qbss_before_traffic_pct",
    "qbss_after_traffic_pct",
    "mac_address",
    "sniffer_pcap_file",
    "connection_established",
    "retry_on_fail",
]


def _upgrade_manifest_header(path):
    """Rewrite an existing manifest under MANIFEST_FIELDS if its header is
    older (e.g. resuming a session recorded before the qbss_*_traffic_pct
    columns existed) -- appending rows with more fields than the header
    would shift every later column."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == MANIFEST_FIELDS:
            return
        rows = list(reader)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def append_manifest(session_dir, row_dict):
    path = manifest_path(session_dir)
    is_new = not path.exists()
    if not is_new:
        _upgrade_manifest_header(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row_dict)
