#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

UTIL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "utilities",
)
if UTIL not in sys.path:
    sys.path.insert(0, UTIL)

import pool_restore as pr  # noqa: E402


class ResolveFileidTests(unittest.TestCase):
    def test_prefers_live_pool_fileid_over_stale_stub(self) -> None:
        cfg = {"token": "t"}
        item = {"sha256": "aa" * 32, "fileid": 999}
        with mock.patch.object(
            pr.pc,
            "stat_file",
            return_value={"fileid": 12345},
        ) as st:
            fid = pr._resolve_fileid(cfg, "/pool", item)
        self.assertEqual(fid, 12345)
        st.assert_called_once()

    def test_falls_back_to_item_fileid_when_pool_missing(self) -> None:
        cfg = {}
        item = {"sha256": "bb" * 32, "fileid": 777}
        with mock.patch.object(pr.pc, "stat_file", side_effect=OSError("nope")):
            fid = pr._resolve_fileid(cfg, "/pool", item)
        self.assertEqual(fid, 777)

    def test_stub_only_when_no_sha(self) -> None:
        fid = pr._resolve_fileid({}, "/pool", {"fileid": 55})
        self.assertEqual(fid, 55)


if __name__ == "__main__":
    unittest.main()
