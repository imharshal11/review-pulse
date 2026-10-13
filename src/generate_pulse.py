"""Weekly Product Pulse generator (controlled summarization).

Reads ONLY outputs/insights.json (never the raw reviews) and writes a short,
word-limited markdown pulse to outputs/weekly_pulse.md plus a machine-readable
copy to outputs/pulse.json.

It reuses the call_llm helper (JSON mode + retry + cache) from
analyze_reviews.py, so finished calls are never repeated.
"""

import json
import os
import re
from datetime import date

from dotenv import load_dotenv

# Same retry + cache helper used by the analysis script.
from analyze_reviews import call_llm

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
INSIGHTS_JSON = "outputs/insights.json"
PULSE_MD = "outputs/weekly_pulse.md"
PULSE_JSON = "outputs/pulse.json"

MAX_WORDS = 250        # hard limit for the final markdown
SHORTEN_TRIES = 3      # how many times we ask the LLM to shorten
EXCERPT_WORDS = 25     # fallback excerpt length in words
EXCERPT_WORDS_SHORT = 18  # fallback excerpt length if still too long
SPAN_MIN_WORDS = 10    # LLM quote spans must be at least this long
SPAN_MAX_WORDS = 25    # ...and at most this long

# Trailing punctuation trimmed from a quoted span before adding "...".
TRAILING_PUNCT = ".,!?;:\u2026\"'\u201d\u2019)]}"

# The big model needs a little room, but not much - this is a short document.
PULSE_KWARGS = {"max_completion_tokens": 4000}

load_dotenv()
GROQ_MODEL = os.getenv("GROQ_MODEL")


# ---------------------------------------------------------------------------
# CODE-BUILT PARTS (no LLM)
# ---------------------------------------------------------------------------
def trend_text(trend):
    """Format a trend as '↑ 90 → 111' (or down / flat)."""
    if not trend:
        return "→ no trend"
    arrow = {"up": "\u2191", "down": "\u2193"}.get(trend.get("direction"), "\u2192")
    return (
        f"{arrow} {trend.get('previous_4_weeks', 0)} "
        f"\u2192 {trend.get('last_4_weeks', 0)}"
    )


def _after_nth_word(text, limit):
    """Character position just after the limit-th word of text."""
    count = 0
    pos = 0
    for match in re.finditer(r"\S+", text):
        count += 1
        if count > limit:
            return match.start()
        pos = match.end()
    return pos


def _span_ok(text, span, lo, hi):
    """True if span is an exact substring of text with lo..hi words."""
    span = str(span).strip()
    if not span or span not in text:
        return False
    n = len(span.split())
    return lo <= n <= hi


def quote_excerpt(text, span, limit=EXCERPT_WORDS):
    """Turn one model-chosen span into a displayable exact excerpt.

    Verifies the span is an exact substring of the original text, strips
    trailing punctuation, and adds "..." at the start and/or end when the
    span is a slice of the original. Never emits a "." before the "...".
    Falls back to the first `limit` words when the span is unusable.
    """
    text = str(text)
    span = str(span).strip()

    if not _span_ok(text, span, SPAN_MIN_WORDS, SPAN_MAX_WORDS):
        # Fall back to the head of the review (still an exact substring).
        span = " ".join(text.split()[:limit])
        assert span in text, "fallback excerpt is not an exact substring"
        lead = ""
        trail = "\u2026" if len(text.split()) > limit else ""
    else:
        # Exact span; mark whether it is cut from the start / end.
        lead = "\u2026" if not text.startswith(span) else ""
        trail = "\u2026" if not text.endswith(span) else ""

    # Trim trailing punctuation so we never produce a "." before the "...".
    trimmed = span.rstrip(TRAILING_PUNCT)
    if not trimmed:
        trimmed = span

    return f"{lead}{trimmed}{trail}"


def make_excerpts(quotes, spans):
    """Build the three display excerpts, validating each model span."""
    excerpts = []
    for i, q in enumerate(quotes):
        span = spans[i] if i < len(spans) else ""
        if not _span_ok(str(q["text"]), span, SPAN_MIN_WORDS, SPAN_MAX_WORDS):
            print(f"  quote {i + 1}: unusable span, using the first "
                  f"{EXCERPT_WORDS} words")
        excerpts.append(quote_excerpt(q["text"], span))
    return excerpts


def count_words(markdown):
    """Count words after stripping markdown symbols."""
    cleaned = re.sub(r"[#*>\[\]`\"'\u201c\u201d\u2014\u2013\u2026\u2192\u2605()]", " ", markdown)
    return len([w for w in cleaned.split() if w])


