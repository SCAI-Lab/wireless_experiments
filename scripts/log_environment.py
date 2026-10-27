#!/usr/bin/env python3
"""Record this laptop's software and firmware (and the iperf3 load client's,
over SSH) without starting a session. Sessions do this automatically at
start -- see session_lib/environment.py.

    python3 scripts/log_environment.py [OUT_DIR] [--config config.yaml]
"""

import argparse
import logging
import time
from pathlib import Path

from session_lib import load_config, snapshot_environment

logging.basicConfig(level=logging.INFO, format="%(message)s")
p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("out_dir", nargs="?", default=f"environment_{time.strftime('%Y%m%d_%H%M%S')}")
p.add_argument("--config", default="config.yaml")
args = p.parse_args()
Path(args.out_dir).mkdir(parents=True, exist_ok=True)
snapshot_environment(load_config(args.config), args.out_dir)
