#!/usr/bin/env python3
"""Append a hand-curated skill (or directory of skills) to local_extras.jsonl.

Reads `SKILL.md` files, parses their frontmatter (name + description), runs
the body through `sanitize.sanitize_entry` (security defense layer), and
appends a JSON row to `local_extras.jsonl` at the repo root. The committed
`local_extras.jsonl` is then merged into `skillbank.jsonl` at pack time by
`scripts/pack.py::merge_local_extras`.

Usage:
    scripts/add_local_skill.py path/to/SKILL.md [--id <slug>]
    scripts/add_local_skill.py path/to/dir/                # recursive walk
    scripts/add_local_skill.py path/to/bundle.skill        # unzip + walk

Behavior:
  - Idempotent on id: if the id already exists in local_extras.jsonl, the
    new row REPLACES the old (so re-importing a bundle just refreshes it).
  - Bundle .skill files are unzipped to a tempdir; the walker recurses for
    SKILL.md and MODEL.md (MODEL.md is treated as a SKILL.md per the
    convention used by multi-skill bundles like elons-brain).
  - For nested SKILL.md (`<bundle>/skills/<slug>/SKILL.md`), the id becomes
    `local__<bundle>__skills__<slug>` to match the seed.py path-flattening
    convention. The top-level `<bundle>/SKILL.md` becomes `local__<bundle>`.

Pure stdlib. Idempotent. Re-runnable.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_index import parse_frontmatter  # noqa: E402
from sanitize import sanitize_entry  # noqa: E402

LOCAL_EXTRAS = ROOT / "local_extras.jsonl"


def _build_row(skill_md: Path, src_root: Path, body_strips_frontmatter: bool) -> dict | None:
    """Build a `local__<id>` row from a SKILL.md / MODEL.md file."""
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    name = fm.get("name", "").strip()
    desc = fm.get("description", "").strip()
    if not name or not desc:
        print(
            f"  ! {skill_md} missing name or description in frontmatter; skipping",
            file=sys.stderr,
        )
        return None
    rel_dir = skill_md.parent.relative_to(src_root) if skill_md.parent != src_root else Path(".")
    if rel_dir == Path("."):
        # Top-level SKILL.md in a bundle: id = local__<bundle-slug>.
        slug = src_root.name
    else:
        # Nested: id = local__<bundle>__<rel-path-with-__>
        path_segments = (src_root.name,) + rel_dir.parts
        slug = "__".join(path_segments)
    eid = f"local__{slug}"
    if body_strips_frontmatter:
        end = text.find("\n---", 4)
        body = text[end + 4:].lstrip("\n") if end >= 0 else text
    else:
        body = text
    return sanitize_entry({"id": eid, "name": name, "description": desc, "body": body})


def _collect_rows(source: Path, body_strips_frontmatter: bool) -> list[dict]:
    """Walk `source` for SKILL.md and MODEL.md, return sanitized rows."""
    rows: list[dict] = []
    candidates = sorted(
        list(source.rglob("SKILL.md")) + list(source.rglob("MODEL.md"))
    )
    if not candidates:
        print(f"  ! no SKILL.md or MODEL.md found under {source}", file=sys.stderr)
        return rows
    for f in candidates:
        row = _build_row(f, source, body_strips_frontmatter)
        if row is not None:
            rows.append(row)
    return rows


def _load_existing() -> tuple[dict[str, dict], list[str]]:
    """Return (id→row index, raw lines preserved for non-replaced rows)."""
    if not LOCAL_EXTRAS.exists():
        return {}, []
    by_id: dict[str, dict] = {}
    lines: list[str] = []
    with LOCAL_EXTRAS.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Preserve unreadable line; don't index it.
                lines.append(line)
                continue
            if isinstance(row, dict) and "id" in row:
                by_id[row["id"]] = row
            lines.append(line)
    return by_id, lines


def _write(rows_by_id: dict[str, dict]) -> None:
    """Atomic write of local_extras.jsonl, sorted by id for stable diffs."""
    tmp = LOCAL_EXTRAS.with_suffix(LOCAL_EXTRAS.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for eid in sorted(rows_by_id.keys()):
            out.write(json.dumps(rows_by_id[eid], ensure_ascii=False) + "\n")
    tmp.replace(LOCAL_EXTRAS)


def _resolve_source(path: Path) -> tuple[Path, bool]:
    """Returns (walk_root, is_temp). When given a .skill (zip), unzip to a
    tempdir and return that path. Caller is responsible for cleanup if
    `is_temp` is True (handled by caller via TemporaryDirectory)."""
    if path.is_file() and path.suffix == ".skill":
        tmp = Path(tempfile.mkdtemp(prefix="skill-extract-"))
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp)
        # The .skill bundles wrap content in a single top-level dir matching
        # the bundle slug (elons-brain.skill → elon/, musk-5-step.skill → musk-5-step/).
        # If there's exactly one top-level dir, descend into it for a stable id-prefix.
        children = [c for c in tmp.iterdir() if c.is_dir()]
        return (children[0] if len(children) == 1 else tmp, True)
    return (path, False)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "source",
        type=Path,
        help="Path to a SKILL.md, a directory containing SKILL.md files, or a .skill bundle.",
    )
    parser.add_argument(
        "--strip-frontmatter",
        action="store_true",
        help="Strip the YAML frontmatter from the body field before writing "
             "(matches skillier-lite's convention). Default keeps the whole "
             "file in body (matches skillier-full).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing local_extras.jsonl.",
    )
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"ERROR: {args.source} does not exist", file=sys.stderr)
        return 1

    walk_root, is_temp = _resolve_source(args.source)
    try:
        if walk_root.is_file():
            row = _build_row(walk_root, walk_root.parent, args.strip_frontmatter)
            new_rows = [row] if row else []
        else:
            new_rows = _collect_rows(walk_root, args.strip_frontmatter)
    finally:
        if is_temp and walk_root.exists():
            # walk_root might be inside the tempdir or the tempdir itself.
            tmp_root = walk_root
            while tmp_root.parent.name.startswith("skill-extract-"):
                tmp_root = tmp_root.parent
            shutil.rmtree(tmp_root, ignore_errors=True)

    if not new_rows:
        print("nothing to add", file=sys.stderr)
        return 1

    existing_by_id, _ = _load_existing()
    added = 0
    replaced = 0
    for row in new_rows:
        if row["id"] in existing_by_id:
            replaced += 1
        else:
            added += 1
        existing_by_id[row["id"]] = row

    print(f"local_extras.jsonl: +{added} new, {replaced} replaced, "
          f"total {len(existing_by_id)}")
    for row in new_rows:
        action = "↻" if row["id"] in {r["id"] for r in new_rows[:new_rows.index(row)]} else "+"
        print(f"  {action} {row['id']}  ({row['name']})")

    if args.dry_run:
        print("(dry-run; no write)")
        return 0

    _write(existing_by_id)
    print(f"wrote {LOCAL_EXTRAS}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
