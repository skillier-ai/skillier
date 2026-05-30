"""Tests for pack.merge_local_extras — the local-additions merge step.

Verifies:
  - basic append (new id not in upstream)
  - dedup by id (skip if id already present)
  - dedup by (name, description) fingerprint (skip if duplicate of upstream)
  - sanitizer applied (invisible chars stripped from merged rows)
  - invalid JSON line tolerated (skipped, not fatal)
  - missing required field tolerated (skipped, not fatal)
  - atomic write (a crash mid-merge doesn't corrupt the target)
  - no-op when extras file is missing
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import pack  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open() if line.strip()]


class TestMergeLocalExtras(unittest.TestCase):

    def _setup(self) -> tuple[Path, Path]:
        """Returns (jsonl_path, extras_path) in a fresh tempdir."""
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        return root / "skillbank.jsonl", root / "local_extras.jsonl"

    # ---------- Basic append ----------

    def test_no_op_when_extras_missing(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [{"id": "a", "name": "A", "description": "alpha", "body": "..."}])
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (0, 0, 0))
        self.assertEqual(len(_read_jsonl(jsonl)), 1)

    def test_appends_new_row(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [{"id": "a", "name": "A", "description": "alpha", "body": "x"}])
        _write_jsonl(extras, [{"id": "local__new", "name": "new", "description": "new desc", "body": "..."}])
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (1, 0, 0))
        out = _read_jsonl(jsonl)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[-1]["id"], "local__new")

    # ---------- Dedup ----------

    def test_dedup_by_id_skips(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [{"id": "local__elon", "name": "elon", "description": "ex", "body": "..."}])
        _write_jsonl(extras, [{"id": "local__elon", "name": "elon", "description": "DIFFERENT", "body": "..."}])
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (0, 1, 0))
        out = _read_jsonl(jsonl)
        # The original row is preserved; the extras attempt is dropped.
        self.assertEqual(out[0]["description"], "ex")

    def test_dedup_by_name_description_fingerprint_skips(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [{"id": "anthropics__pdf", "name": "pdf", "description": "Read pdfs.", "body": "..."}])
        # Different id, same (name, description) → fingerprint collision.
        _write_jsonl(extras, [{"id": "local__pdf", "name": "pdf", "description": "Read pdfs.", "body": "..."}])
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (0, 1, 0))

    def test_dedup_case_insensitive_whitespace_insensitive(self) -> None:
        """Fingerprint normalization matches md_to_jsonl: lower+strip."""
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [{"id": "u", "name": "  PDF  ", "description": "Read PDFs.", "body": "..."}])
        _write_jsonl(extras, [{"id": "local__x", "name": "pdf", "description": "  read pdfs.  ", "body": "..."}])
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (0, 1, 0))

    # ---------- Sanitization ----------

    def test_sanitizes_extras_before_dedup_and_write(self) -> None:
        """Invisible chars in the extras row must be stripped at merge time."""
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [])
        # Embed a Unicode Tag block payload ("evil") in the description.
        tag_payload = "".join(chr(0xE0000 + ord(c)) for c in "evil")
        _write_jsonl(extras, [{
            "id": "local__poisoned",
            "name": "poisoned",
            "description": f"legit description{tag_payload}",
            "body": f"body{tag_payload}",
        }])
        appended, _, _ = pack.merge_local_extras(jsonl, extras)
        self.assertEqual(appended, 1)
        out = _read_jsonl(jsonl)
        self.assertEqual(out[0]["description"], "legit description")
        self.assertEqual(out[0]["body"], "body")

    # ---------- Error tolerance ----------

    def test_invalid_json_line_skipped(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [])
        # Mix: one valid row, one broken line.
        with extras.open("w") as f:
            f.write(json.dumps({"id": "local__ok", "name": "n", "description": "d", "body": "x"}) + "\n")
            f.write('{"id": "local__broken", "name": ...\n')  # malformed
            f.write(json.dumps({"id": "local__ok2", "name": "n2", "description": "d2", "body": "x"}) + "\n")
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (2, 0, 1))
        ids = {r["id"] for r in _read_jsonl(jsonl)}
        self.assertEqual(ids, {"local__ok", "local__ok2"})

    def test_missing_required_field_skipped(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [])
        with extras.open("w") as f:
            # missing 'description'
            f.write(json.dumps({"id": "local__bad", "name": "n", "body": "x"}) + "\n")
            # empty 'name'
            f.write(json.dumps({"id": "local__bad2", "name": "", "description": "d", "body": "x"}) + "\n")
            # valid
            f.write(json.dumps({"id": "local__ok", "name": "n", "description": "d", "body": "x"}) + "\n")
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (1, 0, 2))

    def test_blank_lines_in_extras_ignored(self) -> None:
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [])
        with extras.open("w") as f:
            f.write("\n\n")
            f.write(json.dumps({"id": "x", "name": "n", "description": "d", "body": "x"}) + "\n")
            f.write("\n")
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (1, 0, 0))

    # ---------- Atomicity ----------

    def test_atomic_write_preserves_original_on_crash(self) -> None:
        """If tmp.replace fails (e.g. permission, disk full), the original
        jsonl must be intact, not truncated."""
        jsonl, extras = self._setup()
        orig = [{"id": "u", "name": "u", "description": "u-desc", "body": "..."}]
        _write_jsonl(jsonl, orig)
        orig_bytes = jsonl.read_bytes()
        _write_jsonl(extras, [{"id": "local__new", "name": "n", "description": "d", "body": "x"}])

        # Make the os.replace call (under the hood of Path.replace) raise.
        with mock.patch.object(Path, "replace", side_effect=OSError("simulated disk full")):
            with self.assertRaises(OSError):
                pack.merge_local_extras(jsonl, extras)
        # Original is untouched.
        self.assertEqual(jsonl.read_bytes(), orig_bytes)

    def test_no_op_path_does_not_touch_file(self) -> None:
        """When 0 rows appended, the jsonl must not be rewritten (preserves
        mtime, avoids spurious churn)."""
        jsonl, extras = self._setup()
        _write_jsonl(jsonl, [{"id": "a", "name": "a", "description": "x", "body": "..."}])
        _write_jsonl(extras, [{"id": "a", "name": "a", "description": "x", "body": "..."}])  # dup
        mtime_before = jsonl.stat().st_mtime_ns
        appended, dup, invalid = pack.merge_local_extras(jsonl, extras)
        self.assertEqual((appended, dup, invalid), (0, 1, 0))
        self.assertEqual(jsonl.stat().st_mtime_ns, mtime_before)


if __name__ == "__main__":
    unittest.main()
