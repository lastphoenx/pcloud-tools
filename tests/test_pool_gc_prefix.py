#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pcloud_pool_gc as pgc  # noqa: E402
import pcloud_bin_lib as pc  # noqa: E402


class PoolGcPrefixIterTests(unittest.TestCase):
    def test_iter_pool_files_by_prefix_sorts_without_dict_compare(self) -> None:
        top = {
            "metadata": {
                "contents": [
                    {"isfolder": True, "name": "bb", "path": "/pool/bb"},
                    {"isfolder": True, "name": "aa", "path": "/pool/aa"},
                ],
            },
        }
        empty_tree = {"metadata": {}}

        recursive_paths: list[str] = []

        def _lf(cfg, **kwargs):
            path = kwargs.get("path")
            if path == "/pool" and not kwargs.get("recursive"):
                return top
            if kwargs.get("recursive"):
                recursive_paths.append(path)
            return empty_tree

        with mock.patch.object(pc, "listfolder", side_effect=_lf):
            with mock.patch.object(pgc, "_log"):
                list(pgc._iter_pool_files_by_prefix({}, "/pool"))
        self.assertEqual(recursive_paths, ["/pool/aa", "/pool/bb"])


class PoolGcAuditModeTests(unittest.TestCase):
    def test_audit_mode_aborts_before_work(self) -> None:
        with mock.patch.object(pgc, "_log"):
            with mock.patch.object(pgc, "_load_env_file", return_value={}):
                out = pgc.run_pool_gc(
                    {"token": "t", "device": "x", "timeout": 30},
                    "/Backup/pool",
                    dry=True,
                    audit_mode=True,
                )
        self.assertTrue(out.get("aborted"))
        self.assertEqual(out.get("error"), "audit_mode_disabled")


if __name__ == "__main__":
    unittest.main()
