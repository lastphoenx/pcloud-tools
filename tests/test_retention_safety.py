#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_retention_safety as prs  # noqa: E402
import pcloud_pool_gc as pgc  # noqa: E402


class RetentionModeTests(unittest.TestCase):
    def test_time_mode_when_days_full_positive(self) -> None:
        env = {"PCLOUD_REMOTE_RETENTION_DAYS_FULL": "62"}
        mode, err = prs.resolve_retention_mode(env, allow_rtb_mode=False)
        self.assertEqual(mode, prs.MODE_TIME)
        self.assertIsNone(err)

    def test_unset_when_days_zero_and_no_explicit_mirror(self) -> None:
        env = {"PCLOUD_REMOTE_RETENTION_DAYS_FULL": "0"}
        mode, err = prs.resolve_retention_mode(env, allow_rtb_mode=False)
        self.assertEqual(mode, prs.MODE_UNSET)
        self.assertIsNotNone(err)

    def test_rtb_mirror_requires_allow_flag(self) -> None:
        env = {
            "PCLOUD_REMOTE_RETENTION_DAYS_FULL": "0",
            "PCLOUD_RETENTION_MODE": "rtb-mirror",
        }
        mode, err = prs.resolve_retention_mode(env, allow_rtb_mode=False)
        self.assertEqual(mode, prs.MODE_RTB_MIRROR)
        self.assertIn("--allow-rtb-mode", err or "")

    def test_rtb_mirror_ok_with_flag(self) -> None:
        env = {
            "PCLOUD_REMOTE_RETENTION_DAYS_FULL": "0",
            "PCLOUD_RETENTION_MODE": "rtb-mirror",
        }
        mode, err = prs.resolve_retention_mode(env, allow_rtb_mode=True)
        self.assertEqual(mode, prs.MODE_RTB_MIRROR)
        self.assertIsNone(err)


class ResolveDeletesTests(unittest.TestCase):
    def test_no_silent_rtb_mirror_fallback(self) -> None:
        remote = {"2026-01-01-120000", "2026-01-02-120000"}
        local = {"2026-01-01-120000"}
        to_del, _mode, err = pgc._resolve_retention_deletes(
            remote, local, {"PCLOUD_REMOTE_RETENTION_DAYS_FULL": "0"},
            allow_rtb_mode=False,
        )
        self.assertEqual(to_del, set())
        self.assertIsNotNone(err)

    def test_time_mode_deletes_by_policy(self) -> None:
        remote = {"2020-01-06-120000", "2020-01-07-120000"}
        to_del, mode, err = pgc._resolve_retention_deletes(
            remote,
            set(),
            {"PCLOUD_REMOTE_RETENTION_DAYS_FULL": "1", "PCLOUD_REMOTE_RETENTION_WEEKLY_WEEKS": "54"},
            allow_rtb_mode=False,
        )
        self.assertIsNone(err)
        self.assertIn("zeit:", mode)
        self.assertIn("2020-01-06-120000", to_del)


class SnapshotLimitTests(unittest.TestCase):
    def test_absolute_limit_blocks_august_scenario(self) -> None:
        err = prs.check_snapshot_delete_allowed(43, 97, {}, None)
        self.assertIsNotNone(err)
        self.assertIn("40", err or "")

    def test_normal_month_passes(self) -> None:
        self.assertIsNone(prs.check_snapshot_delete_allowed(16, 97, {}, None))

    def test_cli_override_allows_60(self) -> None:
        self.assertIsNone(prs.check_snapshot_delete_allowed(43, 97, {}, 60))

    def test_relative_limit(self) -> None:
        err = prs.check_snapshot_delete_allowed(35, 100, {}, None)
        self.assertIsNotNone(err)
        self.assertIn("30", err or "")


class GcPctLimitTests(unittest.TestCase):
    def test_august_pool_pct_blocked(self) -> None:
        err = prs.check_gc_delete_allowed(115000, 249095, {}, None)
        self.assertIsNotNone(err)

    def test_current_backlog_passes(self) -> None:
        self.assertIsNone(prs.check_gc_delete_allowed(9524, 249095, {}, None))

    def test_cli_override_pct(self) -> None:
        self.assertIsNone(prs.check_gc_delete_allowed(115000, 249095, {}, 50.0))


class RtbMarkerTests(unittest.TestCase):
    def test_missing_marker_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNotNone(prs.check_rtb_backup_marker(tmp))

    def test_marker_present_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "backup.marker"), "w", encoding="utf-8").close()
            self.assertIsNone(prs.check_rtb_backup_marker(tmp))


class ConsistencyGateTests(unittest.TestCase):
    def test_gate_blocks_critical_level(self) -> None:
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=2, stdout="{}", stderr="[CRITICAL] x")
            err = prs.run_consistency_gate(ROOT, ".env", max_allowed_level=1)
        self.assertIsNotNone(err)
        self.assertIn("level=2", err or "")

    def test_gate_allows_warning(self) -> None:
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="{}", stderr="[WARN] x")
            err = prs.run_consistency_gate(ROOT, ".env", max_allowed_level=1)
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
