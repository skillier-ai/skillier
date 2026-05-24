# Skillier — public release mirror

This repo is the **canonical download point** for [Skillier](https://skillier.ai) — a single skill that surfaces the right expert skill for every AI task, with installs for **Claude Code**, **Claude Desktop**, and **OpenClaw**.

## Install

### Claude Code · OpenClaw (one-liner)

```bash
curl -fsSL https://skillier.ai/install | sh                # Lite (local-only, free, MIT)
curl -fsSL https://skillier.ai/install | sh -s -- --full   # Full (hosted backend, free during early access)
```

The script auto-detects your host and downloads the matching bundle from this repo's latest release.

### Claude Desktop or claude.com — drag-drop (same flow either way)

1. Visit the [latest release](https://github.com/skillier-ai/skillier/releases/latest)
2. Download **`skillier.skill`** (Lite) or **`skillier-full.skill`** (Full — backend URL baked in)
3. Open Claude Desktop OR go to **claude.com** → Settings → **Capabilities** → scroll to **Skills** at the bottom → **Customize** → drop the file

## What's in each bundle

Every `.skill` (and `.tar.gz`) bundle ships:

- `SKILL.md` — frontmatter triggers + flow instructions
- `scripts/` — pure-stdlib Python (`search.py`, `load.py`, `pack.py`, …)
- `skillbank.jsonl` — 627 deduped skills with `source` field per row for upstream attribution
- `index/bm25.json` — precomputed BM25 postings
- `LICENSE` — MIT (Lite). Full's runtime client is also MIT; the hosted backend service has separate terms.

## License & attribution

- **Skillier Lite client + bundle**: MIT.
- **Each of the 627 packaged skills**: remains the property of its original author. The `source` field in every `skillbank.jsonl` row points to the upstream repo. Six sources today: [anthropics/skills](https://github.com/anthropics/skills), [alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills), [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills), [trailofbits/skills](https://github.com/trailofbits/skills), [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills), [secondsky/claude-skills](https://github.com/secondsky/claude-skills).

## Source code

The Skillier source repositories are kept private (operational separation between code and release distribution). The MIT license is honored at the bundle level — every downloaded `.skill` contains the LICENSE file plus the full client source under `scripts/`.

Takedown requests for any bundled skill: open an issue here or email **hi@skillier.ai**. We respond same week.

## Issues, bug reports, ideas

Open a GitHub issue here, or email **hi@skillier.ai**.
