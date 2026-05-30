#!/usr/bin/env python3
"""Build a BM25 index → index/bm25.json.

Reads from skillbank.jsonl (preferred, distribution-friendly) or
skillbank/*.md (dev-friendly) — whichever is present.

Pure stdlib — no pip deps.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from sanitize import sanitize_entry

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "skillbank.jsonl"
SKILLBANK_DIR = ROOT / "skillbank"
INDEX_DIR = ROOT / "index"
INDEX_PATH = INDEX_DIR / "bm25.json"

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
JUNK_STEMS = {"README", "TEMPLATE"}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser — handles key:value and folded/literal blocks."""
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return {}
    norm = text.replace("\r\n", "\n")
    end = norm.find("\n---", 4)
    if end < 0:
        return {}
    lines = norm[4:end].split("\n")
    result: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([a-zA-Z_][\w-]*):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|"):
            cont: list[str] = []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith(" ") or nxt.startswith("\t"):
                    cont.append(nxt.strip())
                    i += 1
                elif nxt == "":
                    i += 1
                else:
                    break
            sep = " " if val == ">" else "\n"
            result[key] = sep.join(c for c in cont if c)
            continue
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        result[key] = val
        i += 1
    return result


def _load_drop_set():
    """Curation gate verdict ({stem: reason}) from scripts/curate.py. Empty when
    the gate hasn't been run — dir-mode indexing is then ungated. A present-but-
    corrupt verdict warns loudly and falls back to ungated (mirrors pack.py)."""
    drop_path = SKILLBANK_DIR / "_curation" / "drop.json"
    if not drop_path.exists():
        return {}
    try:
        data = json.loads(drop_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: {drop_path} present but unreadable ({e}); indexing UNGATED", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"WARNING: {drop_path} not a JSON object; indexing UNGATED", file=sys.stderr)
        return {}
    return data


def iter_entries_from_dir():
    drop_set = _load_drop_set()
    for f in sorted(SKILLBANK_DIR.glob("*.md")):
        stem = f.stem
        if stem in drop_set:
            continue
        last_seg = stem.rsplit("__", 1)[-1]
        if last_seg in JUNK_STEMS:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = parse_frontmatter(text)
        name = fm.get("name") or stem
        desc = fm.get("description", "")
        if not desc:
            continue
        yield sanitize_entry({"id": stem, "name": name, "description": desc})


def iter_entries_from_jsonl():
    with JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not e.get("description"):
                continue
            yield sanitize_entry({"id": e["id"], "name": e.get("name") or e["id"], "description": e["description"]})


def iter_entries_from_jsonl_at(jsonl_path: Path):
    """Variant of iter_entries_from_jsonl that takes an explicit path —
    used by pack.py for the atomic-staging build (reads the freshly built
    JSONL in tempdir before it lands at the repo root)."""
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not e.get("description"):
                continue
            yield sanitize_entry({"id": e["id"], "name": e.get("name") or e["id"], "description": e["description"]})


def _index_entries(entries):
    """Pure index construction from an iterable of {id, name, description}.
    Extracted so it can be tested without going through file I/O."""
    postings: dict[str, dict[str, int]] = {}
    dl: dict[str, int] = {}
    meta: dict[str, dict[str, str]] = {}
    total_len = 0
    seen_fp: set[tuple[str, str]] = set()
    skipped_dup = 0
    for e in entries:
        fp = (e["name"].lower().strip(), e["description"].lower().strip())
        if fp in seen_fp:
            skipped_dup += 1
            continue
        seen_fp.add(fp)
        tokens = tokenize(f"{e['name']} {e['description']}")
        doc_id = e["id"]
        dl[doc_id] = len(tokens)
        total_len += len(tokens)
        meta[doc_id] = {"name": e["name"], "description": e["description"]}
        tf_local: dict[str, int] = {}
        for tok in tokens:
            tf_local[tok] = tf_local.get(tok, 0) + 1
        for tok, tf in tf_local.items():
            postings.setdefault(tok, {})[doc_id] = tf
    n = len(dl)
    avgdl = total_len / n if n else 0.0
    return {
        "N": n,
        "avgdl": avgdl,
        "postings": postings,
        "dl": dl,
        "meta": meta,
        "_skipped_dup": skipped_dup,
    }


def build_index_at(jsonl_path: Path, out_path: Path) -> int:
    """Build the index from a specific jsonl path and write to a specific
    out_path. Returns the number of indexed skills. Used by pack.py's
    atomic-staging path."""
    entries = list(iter_entries_from_jsonl_at(jsonl_path))
    idx = _index_entries(entries)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in idx.items() if not k.startswith("_")}
    out_path.write_text(json.dumps(payload))
    return idx["N"]


def main() -> int:
    if JSONL.exists():
        source = "skillbank.jsonl"
        entries = list(iter_entries_from_jsonl())
    elif SKILLBANK_DIR.exists():
        source = "skillbank/"
        entries = list(iter_entries_from_dir())
    else:
        print(f"no skillbank at {JSONL} or {SKILLBANK_DIR}", file=sys.stderr)
        return 1

    idx = _index_entries(entries)
    INDEX_DIR.mkdir(exist_ok=True)
    payload = {k: v for k, v in idx.items() if not k.startswith("_")}
    INDEX_PATH.write_text(json.dumps(payload))
    print(
        f"indexed {idx['N']} skills from {source} → "
        f"{INDEX_PATH.relative_to(ROOT)} (skipped {idx['_skipped_dup']} dup)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
