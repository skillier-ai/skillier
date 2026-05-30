#!/usr/bin/env python3
"""Curation + security gate for the skillbank.

Runs OFFLINE (maintainer-side), reads every skillbank/*.md, and emits a verdict
file `skillbank/_curation/drop.json` ({stem: reason}) plus a human-readable
`report.md`. It does NOT write skillbank.jsonl — `scripts/pack.py:md_to_jsonl`
stays the single jsonl producer and is modified to honor drop.json. This keeps
the embedding dependency (fastembed) OUT of the pure-stdlib distributable path.

Three sub-gates, applied in order (a skill dropped by an earlier gate is not
considered by a later one):

  A. Security / injection (hard reject)
     - Reject if any field (name/description/body) contains a codepoint in the
       Unicode Tag block (U+E0000-E007F — the primary Anthropic ASCII-smuggling
       vector) or bidi-override (U+202A-202E). Zero legitimate use in a SKILL.md.
     - Other invisibles (ZWSP, soft-hyphen, variation selectors) are left to the
       existing strip-and-keep path in build_index / ingest (benign uses exist).
     - Visible prompt-injection patterns are REPORT-ONLY, never auto-rejected —
       auto-reject would nuke legitimate security skills that quote injections.

  B. Instructional-density floor (reject thin API/MCP wrappers)
     - prose_chars = body length after removing YAML frontmatter and all fenced
       code blocks, whitespace-collapsed. Reject if prose_chars < --density-floor
       (default 200). skillier strips code/assets, so a body that is mostly code
       ships nothing usable once stripped.

  C. Embedding near-duplicate dedup (cosine)
     - Embed each description with BGE-small (fastembed) and greedily keep in
       source-priority order; reject a candidate whose description cosine-exceeds
       --dedup-threshold (default 0.90) against any already-kept description.

Usage:
    python scripts/curate.py [--density-floor 200] [--dedup-threshold 0.90]
                             [--no-dedup]

Pure-stdlib except for gate C (numpy + fastembed), which is import-guarded so the
cheap gates run even where the embedding stack is absent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLBANK_DIR = ROOT / "skillbank"
CURATION_DIR = SKILLBANK_DIR / "_curation"
DROP_PATH = CURATION_DIR / "drop.json"
REPORT_PATH = CURATION_DIR / "report.md"

sys.path.insert(0, str(ROOT / "scripts"))
from build_index import parse_frontmatter, JUNK_STEMS  # noqa: E402

# --- Gate A: injection codepoint ranges (hard reject) ---------------------
# Subset of sanitize.INVISIBLE_RANGES with ZERO legitimate use in skill text.
# These are the smuggling vectors; presence => reject the whole skill rather
# than silently strip (strip-and-keep is fine for benign invisibles elsewhere).
INJECTION_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x202A, 0x202E, "bidi"),          # LRE/RLE/PDF/LRO/RLO — bidi override
    (0xE0000, 0xE007F, "tag-block"),   # Unicode Tag block — ASCII smuggling
)
# NOT hard-rejected here (only stripped downstream by sanitize.strip_invisible):
# variation-selector-supplement U+E0100-E01EF and the basic invisibles (ZWSP,
# soft-hyphen, BOM, word-joiner). VSS *can* encode data but has rare legitimate
# CJK glyph-variant use, so a corpus-wide reject would have false positives; the
# ingest-time strip (build_index + server) neutralizes it without dropping the
# skill. tag-block and bidi-override have ZERO legitimate use in a SKILL.md, so
# their mere presence is treated as an injection attempt and the skill is dropped.


def injection_reason(text: str) -> str | None:
    """Return the injection range label if `text` contains a smuggling-vector
    codepoint, else None. tag-block takes precedence (higher severity)."""
    if not text:
        return None
    hit_bidi = False
    for ch in text:
        cp = ord(ch)
        if 0xE0000 <= cp <= 0xE007F:
            return "tag-block"
        if 0x202A <= cp <= 0x202E:
            hit_bidi = True
    return "bidi" if hit_bidi else None


def gate_a_injection(entry: dict) -> str | None:
    """Hard-reject reason `inject:<label>` if any field carries a smuggling
    vector, else None. Checks name, description, body."""
    for field in ("name", "description", "body"):
        label = injection_reason(entry.get(field, ""))
        if label is not None:
            return f"inject:{label}"
    return None


# --- Visible injection (report-only) --------------------------------------
_VISIBLE_INJECTION_RE = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:your\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|preceding|system)\s+"
    r"(?:instructions?|prompts?|messages?|context|rules?)"
    r"|you\s+are\s+now\s+(?:a|an|in|the)\b"
    r"|reveal\s+(?:your|the)\s+(?:system\s+)?prompt"
    r"|(?:print|output|repeat)\s+(?:your|the)\s+(?:system\s+)?prompt",
    re.IGNORECASE,
)


def detect_visible_injection(body: str) -> bool:
    """True if the body contains a high-confidence visible prompt-injection
    phrase. REPORT-ONLY (advisory) — callers must not auto-drop on this:
    false positives on legitimate security skills that quote injections as
    content are expected, and the regex is best-effort (an attacker can phrase
    around it). Invisibles are stripped first so a zero-width-space inserted
    between words ("ignore<ZWSP>previous<ZWSP>instructions") can't defeat the
    `\\s+` token gaps."""
    from sanitize import strip_invisible  # noqa: E402 — resolves via sys.path bump above

    cleaned = strip_invisible(body or "").cleaned
    return bool(_VISIBLE_INJECTION_RE.search(cleaned))


# --- Gate B: instructional-density floor ----------------------------------
# Match a fenced code block. The `(?:```|\Z)` close-or-EOF arm strips an
# UNCLOSED fence too (``` with no terminator) — otherwise a skill could pad
# prose_chars past the floor with an unterminated code dump and evade the gate.
_FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- ... ---). Returns the body."""
    norm = text.replace("\r\n", "\n")
    if not norm.startswith("---\n"):
        return text
    end = norm.find("\n---", 4)
    if end < 0:
        return text
    rest = norm[end + 4:]
    return rest.lstrip("\n")


