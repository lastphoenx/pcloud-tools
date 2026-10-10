#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_consistency_lib as pcl  # noqa: E402


class PoolConsistencyLibTests(unittest.TestCase):
    def test_critical_live_missing_ops(self) -> None:
        r = pcl.evaluate_pool_consistency(
            live={"a", "b"},
            ops_db_names={"a"},
            backup_db_names={"a", "b"},
            manifest_names={"a", "b"},
        )
        self.assertEqual(r["level"], pcl.LEVEL_CRITICAL)
        self.assertEqual(r["live_missing_ops_db"], ["b"])

    def test_critical_zero_ops_refs(self) -> None:
        r = pcl.evaluate_pool_consistency(
            live={"a"},
            ops_db_names={"a"},
            backup_db_names={"a"},
            manifest_names={"a"},
            live_ops_zero_refs=["a"],
        )
        self.assertEqual(r["level"], pcl.LEVEL_CRITICAL)
        self.assertIn("live_zero_ops_refs:1", r["critical_reasons"])

    def test_warn_stale_only(self) -> None:
        r = pcl.evaluate_pool_consistency(
            live={"a"},
            ops_db_names={"a", "old"},
            backup_db_names={"a"},
            manifest_names={"a"},
        )
        self.assertEqual(r["level"], pcl.LEVEL_WARN)

    def test_critical_live_drop(self) -> None:
        live = {f"s{i:03d}" for i in range(80)}
        r = pcl.evaluate_pool_consistency(
            live=live,
            ops_db_names=live,
            backup_db_names=live,
            manifest_names=live,
            last_live_count=100,
        )
        self.assertEqual(r["level"], pcl.LEVEL_CRITICAL)


if __name__ == "__main__":
    unittest.main()
