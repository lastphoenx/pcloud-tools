#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import env_file_util as efu  # noqa: E402


class EnvFileUtilTests(unittest.TestCase):
    def test_strip_inline_comment(self) -> None:
        self.assertEqual(efu.strip_env_value("30   # test"), "30")
        self.assertEqual(efu.strip_env_value('  "30"  # x'), "30")

    def test_load_env_file_strips_comments(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".env") as f:
            f.write("PCLOUD_RETENTION_MAX_DELETE_PCT=30   # comment\n")
            f.write("FOO=bar\n")
            path = f.name
        try:
            env = efu.load_env_file(path)
            self.assertEqual(env["PCLOUD_RETENTION_MAX_DELETE_PCT"], "30")
            self.assertEqual(env["FOO"], "bar")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
