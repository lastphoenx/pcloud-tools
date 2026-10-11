#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_DIR = os.path.join(ROOT, "scripts", "utilities")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SYNC_DIR not in sys.path:
    sys.path.insert(0, SYNC_DIR)

import pool_ops_index_sync as sync  # noqa: E402


class PoolOpsIndexSyncTests(unittest.TestCase):
    def test_main_success_counts_and_exits_0(self) -> None:
        fake_db = mock.MagicMock()
        fake_db.snapshot_names.return_value = ["a", "b"]
        fake_db.count_shas.return_value = 42

        with mock.patch.object(sync.pc, "effective_config", return_value={}):
            with mock.patch.object(sync.pgc, "_load_env_file", return_value={"PCLOUD_DEST": "/pool"}):
                with mock.patch.object(sync.gci, "open_synced_db", return_value=fake_db):
                    rc = sync.main()
        self.assertEqual(rc, 0)
        fake_db.close.assert_called_once()

    def test_main_missing_dest_exits_2(self) -> None:
        with mock.patch.object(sync.pc, "effective_config", return_value={}):
            with mock.patch.object(sync.pgc, "_load_env_file", return_value={}):
                rc = sync.main()
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
