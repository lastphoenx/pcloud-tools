#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pcloud_pool_gc as pgc  # noqa: E402


class LocalManifestRemovalTests(unittest.TestCase):
    def test_remove_existing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifests = os.path.join(tmp, "manifests")
            os.makedirs(manifests)
            snap = "2026-08-06-172826"
            path = os.path.join(manifests, f"{snap}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            env = {"PCLOUD_ARCHIVE_DIR": tmp}
            self.assertTrue(pgc._remove_local_manifest_for_snap(snap, env, dry=False))
            self.assertFalse(os.path.isfile(path))

    def test_dry_run_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifests = os.path.join(tmp, "manifests")
            os.makedirs(manifests)
            snap = "2026-08-10-040001"
            path = os.path.join(manifests, f"{snap}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            env = {"PCLOUD_ARCHIVE_DIR": tmp}
            self.assertTrue(pgc._remove_local_manifest_for_snap(snap, env, dry=True))
            self.assertTrue(os.path.isfile(path))

    def test_missing_manifest_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PCLOUD_ARCHIVE_DIR": tmp}
            self.assertFalse(
                pgc._remove_local_manifest_for_snap("2026-01-01-120000", env, dry=False)
            )


if __name__ == "__main__":
    unittest.main()
