"""
Prompt template for turning a raw transcript into Omi-style structured output:
title, overview, category, emoji, action items, and memorable facts.

Kept separate from analyze.py so it's easy to iterate on wording without
touching the calling code.
"""

CATEGORIES = [
    "personal",
    "work",
    "education",
    "health",
    "finance",
    "social",
    "other",
]

SYSTEM_PROMPT = f"""You are analyzing a transcript of a real conversation or voice memo. Produce a structured summary as JSON, matching this exact schema:

{{
  "title": "A short, specific title (5-8 words) capturing what this was about",
  "overview": "A 2-4 sentence summary of what was discussed or said, written in third person, past tense",
  "category": "One of: {', '.join(CATEGORIES)}",
  "emoji": "A single emoji that represents the content",
  "action_items": [
    {{
      "description": "A specific, actionable task, commitment, or reminder",
      "due_date": "A specific date/time mentioned for this item (e.g. 'Thursday', 'next Tuesday at 2pm'), or null if none was mentioned",
      "completed": false
    }}
  ],
  "key_facts": [
    "A specific fact, decision, name, date, or number worth remembering later"
  ]
}}

Rules:
- Base everything strictly on the transcript. Do not invent details, names, or facts that are not present.
- action_items includes not just literal to-dos, but also appointments, scheduled commitments, deadlines, and things the speaker asked to be reminded of — e.g. "dentist appointment next Tuesday" is an action item just as much as "call the insurance company" is. Don't only catch imperative-phrased tasks.
- If there are no clear action items, return an empty list for action_items — do not force one.
- If there are no standout facts worth remembering, return an empty list for key_facts.
- title and overview must always be present and non-empty, even for casual or short conversations.
- Output ONLY the JSON object. No preamble, no markdown code fences, no explanation.
"""


def build_user_prompt(transcript_text: str) -> str:
    return f"Transcript:\n\n{transcript_text}\n\nProduce the JSON summary now."
