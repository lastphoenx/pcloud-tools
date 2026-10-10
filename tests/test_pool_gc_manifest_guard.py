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
        db._ensure_snapshot("snap-ok")
        db.conn.commit()
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
        lookup = mg.ManifestProtectedShaLookup(FakeLookup(), guard.protected_shas)
        self.assertIn(sha, lookup)

    def test_empty_archive_index_not_weak_via_manifest_load(self) -> None:
        self._write_manifest("snap-empty", [])
        protected = mg.load_manifest_shas_for_snapshots(
            self.manifests, {"snap-empty"},
        )
        self.assertEqual(protected, set())


if __name__ == "__main__":
    unittest.main()