def prose_chars(body: str) -> int:
    """Length of the body after removing fenced code blocks and collapsing
    whitespace. The instructional signal that survives skillier's code-strip."""
    no_code = _FENCED_CODE_RE.sub(" ", body)
    collapsed = _WS_RE.sub(" ", no_code).strip()
    return len(collapsed)


def gate_b_density(entry: dict, floor: int) -> str | None:
    """Reject reason `density:prose<{floor}` if the body's prose is below the
    floor, else None."""
    body = strip_frontmatter(entry.get("body", ""))
    if prose_chars(body) < floor:
        return f"density:prose<{floor}"
    return None


# --- Gate C: source-priority + cosine dedup -------------------------------
# Lower number = higher priority = kept on a near-duplicate collision. Bulk
# auto-gen sources (antigravity, lap) sit far below curated sources so a real
# skill always wins a tie against a bulk near-duplicate.
SOURCE_PRIORITY: dict[str, int] = {
    "anthropics": 0,
    "obra": 1,
    "superpowers-skills": 1,
    "superpowers-lab": 2,
    "trailofbits": 2,
    "vercel": 3,
    "gemini-cli": 3,
    "stitch": 3,
    "claude-cookbooks": 4,
    "secondsky": 5,
    "composio": 6,
    "wshobson-agents": 6,
    "bmad": 7,
    "gstack": 7,
    "kdense-scientific": 7,
    "mhattingpete": 8,
    "alireza": 9,
    "davila7": 9,
    "antigravity": 50,
    "lap": 51,
}
_DEFAULT_PRIORITY = 30


def source_of(stem: str) -> str:
    return stem.split("__", 1)[0] if "__" in stem else stem


def priority_of(stem: str) -> int:
    return SOURCE_PRIORITY.get(source_of(stem), _DEFAULT_PRIORITY)


