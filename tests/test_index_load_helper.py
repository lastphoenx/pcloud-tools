#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTIL = os.path.join(ROOT, "scripts", "utilities")
if UTIL not in sys.path:
    sys.path.insert(0, UTIL)

import index_load_helper as ilh  # noqa: E402


class IndexLoadHelperTests(unittest.TestCase):
    def test_is_v2_pool_index(self) -> None:
        self.assertTrue(ilh.is_v2_pool_index({"version": 2, "pool_refs": {"a": {}}}))
        self.assertFalse(ilh.is_v2_pool_index({"version": 1, "items": {"x": {}}}))

    def test_legacy_rejects_remote_v2(self) -> None:
        v2 = json.dumps({"version": 2, "pool_refs": {"aa": {}}})
        with mock.patch.object(ilh.pc, "get_textfile", return_value=v2):
            with self.assertRaises(RuntimeError):
                ilh.load_content_index_legacy_items(
                    {}, "/pool/_snapshots", prefer_local=False,
                )


if __name__ == "__main__":
    unittest.main()
