#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_gc_manifest_guard as mg  # noqa: E402
import pool_index_db as pidb  # noqa: E402


class ManifestGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.manifests = os.path.join(self.tmp, "manifests")
        os.makedirs(self.manifests)
        self.ops_db = os.path.join(self.tmp, "pool_index_gc.sqlite3")
        db = pidb.PoolIndexDB(self.ops_db)
        db.register_batch(
            "snap-ok",
            [("c" * 64, "file.bin", 1, 1, 10)],
        )
        db.close()

    def _write_manifest(self, snap: str, shas: list[str]) -> None:
        items = [{"type": "file", "path": f"/{i}.bin", "sha256": s} for i, s in enumerate(shas)]
        with open(os.path.join(self.manifests, f"{snap}.json"), "w", encoding="utf-8") as f:
            json.dump({"items": items}, f)

    def test_abort_when_live_missing_index_and_manifest(self) -> None:
        logs: list[str] = []
        guard = mg.build_gc_manifest_guard(
            {"snap-missing"},
            self.ops_db,
            {"PCLOUD_ARCHIVE_DIR": self.tmp},
            log=logs.append,
        )
        self.assertIsNotNone(guard.abort_reason)
        self.assertIn("snap-missing", guard.missing_manifest)

    def test_protects_manifest_shas_when_missing_in_ops_db(self) -> None:
        sha = "a" * 64
        self._write_manifest("snap-gap", [sha])
        logs: list[str] = []

        class FakeLookup:
            def __contains__(self, item: object) -> bool:
                return False

            def close(self) -> None:
                pass

        guard = mg.build_gc_manifest_guard(
            {"snap-gap"},
            self.ops_db,
            {"PCLOUD_ARCHIVE_DIR": self.tmp},
            log=logs.append,
        )
        self.assertIsNone(guard.abort_reason)
        self.assertEqual(guard.missing_in_ops_db, ["snap-gap"])
        lookup = mg.ManifestProtectedShaLookup(FakeLookup(), guard.protected)
        self.assertIn(sha, lookup)
        guard.protected.close()

    def test_no_gap_skips_manifest_io(self) -> None:
        logs: list[str] = []
        guard = mg.build_gc_manifest_guard(
            {"snap-ok"},
            self.ops_db,
            {"PCLOUD_ARCHIVE_DIR": self.tmp},
            log=logs.append,
        )
        self.assertIsNone(guard.abort_reason)
        self.assertEqual(guard.manifest_gap_snapshots, [])
        self.assertEqual(len(guard.protected), 0)
        self.assertTrue(any("kein Manifest-Lesen" in ln for ln in logs))
        guard.protected.close()

    def test_zero_refs_counts_as_gap(self) -> None:
        db = pidb.PoolIndexDB(self.ops_db)
        db._ensure_snapshot("snap-empty-idx")
        db.conn.commit()
        db.close()
        sha = "b" * 64
        self._write_manifest("snap-empty-idx", [sha])
        guard = mg.build_gc_manifest_guard(
            {"snap-empty-idx"},
            self.ops_db,
            {"PCLOUD_ARCHIVE_DIR": self.tmp},
            log=lambda _m: None,
        )
        self.assertIsNone(guard.abort_reason)
        self.assertEqual(guard.zero_refs_in_ops_db, ["snap-empty-idx"])
        self.assertIn(sha, guard.protected)
        guard.protected.close()

    def test_corrupt_manifest_aborts_gap_snapshot(self) -> None:
        snap = "snap-bad"
        with open(os.path.join(self.manifests, f"{snap}.json"), "w", encoding="utf-8") as f:
            f.write("{not-json")
        guard = mg.build_gc_manifest_guard(
            {snap},
            self.ops_db,
            {"PCLOUD_ARCHIVE_DIR": self.tmp},
            log=lambda _m: None,
        )
        self.assertIsNotNone(guard.abort_reason)
        self.assertIn(snap, guard.corrupt_manifest)
        guard.protected.close()


if __name__ == "__main__":
    unittest.main()
