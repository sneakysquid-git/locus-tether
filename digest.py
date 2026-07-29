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
import html
import json
import logging
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import config
import integrations

log = logging.getLogger("omi.digest")


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


_CATEGORY_COLORS = {
    "personal": "#6c5ce7",
    "work": "#0984e3",
    "education": "#00b894",
    "health": "#e17055",
    "finance": "#00cec9",
    "social": "#fd79a8",
    "other": "#636e72",
}


def render_html(target_date: date, analyses: list[dict], things_link: Optional[str] = None) -> str:
    """
    Renders a clean, inline-styled HTML version of the digest. Inline styles
    throughout rather than a <style> block, since email clients (Gmail
    especially) are inconsistent about honoring <head>-level CSS — inlining
    is the safest baseline for actually looking right everywhere.

    Checkboxes here are visual only (a styled ☐ character) — no email client
    (Gmail, Outlook, Apple Mail) allows interactive form elements or
    JavaScript in email bodies, so a genuinely clickable/functional checkbox
    inside the email itself isn't possible anywhere, not just here.
    """
    esc = html.escape

    body_font = "font-family: -apple-system, Helvetica, Arial, sans-serif;"

    parts = [
        "<!DOCTYPE html>",
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>',
        "<body>",
        f'<div style="{body_font} max-width: 600px; margin: 0 auto; color: #2d3436;">',
        f'<h1 style="font-size: 22px; border-bottom: 2px solid #dfe6e9; padding-bottom: 10px;">'
        f'Daily Digest — {target_date.isoformat()}</h1>',
    ]

    if not analyses:
        parts.append('<p style="color: #636e72;">No conversations recorded today.</p>')
        parts.append("</div></body></html>")
        return "".join(parts)

    parts.append(
        f'<p style="color: #636e72; font-size: 14px;">{len(analyses)} conversation(s) recorded.</p>'
    )

    # --- Action items, rolled up first ---
    all_action_items = []
    for a in analyses:
        for item in a.get("action_items", []):
            all_action_items.append((a.get("title", a.get("_stem", "")), item))

    if all_action_items:
        parts.append('<h2 style="font-size: 17px; margin-top: 24px;">Action items today</h2>')
        parts.append('<div style="margin-bottom: 8px;">')
        for source_title, item in all_action_items:
            due = item.get("due_date")
            due_html = (
                f'<span style="color: #e17055; font-size: 13px;"> (due: {esc(due)})</span>'
                if due
                else ""
            )
            parts.append(
                '<div style="display: flex; align-items: baseline; padding: 6px 0; '
                'border-bottom: 1px solid #f1f2f6;">'
                '<span style="font-size: 18px; margin-right: 10px; color: #636e72;">&#9744;</span>'
                f'<span>{esc(item["description"])}{due_html} '
                f'<span style="color: #b2bec3; font-size: 12px;">— {esc(source_title)}</span></span>'
                "</div>"
            )
        parts.append("</div>")

        if things_link:
            parts.append(
                f'<a href="{things_link}" style="display: inline-block; margin: 12px 0; '
                'padding: 10px 18px; background-color: #2d3436; color: #ffffff; '
                'text-decoration: none; border-radius: 6px; font-size: 14px;">'
                "Add all to Things 3</a>"
            )
            parts.append(
                '<p style="color: #b2bec3; font-size: 12px; margin-top: -4px;">'
                "(Only works when opened on a Mac/iPhone/iPad with Things 3 installed.)</p>"
            )

    # --- Per-conversation detail ---
    parts.append('<h2 style="font-size: 17px; margin-top: 28px;">Conversations</h2>')
    for a in analyses:
        emoji = a.get("emoji", "")
        title = esc(a.get("title", a.get("_stem", "")))
        category = a.get("category", "uncategorized")
        color = _CATEGORY_COLORS.get(category, _CATEGORY_COLORS["other"])
        overview = esc(a.get("overview", ""))

        parts.append(
            '<div style="border: 1px solid #dfe6e9; border-radius: 8px; padding: 14px 16px; '
            'margin-bottom: 12px;">'
        )
        parts.append(
            f'<div style="font-size: 16px; font-weight: 600;">{emoji} {title}</div>'
        )
        parts.append(
            f'<span style="display: inline-block; background-color: {color}; color: #ffffff; '
            f'font-size: 11px; padding: 2px 8px; border-radius: 10px; margin: 6px 0;">'
            f"{esc(category)}</span>"
        )
        parts.append(f'<p style="font-size: 14px; line-height: 1.5;">{overview}</p>')

        key_facts = a.get("key_facts", [])
        if key_facts:
            parts.append('<div style="font-size: 13px; margin-top: 8px;"><strong>Key facts:</strong></div>')
            parts.append('<ul style="font-size: 13px; margin: 4px 0; padding-left: 20px;">')
            for fact in key_facts:
                parts.append(f"<li>{esc(fact)}</li>")
            parts.append("</ul>")

        parts.append("</div>")

    parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)


