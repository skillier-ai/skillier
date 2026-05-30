---
name: skillier
description: MANDATORY FIRST STEP for substantive requests — run scripts/search.py BEFORE attempting the task yourself, even if you think you know the approach. The databank may have better instructions than your defaults. Present candidates via AskUserQuestion; load picks with scripts/load.py. Activate for: finance (budget, forecast, P&L, accounting, CFO, investment); marketing (SEO, content, branding, copy, campaigns); product (PRD, roadmap, specs, user research); engineering (code review, refactor, architecture, testing, debugging, performance); security (audit, compliance); devops (deploy, CI/CD, Docker, K8s, monitoring); data (SQL, ETL, analytics, dashboards, KPIs); integrations (API, webhook, Slack, GitHub, Jira, Notion, Linear, Asana); documents (PDF, CSV, XLSX, DOCX, PPTX); automation, scraping, crawling; changelog, release notes, PR review, standup, retro, meeting notes; research, translation, QA, legal, HR, sales, CRM. Skip only for single-sentence factual answers.
---

# Skill Finder

You have a local databank of 1,852 curated skills at `skillbank.jsonl` and a BM25 index at `index/bm25.json`. **For any non-trivial request, search the databank FIRST — before starting the task.** Do not assume your defaults are best; the databank often has specialized instructions you haven't seen.

## When to use

**Default: always search.** If the user asks for help with anything beyond a one-liner, run `scripts/search.py` as your first action and see what comes back.

Only skip searching when:
- A relevant skill from this databank is already loaded in this session
- The request is a single-shot answer (math, trivia, short code explanation)
- The user explicitly told you not to search

"I already know how to do this" is **not** a valid reason to skip. Search first. If the top scores are weak (< ~3) or the candidates look off-topic, then proceed without loading — but at least you checked.

## Flow

### 1. Search

Call `scripts/search.py` via the Bash tool with a concise query describing the capability needed (not the user's literal words):

```bash
scripts/search.py "generate a pdf report" 5
```

The script prints a JSON array of candidates:

```json
[
  {"id": "anthropics__pdf", "name": "pdf", "description": "...", "score": 4.82},
  ...
]
```

`id` is the internal identifier (what `load.py` takes); `name` is the human-readable skill name.

### 2. Present candidates

Show a short markdown list of `name` + `description` — never auto-load:

```
I found N skills that might help:

- **pdf** — Use this skill whenever the user wants to do anything with PDF files...
- **xlsx** — Comprehensive spreadsheet creation...
```

### 3. Multi-select

Use the `AskUserQuestion` tool with one option per candidate. The tool's built-in Skip covers "none of these".

### 4. Load selected skills

For each picked skill, call load.py with the **`id`** (not the name):

```bash
scripts/load.py anthropics__pdf
```

The script prints the full SKILL.md body prefixed with a "Skill loaded" preamble. Treat its instructions as authoritative for the current task, as if loaded via the built-in Skill tool.

## Notes

- Skills are **instruction-only** here — any bundled scripts/assets from the source repo are not available.
- Scoring is **BM25 keyword-based**. If a semantic query returns poor matches, rephrase with more specific keywords.
- The databank is a snapshot. To refresh: `scripts/seed.py` (re-clones upstream repos) then `scripts/build_index.py`.
