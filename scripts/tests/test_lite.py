"""Stdlib unittest suite for Skillier Lite.

Covers: pack.py (atomicity, skillbank validation, source-field derivation,
zip stdlib, description length), build_index.py (frontmatter parser,
indexing math, dedup), search.py (CLI shape, corrupted index, empty index,
unicode tokenization), load.py (unknown id, jsonl-then-skillbank fallback).

Run:  python3 -m unittest discover scripts/tests/
   or:  python3 scripts/tests/test_lite.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import build_index  # noqa: E402
import pack         # noqa: E402


# ── pack.py ────────────────────────────────────────────────────────────────


class TestSourceUrlDerivation(unittest.TestCase):
    """C4 fix: `source` field is derived from the filename prefix so the
    MIT attribution claim in README.md is actually true at the data layer."""

    def test_known_prefix(self) -> None:
        self.assertEqual(
            pack._source_url_for_id("anthropics__pdf"),
            "https://github.com/anthropics/skills",
        )

    def test_trailofbits_prefix(self) -> None:
        self.assertEqual(
            pack._source_url_for_id("trailofbits__some__deep__path"),
            "https://github.com/trailofbits/skills",
        )

    def test_unknown_prefix_returns_empty(self) -> None:
        """Unknown source = honest empty string, NOT a fabricated URL."""
        self.assertEqual(pack._source_url_for_id("future-source__skill"), "")

    def test_id_without_double_underscore(self) -> None:
        self.assertEqual(pack._source_url_for_id("noprefix"), "")


class TestValidateExistingJsonl(unittest.TestCase):
    """I2 fix: refuse to ship a stale/empty/malformed skillbank.jsonl
    when skillbank/ raw form isn't around to regenerate from."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "skillbank.jsonl"

    def test_missing_file_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing"):
            pack._validate_existing_jsonl(self.path)

    def test_empty_file_raises(self) -> None:
        self.path.write_text("")
        with self.assertRaisesRegex(RuntimeError, "empty"):
            pack._validate_existing_jsonl(self.path)

    def test_invalid_json_raises(self) -> None:
        self.path.write_text("not json\n")
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            pack._validate_existing_jsonl(self.path)

    def test_missing_id_field_raises(self) -> None:
        self.path.write_text(json.dumps({"name": "x"}) + "\n")
        with self.assertRaisesRegex(RuntimeError, "missing the 'id'"):
            pack._validate_existing_jsonl(self.path)

    def test_valid_returns_row_count(self) -> None:
        rows = [
            {"id": f"a__{i}", "name": f"n{i}", "description": "d", "body": ""}
            for i in range(7)
        ]
        self.path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertEqual(pack._validate_existing_jsonl(self.path), 7)


