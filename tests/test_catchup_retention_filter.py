#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import catchup_retention_filter as crf  # noqa: E402
import pool_retention_safety as prs  # noqa: E402


class CatchupRetentionFilterTests(unittest.TestCase):
    def _env_time(self, days_full: int = 62) -> dict:
        return {
            "PCLOUD_REMOTE_RETENTION_DAYS_FULL": str(days_full),
            "PCLOUD_REMOTE_RETENTION_WEEKLY_WEEKS": "54",
        }

    def test_old_retention_candidate_skipped(self) -> None:
        old = "2026-08-06-172826"
        remote = {"2026-10-11-040229"}
        local = {old, "2026-10-11-040229"}
        res = crf.filter_catchup_missing([old], remote, local, self._env_time())
        self.assertTrue(res["ok"])
        self.assertEqual(res["catchup"], [])
        self.assertIn(old, res["skipped_retention"])

    def test_young_missing_stays_in_catchup(self) -> None:
        young = "2026-10-10-040228"
        remote = set()
        local = {young}
        res = crf.filter_catchup_missing([young], remote, local, self._env_time())
        self.assertTrue(res["ok"])
        self.assertEqual(res["catchup"], [young])
        self.assertEqual(res["skipped_retention"], [])

    def test_latest_never_skipped_even_if_retention_candidate(self) -> None:
        old = "2026-08-06-172826"
        latest = "2026-10-11-040229"
        remote = {latest}
        local = {old, latest}
        res = crf.filter_catchup_missing(
            [old, latest], remote, local, self._env_time(), latest_name=latest,
        )
        self.assertIn(latest, res["catchup"])
        self.assertIn(old, res["skipped_retention"])

    def test_env_skip_and_retention_together(self) -> None:
        old = "2026-08-06-172826"
        legacy = "2026-05-21-040007"
        env = {
            **self._env_time(),
            "PCLOUD_CATCHUP_SKIP_SNAPSHOTS": legacy,
        }
        remote = {"2026-10-11-040229"}
        local = {old, legacy, "2026-10-11-040229"}
        res = crf.filter_catchup_missing(
            [old, legacy], remote, local, env,
        )
        self.assertEqual(res["catchup"], [])
        self.assertIn(old, res["skipped_retention"])
        self.assertIn(legacy, res["skipped_env"])

    def test_rtb_mirror_mode_no_retention_filter(self) -> None:
        old = "2026-08-06-172826"
        env = {
            "PCLOUD_REMOTE_RETENTION_DAYS_FULL": "0",
            "PCLOUD_RETENTION_MODE": "rtb-mirror",
        }
        res = crf.filter_catchup_missing([old], set(), {old}, env)
        self.assertTrue(res["ok"])
        self.assertEqual(res["catchup"], [old])
        self.assertEqual(res["skipped_retention"], [])

    def test_filter_error_returns_unfiltered(self) -> None:
        with mock.patch.object(
            crf, "compute_time_retention_keep", side_effect=RuntimeError("boom"),
        ):
            res = crf.filter_catchup_missing(
                ["2026-08-06-172826"], set(), {"2026-08-06-172826"}, self._env_time(),
            )
        self.assertFalse(res["ok"])
        self.assertEqual(res["catchup"], ["2026-08-06-172826"])
        self.assertTrue(any("catchup_filter_error" in w for w in res["warnings"]))

    def test_unusually_large_skip_warns(self) -> None:
        missing = [f"2026-01-{d:02d}-120000" for d in range(1, 10)]
        local = set(missing) | {"2026-10-11-040229"}
        remote = {"2026-10-11-040229"}
        with mock.patch.object(crf, "compute_time_retention_keep") as m:
            m.return_value = (set(), set(missing))
            res = crf.filter_catchup_missing(
                missing, remote, local, self._env_time(), max_skip_warn=5,
            )
        self.assertTrue(
            any("unusually_large" in w for w in res["warnings"]),
        )


if __name__ == "__main__":
    unittest.main()
