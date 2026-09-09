"""Unit tests for delta resume checkpoint logic."""

import unittest

from pcloud_push_json_pool_manifest_to_pcloud import _delta_resume_auto_allowed


class TestDeltaResumeAutoAllowed(unittest.TestCase):
    def test_requires_started_with_matching_basis(self):
        allowed, reason, _, _ = _delta_resume_auto_allowed(
            started={"snapshot": "snap-a", "basis": "basis-x"},
            progress=None,
            snapshot_name="snap-a",
            basis_snapshot_name="basis-y",
        )
        self.assertFalse(allowed)
        self.assertIn("basis", reason)

    def test_legacy_fallback_without_progress(self):
        allowed, reason, checkpoint, skip_phase3 = _delta_resume_auto_allowed(
            started={"snapshot": "snap-a", "basis": "basis-x"},
            progress=None,
            snapshot_name="snap-a",
            basis_snapshot_name="basis-x",
        )
        self.assertTrue(allowed)
        self.assertIn("legacy", reason)
        self.assertIn("legacy", checkpoint["copyfolder"])
        self.assertFalse(skip_phase3)

    def test_strict_progress_requires_copyfolder_done(self):
        allowed, reason, _, _ = _delta_resume_auto_allowed(
            started={"snapshot": "snap-a", "basis": "basis-x"},
            progress={
                "format_version": 1,
                "snapshot": "snap-a",
                "basis": "basis-x",
                "phases": {"copyfolder": {"status": "in_progress"}},
            },
            snapshot_name="snap-a",
            basis_snapshot_name="basis-x",
        )
        self.assertFalse(allowed)
        self.assertIn("copyfolder", reason)

    def test_progress_copyfolder_done_allows_resume(self):
        allowed, _, checkpoint, skip_phase3 = _delta_resume_auto_allowed(
            started={"snapshot": "snap-a", "basis": "basis-x"},
            progress={
                "format_version": 1,
                "snapshot": "snap-a",
                "basis": "basis-x",
                "phases": {
                    "copyfolder": {"status": "done"},
                    "cleanup": {"status": "done"},
                    "pool_upload": {"status": "failed"},
                },
            },
            snapshot_name="snap-a",
            basis_snapshot_name="basis-x",
        )
        self.assertTrue(allowed)
        self.assertEqual(checkpoint["pool_upload"], "failed")
        self.assertTrue(skip_phase3)


if __name__ == "__main__":
    unittest.main()