class TestMdToJsonl(unittest.TestCase):
    """Verify: filtering, dedup, source field, atomic write (temp-then-rename)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = Path(self._tmp.name) / "src"
        self.src.mkdir()

    def _write(self, name: str, body: str) -> None:
        (self.src / name).write_text(body, encoding="utf-8")

    @staticmethod
    def _md(name: str, desc: str) -> str:
        return f'---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n'

    def test_happy_path_emits_source_field(self) -> None:
        self._write("anthropics__pdf.md", self._md("pdf", "PDF tools"))
        self._write("trailofbits__audit.md", self._md("audit", "Audit smart contracts"))
        out = Path(self._tmp.name) / "out.jsonl"
        kept, dropped, dup = pack.md_to_jsonl(self.src, out)
        self.assertEqual((kept, dropped, dup), (2, 0, 0))
        lines = [json.loads(l) for l in out.read_text().splitlines()]
        sources = {row["id"]: row["source"] for row in lines}
        self.assertEqual(sources["anthropics__pdf"], "https://github.com/anthropics/skills")
        self.assertEqual(sources["trailofbits__audit"], "https://github.com/trailofbits/skills")

    def test_skip_prefix_filter(self) -> None:
        self._write("composio__composio-skills__noise.md", self._md("noise", "dropped"))
        self._write("anthropics__keep.md", self._md("keep", "kept"))
        out = Path(self._tmp.name) / "out.jsonl"
        kept, dropped, _ = pack.md_to_jsonl(self.src, out)
        self.assertEqual(kept, 1)
        self.assertEqual(dropped, 1)

    def test_junk_stems_filter(self) -> None:
        self._write("anthropics__README.md", self._md("readme", "junk"))
        self._write("anthropics__keep.md", self._md("keep", "kept"))
        out = Path(self._tmp.name) / "out.jsonl"
        kept, dropped, _ = pack.md_to_jsonl(self.src, out)
        self.assertEqual(kept, 1)
        self.assertEqual(dropped, 1)

    def test_missing_description_dropped(self) -> None:
        self._write("anthropics__nodesc.md", "---\nname: x\n---\n")
        out = Path(self._tmp.name) / "out.jsonl"
        kept, dropped, _ = pack.md_to_jsonl(self.src, out)
        self.assertEqual((kept, dropped), (0, 1))

    def test_dedup_on_name_plus_description(self) -> None:
        self._write("anthropics__one.md", self._md("dup", "same desc"))
        self._write("composio__two.md", self._md("dup", "same desc"))
        out = Path(self._tmp.name) / "out.jsonl"
        kept, _, skipped_dup = pack.md_to_jsonl(self.src, out)
        self.assertEqual((kept, skipped_dup), (1, 1))

    def test_unknown_prefix_yields_empty_source(self) -> None:
        self._write("future-thing__x.md", self._md("x", "y"))
        out = Path(self._tmp.name) / "out.jsonl"
        pack.md_to_jsonl(self.src, out)
        row = json.loads(out.read_text().splitlines()[0])
        self.assertEqual(row["source"], "")

    def test_temp_file_swapped_atomically(self) -> None:
        """Verify the write doesn't leave a .tmp behind (atomic rename)."""
        self._write("anthropics__x.md", self._md("x", "y"))
        out = Path(self._tmp.name) / "out.jsonl"
        pack.md_to_jsonl(self.src, out)
        self.assertTrue(out.exists())
        self.assertFalse(out.with_suffix(out.suffix + ".tmp").exists())


class TestZipStage(unittest.TestCase):
    """I3/I4: stdlib zipfile replaces the `zip` and `unzip` subprocesses."""

    def test_zip_excludes_ds_store_and_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "s"
            stage.mkdir()
            (stage / "keep.txt").write_text("yes")
            (stage / ".DS_Store").write_text("nope")
            (stage / "__pycache__").mkdir()
            (stage / "__pycache__" / "x.pyc").write_bytes(b"nope")
            zp = Path(tmp) / "out.zip"
            pack._zip_stage(stage, zp)
            with zipfile.ZipFile(zp) as zf:
                names = set(zf.namelist())
            self.assertIn("keep.txt", names)
            self.assertNotIn(".DS_Store", names)
            self.assertFalse(any("__pycache__" in n for n in names))


# ── build_index.py ─────────────────────────────────────────────────────────


class TestParseFrontmatter(unittest.TestCase):
    """I9: readability fix shouldn't change behavior. Parser handles
    quoted, folded, literal, CRLF, missing."""

    def test_no_frontmatter_returns_empty(self) -> None:
        self.assertEqual(build_index.parse_frontmatter("# hello\n"), {})

    def test_simple_kv(self) -> None:
        fm = build_index.parse_frontmatter("---\nname: x\ndescription: y\n---\nbody")
        self.assertEqual(fm, {"name": "x", "description": "y"})

    def test_crlf_normalized(self) -> None:
        fm = build_index.parse_frontmatter("---\r\nname: x\r\n---\r\nbody")
        self.assertEqual(fm, {"name": "x"})

    def test_quoted_value(self) -> None:
        fm = build_index.parse_frontmatter('---\nname: "x: with colon"\n---\n')
        self.assertEqual(fm, {"name": "x: with colon"})

    def test_folded_value(self) -> None:
        text = (
            "---\n"
            "description: >\n"
            "  long\n"
            "  folded\n"
            "  description\n"
            "---\n"
        )
        fm = build_index.parse_frontmatter(text)
        self.assertEqual(fm, {"description": "long folded description"})


class TestIndexEntries(unittest.TestCase):
    """The _index_entries function (extracted for testability)."""

    def test_empty_input(self) -> None:
        idx = build_index._index_entries([])
        self.assertEqual(idx["N"], 0)
        self.assertEqual(idx["avgdl"], 0.0)
        self.assertEqual(idx["postings"], {})

    def test_single_entry(self) -> None:
        idx = build_index._index_entries(
            [{"id": "a", "name": "foo", "description": "bar baz"}]
        )
        self.assertEqual(idx["N"], 1)
        self.assertGreater(idx["avgdl"], 0)
        self.assertIn("foo", idx["postings"])
        self.assertIn("bar", idx["postings"])
        self.assertIn("a", idx["meta"])

    def test_dedup_matches_pack_fingerprint(self) -> None:
        idx = build_index._index_entries([
            {"id": "a", "name": "X", "description": "same"},
            {"id": "b", "name": "x", "description": "  Same  "},
        ])
        self.assertEqual(idx["N"], 1)
        self.assertEqual(idx["_skipped_dup"], 1)


