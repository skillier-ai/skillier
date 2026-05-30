#!/usr/bin/env python3
"""Package the skill into a distributable .skill bundle.

- Filters out low-value auto-generated skills (see SKIP_PREFIXES).
- Converts skillbank/*.md → skillbank.jsonl (one packed file instead of
  hundreds of individual .md files). Each row carries a `source` field
  derived from the filename prefix so the MIT attribution claim in
  README.md is actually true at the data layer.
- Rebuilds the BM25 index against the filtered set.
- Atomic: all artifacts are staged into a tempdir and only swapped into
  the repo root / out_dir after the build succeeds end-to-end. A crash
  mid-pack does NOT corrupt the committed skillbank.jsonl or index.
- Emits <out_dir>/skillier.zip and <out_dir>/skillier.skill (same bytes).

Bundle constraints it targets: <30 MB zip size, <200 files inside
(Claude.app importer limits).

Usage: pack.py [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLBANK_DIR = ROOT / "skillbank"
LOCAL_EXTRAS = ROOT / "local_extras.jsonl"

# Skip patterns (filename prefixes) — auto-generated noise that dilutes results.
SKIP_PREFIXES = (
    "composio__composio-skills__",
    "secondsky__plugins__frontend-design__",         # fork of anthropics__frontend-design
    "secondsky__plugins__systematic-debugging__",    # fork of obra__systematic-debugging
)

# Filename-prefix → upstream attribution URL. Used to populate the `source`
# field in each skillbank.jsonl row so MIT attribution downstream is honest.
# Keep these in sync with scripts/seed.py SOURCES — every `name` there MUST
# have an entry here, or rows from that source ship with an empty `source`.
SOURCE_URLS = {
    "anthropics":         "https://github.com/anthropics/skills",
    "alireza":            "https://github.com/alirezarezvani/claude-skills",
    "composio":           "https://github.com/ComposioHQ/awesome-claude-skills",
    "trailofbits":        "https://github.com/trailofbits/skills",
    "vercel":             "https://github.com/vercel-labs/agent-skills",
    "secondsky":          "https://github.com/secondsky/claude-skills",
    "obra":               "https://github.com/obra/superpowers",
    "wshobson-agents":    "https://github.com/wshobson/agents",
    "bmad":               "https://github.com/bmad-code-org/BMAD-METHOD",
    "superpowers-skills": "https://github.com/obra/superpowers-skills",
    "superpowers-lab":    "https://github.com/obra/superpowers-lab",
    "gstack":             "https://github.com/garrytan/gstack",
    "kdense-scientific":  "https://github.com/K-Dense-AI/claude-scientific-skills",
    "mhattingpete":       "https://github.com/mhattingpete/claude-skills-marketplace",
    "gemini-cli":         "https://github.com/google-gemini/gemini-cli",
    "stitch":             "https://github.com/google-labs-code/stitch-skills",
    "claude-cookbooks":   "https://github.com/anthropics/claude-cookbooks",
    "davila7":            "https://github.com/davila7/claude-code-templates",
    "antigravity":        "https://github.com/sickn33/antigravity-awesome-skills",
    "lap":                "https://github.com/Lap-Platform/claude-marketplace",
}

# Import frontmatter parser from build_index.
sys.path.insert(0, str(ROOT / "scripts"))
from build_index import parse_frontmatter, JUNK_STEMS  # noqa: E402


# --- Flat-id re-keying (run once, over the final surviving set) -------------
# Collapse nested upstream ids (`<source>__plugins__<x>__skills__<leaf>`) to a
# linear `<source>__<name>` list. Done post-gate/post-merge so collisions are
# resolved over survivors, not the raw set.
_SCAFFOLD = {"plugins", "skills", "components", "cli-tool", "src", ".gemini", "claude"}
_GENERIC = {"api", "index", "main", "readme", "skill", "template"}


def _meaningful_parts(rel_parts: list[str]) -> list[str]:
    m = [p for p in rel_parts if p not in _SCAFFOLD] or list(rel_parts)
    while len(m) > 1 and m[-1] in _GENERIC:
        m = m[:-1]
    return m


def _resolve_flat_slugs(items: list[tuple]) -> dict:
    meaning = {k: _meaningful_parts(rp) for k, rp in items}
    keys_sorted = sorted(meaning, key=lambda k: (meaning[k], str(k)))
    result: dict = {}
    used: set = set()
    pending = list(keys_sorted)
    max_seg = max((len(meaning[k]) for k in meaning), default=1)
    depth = 1
    while pending and depth <= max_seg:
        groups: dict = {}
        for k in pending:
            m = meaning[k]
            groups.setdefault("__".join(m[-min(depth, len(m)):]), []).append(k)
        nxt: list = []
        for slug, ks in groups.items():
            if len(ks) == 1 and slug not in used:
                result[ks[0]] = slug
                used.add(slug)
            else:
                nxt.extend(ks)
        if len(nxt) == len(pending):
            break
        pending = nxt
        depth += 1
    leftover: dict = {}
    for k in pending:
        leftover.setdefault("__".join(meaning[k]), []).append(k)
    for base, ks in leftover.items():
        for i, k in enumerate(sorted(ks, key=str)):
            slug = base if (i == 0 and base not in used) else f"{base}-{i + 1}"
            result[k] = slug
            used.add(slug)
    return result


def flatten_ids(ids: list[str]) -> dict[str, str]:
    from collections import defaultdict
    by_source: dict[str, list[tuple]] = defaultdict(list)
    for i in ids:
        src = i.split("__", 1)[0]
        rest = i.split("__")[1:] or [i]
        by_source[src].append((i, rest))
    out: dict[str, str] = {}
    for src, items in by_source.items():
        for old_id, slug in _resolve_flat_slugs(items).items():
            out[old_id] = f"{src}__{slug}"
    return out


def rekey_jsonl_flat(jsonl_path: Path) -> tuple[int, int]:
    rows: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    mapping = flatten_ids([r["id"] for r in rows])
    changed = 0
    for r in rows:
        new = mapping.get(r["id"], r["id"])
        if new != r["id"]:
            changed += 1
            r["id"] = new
    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(jsonl_path)
    return len(rows), changed


def _source_url_for_id(skill_id: str) -> str:
    """Derive the upstream source URL from a skill id. The id format is
    `<source-name>__<rest>` (e.g. `anthropics__pdf`). Returns "" when the
    source is unknown so we never lie about attribution."""
    prefix = skill_id.split("__", 1)[0]
    return SOURCE_URLS.get(prefix, "")


def _load_drop_set(src_dir: Path) -> dict[str, str]:
    """Load the curation gate verdict (`_curation/drop.json` = {stem: reason})
    produced by scripts/curate.py. Returns {} when ABSENT (gate optional/forks).
    A PRESENT-yet-unreadable verdict warns LOUDLY and proceeds ungated rather
    than silently shipping the un-vetted corpus."""
    drop_path = src_dir / "_curation" / "drop.json"
    if not drop_path.exists():
        return {}
    try:
        data = json.loads(drop_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"  !! WARNING: {drop_path} present but unreadable ({e}); "
            f"proceeding UNGATED. Re-run scripts/curate.py.",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        print(
            f"  !! WARNING: {drop_path} is not a JSON object; proceeding UNGATED.",
            file=sys.stderr,
        )
        return {}
    return data


def md_to_jsonl(src_dir: Path, out_path: Path) -> tuple[int, int, int]:
    """Convert skillbank/*.md → JSONL.

    Writes to a sibling temp file first then atomic-replaces — a crash
    mid-write must not corrupt the committed skillbank.jsonl that the
    fallback path on a future run would happily ship.
    """
    kept = 0
    dropped = 0
    skipped_dup = 0
    # Match build_index.py's fingerprint so the JSONL on disk is consistent
    # with what every downstream consumer (local index) already considers a
    # unique skill.
    seen_fp: set[tuple[str, str]] = set()
    drop_set = _load_drop_set(src_dir)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for f in sorted(src_dir.glob("*.md")):
            if any(f.name.startswith(p) for p in SKIP_PREFIXES):
                dropped += 1
                continue
            stem = f.stem
            if stem in drop_set:
                dropped += 1
                continue
            last_seg = stem.rsplit("__", 1)[-1]
            if last_seg in JUNK_STEMS:
                dropped += 1
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            name = fm.get("name") or stem
            desc = fm.get("description", "")
            if not desc:
                dropped += 1
                continue
            fp = (name.lower().strip(), desc.lower().strip())
            if fp in seen_fp:
                skipped_dup += 1
                continue
            seen_fp.add(fp)
            out.write(json.dumps({
                "id": stem,
                "name": name,
                "description": desc,
                "body": text,
                "source": _source_url_for_id(stem),
            }, ensure_ascii=False) + "\n")
            kept += 1
    tmp.replace(out_path)
    return kept, dropped, skipped_dup


def merge_local_extras(jsonl_path: Path, extras_path: Path) -> tuple[int, int, int]:
    """Append hand-curated rows from `extras_path` into `jsonl_path`.

    Each row is sanitized via `sanitize.sanitize_entry` and deduped against
    rows already in `jsonl_path` (both by `id` and by `(name, description)`
    fingerprint — same fingerprint the upstream `md_to_jsonl` uses).

    Atomic: writes the merged content to a tmp file and `os.replace`s.

    Silently no-ops when `extras_path` doesn't exist — local additions are
    optional. Invalid JSON lines and rows missing required fields are skipped
    with a stderr warning (not fatal).

    Returns `(appended, skipped_dup, skipped_invalid)`.
    """
    if not extras_path.exists():
        return (0, 0, 0)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sanitize import sanitize_entry  # noqa: E402

    existing_ids: set[str] = set()
    existing_fp: set[tuple[str, str]] = set()
    existing_lines: list[str] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            existing_lines.append(line)
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                if "id" in row:
                    existing_ids.add(row["id"])
                existing_fp.add((
                    str(row.get("name", "")).lower().strip(),
                    str(row.get("description", "")).lower().strip(),
                ))

    appended = 0
    skipped_dup = 0
    skipped_invalid = 0
    new_lines: list[str] = []

    with extras_path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                skipped_invalid += 1
                print(
                    f"  ! {extras_path.name}:{line_no} invalid JSON ({e}); skipping",
                    file=sys.stderr,
                )
                continue
            if not isinstance(row, dict) or not all(
                k in row and isinstance(row[k], str) and row[k]
                for k in ("id", "name", "description")
            ):
                skipped_invalid += 1
                print(
                    f"  ! {extras_path.name}:{line_no} missing required str fields "
                    f"(id/name/description); skipping",
                    file=sys.stderr,
                )
                continue
            row = sanitize_entry(row)
            eid = row["id"]
            fp = (row["name"].lower().strip(), row["description"].lower().strip())
            if eid in existing_ids or fp in existing_fp:
                skipped_dup += 1
                continue
            existing_ids.add(eid)
            existing_fp.add(fp)
            new_lines.append(json.dumps(row, ensure_ascii=False))
            appended += 1

    if appended == 0:
        return (0, skipped_dup, skipped_invalid)

    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for line in existing_lines:
            out.write(line + "\n")
        for line in new_lines:
            out.write(line + "\n")
    tmp.replace(jsonl_path)

    return (appended, skipped_dup, skipped_invalid)


def _validate_existing_jsonl(repo_jsonl: Path) -> int:
    """When skillbank/ is absent (the gitignored raw form is regeneratable
    via seed.py), the committed skillbank.jsonl is the only source. We
    refuse to ship if it's missing / empty / malformed.

    Returns row count if valid. Raises RuntimeError otherwise.
    """
    if not repo_jsonl.exists():
        raise RuntimeError(
            f"{repo_jsonl.name} is missing and no skillbank/ to regenerate "
            f"from. Run scripts/seed.py first, or commit a valid skillbank.jsonl."
        )
    if repo_jsonl.stat().st_size == 0:
        raise RuntimeError(f"{repo_jsonl.name} is empty.")
    row_count = 0
    with repo_jsonl.open(encoding="utf-8") as f:
        first = f.readline().strip()
        if first:
            try:
                obj = json.loads(first)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"{repo_jsonl.name} first line is not valid JSON: {e}"
                ) from e
            if not isinstance(obj, dict) or "id" not in obj:
                raise RuntimeError(
                    f"{repo_jsonl.name} first line missing the 'id' field. "
                    f"Wrong format? Regenerate via scripts/seed.py + this script."
                )
            row_count = 1
        for _ in f:
            row_count += 1
    if row_count < 1:
        raise RuntimeError(f"{repo_jsonl.name} contains 0 rows.")
    return row_count


def _build_index_to(jsonl_path: Path, out_path: Path) -> None:
    """Build the BM25 index from `jsonl_path` and write it to `out_path`.

    Imports build_index in-process and calls its build function with explicit
    paths, so we can write the index to a temp location for the atomic-
    swap dance. Falls back to the script's default behavior (read jsonl_path
    at the conventional path, write to index/bm25.json) if the helper isn't
    available — but the import gives us the same code path with no fork.
    """
    import build_index as bi  # noqa: E402
    bi.build_index_at(jsonl_path=jsonl_path, out_path=out_path)


def _zip_stage(stage_dir: Path, zip_path: Path) -> None:
    """Pure-stdlib zip of stage_dir, max compression, excluding .DS_Store
    and __pycache__/. Cross-platform (no `zip` subprocess dependency)."""
    EXCLUDE_NAMES = {".DS_Store"}
    EXCLUDE_DIRS  = {"__pycache__"}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(stage_dir.rglob("*")):
            if path.is_dir():
                continue
            if path.name in EXCLUDE_NAMES:
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            zf.write(path, arcname=path.relative_to(stage_dir))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(Path.home() / "Desktop"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Validate SKILL.md frontmatter against Claude.app limits before packaging.
    # Buffer to 950 chars (limit is 1024) so trigger-list additions don't
    # silently push it over between releases.
    skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    fm = parse_frontmatter(skill_text)
    desc = fm.get("description", "")
    if len(desc) > 1024:
        print(
            f"ERROR: SKILL.md description is {len(desc)} chars "
            f"(Claude.app limit: 1024). Trim before repacking.",
            file=sys.stderr,
        )
        return 1
    if len(desc) > 950:
        print(
            f"warning: SKILL.md description is {len(desc)} chars — only "
            f"{1024 - len(desc)} chars of headroom before hitting the 1024 "
            f"Claude.app limit. Trim soon.",
            file=sys.stderr,
        )
    print(f"description length: {len(desc)}/1024 chars")

    repo_jsonl = ROOT / "skillbank.jsonl"

    # Atomic build: stage the regenerated jsonl + index into a tempdir,
    # only os.replace into the repo root after both succeed. The committed
    # skillbank.jsonl and index/bm25.json must never be in a half-written
    # state, even if pack.py is killed mid-run.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        staged_jsonl = tmp_path / "skillbank.jsonl"
        staged_index = tmp_path / "bm25.json"

        if SKILLBANK_DIR.exists():
            kept, dropped, skipped_dup = md_to_jsonl(SKILLBANK_DIR, staged_jsonl)
            print(
                f"skillbank: kept {kept}, dropped {dropped}, skipped_dup "
                f"{skipped_dup} → {staged_jsonl.name}"
            )
            if kept == 0:
                print(
                    "ERROR: 0 skills kept. skillbank/ is empty or every entry "
                    "was filtered. Run scripts/seed.py to repopulate.",
                    file=sys.stderr,
                )
                return 1
            # Merge hand-curated rows (sanitized, deduped against upstream).
            appended, dup, invalid = merge_local_extras(staged_jsonl, LOCAL_EXTRAS)
            if LOCAL_EXTRAS.exists():
                print(
                    f"local_extras: appended {appended}, deduped {dup}, "
                    f"invalid {invalid} ← {LOCAL_EXTRAS.name}"
                )
            # Collapse nested ids to a linear `<source>__<name>` over survivors.
            n_rows, n_changed = rekey_jsonl_flat(staged_jsonl)
            print(f"flatten ids: {n_changed}/{n_rows} ids linearized → {staged_jsonl.name}")
        else:
            try:
                row_count = _validate_existing_jsonl(repo_jsonl)
            except RuntimeError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1
            print(
                f"skillbank/ not present — using existing {repo_jsonl.name} "
                f"({row_count} skills)."
            )
            if LOCAL_EXTRAS.exists() and LOCAL_EXTRAS.stat().st_mtime > repo_jsonl.stat().st_mtime:
                print(
                    f"  ! {LOCAL_EXTRAS.name} is newer than {repo_jsonl.name}. "
                    f"To pick up your local edits, run scripts/seed.py + this script.",
                    file=sys.stderr,
                )
            # We still want a freshly built index in case the user touched
            # the committed jsonl by hand. Copy it into the stage and rebuild.
            shutil.copy(repo_jsonl, staged_jsonl)

        _build_index_to(staged_jsonl, staged_index)

        # Stage the bundle contents in a separate dir under tmp_path.
        stage = tmp_path / "skillier"
        stage.mkdir()
        # The .skill bundle uses the staged (freshly built) jsonl + index,
        # not whatever might be on disk at repo root. This guarantees the
        # zip is internally consistent.
        for name in ("SKILL.md", "README.md"):
            shutil.copy(ROOT / name, stage / name)
        shutil.copy(staged_jsonl, stage / "skillbank.jsonl")
        (stage / "index").mkdir()
        shutil.copy(staged_index, stage / "index" / "bm25.json")
        shutil.copytree(ROOT / "scripts", stage / "scripts")

        zip_path = tmp_path / "skillier.zip"
        _zip_stage(stage, zip_path)

        # All build steps succeeded — commit the artifacts atomically.
        # Per-file atomic via os.replace (same filesystem rename). Group
        # atomicity across the 4 destination files (repo jsonl + repo index
        # + out_dir zip + out_dir skill) is not provided by POSIX; staging
        # all of them under one tempdir is the best we can do.
        index_dir = ROOT / "index"
        index_dir.mkdir(exist_ok=True)
        shutil.copy(staged_jsonl, repo_jsonl.with_suffix(repo_jsonl.suffix + ".tmp"))
        os.replace(repo_jsonl.with_suffix(repo_jsonl.suffix + ".tmp"), repo_jsonl)
        shutil.copy(staged_index, (index_dir / "bm25.json").with_suffix(".json.tmp"))
        os.replace((index_dir / "bm25.json").with_suffix(".json.tmp"), index_dir / "bm25.json")

        out_zip = out_dir / "skillier-lite.zip"
        out_skill = out_dir / "skillier-lite.skill"
        for dst in (out_zip, out_skill):
            tmp_dst = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copy(zip_path, tmp_dst)
            os.replace(tmp_dst, dst)

        # Report
        with zipfile.ZipFile(zip_path) as zf:
            file_count = len(zf.namelist())
        size_mb = out_zip.stat().st_size / (1024 * 1024)
        print(f"\n{out_zip.name}: {size_mb:.1f} MB, {file_count} files")
        print(f"Also copied to {out_skill.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
