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
from email.mime.application import MIMEApplication
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


def load_day_speech_coaching(target_date: date) -> list[dict]:
    """
    Returns every *.speech_coach.json in TRANSCRIPTS_DIR for target_date.
    speech_coach.py is deliberately run on-demand (not automatically for
    every recording — see its own docstring), so this list is often empty;
    the digest only shows a "Speaking Style Feedback" section at all when
    you've actually chosen to run it on something that day.

    Grouped by the date of the MATCHING CONVERSATION (its .analysis.json),
    not by whenever speech_coach.py itself happened to be run — you might
    run coaching on a recording days after it happened, and the feedback
    should stay attached to that conversation's own digest day, not get
    split off into whatever day you got around to running the script.
    Falls back to the speech_coach.json's own mtime only if no matching
    analysis exists at all.
    """
    results = []
    for path in sorted(config.TRANSCRIPTS_DIR.glob("*.speech_coach.json")):
        stem = path.stem.removesuffix(".speech_coach")
        analysis_path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
        if analysis_path.exists():
            relevant_date = datetime.fromtimestamp(analysis_path.stat().st_mtime).date()
        else:
            relevant_date = datetime.fromtimestamp(path.stat().st_mtime).date()

        if relevant_date != target_date:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data["_stem"] = stem
        results.append(data)
    return results


def render_markdown(target_date: date, analyses: list[dict], speech_coaching: Optional[list[dict]] = None) -> str:
    lines = [f"# Daily Digest — {target_date.isoformat()}", ""]
    speech_coaching = speech_coaching or []

    if not analyses and not speech_coaching:
        lines.append("No conversations recorded today.")
        return "\n".join(lines)

    if analyses:
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
    if analyses:
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

    # --- Speaking style feedback (only present when speech_coach.py was
    # actually run against something that day — see load_day_speech_coaching)
    if speech_coaching:
        # Match each speech_coach entry back to its conversation title, if
        # one exists, for a more readable label than the raw filename stem.
        titles_by_stem = {a["_stem"]: a.get("title", a["_stem"]) for a in analyses}

        lines.append("## Speaking Style Feedback")
        lines.append("")
        for sc in speech_coaching:
            label = titles_by_stem.get(sc["_stem"], sc["_stem"])
            metrics = sc["metrics"]
            feedback = sc["feedback"]
            pace = metrics["pace"]
            fillers = metrics["fillers"]

            lines.append(f"### {label}")
            lines.append(
                f"*{pace['words_per_minute']} WPM, {pace['duration_seconds']}s"
                + (f", {fillers['total_filler_count']} filler words" if fillers["total_filler_count"] else "")
                + "*"
            )
            lines.append("")

            if feedback.get("strengths"):
                lines.append("**Strengths:**")
                for s in feedback["strengths"]:
                    lines.append(f"- {s}")
                lines.append("")

            if feedback.get("areas_to_improve"):
                lines.append("**Areas to improve:**")
                for area in feedback["areas_to_improve"]:
                    lines.append(f"- {area['observation']}")
                    lines.append(f"  - Example: \"{area['example']}\"")
                    lines.append(f"  - Try instead: {area['suggestion']}")
                lines.append("")

            lines.append(f"**Pace:** {feedback.get('pace_feedback', '')}")
            lines.append("")
            lines.append(f"**Overall:** {feedback.get('overall_take', '')}")
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