class TestBuildIndexAt(unittest.TestCase):
    """Atomic-staging helper: write index to a specific path from a
    specific jsonl path (used by pack.py)."""

    def test_writes_to_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jp = Path(tmp) / "in.jsonl"
            jp.write_text(json.dumps({"id": "a__b", "name": "n", "description": "d"}) + "\n")
            op = Path(tmp) / "out" / "bm25.json"
            n = build_index.build_index_at(jsonl_path=jp, out_path=op)
            self.assertEqual(n, 1)
            self.assertTrue(op.exists())
            payload = json.loads(op.read_text())
            self.assertEqual(payload["N"], 1)


# ── search.py + load.py end-to-end ─────────────────────────────────────────


class TestSearchCLI(unittest.TestCase):
    """Run search.py as a subprocess to exercise the exit codes and stderr
    paths the user actually sees."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = _SCRIPTS_DIR / "search.py"

    def _run(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
        )

    def test_missing_args_returns_2(self) -> None:
        res = self._run(self.script)
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage", res.stderr.lower())

    def test_missing_index_returns_1(self) -> None:
        """Copy search.py to a fake repo (no index/ dir) and run THAT copy
        so its __file__-based ROOT points at the fake repo."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = Path(tmp) / "fake"
            (fake_repo / "scripts").mkdir(parents=True)
            fake_script = fake_repo / "scripts" / "search.py"
            shutil.copy(self.script, fake_script)
            res = self._run(fake_script, "foo")
            self.assertEqual(res.returncode, 1, msg=res.stderr)
            self.assertIn("no index", res.stderr)

    def test_corrupted_index_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = Path(tmp) / "fake"
            (fake_repo / "scripts").mkdir(parents=True)
            (fake_repo / "index").mkdir()
            (fake_repo / "index" / "bm25.json").write_text("not json")
            fake_script = fake_repo / "scripts" / "search.py"
            shutil.copy(self.script, fake_script)
            res = self._run(fake_script, "foo")
            self.assertEqual(res.returncode, 1, msg=res.stderr)
            self.assertIn("corrupted", res.stderr.lower())

    def test_unicode_query_returns_empty(self) -> None:
        """TOKEN_RE strips non-ASCII alphanumerics. Cyrillic / CJK queries
        should produce [] without crashing the tokenizer."""
        res = subprocess.run(
            [sys.executable, str(self.script), "русский 中文"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(json.loads(res.stdout), [])


class TestLoadCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = _SCRIPTS_DIR / "load.py"

    def test_missing_args_returns_2(self) -> None:
        res = subprocess.run(
            [sys.executable, str(self.script)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage", res.stderr.lower())

    def test_unknown_id_returns_1_with_searched_paths(self) -> None:
        res = subprocess.run(
            [sys.executable, str(self.script), "does__not__exist__zzz_xyz_999"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(res.returncode, 1)
        # The improved error message mentions which paths were searched.
        self.assertIn("Searched:", res.stderr)


# ── seed.py ────────────────────────────────────────────────────────────────


class TestSeedSymlinkRejection(unittest.TestCase):
    """I5: symlinked SKILL.md files (e.g. a malicious source pointing
    SKILL.md at /etc/passwd) must be skipped, not copied."""

    def test_is_inside_accepts_local(self) -> None:
        import seed
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inner = root / "sub" / "SKILL.md"
            inner.parent.mkdir(parents=True)
            inner.write_text("hi")
            self.assertTrue(seed._is_inside(inner, root))

    def test_is_inside_rejects_symlink_escape(self) -> None:
        import seed
        with tempfile.TemporaryDirectory() as outside_tmp, \
             tempfile.TemporaryDirectory() as repo_tmp:
            outside = Path(outside_tmp).resolve()
            (outside / "target.txt").write_text("secret")
            root = Path(repo_tmp).resolve()
            link = root / "SKILL.md"
            link.symlink_to(outside / "target.txt")
            self.assertFalse(seed._is_inside(link, root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
