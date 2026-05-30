#!/usr/bin/env python3
"""Print the SKILL.md body for a given skill id (from search.py's JSON output).

Usage: load.py <skill_id>
Pure stdlib.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "skillbank.jsonl"
SKILLBANK_DIR = ROOT / "skillbank"


def load_from_jsonl(skill_id: str) -> str | None:
    with JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("id") == skill_id:
                return entry.get("body", "")
    return None


def load_from_dir(skill_id: str) -> str | None:
    path = SKILLBANK_DIR / f"{skill_id}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: load.py <skill_id>", file=sys.stderr)
        return 2
    skill_id = sys.argv[1]
    tried: list[str] = []
    body: str | None = None
    if JSONL.exists():
        tried.append(str(JSONL))
        body = load_from_jsonl(skill_id)
    if body is None and SKILLBANK_DIR.exists():
        # Also try the raw skillbank/ dir even when JSONL exists — a dedup-
        # skipped id might still be findable as a raw .md file.
        tried.append(str(SKILLBANK_DIR))
        body = load_from_dir(skill_id)
    if body is None and not tried:
        print(
            f"no skillbank found (looked for {JSONL} and {SKILLBANK_DIR}). "
            f"Run scripts/seed.py + scripts/pack.py (or scripts/build_index.py).",
            file=sys.stderr,
        )
        return 1
    if body is None:
        # repr() the user-supplied id so invisibles/newlines can't break out of
        # the error line into the LLM's instruction stream.
        print(
            f"unknown skill id {skill_id!r}. Searched: {', '.join(tried)}. "
            f"Use the `id` field from search.py output.",
            file=sys.stderr,
        )
        return 1
    # Scrub the id before stitching it into the LLM-bound preamble. The id
    # immediately precedes "Follow these instructions" — a perfect injection
    # slot if a poisoned id slipped past ingest sanitization.
    try:
        from sanitize import strip_invisible  # type: ignore[import-not-found]
        safe_id = strip_invisible(skill_id).cleaned
    except ImportError:
        # repr() — DO NOT slice off outer quotes: repr may switch quote style
        # when the id contains a quote char, leaving internal quotes that
        # would break f-string framing.
        safe_id = repr(skill_id)
        print(
            f"Skill {safe_id} loaded. Follow these instructions for the current task:\n\n{body}"
        )
        return 0
    print(
        f"Skill '{safe_id}' loaded. Follow these instructions for the current task:\n\n{body}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