def render_html(
    target_date: date,
    analyses: list[dict],
    things_link: Optional[str] = None,
    speech_coaching: Optional[list[dict]] = None,
) -> str:
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
    speech_coaching = speech_coaching or []

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

    if not analyses and not speech_coaching:
        parts.append('<p style="color: #636e72;">No conversations recorded today.</p>')
        parts.append("</div></body></html>")
        return "".join(parts)

    if analyses:
        parts.append(
            f'<p style="color: #636e72; font-size: 14px;">{len(analyses)} conversation(s) recorded.</p>'
        )

    # --- Table of contents ---
    # Jump-link TOCs are notoriously unreliable across email clients (Gmail
    # in particular has a long history of not honoring in-page anchor
    # navigation inside email bodies) — so this is included as a bonus for
    # clients that do support it, not something to depend on. The SAME
    # anchors are what make the attached PDF's table of contents genuinely
    # clickable (PDF internal links are universally reliable, unlike email).
    toc_entries = []
    for i, a in enumerate(analyses):
        toc_entries.append((f"conv-{i}", f"{a.get('emoji', '')} {a.get('title', a.get('_stem', ''))}"))
    for i, sc in enumerate(speech_coaching):
        titles_by_stem_early = {a.get("_stem", ""): a.get("title", a.get("_stem", "")) for a in analyses}
        label = titles_by_stem_early.get(sc.get("_stem", ""), sc.get("_stem", ""))
        toc_entries.append((f"speech-{i}", f"🎤 {label} (speaking style)"))

    if len(toc_entries) > 1:  # a TOC for one thing isn't worth showing
        parts.append('<h2 style="font-size: 15px; margin-top: 20px; color: #636e72;">Contents</h2>')
        parts.append('<ul style="font-size: 13px; padding-left: 20px; margin: 4px 0;">')
        for anchor_id, label in toc_entries:
            parts.append(f'<li><a href="#{anchor_id}" style="color: #0984e3;">{esc(label)}</a></li>')
        parts.append("</ul>")
        parts.append(
            '<p style="color: #b2bec3; font-size: 11px; margin-top: -4px;">'
            "(Jump links above may not work in every email app — they always work in the attached PDF.)</p>"
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
                '<p style="background-color: #f1f2f6; padding: 10px 14px; border-radius: 6px; '
                'font-size: 13px; color: #2d3436;">'
                "&#128206; See the attached file to add these to Things 3 "
                "(open it on a Mac/iPhone/iPad with Things installed).</p>"
            )

    # --- Per-conversation detail ---
    if analyses:
        parts.append('<h2 style="font-size: 17px; margin-top: 28px;">Conversations</h2>')
        for i, a in enumerate(analyses):
            emoji = a.get("emoji", "")
            title = esc(a.get("title", a.get("_stem", "")))
            category = a.get("category", "uncategorized")
            color = _CATEGORY_COLORS.get(category, _CATEGORY_COLORS["other"])
            overview = esc(a.get("overview", ""))

            parts.append(
                f'<div id="conv-{i}" style="border: 1px solid #dfe6e9; border-radius: 8px; padding: 14px 16px; '
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

    # --- Speaking style feedback (only present when speech_coach.py was
    # actually run against something that day — see load_day_speech_coaching)
    if speech_coaching:
        titles_by_stem = {a.get("_stem", ""): a.get("title", a.get("_stem", "")) for a in analyses}

        parts.append('<h2 style="font-size: 17px; margin-top: 28px;">Speaking Style Feedback</h2>')
        for i, sc in enumerate(speech_coaching):
            label = esc(titles_by_stem.get(sc.get("_stem", ""), sc.get("_stem", "")))
            metrics = sc["metrics"]
            feedback = sc["feedback"]
            pace = metrics["pace"]
            fillers = metrics["fillers"]

            filler_note = f", {fillers['total_filler_count']} filler words" if fillers["total_filler_count"] else ""

            parts.append(
                f'<div id="speech-{i}" style="border: 1px solid #dfe6e9; border-radius: 8px; padding: 14px 16px; '
                'margin-bottom: 12px;">'
            )
            parts.append(f'<div style="font-size: 16px; font-weight: 600;">{label}</div>')
            parts.append(
                f'<div style="font-size: 12px; color: #b2bec3; margin-bottom: 8px;">'
                f"{pace['words_per_minute']} WPM, {pace['duration_seconds']}s{filler_note}</div>"
            )

            if feedback.get("strengths"):
                parts.append('<div style="font-size: 13px;"><strong>Strengths:</strong></div>')
                parts.append('<ul style="font-size: 13px; margin: 4px 0 10px; padding-left: 20px;">')
                for s in feedback["strengths"]:
                    parts.append(f"<li>{esc(s)}</li>")
                parts.append("</ul>")

            if feedback.get("areas_to_improve"):
                parts.append('<div style="font-size: 13px;"><strong>Areas to improve:</strong></div>')
                for area in feedback["areas_to_improve"]:
                    parts.append(
                        f'<div style="font-size: 13px; margin: 6px 0 10px;">'
                        f"{esc(area['observation'])}<br>"
                        f'<span style="color: #636e72;">Example: "{esc(area["example"])}"</span><br>'
                        f'<span style="color: #00b894;">Try instead: {esc(area["suggestion"])}</span></div>'
                    )

            if feedback.get("pace_feedback"):
                parts.append(
                    f'<p style="font-size: 13px;"><strong>Pace:</strong> {esc(feedback["pace_feedback"])}</p>'
                )
            if feedback.get("overall_take"):
                parts.append(
                    f'<p style="font-size: 13px;"><strong>Overall:</strong> {esc(feedback["overall_take"])}</p>'
                )

            parts.append("</div>")

    parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)


