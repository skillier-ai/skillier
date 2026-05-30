#!/usr/bin/env python3
"""Clone upstream skill repos and copy every SKILL.md into skillbank/.

Per-source failures (dead repos, auth issues, network drops) are caught
and reported at the end — a single bad source does NOT abort the whole
seed run with 5 good sources still pending.

SECURITY: SOURCES is hardcoded in this file. Adding third-party entries
is a trust decision — the repos are cloned with `git clone --depth 1`
and then walked via rglob("SKILL.md"). The copy step REFUSES symlinked
SKILL.md targets so a malicious source can't trick this script into
reading arbitrary host files via a `SKILL.md → /etc/passwd` symlink.

Usage:
    python scripts/seed.py [--clean]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLBANK = ROOT / "skillbank"
SOURCES_DIR = SKILLBANK / "_sources"

SOURCES = [
    {
        "name": "anthropics",
        "url": "https://github.com/anthropics/skills.git",
        "subdir": "skills",
    },
    {
        "name": "alireza",
        "url": "https://github.com/alirezarezvani/claude-skills.git",
        "subdir": "",
    },
    {
        "name": "composio",
        "url": "https://github.com/ComposioHQ/awesome-claude-skills.git",
        "subdir": "",
    },
    {
        "name": "trailofbits",
        "url": "https://github.com/trailofbits/skills.git",
        "subdir": "",
    },
    {
        "name": "vercel",
        "url": "https://github.com/vercel-labs/agent-skills.git",
        "subdir": "",
    },
    {
        "name": "secondsky",
        "url": "https://github.com/secondsky/claude-skills.git",
        "subdir": "",
    },
    {
        "name": "obra",
        "url": "https://github.com/obra/superpowers.git",
        "subdir": "skills",
    },
    {
        "name": "wshobson-agents",
        "url": "https://github.com/wshobson/agents.git",
        "subdir": "",
    },
    {
        "name": "bmad",
        "url": "https://github.com/bmad-code-org/BMAD-METHOD.git",
        "subdir": "src",
    },
    # --- 2026-05-30 expansion: verified to contain real SKILL.md (probed via
    # the GitHub trees API before adding; awesome-list repos with 0 SKILL.md —
    # VoltAgent/awesome-agent-skills, VoltAgent/awesome-claude-code-subagents,
    # travisvn/awesome-claude-skills, karanb192/awesome-claude-skills — are
    # deliberately NOT here). The curation gate (scripts/curate.py) decides what
    # actually ships. Counts are probed SKILL.md totals at add time.
    {"name": "superpowers-skills", "url": "https://github.com/obra/superpowers-skills.git", "subdir": ""},  # 31
    {"name": "superpowers-lab", "url": "https://github.com/obra/superpowers-lab.git", "subdir": ""},  # 5
    {"name": "gstack", "url": "https://github.com/garrytan/gstack.git", "subdir": ""},  # 61
    {"name": "kdense-scientific", "url": "https://github.com/K-Dense-AI/claude-scientific-skills.git", "subdir": ""},  # 143
    {"name": "mhattingpete", "url": "https://github.com/mhattingpete/claude-skills-marketplace.git", "subdir": ""},  # 18
    {"name": "gemini-cli", "url": "https://github.com/google-gemini/gemini-cli.git", "subdir": ""},  # 20
    {"name": "stitch", "url": "https://github.com/google-labs-code/stitch-skills.git", "subdir": ""},  # 13
    {"name": "claude-cookbooks", "url": "https://github.com/anthropics/claude-cookbooks.git", "subdir": ""},  # 4
    {"name": "davila7", "url": "https://github.com/davila7/claude-code-templates.git", "subdir": ""},  # 870
    # Flagged bulk (auto-generated) — eligible but MUST pass the curation gate.
    {"name": "antigravity", "url": "https://github.com/sickn33/antigravity-awesome-skills.git", "subdir": ""},  # 4600
    {"name": "lap", "url": "https://github.com/Lap-Platform/claude-marketplace.git", "subdir": ""},  # 1537
]


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True)


def clone_or_pull(src: dict) -> Path:
    dest = SOURCES_DIR / src["name"]
    if dest.exists():
        run(["git", "-C", str(dest), "pull", "--ff-only"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", src["url"], str(dest)])
    return dest


def _is_inside(path: Path, root: Path) -> bool:
    """True iff `path` (after symlink resolution) is contained inside `root`
    (also resolved). Used to reject symlinked SKILL.md files that escape the
    source repo."""
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, FileNotFoundError):
        return False


def copy_skills(src_name: str, src_root: Path) -> int:
    count = 0
    skipped_symlink = 0
    for skill_md in src_root.rglob("SKILL.md"):
        # rglob() returns the symlink path itself when followlinks is False
        # (the default), but reading through it would still follow the link.
        # The defensive check: resolve the target and verify it stays inside
        # src_root. Symlinks to /etc/passwd or anywhere outside the cloned
        # repo are rejected here.
        if not _is_inside(skill_md, src_root):
            print(f"  ! skipping symlinked / out-of-tree SKILL.md: {skill_md}")
            skipped_symlink += 1
            continue
        rel = skill_md.parent.relative_to(src_root)
        slug = str(rel).replace("/", "__").replace("\\", "__") or skill_md.parent.name
        target = SKILLBANK / f"{src_name}__{slug}.md"
        shutil.copyfile(skill_md, target)
        count += 1
    if skipped_symlink:
        print(f"  ({skipped_symlink} symlinked SKILL.md(s) skipped)")
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clean", action="store_true", help="Wipe skillbank/ before seeding"
    )
    args = parser.parse_args()

    if args.clean and SKILLBANK.exists():
        for f in SKILLBANK.glob("*.md"):
            f.unlink()

    SKILLBANK.mkdir(exist_ok=True)

    total = 0
    failed: list[tuple[str, str]] = []
    for src in SOURCES:
        print(f"\n=== {src['name']} ===")
        try:
            repo = clone_or_pull(src)
        except subprocess.CalledProcessError as e:
            msg = f"git failed: {e}"
            print(f"  ! {msg} — skipping {src['name']}")
            failed.append((src["name"], msg))
            continue
        walk_root = repo / src["subdir"] if src.get("subdir") else repo
        try:
            n = copy_skills(src["name"], walk_root)
        except OSError as e:
            msg = f"copy failed: {e}"
            print(f"  ! {msg} — skipping {src['name']}")
            failed.append((src["name"], msg))
            continue
        print(f"  copied {n} skills")
        total += n

    print(f"\nTotal: {total} skills in {SKILLBANK}")
    if failed:
        print("\nSources that failed (run did NOT abort, finished the rest):")
        for name, msg in failed:
            print(f"  - {name}: {msg}")
    print("Next: python scripts/build_index.py")
    # Exit non-zero only if EVERY source failed (otherwise the partial seed
    # is usable — pack.py will still produce a smaller but valid bundle).
    return 1 if failed and not total else 0


if __name__ == "__main__":
    sys.exit(main())
