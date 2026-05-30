#!/usr/bin/env python3
"""BM25 search over the skillbank. Usage: search.py "query text" [top_k]

Prints a JSON array of candidates: [{id, name, description, score}].
Pure stdlib.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "index" / "bm25.json"

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: search.py "<query>" [top_k]', file=sys.stderr)
        return 2
    query = sys.argv[1]
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    if not INDEX_PATH.exists():
        print(f"no index at {INDEX_PATH}. Run scripts/build_index.py first.", file=sys.stderr)
        return 1

    try:
        idx = json.loads(INDEX_PATH.read_text())
        N = idx["N"]
        avgdl = idx["avgdl"]
        postings = idx["postings"]
        dl = idx["dl"]
        meta = idx["meta"]
    except (json.JSONDecodeError, KeyError) as e:
        print(
            f"index at {INDEX_PATH} is corrupted ({e}). "
            f"Run scripts/build_index.py to rebuild.",
            file=sys.stderr,
        )
        return 1

    scores: dict[str, float] = {}
    for tok in tokenize(query):
        p = postings.get(tok)
        if not p:
            continue
        df = len(p)
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        for doc_id, tf in p.items():
            # A doc_id appearing in postings but missing from dl/meta means
            # the index is partially stale (build was interrupted, or two
            # tools wrote it concurrently). Treat that doc as missing rather
            # than crashing the search with a KeyError.
            doc_dl = dl.get(doc_id)
            if doc_dl is None or doc_id not in meta:
                continue
            norm = K1 * (1 - B + B * doc_dl / avgdl) if avgdl else K1
            contrib = idf * (tf * (K1 + 1)) / (tf + norm)
            scores[doc_id] = scores.get(doc_id, 0.0) + contrib

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
    out = [
        {
            "id": doc_id,
            "name": meta[doc_id]["name"],
            "description": meta[doc_id]["description"],
            "score": round(s, 3),
        }
        for doc_id, s in ranked
    ]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
