#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pcloud_push_json_pool_manifest_to_pcloud as push  # noqa: E402


class IntegrityGateNormalizeTests(unittest.TestCase):
    def test_weak_zero_issues_ok_despite_exit_1(self) -> None:
        result = {"ok": False, "issues": 0, "verification_tier": "weak"}
        out = push._normalize_gate_verify_result(result, proc_returncode=1)
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("gate_weak_index_accepted"))

    def test_weak_with_issues_stays_failed(self) -> None:
        result = {"ok": False, "issues": 2, "verification_tier": "weak"}
        out = push._normalize_gate_verify_result(result, proc_returncode=1)
        self.assertFalse(out["ok"])
        self.assertNotIn("gate_weak_index_accepted", out)

    def test_nonzero_exit_clears_ok_when_issues(self) -> None:
        result = {"ok": True, "issues": 1, "verification_tier": "full"}
        out = push._normalize_gate_verify_result(result, proc_returncode=1)
        self.assertFalse(out["ok"])


class IntegrityGateSummaryTests(unittest.TestCase):
    def test_summary_includes_error_and_tier(self) -> None:
        s = push._integrity_gate_failure_summary(
            {
                "error_summary": "verify boom",
                "issues": 3,
                "verification_tier": "full",
                "weak_index_snapshots": ["snap-a", "snap-b"],
                "stubs_vs_index": {"manifest_missing_total": 5},
            },
        )
        self.assertIn("verify boom", s)
        self.assertIn("3 issue(s)", s)
        self.assertIn("tier=full", s)
        self.assertIn("weak_index=", s)
        self.assertIn("stubs_missing=5", s)

    def test_summary_fallback_when_empty(self) -> None:
        s = push._integrity_gate_failure_summary({})
        self.assertIn("integrity failed", s)


if __name__ == "__main__":
    unittest.main()
