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


# --- Phase 6: speaking-style coaching --------------------------------------
SPEECH_COACH_SYSTEM_PROMPT = """You are a speaking coach reviewing a transcript of someone talking, along with objective measurements already computed from the audio (pace, pauses, filler word counts). Give specific, constructive, actionable feedback — not generic advice.

Produce JSON matching this exact schema:

{
  "strengths": [
    "A specific thing the speaker did well, citing an actual phrase or pattern from the transcript"
  ],
  "areas_to_improve": [
    {
      "observation": "A specific pattern noticed (e.g. hedging language, a rambling sentence, a filler word habit)",
      "example": "An actual quote or near-quote from the transcript illustrating it",
      "suggestion": "A concrete, specific alternative phrasing or technique — not generic advice like 'be more confident'"
    }
  ],
  "pace_feedback": "One or two sentences on their pace/pausing, using the actual WPM and pause numbers provided — only flag it if it's genuinely notable, don't invent a critique if the pace was fine",
  "overall_take": "A brief, honest, encouraging summary — 2-3 sentences"
}

Rules:
- Ground every observation in the actual transcript text or the provided metrics — never invent a pattern that isn't there.
- Filler word counts are a rough heuristic (see the note in the data below) — use judgment about whether the count is actually notable for a clip this length, rather than treating any nonzero count as a problem.
- Prioritize quality over quantity: 2-4 real, specific observations beat a padded list of generic ones.
- Be constructive and specific, not harsh — this is coaching, not criticism for its own sake.
- If the clip is very short or there's genuinely not much to comment on, say so honestly in overall_take rather than manufacturing feedback.
- Output ONLY the JSON object. No preamble, no markdown code fences, no explanation.
"""


def build_speech_coach_prompt(transcript_text: str, metrics: dict) -> str:
    fillers = metrics["fillers"]
    pauses = metrics["pauses"]
    pace = metrics["pace"]

    metrics_summary = f"""Computed metrics (filler-word counts are a rough word-matching heuristic, not perfect intent detection — use judgment about whether they're actually meaningful for a clip this length):
- Duration: {pace['duration_seconds']}s, {pace['word_count']} words, {pace['words_per_minute']} words/minute
- Pauses: {pauses['pause_count']} total, longest {pauses['longest_pause_seconds']}s, notable pauses (>1.5s): {pauses['notable_pauses']}
- Filler words detected: {fillers['filler_counts']} ({fillers['filler_rate_per_100_words']} per 100 words)"""

    return f"Transcript:\n\n{transcript_text}\n\n{metrics_summary}\n\nProduce the JSON coaching feedback now."
