"""Invisible-character sanitizer for skill content.

Defends against ASCII smuggling / invisible prompt injection via Unicode Tag
block (U+E0000-E007F) and adjacent format-character vectors. Anthropic models
decode Tag block to ASCII; the bytes survive copy-paste and most rendering
pipelines, so a malicious skill author can embed instructions invisible to
humans reviewing the SKILL.md.

Applied at three boundaries:
  1. Ingest (scripts/build_index.py, server/app/ingest.py) — canonical strip
  2. Server response (server/app/models.py field validators) — defense in depth
  3. Pre-commit (scripts/check_invisible.py) — block injections at PR time

**Out of scope** (documented decisions, not oversights):

  - **Homoglyphs / visually-confusable chars** (Cyrillic А vs Latin A, math-
    alphanumeric 𝐀 vs A, fullwidth Ａ vs A). These are *visible* — a careful
    reviewer can see "𝐒𝐐𝐋" looks off. The threat is real (different LLM
    tokenization) but lower-severity than invisibles, and NFKC normalization
    (which would collapse them) has too many false positives on legitimate
    bilingual content. Reassess if a real exploit surfaces.

  - **Punycode / IDN spoofing in URLs** — caught at the URL layer by
    pack._validate_backend_url's scheme check + downstream HTTPS hostname
    validation, not this module's job.

  - **Steganographic encoding in legitimate characters** (e.g. binary in
    capitalization patterns, whitespace tab/space alternation). Defensible
    only with statistical/ML detection; out of scope for stdlib pure-Python.

Pure stdlib. No pip deps.

See:
  - https://aws.amazon.com/blogs/security/defending-llm-applications-against-unicode-character-smuggling/
  - https://arxiv.org/html/2603.00164v1 (Reverse CAPTCHA)
  - https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/
"""
from __future__ import annotations

import unicodedata
from typing import NamedTuple

# Ranges chosen for: invisible to humans in standard renderers AND interpretable
# (or smuggle-able) by LLMs. Each range carries a `reason` for the log line.
INVISIBLE_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0000, 0x001F, "C0-control"),       # NUL, BEL, LF, CR, etc. Most break layout but a few (TAB, etc.) are kept by other tools — strip all defensively.
    (0x007F, 0x007F, "DEL"),
    (0x00AD, 0x00AD, "soft-hyphen"),
    (0x061C, 0x061C, "arabic-letter-mark"),
    (0x180E, 0x180E, "mongolian-vowel-separator"),
    (0x200B, 0x200F, "zero-width+bidi-mark"),  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    (0x202A, 0x202E, "bidi-override"),
    (0x2060, 0x206F, "word-joiner+invisible-operator"),
    (0xFE00, 0xFE0F, "variation-selector"),
    (0xFEFF, 0xFEFF, "BOM/ZWNBSP"),
    (0xE0000, 0xE007F, "unicode-tag-block"),   # *** primary Anthropic injection vector ***
    (0xE0100, 0xE01EF, "variation-selector-supplement"),
)

# Whitespace chars allowed despite being in the C0 range. Keep newline and tab
# because they are legitimate in skill bodies. Strip everything else.
_ALLOWED_C0 = frozenset({0x09, 0x0A, 0x0D})  # TAB, LF, CR

# Pre-compute the codepoint→reason lookup once for hot-path speed.
def _build_codepoint_map() -> dict[int, str]:
    m: dict[int, str] = {}
    for lo, hi, reason in INVISIBLE_RANGES:
        for cp in range(lo, hi + 1):
            if cp in _ALLOWED_C0:
                continue
            m[cp] = reason
    return m

_FORBIDDEN: dict[int, str] = _build_codepoint_map()


class SanitizeResult(NamedTuple):
    cleaned: str
    removed_count: int
    removed_by_reason: dict[str, int]


def strip_invisible(text: str) -> SanitizeResult:
    """Remove all invisible/format characters from `text`.

    Returns the cleaned string, total count of removed characters, and a
    breakdown by reason (range label) for logging / threat intel.

    Idempotent: strip_invisible(strip_invisible(s).cleaned).removed_count == 0.

    Belt-and-suspenders: also drops any Unicode Category Cf character we did
    not explicitly enumerate. Cf = "format" — by definition invisible.
    """
    if not text:
        return SanitizeResult(text, 0, {})

    out: list[str] = []
    removed = 0
    by_reason: dict[str, int] = {}

    for ch in text:
        cp = ord(ch)
        reason = _FORBIDDEN.get(cp)
        if reason is None and unicodedata.category(ch) == "Cf" and cp not in _ALLOWED_C0:
            reason = "other-Cf-category"
        if reason is not None:
            removed += 1
            by_reason[reason] = by_reason.get(reason, 0) + 1
            continue
        out.append(ch)

    return SanitizeResult("".join(out), removed, by_reason)


def contains_invisible(text: str) -> bool:
    """Cheap predicate for pre-commit hooks. Returns True if any forbidden
    codepoint is present. Does not enumerate; short-circuits on first hit.
    """
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if cp in _FORBIDDEN:
            return True
        if unicodedata.category(ch) == "Cf" and cp not in _ALLOWED_C0:
            return True
    return False


def format_log_line(skill_id: str, field: str, result: SanitizeResult) -> str:
    """Structured log line for threat-intel ingestion. One line per detection.

    `skill_id` is escaped via repr() so that an attacker-controlled id
    containing newlines, tabs, or invisible chars cannot break log framing
    or inject fake k=v pairs into downstream log shippers.
    """
    parts = ",".join(f"{r}={n}" for r, n in sorted(result.removed_by_reason.items()))
    safe_id = repr(str(skill_id))  # quotes + escapes \n, \t, control chars, non-ASCII
    return f"[sanitize] skill_id={safe_id} field={field} removed={result.removed_count} ranges={parts}"


# Default fields stripped at ingest. We deliberately exclude `id` from this
# tuple because the id doubles as a filesystem stem (skillbank/<id>.md), an
# HTTP path component (/v1/skills/<id>), and a primary key — silently
# stripping bytes from it would create lookup divergence between cleaned id
# and the raw file on disk. Instead, id/source/updated_at are protected by:
#   - Pydantic field validators on SearchHit/SkillDetail (output scrubbing)
#   - `repr()` escape in format_log_line (log-line safety)
#   - The pre-commit hook (catches malicious ids at filename level via
#     the broader `scripts/.*\.py|SKILL\.md` pattern in .pre-commit-config.yaml)
DEFAULT_ENTRY_FIELDS = ("name", "description", "body")


def sanitize_entry(
    entry: dict,
    fields: tuple[str, ...] = DEFAULT_ENTRY_FIELDS,
    log_stream=None,
) -> dict:
    """Strip invisibles from the listed fields of a skill entry dict.

    Logs each detection via `format_log_line` to `log_stream` (defaults to
    sys.stderr). Returns a NEW dict — does not mutate the input.

    Non-string / empty / missing fields are skipped silently. This is the
    canonical place for ingest-time sanitization; both scripts/build_index.py
    and server/app/ingest.py compose this function.
    """
    import sys as _sys
    out = dict(entry)
    stream = log_stream if log_stream is not None else _sys.stderr
    for field in fields:
        val = out.get(field)
        if not isinstance(val, str) or not val:
            continue
        result = strip_invisible(val)
        if result.removed_count > 0:
            print(
                format_log_line(out.get("id", "?"), field, result),
                file=stream,
            )
            out[field] = result.cleaned
    return out
