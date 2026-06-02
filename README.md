# Skillier

[![Skillproof: safety](https://skillproof-api.fly.dev/v1/badge/yF1yaDHgax8d.svg)](https://proof.skillier.ai/r/yF1yaDHgax8d) [![Skillproof: quality](https://skillproof-api.fly.dev/v1/badge/yF1yaDHgax8d.svg?pillar=quality)](https://proof.skillier.ai/r/yF1yaDHgax8d)

> The lite version (this repo) is A single skill that searches a packed databank of **1,852 curated expert skills** with stdlib BM25, surfaces the top candidates, lets you multi-select via `AskUserQuestion`, and loads the chosen `SKILL.md` bodies so your AI follows them.

**This repository is the open-source home of Skillier Lite** — the local-only, no-network, no-dependency, no-telemetry edition. The source you see here *is* the product: clone it, read it, run it.

The repo's **[Releases](https://github.com/skillier-ai/skillier/releases)** are also the canonical **download point** for prebuilt bundles of both editions:

| Bundle | Edition | Open source? |
| --- | --- | --- |
| `skillier-lite.skill` / `skillier-lite.tar.gz` | **Skillier Lite** — same thing as this repo, packaged for drag-drop install | ✅ Yes (this repo) |
| `skillier.skill` / `skillier.tar.gz` | **Skillier** — the full hybrid-semantic client with the hosted backend baked in | ❌ No — distributed as a build only |

> **Asset naming:** current releases use the names above (`skillier-lite.*` for Lite, `skillier.*` for Skillier). Older releases (≤ v1.4.0) used `skillier.*` for Lite and `skillier-full.*` for the full edition.

**Skillier** (the full edition — hosted at **[skillier.ai](https://skillier.ai)**, backend at **`https://api.skillier.ai`**) adds BGE-small semantic search fused with BM25 via Reciprocal Rank Fusion, an opt-in cross-encoder reranker, telemetry-driven pick boost, and an admin stats endpoint. Its runtime build is downloadable from Releases here, but its source is not open. **Lite stays the offline fallback even when you install Skillier** — the full client falls back to this same local BM25 index automatically when the backend is unreachable.

---

## How it works

1. Claude calls `scripts/search.py "<query>"` → JSON array of candidate skills with scores.
2. Claude shows the candidates to you via `AskUserQuestion` (multi-select).
3. For each pick, Claude calls `scripts/load.py <id>` → returns the full SKILL.md body prefixed with a "Skill loaded" preamble.
4. Claude follows the loaded skill's instructions for the remainder of the turn.

`SKILL.md` in this directory is the contract Claude reads. Ranking (BM25) and storage (packed JSONL + precomputed inverted-index JSON) are implementation details Claude doesn't need to know about.

## Install

### Claude Code CLI

```bash
git clone https://github.com/skillier-ai/skillier ~/.claude/skills/skillier
```

Claude auto-discovers the skill on the next session start.

### Claude Desktop or claude.com (drag-drop)

1. Open the **[latest release](https://github.com/skillier-ai/skillier/releases/latest)**.
2. Download **`skillier-lite.skill`** (Lite) or **`skillier.skill`** (Skillier — backend URL baked in).
3. Open Claude Desktop OR go to claude.com → sidebar → **Customize** → **Skills** → click **+** → **Create skill** → **Upload a skill** → drop the file.

Or build the Lite bundle yourself from this repo: `python3 scripts/pack.py` writes `~/Desktop/skillier-lite.zip` (and a `.skill` copy). `pack.py` respects Claude.app's import limits (≤1024-char `description`, ≤200 files, ≤30 MB unzipped), which is why the databank ships as a single packed `skillbank.jsonl` instead of thousands of `.md` files.

### OpenClaw

```bash
git clone https://github.com/skillier-ai/skillier ~/.openclaw/workspace/skills/skillier
```

### One-liner (any host)

```bash
curl -fsSL https://skillier.ai/install | sh                # Lite (local-only, free, MIT)
curl -fsSL https://skillier.ai/install | sh -s -- --full   # Skillier (hosted backend)
```

The install script detects your host and downloads the matching bundle from this repo's latest release.

## Sources

The databank is seeded from upstream skill repositories (see [`scripts/seed.py`](scripts/seed.py)). Every `skillbank.jsonl` row carries a `source` field with the upstream repo URL for attribution. After dedup (same `name + description` across sources), cross-source fork filtering, the curation gate (see [`scripts/curate.py`](scripts/curate.py)), and dropping two auto-generated bulk sources to keep the bundle under 30 MB unzipped: **1,852 skills indexed across 18 sources.**

| Source | Skills |
| --- | ---: |
| [davila7/claude-code-templates](https://github.com/davila7/claude-code-templates) | 661 |
| [alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills) | 386 |
| [secondsky/claude-skills](https://github.com/secondsky/claude-skills) | 203 |
| [wshobson/agents](https://github.com/wshobson/agents) | 149 |
| [K-Dense-AI/claude-scientific-skills](https://github.com/K-Dense-AI/claude-scientific-skills) | 139 |
| [trailofbits/skills](https://github.com/trailofbits/skills) | 74 |
| [garrytan/gstack](https://github.com/garrytan/gstack) | 56 |
| [bmad-code-org/BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) | 41 |
| [obra/superpowers-skills](https://github.com/obra/superpowers-skills) | 31 |
| [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills) | 22 |
| [mhattingpete/claude-skills-marketplace](https://github.com/mhattingpete/claude-skills-marketplace) | 18 |
| [anthropics/skills](https://github.com/anthropics/skills) | 17 |
| [obra/superpowers](https://github.com/obra/superpowers) | 14 |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | 13 |
| [google-labs-code/stitch-skills](https://github.com/google-labs-code/stitch-skills) | 13 |
| [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills) | 6 |
| [obra/superpowers-lab](https://github.com/obra/superpowers-lab) | 5 |
| [anthropics/claude-cookbooks](https://github.com/anthropics/claude-cookbooks) | 4 |
| **Total** | **1,852** |

### Adding your own skills

Local additions live in [`local_extras.jsonl`](local_extras.jsonl) — one JSON row per skill, committed alongside `skillbank.jsonl`. `pack.py` calls `merge_local_extras` at build time, sanitizing each row through the security defense layer and deduping against upstream entries.

Use the helper for ergonomics:

```bash
scripts/add_local_skill.py path/to/SKILL.md         # single skill
scripts/add_local_skill.py path/to/dir/             # recursive directory walk
scripts/add_local_skill.py path/to/bundle.skill --strip-frontmatter
```

Idempotent — re-running with the same id replaces the row.

## Security

Skill content reaches the LLM verbatim, so a malicious skill author could embed invisible instructions via Unicode Tag block (`U+E0000–E007F`) or zero-width / bidi / variation-selector chars. The ingest pipeline strips every codepoint in `INVISIBLE_RANGES` (see [`scripts/sanitize.py`](scripts/sanitize.py)) and logs each detection. [`scripts/check_invisible.py`](scripts/check_invisible.py) can be wired as a pre-commit hook to block injected SKILL.md at PR time. Both the sanitizer and its test suite ([`scripts/tests/test_sanitize.py`](scripts/tests/test_sanitize.py)) are open in this repo.

## Repo layout

```
SKILL.md                  # frontmatter triggers + flow instructions Claude reads
skillbank.jsonl           # packed skill databank (one skill per line)
local_extras.jsonl        # hand-curated additions (committed; merged by pack.py)
index/bm25.json           # precomputed BM25 postings + meta
scripts/
├── search.py             # stdlib BM25 query, prints JSON — Claude calls this
├── load.py               # prints a skill's body by id — Claude calls this
├── sanitize.py           # invisible-char stripper (defense layer, see Security)
├── check_invisible.py    # pre-commit hook script for skillbank content
├── add_local_skill.py    # helper: parse SKILL.md/.skill bundle → append to local_extras.jsonl
├── build_index.py        # rebuilds index/bm25.json from skillbank.jsonl or skillbank/
├── curate.py             # quality/curation gate over the raw seeded corpus
├── seed.py               # clones upstream repos → skillbank/*.md
└── pack.py               # md_to_jsonl + merge_local_extras → skillbank.jsonl + .zip/.skill
```

`skillbank/` (raw per-file form from upstream repos) is gitignored — regenerated via `scripts/seed.py`.

## Refresh the databank

```bash
scripts/seed.py --clean   # re-clones all upstream repos → skillbank/*.md
scripts/pack.py           # writes skillbank.jsonl, rebuilds index/bm25.json, emits ~/Desktop/skillier-lite.zip
```

Edit `SOURCES` in [scripts/seed.py](scripts/seed.py) to add upstream repos (and add the matching `name → URL` entry to `SOURCE_URLS` in [scripts/pack.py](scripts/pack.py) so attribution stays honest). Edit `SKIP_PREFIXES` in [scripts/pack.py](scripts/pack.py) to adjust the low-value filter.

## License

MIT. See [LICENSE](LICENSE).

The 1,852 skills bundled in `skillbank.jsonl` remain the property of their original authors under the licenses of the upstream source repositories. Each entry carries a `source` field with the upstream URL for attribution.

## Contact

Issues, takedown requests, questions: **hi@skillier.ai** or open a GitHub issue.
