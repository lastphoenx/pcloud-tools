#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI für Bash-Wrapper: Catch-up-Liste filtern, JSON auf stdout."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from catchup_retention_filter import filter_catchup_missing  # noqa: E402
from env_file_util import load_env_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Catch-up retention/env filter (JSON)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--rtb-root", default=None)
    ap.add_argument("--missing", nargs="*", default=[], metavar="SNAP")
    ap.add_argument("--remote-complete", nargs="*", default=[], metavar="SNAP")
    ap.add_argument("--local", nargs="*", default=[], metavar="SNAP")
    ap.add_argument("--latest", default=None, help="Override latest snap name")
    ap.add_argument("--max-skip-warn", type=int, default=5)
    args = ap.parse_args()

    env_vars = load_env_file(args.env_file)
    rtb = args.rtb_root or env_vars.get("RTB") or os.environ.get("RTB", "/mnt/backup/rtb_nas")

    result = filter_catchup_missing(
        list(args.missing),
        set(args.remote_complete),
        set(args.local),
        env_vars,
        latest_name=args.latest or None,
        rtb_root=rtb,
        max_skip_warn=args.max_skip_warn,
    )
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
