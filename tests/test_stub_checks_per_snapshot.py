#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTIL = os.path.join(ROOT, "scripts", "utilities")
if UTIL not in sys.path:
    sys.path.insert(0, UTIL)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_verify_backup as pvb  # noqa: E402


class StubChecksPerSnapshotTests(unittest.TestCase):
    def test_calls_check_stubs_with_snaps_root(self) -> None:
        manifests = {"snap-a": {"f.txt": "aa" * 32}}
        stub_paths = {"/pool/_snapshots/snap-a/f.txt.meta.json"}

        with mock.patch.object(
            pvb, "_fetch_pool_refs", return_value=({"aa" * 32: {"snapshots": {"snap-a": ["f.txt"]}}}, "archive"),
        ):
            with mock.patch.object(pvb, "check_stubs_vs_index") as csi:
                csi.return_value = {
                    "missing_from_index": 0,
                    "manifest_missing_total": 0,
                    "mode": "manifest_scoped",
                }
                pvb._stub_checks_per_snapshot(
                    {},
                    "/pool/_snapshots",
                    manifests,
                    stub_paths,
                    ["snap-a"],
                )
        csi.assert_called_once()
        args, kwargs = csi.call_args
        self.assertEqual(args[2], "/pool/_snapshots")
        self.assertEqual(kwargs.get("snapshot_filter"), {"snap-a"})

    def test_weak_index_skips_index_missing_counts(self) -> None:
        manifests = {"snap-a": {"f.txt": "bb" * 32}}
        with mock.patch.object(
            pvb,
            "_fetch_pool_refs",
            return_value=({}, "archive/snap-a_index.json (noch nicht vorhanden — Manifest-only)"),
        ):
            with mock.patch.object(pvb, "check_stubs_vs_index") as csi:
                csi.return_value = {
                    "missing_from_index": 99,
                    "missing_from_index_examples": ["/bad"],
                    "manifest_missing_total": 0,
                    "manifest_missing_stubs": {},
                    "mode": "manifest_scoped",
                }
                merged, weak, _src = pvb._stub_checks_per_snapshot(
                    {}, "/pool/_snapshots", manifests, set(), ["snap-a"],
                )
        self.assertIn("snap-a", weak)
        self.assertEqual(merged.get("missing_from_index"), 0)
        self.assertEqual(merged.get("index_check_skipped_snapshots"), ["snap-a"])

    def test_empty_refs_with_present_archive_not_weak(self) -> None:
        manifests = {"snap-a": {"f.txt": "cc" * 32}}
        with mock.patch.object(
            pvb,
            "_fetch_pool_refs",
            return_value=({}, "archive/snap-a_index.json (0 refs)"),
        ):
            with mock.patch.object(pvb, "check_stubs_vs_index") as csi:
                csi.return_value = {
                    "missing_from_index": 0,
                    "manifest_missing_total": 0,
                    "mode": "manifest_scoped",
                }
                merged, weak, _src = pvb._stub_checks_per_snapshot(
                    {}, "/pool/_snapshots", manifests, set(), ["snap-a"],
                )
        self.assertNotIn("snap-a", weak)
        csi.assert_called_once()
        # Archiv vorhanden, aber 0 pool_refs — kein Weak-Skip, normale Check-B mit leerem Index
        self.assertEqual(csi.call_args[0][0], {})
        self.assertNotIn("snap-a", merged.get("index_check_skipped_snapshots") or [])


if __name__ == "__main__":
    unittest.main()