def render_pdf(html_body: str) -> Optional[bytes]:
    """
    Converts the digest's HTML into a PDF via WeasyPrint, reusing the exact
    same anchors (id="conv-N", id="speech-N") that back the email's
    (best-effort) jump-link TOC.

    WeasyPrint specifically, not wkhtmltopdf: tested both directly against
    this project's actual HTML output. wkhtmltopdf (at least the commonly
    apt-packaged "unpatched Qt" build) converts same-page anchor links into
    EXTERNAL file:// URI links pointing back at the temporary source HTML
    file — which no longer exists once generation finishes, making every
    link in the shipped PDF silently dead. WeasyPrint converts the same
    anchors into genuine internal /Dest links, confirmed by inspecting the
    actual PDF's link annotations and named-destination table (they resolve
    to real page objects, not dangling references). This is the reliable
    place for a working table of contents; the email body's own TOC is a
    bonus for clients that happen to support in-page jump links, not
    something to depend on (see the note next to it in render_html).

    Returns None (rather than raising) if WeasyPrint isn't installed —
    treated as a soft failure by the caller, same philosophy as a failed
    email send: something being unavailable shouldn't take down the rest of
    the digest.
    """
    try:
        import weasyprint
    except ImportError:
        log.warning("weasyprint not installed — skipping PDF attachment. Install via: pip3 install weasyprint")
        return None

    try:
        return weasyprint.HTML(string=html_body).write_pdf()
    except Exception as e:
        log.warning("PDF generation failed, skipping PDF attachment: %s", e)
        return None


def build_things_attachment(things_link: str) -> str:
    """
    A tiny standalone HTML file with a button linking to Things. Meant to be
    attached to the email (not embedded inline) — see send_email()'s
    docstring for why: Gmail strips the href from any inline link using a
    non-standard URL scheme like things:///, but respects it fine once the
    HTML is opened as a local file outside Gmail's own rendering (e.g.
    double-clicking the attachment on a Mac, or opening it in Files on iOS).
    """
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, Helvetica, Arial, sans-serif; text-align: center; padding: 40px;">
<p style="color: #636e72; margin-bottom: 20px;">Tap below to add today's action items to Things 3.</p>
<a href="{things_link}" style="display: inline-block; padding: 14px 28px; background-color: #2d3436;
color: #ffffff; text-decoration: none; border-radius: 8px; font-size: 16px;">Add to Things 3</a>
</body></html>"""


def send_email(target_date: date, markdown: str, html_body: str, things_link: Optional[str]) -> None:
    """
    Emails the digest as a multipart/mixed message: an alternative
    text+HTML body, plus (if there are any action items) a separate HTML
    attachment for Things 3 import.

    Why the Things link is a SEPARATE ATTACHMENT rather than a button
    embedded directly in the HTML body: Gmail's HTML sanitizer strips the
    href attribute entirely from any link using a non-standard URL scheme
    like things:/// — this is a long-documented Gmail behavior, not
    something specific to this email. An embedded button would always
    render as inert plain text in Gmail specifically (Apple Mail is more
    permissive, but Gmail is the common case). Opening the attachment
    separately bypasses Gmail's live HTML rendering entirely — the browser
    that opens the attachment handles the custom scheme normally.

    Does nothing if DIGEST_EMAIL_ENABLED is false. Raises on failure rather
    than swallowing errors — caller decides how to handle that (see main(),
    which logs and continues rather than treating a failed send as fatal,
    since the markdown file itself was already written successfully either
    way).
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

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"Omi Daily Digest — {target_date.isoformat()}"
    msg["From"] = config.DIGEST_EMAIL_FROM
    msg["To"] = config.DIGEST_EMAIL_TO

    body = MIMEMultipart("alternative")
    # Plain text part MUST be attached before the HTML part — email clients
    # that support multipart/alternative render the LAST part they
    # understand, so ordering here is what makes HTML the preferred display
    # while plain text remains the fallback for clients that don't render HTML.
    body.attach(MIMEText(markdown, "plain", "utf-8"))
    body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)

    if things_link:
        attachment = MIMEText(build_things_attachment(things_link), "html", "utf-8")
        attachment.add_header(
            "Content-Disposition", "attachment", filename=f"add-to-things-{target_date.isoformat()}.html"
        )
        msg.attach(attachment)

    pdf_bytes = render_pdf(html_body)
    if pdf_bytes:
        pdf_attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        pdf_attachment.add_header(
            "Content-Disposition", "attachment", filename=f"digest-{target_date.isoformat()}.pdf"
        )
        msg.attach(pdf_attachment)

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
    speech_coaching = load_day_speech_coaching(target_date)
    markdown = render_markdown(target_date, analyses, speech_coaching)
    things_link = integrations.build_things_link(analyses, target_date)
    html_body = render_html(target_date, analyses, things_link, speech_coaching)

    out_path = config.DIGESTS_DIR / f"{target_date.isoformat()}.md"
    out_path.write_text(markdown, encoding="utf-8")

    print(f"Found {len(analyses)} conversation(s) for {target_date.isoformat()}.")
    if speech_coaching:
        print(f"Found {len(speech_coaching)} speech coaching report(s) for {target_date.isoformat()}.")
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
