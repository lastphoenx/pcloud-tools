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

        with mock.patch.object(
            gci, "remote_master_sha256", return_value=old_remote_sha,
        ):
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

    def test_retry_aborts_when_remote_index_advanced(self) -> None:
        db = pidb.open_db(self.ops_db, create=True)
        db.import_from_json_streaming(self.master)
        db.set_meta("master_sha256", "aa" * 64)
        db.set_meta("upload_pending", "1")
        db.set_meta("master_pending_sha256", "bb" * 64)
        db.set_meta("upload_pending_snaps", "snap-a")
        db.close()

        logs: list[str] = []

        def log(msg: str) -> None:
            logs.append(msg)

        with mock.patch.object(
            gci, "remote_master_sha256", return_value="cc" * 32,
        ):
            with mock.patch.object(gci, "_upload_master_resumable") as up:
                out = gci.maybe_flush_pending_master_upload(
                    {}, "/pool/_snapshots", self._env(), log=log,
                )
                up.assert_not_called()

        self.assertFalse(out)
        db2 = pidb.open_db(self.ops_db, create=False)
        self.assertEqual(db2.get_meta("upload_pending"), "0")
        self.assertEqual(db2.get_meta("reapply_purge_after_sync"), "snap-a")
        db2.close()

    def test_query_path_does_not_upload_pending(self) -> None:
        db = pidb.open_db(self.ops_db, create=True)
        db.import_from_json_streaming(self.master)
        db.set_meta("upload_pending", "1")
        db.set_meta("master_pending_sha256", "dd" * 32)
        db.close()
        logs: list[str] = []

        def log(msg: str) -> None:
            logs.append(msg)

        with mock.patch.object(gci, "_open_ops_db_if_current") as cur:
            cur.return_value = pidb.open_db(self.ops_db, create=False)
            with mock.patch.object(gci, "_upload_master_resumable") as up:
                gci.open_ops_db_for_queries(
                    {}, "/pool/_snapshots", self._env(), log=log,
                )
                up.assert_not_called()
        self.assertTrue(any("nur Lesen" in m for m in logs))

    def test_noop_purge_after_sync_skips_export(self) -> None:
        db = pidb.open_db(self.ops_db, create=True)
        db.import_from_json_streaming(self.master)
        db.close()
        logs: list[str] = []

        def log(msg: str) -> None:
            logs.append(msg)

        with mock.patch.object(
            gci, "open_synced_db",
            side_effect=lambda *a, **k: pidb.open_db(self.ops_db, create=False),
        ):
            with mock.patch.object(gci, "export_master_from_db") as exp:
                stats = gci.apply_index_purge_for_deleted_snaps(
                    {},
                    "/pool/_snapshots",
                    self._env(),
                    {"snap-missing"},
                    dry=False,
                    log=log,
                )
                exp.assert_not_called()
        self.assertEqual(stats["removed_snap_refs"], 0)

    def test_reapply_keeps_meta_if_purge_fails(self) -> None:
        db = pidb.open_db(self.ops_db, create=True)
        db.set_meta("reapply_purge_after_sync", "snap-x,snap-y")
        db.close()

        with mock.patch.object(
            gci,
            "apply_index_purge_for_deleted_snaps",
            side_effect=RuntimeError("disk"),
        ):
            with self.assertRaises(RuntimeError):
                gci._reapply_purge_after_remote_drift(
                    {}, "/pool/_snapshots", self._env(), log=lambda _m: None,
                )
        db2 = pidb.open_db(self.ops_db, create=False)
        self.assertEqual(db2.get_meta("reapply_purge_after_sync"), "snap-x,snap-y")
        db2.close()


class ReferencedShaIterTests(unittest.TestCase):
    def test_lookup_iter_shas_matches_count(self) -> None:
        tmp = tempfile.mkdtemp()
        ops_db = os.path.join(tmp, "pool_index_gc.sqlite3")
        db = pidb.PoolIndexDB(ops_db)
        db.register_batch("snap-a", [("aa" * 32, "p", 1, 1, 1)])
        db.register_batch("snap-b", [("bb" * 32, "q", 2, 2, 2)])
        db.close()
        db2 = pidb.PoolIndexDB(ops_db)
        lookup = gci.RemoteSnapReferencedShaLookup(db2, {"snap-a", "snap-b"})
        shas = list(lookup.iter_shas())
        lookup.close()
        self.assertEqual(len(shas), 2)
        self.assertEqual(set(shas), {"aa" * 32, "bb" * 32})


if __name__ == "__main__":
    unittest.main()
