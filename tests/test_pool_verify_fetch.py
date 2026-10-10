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


class PoolVerifyFetchTests(unittest.TestCase):
    def test_merge_pool_refs_combines_snapshots(self) -> None:
        into = {
            "aa": {"snapshots": {"s1": ["a.txt"]}, "fileid": 1},
        }
        pvb._merge_pool_refs(into, {
            "aa": {"snapshots": {"s2": ["b.txt"]}},
            "bb": {"snapshots": {"s2": ["c.txt"]}},
        })
        self.assertEqual(into["aa"]["snapshots"]["s1"], ["a.txt"])
        self.assertEqual(into["aa"]["snapshots"]["s2"], ["b.txt"])
        self.assertIn("bb", into)

    def test_fetch_pool_refs_multi_uses_archives_not_master(self) -> None:
        cfg = {"token": "t"}
        archives = {
            "/snap/_index/archive/a_index.json": {"pool_refs": {"x": {"snapshots": {"a": []}}}},
            "/snap/_index/archive/b_index.json": {"pool_refs": {"y": {"snapshots": {"b": []}}}},
        }

        def _load(_cfg, path):
            return archives.get(path)

        with mock.patch.object(pvb, "_load_remote_json_at", side_effect=_load):
            refs, src = pvb._fetch_pool_refs(cfg, "/snap", ["a", "b"])
        self.assertIn("archive merge", src)
        self.assertEqual(set(refs.keys()), {"x", "y"})

    def test_fetch_pool_refs_multi_fails_if_archive_missing(self) -> None:
        def _load(_cfg, path):
            if path.endswith("a_index.json"):
                return {"pool_refs": {"x": {}}}
            return None

        with mock.patch.object(pvb, "_load_remote_json_at", side_effect=_load):
            with self.assertRaises(RuntimeError):
                pvb._fetch_pool_refs({}, "/snap", ["a", "b"])

    def test_load_remote_json_propagates_timeout(self) -> None:
        with mock.patch.object(
            pvb.pc, "classify_remote_file_stat", side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaises(TimeoutError):
                pvb._load_remote_json_at({}, "/snap/_index/archive/x_index.json")

    def test_fetch_all_manifest_snaps_via_archives(self) -> None:
        archives = {
            "/snap/_index/archive/s1_index.json": {"pool_refs": {"aa": {}}},
        }

        def _load(_cfg, path):
            return archives.get(path)

        with mock.patch.object(pvb, "_load_remote_json_at", side_effect=_load):
            refs, src = pvb._fetch_pool_refs(
                {}, "/snap", None, archive_snapshots=["s1"],
            )
        self.assertIn("alle remote", src)
        self.assertIn("aa", refs)


if __name__ == "__main__":
    unittest.main()
