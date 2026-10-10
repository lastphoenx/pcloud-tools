#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Peak-RSS eines Jobs messen (Linux: Paket ``time`` → /usr/bin/time -v).

Beispiel:
  python scripts/measure_job_rss.py -- python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --dry-run
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure peak RSS of a command")
    ap.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command (prefix with -- if it has flags)",
    )
    args = ap.parse_args()
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("Command required, e.g. measure_job_rss.py -- python script.py ...")
    time_bin = shutil.which("time")
    if time_bin and os.name != "nt":
        return subprocess.run([time_bin, "-v", *cmd], check=False).returncode
    print(
        "[measure_job_rss] GNU time nicht gefunden — auf pi-nas: /usr/bin/time -v …",
        file=sys.stderr,
    )
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
