#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pcloud_pool_gc as pgc  # noqa: E402


class RetentionApplyWiringTests(unittest.TestCase):
    def test_apply_aborts_when_snapshot_limit_exceeded(self) -> None:
        to_del = {f"2026-01-{i:02d}-120000" for i in range(1, 44)}
        env = {"PCLOUD_REMOTE_RETENTION_DAYS_FULL": "62"}
        with mock.patch.object(pgc, "_load_env_file", return_value=env):
            with mock.patch.object(pgc, "_list_local_rtb_snaps", return_value=set()):
                with mock.patch.object(pgc, "_list_remote_snapshot_names", return_value=to_del | {"keep"}):
                    with mock.patch.object(
                        pgc,
                        "_resolve_retention_deletes",
                        return_value=(to_del, "zeit:62d+54w", None),
                    ):
                        with mock.patch.object(pgc, "_retention_safety_abort", return_value=None):
                            with mock.patch.object(pgc.prs, "run_consistency_gate", return_value=None):
                                with mock.patch.object(pgc.prs, "validate_env_file_exists", return_value=None):
                                    result = pgc.run_retention_apply(
                                        {},
                                        "/pool",
                                        "/mnt/backup/rtb_nas",
                                        dry=False,
                                        env_file=".env",
                                        allow_rtb_mode=False,
                                        skip_consistency_check=True,
                                    )
        self.assertIn("aborted", result)
        self.assertEqual(result.get("deleted", 0), 0)


if __name__ == "__main__":
    unittest.main()
