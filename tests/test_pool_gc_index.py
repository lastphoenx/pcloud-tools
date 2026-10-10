#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests für pool_gc_index (Upload-Pending, Purge ohne erfolgreichen Upload)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_gc_index as gci
import pool_index_db as pidb


def _mini_master(path: str) -> None:
    obj = {
        "version": 2,
        "pool_refs": {
            "aa" * 32: {
                "fileid": 1,
                "hash": 1,
                "size": 10,
                "snapshots": {"snap-a": ["p/x"]},
            },
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


class UploadPendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.master = os.path.join(self.tmp, "content_index_master.json")
        self.staging = os.path.join(self.tmp, "staging", "content_index_pending.json")
        self.ops_db = os.path.join(self.tmp, "pool_index_gc.sqlite3")
        _mini_master(self.master)
        os.makedirs(os.path.dirname(self.staging), exist_ok=True)

    def _env(self) -> dict:
        return {
            "PCLOUD_ARCHIVE_DIR": self.tmp,
            "PCLOUD_POOL_INDEX_MASTER": self.master,
            "PCLOUD_GC_INDEX_DB_PATH": self.ops_db,
        }

    def test_upload_failure_leaves_pending_and_retry_clears(self) -> None:
        db = pidb.open_db(self.ops_db, create=True)
        db.import_from_json_streaming(self.master)
        db.close()

        cfg = {}
        snaps_root = "/pool/_snapshots"
        deleted = {"snap-a"}
        logs: list[str] = []

        def log(msg: str) -> None:
            logs.append(msg)

        with mock.patch.object(gci, "open_synced_db") as open_mock:
            open_mock.side_effect = lambda *a, **k: pidb.open_db(self.ops_db, create=False)
            with mock.patch.object(gci, "_upload_master_resumable", side_effect=RuntimeError("net down")):
                with self.assertRaises(RuntimeError):
                    gci.apply_index_purge_for_deleted_snaps(
                        cfg, snaps_root, self._env(), deleted, dry=False, log=log,
                    )

        db2 = pidb.open_db(self.ops_db, create=False)
        self.assertEqual(db2.get_meta("upload_pending"), "1")
        self.assertEqual(db2.snap_ref_count("snap-a"), 0)
        old_remote_sha = db2.get_meta("master_sha256")
        db2.close()

        with mock.patch.object(gci, "_upload_master_resumable"):
            gci.maybe_flush_pending_master_upload(
                cfg, snaps_root, self._env(), log=log,
            )

        db3 = pidb.open_db(self.ops_db, create=False)
        self.assertEqual(db3.get_meta("upload_pending"), "0")
        self.assertTrue((db3.get_meta("master_sha256") or "").strip())
        if old_remote_sha:
            self.assertNotEqual(db3.get_meta("master_sha256"), old_remote_sha)
        db3.close()


if __name__ == "__main__":
    unittest.main()
