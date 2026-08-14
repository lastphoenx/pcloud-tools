#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for pool_index_db (C1). No network, no pcloud_bin_lib."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pool_index_db as pidb  # noqa: E402


def _write_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def _mini_v2() -> dict:
    return {
        "version": 2,
        "pool_refs": {
            "aa" * 32: {
                "fileid": 11,
                "hash": 5016324286669844513,
                "size": 100,
                "snapshots": {
                    "snap-a": ["p/one.txt"],
                    "snap-b": ["p/one.txt", "p/copy.txt"],
                },
            },
            "bb" * 32: {
                "fileid": 22,
                "hash": 99,
                "size": 200,
                "snapshots": {"snap-a": ["p/two.txt"]},
            },
            "cc" * 32: {
                "fileid": 33,
                "hash": 1,
                "size": 3,
                "snapshots": {},
            },
            "dd" * 32: {
                "fileid": None,
                "hash": None,
                "size": None,
                "snapshots": {"snap-a": []},
            },
        },
    }


class PoolIndexDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.db_path = os.path.join(self.dir, "pool_index.sqlite3")
        self.db = pidb.open_db(self.db_path)

    def tearDown(self) -> None:
        self.db.close()
        self._td.cleanup()

    def test_import_tolerance(self) -> None:
        mixed = {
            "version": 1,
            "pool_refs": {
                "AA" * 32: ["snap-old"],
                "bb" * 32: {"snapshots": ["snap-v1"], "fileid": 1, "size": 5},
                "cc" * 32: {
                    "fileid": 2,
                    "hash": 7,
                    "size": 9,
                    "snapshots": {"snap-v2": ["x/y"]},
                },
            },
        }
        src = os.path.join(self.dir, "mixed.json")
        _write_json(src, mixed)
        st = self.db.import_from_json(src)
        self.assertEqual(st["shas"], 3)
        names = set(self.db.snapshot_names())
        self.assertEqual(names, {"snap-old", "snap-v1", "snap-v2"})
        self.assertEqual(self.db.snapshot_pair_count("snap-v2"), 1)
        self.assertEqual(self.db.snapshot_pair_count("snap-old"), 1)

    def test_round_trip_fidelity(self) -> None:
        src = os.path.join(self.dir, "master.json")
        data = _mini_v2()
        _write_json(src, data)
        self.db.import_from_json(src)
        out = os.path.join(self.dir, "export.json")
        self.db.export_content_index_json(out)
        with open(out, encoding="utf-8") as f:
            exported = json.load(f)
        self.assertEqual(exported["version"], 2)
        self.assertNotIn("items", exported)
        refs = exported["pool_refs"]
        self.assertEqual(set(refs), set(data["pool_refs"]))
        self.assertEqual(refs["cc" * 32]["snapshots"], {})
        self.assertEqual(refs["dd" * 32]["snapshots"]["snap-a"], [])
        self.assertEqual(
            sorted(refs["aa" * 32]["snapshots"]["snap-b"]),
            ["p/copy.txt", "p/one.txt"],
        )
        self.assertEqual(refs["aa" * 32]["hash"], 5016324286669844513)

    def test_coord_semantics(self) -> None:
        src = os.path.join(self.dir, "c.json")
        _write_json(
            src,
            {
                "version": 2,
                "pool_refs": {
                    "ee" * 32: {
                        "fileid": 10,
                        "hash": 20,
                        "size": 30,
                        "snapshots": {"s1": ["a"]},
                    }
                },
            },
        )
        self.db.import_from_json(src)
        self.db.register_batch(
            "s2",
            [("ee" * 32, "a", 999, 888, 777)],
        )
        out = os.path.join(self.dir, "e.json")
        self.db.export_content_index_json(out)
        with open(out, encoding="utf-8") as f:
            refs = json.load(f)["pool_refs"]
        self.assertEqual(refs["ee" * 32]["fileid"], 10)
        self.assertEqual(refs["ee" * 32]["hash"], 20)
        self.assertEqual(refs["ee" * 32]["size"], 30)

        self.db.register_batch("s3", [("ff" * 32, "b", 1, 2, 3)])
        self.db.register_batch("s3", [("ff" * 32, "b", None, None, None)])
        self.db.export_content_index_json(out)
        with open(out, encoding="utf-8") as f:
            refs = json.load(f)["pool_refs"]
        self.assertEqual(refs["ff" * 32]["fileid"], 1)
        self.assertEqual(refs["ff" * 32]["hash"], 2)

    def test_bulk_merge_matrix(self) -> None:
        src = os.path.join(self.dir, "m.json")
        _write_json(
            src,
            {
                "version": 2,
                "pool_refs": {
                    "s1" * 32: {
                        "fileid": 1,
                        "size": 1,
                        "snapshots": {"basis": ["keep.txt", "gone.txt"]},
                    },
                    "s2" * 32: {
                        "fileid": 2,
                        "size": 2,
                        "snapshots": {"basis": ["changed.txt"]},
                    },
                    "s3" * 32: {
                        "fileid": 3,
                        "size": 3,
                        "snapshots": {"basis": ["orphan.txt"]},
                    },
                },
            },
        )
        self.db.import_from_json(src)
        cur = {
            "keep.txt": "s1" * 32,
            "changed.txt": "s9" * 32,
            "added.txt": "s8" * 32,
        }
        st = self.db.merge_basis_snapshot("new", "basis", cur)
        self.assertFalse(st["basis_missing"])
        self.assertEqual(st["merged"], 1)
        self.assertGreaterEqual(st["skipped_removed"], 2)
        self.assertEqual(st["skipped_changed"], 1)
        self.assertEqual(self.db.snapshot_pair_count("new"), 1)

        self.db.register_batch(
            "new",
            [
                ("s9" * 32, "changed.txt", None, None, 4),
                ("s8" * 32, "added.txt", None, None, 5),
            ],
        )
        self.assertEqual(self.db.snapshot_pair_count("new"), len(cur))

        empty = pidb.open_db(os.path.join(self.dir, "empty.sqlite3"))
        try:
            st0 = empty.merge_basis_snapshot("new", "nope", cur)
            self.assertTrue(st0["basis_missing"])
            self.assertEqual(st0["merged"], 0)
        finally:
            empty.close()

    def test_invariant_reused_regression(self) -> None:
        src = os.path.join(self.dir, "r.json")
        _write_json(
            src,
            {
                "version": 2,
                "pool_refs": {
                    "u1" * 32: {"size": 1, "snapshots": {"basis": ["a.txt"]}},
                    "u2" * 32: {"size": 2, "snapshots": {"basis": ["b.txt"]}},
                },
            },
        )
        self.db.import_from_json(src)
        cur = {
            "a.txt": "u1" * 32,
            "b.txt": "u2" * 32,
            "c.txt": "u3" * 32,
        }
        self.db.merge_basis_snapshot("new", "basis", cur)
        self.db.register_batch("new", [("u3" * 32, "c.txt", None, None, 3)])
        self.assertEqual(self.db.snapshot_pair_count("new"), len(cur))

        db2 = pidb.open_db(os.path.join(self.dir, "r2.sqlite3"))
        try:
            db2.import_from_json(src)
            db2.merge_basis_snapshot("new", "basis", cur)
            # omit reused/added c.txt on purpose
            self.assertNotEqual(db2.snapshot_pair_count("new"), len(cur))
        finally:
            db2.close()

    def test_purge_keeps_shas(self) -> None:
        src = os.path.join(self.dir, "p.json")
        _write_json(src, _mini_v2())
        self.db.import_from_json(src)
        n = self.db.purge_snapshot("snap-a")
        self.assertGreater(n, 0)
        self.assertEqual(self.db.snapshot_pair_count("snap-a"), 0)
        self.assertGreater(self.db.count_shas(), 0)
        out = os.path.join(self.dir, "after.json")
        self.db.export_content_index_json(out)
        with open(out, encoding="utf-8") as f:
            refs = json.load(f)["pool_refs"]
        self.assertIn("cc" * 32, refs)
        self.assertEqual(refs["cc" * 32]["snapshots"], {})
        self.assertNotIn("snap-a", refs["bb" * 32]["snapshots"])

    def test_build_snapshot_index(self) -> None:
        src = os.path.join(self.dir, "b.json")
        data = _mini_v2()
        _write_json(src, data)
        self.db.import_from_json(src)
        built = self.db.build_snapshot_index("snap-a")
        self.assertEqual(built["version"], 2)
        self.assertIn("aa" * 32, built["pool_refs"])
        self.assertNotIn("cc" * 32, built["pool_refs"])
        self.assertEqual(
            built["pool_refs"]["aa" * 32]["snapshots"]["snap-a"], ["p/one.txt"]
        )
        self.assertNotIn("snap-b", built["pool_refs"]["aa" * 32]["snapshots"])

    def test_archive_fallback_equivalence(self) -> None:
        src = os.path.join(self.dir, "full.json")
        _write_json(src, _mini_v2())
        self.db.import_from_json(src)
        arch = self.db.build_snapshot_index("snap-a")
        arch_path = os.path.join(self.dir, "snap-a_index.json")
        _write_json(arch_path, arch)

        db_a = pidb.open_db(os.path.join(self.dir, "a.sqlite3"))
        db_b = pidb.open_db(os.path.join(self.dir, "b.sqlite3"))
        try:
            db_a.import_from_json(src)
            db_b.import_from_json(src)
            db_b.purge_snapshot("snap-a")
            cur = {"p/one.txt": "aa" * 32, "p/two.txt": "bb" * 32}
            st_db = db_a.merge_basis_snapshot("new", "snap-a", cur)
            st_ar = db_b.merge_basis_from_archive_json("new", arch_path, cur)
            self.assertEqual(st_db["merged"], st_ar["merged"])
            self.assertEqual(db_a.snapshot_pair_count("new"), db_b.snapshot_pair_count("new"))
        finally:
            db_a.close()
            db_b.close()

    def test_fingerprint(self) -> None:
        src = os.path.join(self.dir, "fp.json")
        _write_json(src, _mini_v2())
        self.db.import_from_json(src)
        self.assertTrue(self.db.master_fingerprint_matches(src))
        time.sleep(0.02)
        with open(src, "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertFalse(self.db.master_fingerprint_matches(src))
        self.assertIsNone(self.db.master_fingerprint_matches(os.path.join(self.dir, "nope.json")))

    def test_digest_stable(self) -> None:
        src = os.path.join(self.dir, "d.json")
        data = _mini_v2()
        _write_json(src, data)
        self.db.import_from_json(src)
        d1 = self.db.digest()
        d_json = pidb.digest_from_json(src)
        self.assertEqual(d1, d_json)

        src2 = os.path.join(self.dir, "d2.json")
        shuffled = {
            "version": 2,
            "pool_refs": dict(reversed(list(data["pool_refs"].items()))),
        }
        _write_json(src2, shuffled)
        db2 = pidb.open_db(os.path.join(self.dir, "d2.sqlite3"))
        try:
            db2.import_from_json(src2)
            self.assertEqual(db2.digest(), d1)
        finally:
            db2.close()

        self.db.register_batch("snap-x", [("aa" * 32, "mut.txt", None, None, None)])
        self.assertNotEqual(self.db.digest(), d1)

    def test_export_shape(self) -> None:
        src = os.path.join(self.dir, "s.json")
        _write_json(src, _mini_v2())
        self.db.import_from_json(src)
        out = os.path.join(self.dir, "e1.json")
        out2 = os.path.join(self.dir, "e2.json")
        self.db.export_content_index_json(out)
        self.db.export_content_index_json(out2)
        with open(out, "rb") as f:
            b1 = f.read()
        with open(out2, "rb") as f:
            b2 = f.read()
        self.assertEqual(b1, b2)
        self.assertNotIn(b", ", b1)
        self.assertTrue(b1.startswith(b'{"version":2,"pool_refs":{'))
        self.assertNotIn(b'"items"', b1)

    def test_merge_cloned_refs_tiers(self) -> None:
        src = os.path.join(self.dir, "t.json")
        _write_json(src, _mini_v2())
        self.db.import_from_json(src)
        cur = {"p/one.txt": "aa" * 32}
        st = self.db.merge_cloned_refs("n1", "snap-a", cur)
        self.assertEqual(st["tier"], "db")

        db2 = pidb.open_db(os.path.join(self.dir, "tier2.sqlite3"))
        try:
            arch = os.path.join(self.dir, "arch.json")
            _write_json(arch, self.db.build_snapshot_index("snap-a"))
            st2 = db2.merge_cloned_refs("n1", "snap-a", cur, archive_json_path=arch)
            self.assertEqual(st2["tier"], "archive_json")
        finally:
            db2.close()

        db3 = pidb.open_db(os.path.join(self.dir, "tier3.sqlite3"))
        try:
            st3 = db3.merge_cloned_refs("n1", "snap-a", cur)
            self.assertEqual(st3["tier"], "manifest")
            self.assertEqual(db3.snapshot_pair_count("n1"), 1)
        finally:
            db3.close()


if __name__ == "__main__":
    unittest.main()