def assemble_markdown(title, based_on, top_lines, quote_lines,
                      key_observation, action_lines):
    """Put the pulse together in the required structure."""
    return (
        f"# {title}\n"
        f"{based_on}\n\n"
        "## Top themes\n"
        "_Trend: 1\u20132\u2605 reviews, previous 4 weeks \u2192 last 4 "
        "weeks._\n"
        + "\n".join(top_lines)
        + "\n\n## What users say\n"
        + "\n".join(quote_lines)
        + f"\n\n## Key observation\n{key_observation}\n\n"
        "## Action ideas\n"
        + "\n".join(action_lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# LLM PART (one call, JSON mode)
# ---------------------------------------------------------------------------
def build_context(insights, quotes):
    """The only information the LLM is allowed to see."""
    by_name = {t["name"]: t for t in insights["themes"]}

    themes = []
    for name in insights["top_themes"]:
        theme = by_name[name]
        themes.append(
            {
                "name": name,
                "description": theme.get("description", ""),
                "problem_count": theme.get("problem_count", 0),
                "trend": theme.get("trend", {}),
            }
        )

    fee = insights.get("fee_issue") or {}
    return {
        "themes": themes,
        # Full text so the model can copy an exact span out of it.
        "quotes": [
            {
                "text": str(q["text"]),
                "theme": q["theme"],
                "rating": q["rating"],
            }
            for q in quotes
        ],
        "fee_issue": {
            "fee_name": fee.get("fee_name", ""),
            "what_users_think": fee.get("what_users_think", ""),
            "what_confuses_them": fee.get("what_confuses_them", ""),
            "stance_counts": fee.get("stance_counts", {}),
        },
    }


def pulse_prompt(context):
    """Build the user message for the (single) summarization call."""
    return (
        "Here is the weekly review context for a stock/mutual-fund investing "
        "app (JSON):\n\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "Write the four fields below.\n"
        "- theme_insights: exactly 3 strings, one per theme in the order "
        "given, each at most 20 words.\n"
        "- key_observation: 1-2 sentences, at most 40 words, about what is "
        "going wrong or trending.\n"
        "- action_ideas: exactly 3 strings, one per theme in the order "
        "given, each at most 20 words. Each idea must respond to a SPECIFIC "
        "detail from that theme's quote or insight above (not generic "
        "advice) and must start with a verb. The third idea must address "
        "the fee confusion (users not understanding charges beyond "
        "brokerage).\n"
        "- quote_spans: exactly 3 strings, one per quote in the order "
        "given. For each, copy the single most informative contiguous span "
        "of 10-25 words EXACTLY from that quote's text: no edits, no added "
        "words, no changed punctuation.\n"
        "Rules: do NOT write any numbers, percentages, fee amounts or fee "
        "rules (except inside quote_spans, which must stay verbatim). Do "
        "NOT invent facts. Neutral, professional tone.\n"
        'Reply as JSON: {"theme_insights": ["...", "...", "..."], '
        '"key_observation": "...", "action_ideas": ["...", "...", "..."], '
        '"quote_spans": ["...", "...", "..."]}'
    )


SYSTEM = (
    "You write a short, neutral product pulse for a product team. "
    "You only describe what the given review data shows. "
    "Always answer with a single valid JSON object and nothing else."
)


def write_pulse(context):
    """One summarization call; returns the tidy field dict."""
    raw = call_llm(GROQ_MODEL, SYSTEM, pulse_prompt(context), **PULSE_KWARGS)
    return tidy(raw)


def tidy(raw):
    """Coerce the model's JSON into the fields we need."""
    return {
        "theme_insights": [
            str(s).strip() for s in (raw.get("theme_insights") or [])[:3]
        ],
        "key_observation": str(raw.get("key_observation", "")).strip(),
        "action_ideas": [
            str(s).strip() for s in (raw.get("action_ideas") or [])[:3]
        ],
        "quote_spans": [
            str(s).strip() for s in (raw.get("quote_spans") or [])[:3]
        ],
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    if not GROQ_MODEL:
        raise SystemExit("Set GROQ_MODEL in .env")

    with open(INSIGHTS_JSON, encoding="utf-8") as fh:
        insights = json.load(fh)

    period_end = date.fromisoformat(insights["period_end"])
    quotes = insights["quotes"]

    # ---- Code-built parts (no LLM) ----
    title = f"Groww Weekly Product Pulse \u2014 Week ending {period_end:%d %b %Y}"
    based_on = (
        f"Based on {insights['total_reviews']} Play Store reviews, "
        f"{insights['period_start']} to {insights['period_end']}."
    )

    context = build_context(insights, quotes)
    print(f"Loaded {INSIGHTS_JSON}; writing the pulse...")

    # ---- LLM-written parts (one call) ----
    written = write_pulse(context)
    print("  got theme insights, key observation, action ideas and spans")

    # Code verifies each span against the original text and formats it.
    excerpts = make_excerpts(quotes, written["quote_spans"])

    def build(excerpt_list):
        """Assemble the markdown from the current text and excerpts."""
        top_lines = []
        for i, name in enumerate(insights["top_themes"], 1):
            theme = next(t for t in insights["themes"] if t["name"] == name)
            insight = (
                written["theme_insights"][i - 1]
                if i - 1 < len(written["theme_insights"]) else ""
            )
            top_lines.append(
                f"{i}. **{name}** \u2014 {theme['problem_count']} complaints "
                f"({trend_text(theme.get('trend'))}). {insight}".rstrip()
            )

        quote_lines = [
            f"> \"{excerpt}\" \u2014 {q['rating']}\u2605, {q['theme']}"
            for q, excerpt in zip(quotes, excerpt_list)
        ]

        action_lines = [
            f"{i}. {idea}"
            for i, idea in enumerate(written["action_ideas"], 1)
        ]
        # Pad so the structure stays complete even if the model returned less.
        while len(action_lines) < 3:
            action_lines.append(f"{len(action_lines) + 1}.")

        return assemble_markdown(
            title, based_on, top_lines, quote_lines,
            written["key_observation"], action_lines,
        )

    markdown = build(excerpts)
    words = count_words(markdown)

    # ---- Word limit check: ask the LLM to shorten, up to SHORTEN_TRIES times --
    tries = 0
    while words > MAX_WORDS and tries < SHORTEN_TRIES:
        tries += 1
        print(f"  {words} words > {MAX_WORDS}, shortening (try {tries})...")
        # Tighter limits each round so the next draft really fits.
        ask = 20 - 4 * tries
        user = (
            pulse_prompt(context)
            + "\nYour previous answer was:\n"
            + json.dumps(written, ensure_ascii=False)
            + f"\nIt produced a pulse of {words} words, over the {MAX_WORDS} "
            f"word limit. Rewrite it SHORTER: theme insights at most {ask} "
            f"words each, key observation at most {ask * 2} words, action "
            f"ideas at most {ask} words each. Same rules as before."
        )
        raw = call_llm(GROQ_MODEL, SYSTEM, user, **PULSE_KWARGS)
        # Keep the already-verified spans; only the prose is rewritten.
        prev_spans = written["quote_spans"]
        written = tidy(raw)
        written["quote_spans"] = prev_spans
        markdown = build(excerpts)
        words = count_words(markdown)

    # Last resort: shorten the quote excerpts themselves.
    if words > MAX_WORDS:
        print(f"  still {words} words; shortening quote excerpts to "
              f"~{EXCERPT_WORDS_SHORT} words")
        excerpts = [
            quote_excerpt(q["text"], span, limit=EXCERPT_WORDS_SHORT)
            for q, span in zip(quotes, written["quote_spans"])
        ]
        markdown = build(excerpts)
        words = count_words(markdown)

    assert words <= MAX_WORDS, f"pulse is {words} words (limit {MAX_WORDS})"

    # ---- Outputs ----
    with open(PULSE_MD, "w", encoding="utf-8") as fh:
        fh.write(markdown)

    pulse_json = {
        "date": date.today().isoformat(),
        "top_themes": insights["top_themes"],
        "weekly_pulse": markdown,
        "word_count": words,
        "theme_insights": written["theme_insights"],
        "key_observation": written["key_observation"],
        "action_ideas": written["action_ideas"],
        "quotes": [
            {
                "excerpt": excerpt,
                "text": q["text"],
                "review_id": q["review_id"],
            }
            for q, excerpt in zip(quotes, excerpts)
        ],
    }
    with open(PULSE_JSON, "w", encoding="utf-8") as fh:
        json.dump(pulse_json, fh, ensure_ascii=False, indent=2)

    print()
    print(markdown)
    print(f"\nSaved to {PULSE_MD} and {PULSE_JSON}")
    print(f"Word count: {words} / {MAX_WORDS}")


if __name__ == "__main__":
    main()
