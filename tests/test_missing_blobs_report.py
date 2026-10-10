#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTIL = os.path.join(ROOT, "scripts", "utilities")
if UTIL not in sys.path:
    sys.path.insert(0, UTIL)

import missing_blobs_report as mbr  # noqa: E402


class MissingBlobsReportTests(unittest.TestCase):
    def test_missing_shas_from_report(self) -> None:
        tmp = tempfile.mkdtemp()
        rep = os.path.join(tmp, "chk_snap-a.json")
        with open(rep, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "snapshot": "snap-a",
                    "manifest_vs_pool": {
                        "per_snapshot": {
                            "snap-a": {"missing_shas": ["aa" * 32], "missing_count": 1},
                        },
                    },
                },
                f,
            )
        self.assertEqual(mbr.missing_shas_from_report(rep, "snap-a"), {"aa" * 32})


if __name__ == "__main__":
    unittest.main()
