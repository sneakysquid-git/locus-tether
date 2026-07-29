"""
Phase 5: compile all of a given day's analyzed conversations into a single
readable markdown digest — so you see what got captured without opening
individual transcript/analysis files by hand.

Runs on the HOST directly (no GPU/Ollama needed here — this just reads
existing *.analysis.json files and formats them), typically via a daily
systemd timer. See digest.timer / digest.service.

Usage:
    python3 digest.py                # today
    python3 digest.py 2026-07-29     # a specific date (local time)
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import config


def load_day_analyses(target_date: date) -> list[dict]:
    """
    Returns every *.analysis.json in TRANSCRIPTS_DIR whose file modification
    time falls on target_date (local time). Each dict includes the parsed
    analysis content plus '_stem' (the recording's filename stem, useful for
    cross-referencing back to the full transcript if needed).
    """
    results = []
    for path in sorted(config.TRANSCRIPTS_DIR.glob("*.analysis.json")):
        mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
        if mtime != target_date:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # skip anything unreadable rather than crash the whole digest
        data["_stem"] = path.stem.removesuffix(".analysis")
        results.append(data)
    return results


def render_markdown(target_date: date, analyses: list[dict]) -> str:
    lines = [f"# Daily Digest — {target_date.isoformat()}", ""]

    if not analyses:
        lines.append("No conversations recorded today.")
        return "\n".join(lines)

    lines.append(f"**{len(analyses)} conversation(s) recorded.**")
    lines.append("")

    # --- Top summary: every open action item across the whole day, grouped
    # together first, since this is the part most worth seeing at a glance
    # rather than hunting through each conversation individually.
    all_action_items = []
    for a in analyses:
        for item in a.get("action_items", []):
            all_action_items.append((a.get("title", a["_stem"]), item))

    if all_action_items:
        lines.append("## Action items today")
        lines.append("")
        for source_title, item in all_action_items:
            due = item.get("due_date")
            due_str = f" _(due: {due})_" if due else ""
            lines.append(f"- [ ] {item['description']}{due_str} — from *{source_title}*")
        lines.append("")

    # --- Per-conversation detail
    lines.append("## Conversations")
    lines.append("")
    for a in analyses:
        emoji = a.get("emoji", "")
        title = a.get("title", a["_stem"])
        category = a.get("category", "uncategorized")
        overview = a.get("overview", "")

        lines.append(f"### {emoji} {title}")
        lines.append(f"*Category: {category}*")
        lines.append("")
        lines.append(overview)
        lines.append("")

        key_facts = a.get("key_facts", [])
        if key_facts:
            lines.append("**Key facts:**")
            for fact in key_facts:
                lines.append(f"- {fact}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main():
    if len(sys.argv) == 2:
        target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    elif len(sys.argv) == 1:
        target_date = date.today()
    else:
        print("Usage: python3 digest.py [YYYY-MM-DD]")
        sys.exit(1)

    config.ensure_dirs()
    analyses = load_day_analyses(target_date)
    markdown = render_markdown(target_date, analyses)

    out_path = config.DIGESTS_DIR / f"{target_date.isoformat()}.md"
    out_path.write_text(markdown, encoding="utf-8")

    print(f"Found {len(analyses)} conversation(s) for {target_date.isoformat()}.")
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
