#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pcloud_push_json_pool_manifest_to_pcloud as push  # noqa: E402


class BackupIndexBootstrapTests(unittest.TestCase):
    def test_bootstrap_skips_when_remote_index_missing(self) -> None:
        db = mock.MagicMock()
        with mock.patch.object(push.pc, "stat_file_safe", return_value={}):
            with mock.patch("pool_gc_index.download_remote_master") as dl:
                push._bootstrap_backup_db_from_remote_master(
                    {"token": "t"},
                    db,
                    "/tmp/content_index_master.json",
                    "/backup-root/_snapshots",
                )
        dl.assert_not_called()
        db.import_from_json_streaming.assert_not_called()

    @mock.patch.dict(os.environ, {"PCLOUD_POOL_INDEX_DB": "1"})
    def test_open_pool_index_uses_passed_cfg_not_effective_config(self) -> None:
        db = mock.MagicMock()
        db.count_shas.return_value = 0
        db.master_fingerprint_matches.return_value = False
        cfg = {"host": "example", "token": "secret"}
        with mock.patch.object(push.pc, "effective_config", side_effect=AssertionError("no")):
            with mock.patch.object(push.pc, "stat_file_safe", return_value={}):
                with mock.patch("pool_index_db.open_db", return_value=db):
                    with mock.patch(
                        "pool_index_db.default_master_path",
                        return_value="/nope/content_index_master.json",
                    ):
                        with mock.patch(
                            "pool_index_db.default_db_path",
                            return_value="/tmp/pool_index.sqlite3",
                        ):
                            with mock.patch.object(push.os.path, "isfile", return_value=False):
                                out = push._open_pool_index_db_for_run(
                                    cfg, "/backup-root/_snapshots",
                                )
        self.assertIs(out, db)


if __name__ == "__main__":
    unittest.main()
