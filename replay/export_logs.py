#!/usr/bin/env python3
"""
Export replay logs for publication (Zenodo): a cleaned copy, one zip per set
============================================================================

The logs are never modified. Each set of tags is copied to
<out>/staging/<name>/ and the copy is cleaned: in every text file, the
strings given with --redact OLD=NEW (the SLURM user, the node's hostname)
and /home/<user> paths are replaced. Afterwards no file may contain an OLD
string, a /home/ path or an IP address other than loopback; otherwise the
export stops. analyze_e2e.py reads none of the changed lines (they are in
the SLURM job's environment and output files).

Usage:
    python3 export_logs.py --out ~/zenodo --redact user=USER --redact node=NODE \\
        --set replay_logs_chamber logs_slurm/full_W1_sessions logs_slurm/sweep_W0.1_sessions ... \\
        --set replay_logs_gym logs_slurm/gym_W1_sessions ...
"""

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

TEXT_SUFFIXES = {".log", ".json", ".jsonl", ".csv", ".txt", ".out", ".md"}
HOME_RE = re.compile(r"/home/[A-Za-z0-9._-]+")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}


def clean_text(text, redact):
    text = HOME_RE.sub("~", text)
    for old, new in redact:
        text = re.sub(rf"\b{re.escape(old)}\b", new, text)
    return text


def leftovers(text, redact):
    found = {old for old, _ in redact if re.search(rf"\b{re.escape(old)}\b", text)}
    found |= set(HOME_RE.findall(text))
    # versions like 2.0.51.1 also match; only flag plausible addresses
    found |= {ip for ip in IP_RE.findall(text)
              if ip not in ALLOWED_IPS and all(int(x) < 256 for x in ip.split("."))
              and ip.split(".")[0] in ("10", "172", "192")}
    return found


def export_set(name, paths, staging, redact):
    dst_root = staging / name
    shutil.rmtree(dst_root, ignore_errors=True)
    dst_root.mkdir(parents=True)
    changed = 0
    for src in paths:
        dst = dst_root / src.name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    for p in sorted(dst_root.rglob("*")):
        if p.is_file() and p.suffix in TEXT_SUFFIXES:
            text = p.read_text(errors="replace")
            new = clean_text(text, redact)
            left = leftovers(new, redact)
            assert not left, f"{p}: {sorted(left)[:5]} remain"
            if new != text:
                p.write_text(new)
                changed += 1
                print(f"  cleaned {p.relative_to(staging)}")
    return dst_root, changed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--redact", action="append", default=[], metavar="OLD=NEW")
    ap.add_argument("--set", nargs="+", action="append", required=True, metavar=("NAME", "PATH"),
                    help="zip name followed by the tag folders and files it holds")
    args = ap.parse_args()
    redact = [tuple(r.split("=", 1)) for r in args.redact]
    staging = args.out / "staging"
    for name, *paths in args.set:
        print(f"== {name}")
        dst, n = export_set(name, [Path(p) for p in paths], staging, redact)
        print(f"  {n} files cleaned")
        z = args.out / f"{name}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(dst.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(staging))
        print(f"  -> {z} ({z.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    sys.exit(main())
