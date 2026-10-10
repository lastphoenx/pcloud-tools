#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_gap_report as pgr  # noqa: E402
import pool_index_db as pidb  # noqa: E402


class PoolGapReportSqlTests(unittest.TestCase):
    def test_refs_for_shas_joins_live_only(self) -> None:
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "ops.sqlite3")
        db = pidb.PoolIndexDB(db_path)
        sha = "aa" * 32
        db.register_batch("snap-live", [(sha, "path/a", 9, 1, 100)])
        db.register_batch("snap-stale", [(sha, "path/b", 9, 1, 100)])
        db.close()
        rows = pgr._refs_for_shas(db_path, {"snap-live"}, [sha])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "snap-live")
        self.assertEqual(rows[0][2], "path/a")


if __name__ == "__main__":
    unittest.main()