def _embed_bge(texts: list[str]):
    """Embed with BGE-small via fastembed (ONNX). Same model the server embeds
    descriptions with, so offline dedup matches online semantics. Imported
    lazily so the cheap gates run without the embedding stack installed."""
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return list(model.embed(texts, batch_size=64))


def cosine_dedup(
    stems: list[str],
    vectors: dict,
    threshold: float,
) -> dict[str, str]:
    """Greedy near-duplicate dedup over description embeddings.

    `stems` is the candidate set; `vectors` maps stem -> np.ndarray of any
    magnitude (each is L2-normalized to a unit vector before comparison).
    Iterates stems in (priority, stem) order so the highest-priority source is
    kept and lower-priority near-duplicates are dropped with reason
    `dup-of:<kept-stem>`. Deterministic: no RNG, stable sort.

    Pure numpy; imported lazily so the cheap gates don't require it.
    """
    import numpy as np

    order = sorted(stems, key=lambda s: (priority_of(s), s))
    if not order:
        return {}
    dim = len(next(iter(vectors.values())))
    kept_mat = np.empty((len(order), dim), dtype=np.float32)
    kept_stems: list[str] = []
    m = 0
    drops: dict[str, str] = {}

    for stem in order:
        v = np.asarray(vectors[stem], dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if norm == 0.0:
            # Degenerate (empty/zero) embedding — cannot compare; keep it and
            # let the density floor / exact-fp dedup handle it elsewhere.
            kept_mat[m] = v
            kept_stems.append(stem)
            m += 1
            continue
        vn = v / norm
        if m > 0:
            sims = kept_mat[:m] @ vn
            j = int(np.argmax(sims))
            if float(sims[j]) > threshold:
                drops[stem] = f"dup-of:{kept_stems[j]}"
                continue
        kept_mat[m] = vn
        kept_stems.append(stem)
        m += 1

    return drops


# --- Orchestration --------------------------------------------------------


def parse_skill(path: Path) -> dict | None:
    """Parse a skillbank/*.md file into {stem, name, description, body}.
    Returns None for JUNK stems (README/TEMPLATE) or files with no description
    (nothing for BM25 to index — already dropped upstream)."""
    stem = path.stem
    if stem.rsplit("__", 1)[-1] in JUNK_STEMS:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    fm = parse_frontmatter(text)
    desc = fm.get("description", "")
    if not desc:
        return None
    return {
        "stem": stem,
        "name": fm.get("name") or stem,
        "description": desc,
        "body": text,
    }


def run_gate(
    entries: list[dict],
    density_floor: int,
    dedup_threshold: float,
    do_dedup: bool,
    embed_fn=None,
) -> tuple[dict[str, str], list[str], dict]:
    """Apply gates A, B, C. Returns (drops, visible_injection_stems, stats).

    `embed_fn` maps list[str] -> list[vector]; injected for tests. When None and
    do_dedup is True, the BGE-small fastembed model is used.
    """
    drops: dict[str, str] = {}
    visible_injection: list[str] = []
    by_source_total: dict[str, int] = {}
    for e in entries:
        by_source_total[source_of(e["stem"])] = by_source_total.get(source_of(e["stem"]), 0) + 1

    survivors_ab: list[dict] = []
    for e in entries:
        reason = gate_a_injection(e)
        if reason:
            drops[e["stem"]] = reason
            continue
        reason = gate_b_density(e, density_floor)
        if reason:
            drops[e["stem"]] = reason
            continue
        if detect_visible_injection(e.get("body", "")):
            visible_injection.append(e["stem"])
        survivors_ab.append(e)

    n_after_ab = len(survivors_ab)
    dup_drops: dict[str, str] = {}
    if do_dedup and survivors_ab:
        descriptions = [e["description"] for e in survivors_ab]
        stems = [e["stem"] for e in survivors_ab]
        if embed_fn is None:
            vecs = _embed_bge(descriptions)
        else:
            vecs = embed_fn(descriptions)
        vectors = dict(zip(stems, vecs))
        dup_drops = cosine_dedup(stems, vectors, dedup_threshold)
        drops.update(dup_drops)

    stats = {
        "total": len(entries),
        "by_source_total": by_source_total,
        "dropped_inject": sum(1 for r in drops.values() if r.startswith("inject:")),
        "dropped_density": sum(1 for r in drops.values() if r.startswith("density:")),
        "dropped_dup": len(dup_drops),
        "visible_injection": len(visible_injection),
        "kept": len(entries) - len(drops),
        "n_after_ab": n_after_ab,
    }
    return drops, visible_injection, stats


def write_report(
    drops: dict[str, str],
    visible_injection: list[str],
    stats: dict,
    density_floor: int,
    dedup_threshold: float,
) -> str:
    kept_by_source: dict[str, int] = {}
    dropped_by_source: dict[str, int] = {}
    for src, total in stats["by_source_total"].items():
        kept_by_source[src] = total
    for stem in drops:
        src = source_of(stem)
        dropped_by_source[src] = dropped_by_source.get(src, 0) + 1
        kept_by_source[src] = kept_by_source.get(src, 0) - 1

    lines = [
        "# Curation gate report",
        "",
        f"- density floor: {density_floor} prose chars",
        f"- dedup threshold: cosine > {dedup_threshold}",
        "",
        "## Totals",
        f"- input skills: {stats['total']}",
        f"- dropped (injection): {stats['dropped_inject']}",
        f"- dropped (density floor): {stats['dropped_density']}",
        f"- dropped (near-duplicate): {stats['dropped_dup']}",
        f"- **kept: {stats['kept']}**",
        f"- visible-injection flagged (report-only, kept): {stats['visible_injection']}",
        "",
        "## Per source (kept / dropped)",
        "| source | kept | dropped |",
        "|---|---|---|",
    ]
    for src in sorted(stats["by_source_total"], key=lambda s: -stats["by_source_total"][s]):
        lines.append(f"| {src} | {kept_by_source.get(src, 0)} | {dropped_by_source.get(src, 0)} |")
    if visible_injection:
        lines += ["", "## Visible-injection flags (review manually)", ""]
        lines += [f"- `{s}`" for s in sorted(visible_injection)[:200]]
        if len(visible_injection) > 200:
            lines.append(f"- … and {len(visible_injection) - 200} more")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--density-floor", type=int, default=200)
    ap.add_argument("--dedup-threshold", type=float, default=0.90)
    ap.add_argument("--no-dedup", action="store_true", help="skip gate C (no embeddings)")
    args = ap.parse_args()

    if not SKILLBANK_DIR.exists():
        print(f"no skillbank at {SKILLBANK_DIR}; run scripts/seed.py first", file=sys.stderr)
        return 1

    entries: list[dict] = []
    for f in sorted(SKILLBANK_DIR.glob("*.md")):
        e = parse_skill(f)
        if e is not None:
            entries.append(e)
    print(f"parsed {len(entries)} skills with descriptions", file=sys.stderr)

    drops, visible_injection, stats = run_gate(
        entries,
        density_floor=args.density_floor,
        dedup_threshold=args.dedup_threshold,
        do_dedup=not args.no_dedup,
    )

    CURATION_DIR.mkdir(parents=True, exist_ok=True)
    DROP_PATH.write_text(json.dumps(drops, ensure_ascii=False, indent=0, sort_keys=True))
    REPORT_PATH.write_text(write_report(drops, visible_injection, stats, args.density_floor, args.dedup_threshold))

    print(
        f"gate: kept {stats['kept']}/{stats['total']} "
        f"(inject {stats['dropped_inject']}, density {stats['dropped_density']}, "
        f"dup {stats['dropped_dup']}, visible-injection flagged {stats['visible_injection']})",
        file=sys.stderr,
    )
    print(f"→ {DROP_PATH.relative_to(ROOT)}  +  {REPORT_PATH.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