def send_email(target_date: date, markdown: str, html_body: str, things_link: Optional[str]) -> None:
    """
    Emails the digest as a multipart message: a plain-text fallback (the
    markdown, which reads fine unrendered) plus the HTML version most
    clients will actually display. Does nothing if DIGEST_EMAIL_ENABLED is
    false. Raises on failure rather than swallowing errors — caller decides
    how to handle that (see main(), which logs and continues rather than
    treating a failed send as fatal, since the markdown file itself was
    already written successfully either way).
    """
    if not config.DIGEST_EMAIL_ENABLED:
        return

    missing = [
        name
        for name, value in [
            ("OMI_DIGEST_SMTP_HOST", config.DIGEST_SMTP_HOST),
            ("OMI_DIGEST_SMTP_USER", config.DIGEST_SMTP_USER),
            ("OMI_DIGEST_SMTP_PASSWORD", config.DIGEST_SMTP_PASSWORD),
            ("OMI_DIGEST_EMAIL_FROM", config.DIGEST_EMAIL_FROM),
            ("OMI_DIGEST_EMAIL_TO", config.DIGEST_EMAIL_TO),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"DIGEST_EMAIL_ENABLED is true but these are unset: {', '.join(missing)}. "
            f"Check .env.digest (copy from .env.digest.example if it doesn't exist yet)."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Omi Daily Digest — {target_date.isoformat()}"
    msg["From"] = config.DIGEST_EMAIL_FROM
    msg["To"] = config.DIGEST_EMAIL_TO
    # Plain text part MUST be attached before the HTML part — email clients
    # that support multipart/alternative render the LAST part they
    # understand, so ordering here is what makes HTML the preferred display
    # while plain text remains the fallback for clients that don't render HTML.
    msg.attach(MIMEText(markdown, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Port 465 = implicit TLS from the start of the connection.
    # Port 587 (and most others) = plain connection, then upgrade via STARTTLS.
    # Covers the two overwhelmingly common conventions without needing the
    # user to specify which mode separately from the port number itself.
    if config.DIGEST_SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.DIGEST_SMTP_HOST, config.DIGEST_SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(config.DIGEST_SMTP_HOST, config.DIGEST_SMTP_PORT, timeout=30)
        server.starttls()

    try:
        server.login(config.DIGEST_SMTP_USER, config.DIGEST_SMTP_PASSWORD)
        server.sendmail(config.DIGEST_EMAIL_FROM, [config.DIGEST_EMAIL_TO], msg.as_string())
    finally:
        server.quit()


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
    things_link = integrations.build_things_link(analyses, target_date)
    html_body = render_html(target_date, analyses, things_link)

    out_path = config.DIGESTS_DIR / f"{target_date.isoformat()}.md"
    out_path.write_text(markdown, encoding="utf-8")

    print(f"Found {len(analyses)} conversation(s) for {target_date.isoformat()}.")
    print(f"Wrote: {out_path}")

    try:
        send_email(target_date, markdown, html_body, things_link)
        if config.DIGEST_EMAIL_ENABLED:
            print(f"Emailed digest to {config.DIGEST_EMAIL_TO}")
    except Exception as e:
        # The markdown file already exists at this point regardless — a
        # failed send shouldn't be treated as the whole digest job failing.
        logging.basicConfig(level=logging.WARNING)
        log.warning("Failed to email digest (file was still written): %s", e)


if __name__ == "__main__":
    main()
