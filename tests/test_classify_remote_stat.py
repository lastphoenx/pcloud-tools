#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pcloud_bin_lib as pc  # noqa: E402


class ClassifyRemoteFileStatTests(unittest.TestCase):
    def test_absent_on_2055(self) -> None:
        with mock.patch.object(
            pc, "stat_file", side_effect=RuntimeError("API error 2055: not found"),
        ):
            self.assertEqual(pc.classify_remote_file_stat({"token": "t"}, path="/x"), "absent")

    def test_present_with_fileid(self) -> None:
        with mock.patch.object(pc, "stat_file", return_value={"fileid": 99}):
            self.assertEqual(pc.classify_remote_file_stat({"token": "t"}, path="/x"), "present")

    def test_timeout_propagates(self) -> None:
        with mock.patch.object(pc, "stat_file", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TimeoutError):
                pc.classify_remote_file_stat({"token": "t"}, path="/x")


if __name__ == "__main__":
    unittest.main()
