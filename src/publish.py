"""Approval-gated publishing through the n8n MCP server.

Reads outputs/pulse.json, outputs/fee_explainer.json and outputs/insights.json,
builds the Doc entry and the HTML Gmail draft (previewed via
outputs/email_preview.html), shows the Doc entry and email subject in the
terminal, and only publishes after the exact word APPROVE is typed. The
content is bound to the approval with a SHA-256 payload hash that is
re-checked at every step, so content approved once can never change under
cover of that approval.

Reusable functions (a Streamlit UI will import them):
  build_payloads()            read the inputs and build the payload parts
  compute_hash(payloads)      SHA-256 over the canonical payload JSON
  request_approval_cli(...)   preview + print the Doc entry; type APPROVE
  publish(...)                run the ordered MCP steps for an approval

Usage:
  python src/publish.py
  python src/publish.py --new-entry
  python src/publish.py --resume <approval_id> [--new-entry]

The MCP connection approach is the one from src/mcp_check.py: truststore, then
Streamable HTTP (SSE as fallback) with a Bearer token from .env. The token is
never printed and failures are friendly messages, never raw tracebacks.
"""

# Use the OS trust store so corporate/self-signed roots are accepted.
# Must run before any httpx/mcp import (they build SSL contexts at import).
import truststore
truststore.inject_into_ssl()

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
import threading
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

# Reuse the connection helpers / friendly error reasons from the check script.
import mcp_check
from mcp_check import (
    _friendly_reason,
    _is_connection_error,
    _log_traceback,
    _reason_rank,
    _record_status,
    _unwrap_group,
)

try:
    import httpx2 as httpx  # mcp >= 2 talks HTTP through its vendored httpx fork
except ModuleNotFoundError:  # older mcp releases use plain httpx
    import httpx

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
PULSE_JSON = OUTPUTS / "pulse.json"
FEE_JSON = OUTPUTS / "fee_explainer.json"
INSIGHTS_JSON = OUTPUTS / "insights.json"
FEE_MD = OUTPUTS / "fee_explainer.md"
APPROVALS_DIR = OUTPUTS / "approvals"
PUBLISH_LOG = OUTPUTS / "publish_log.jsonl"
EMAIL_PREVIEW = OUTPUTS / "email_preview.html"

# The three payload strings the approval hash covers. The approval footer
# lines (which carry the hash itself) are built at publish time and are NOT
# part of the hashed text, so embedding the hash cannot change the hash.
HASH_KEYS = ("doc_entry_body", "email_subject", "email_body_body")

DOC_HEADER = "===== Review Pulse entry — week ending {week_label} ====="
DOC_END = "===== end of entry ====="
DOC_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Email layout (HTML with inline CSS; Gmail strips <style> blocks).
EMAIL_TITLE = "Weekly Product Pulse"
GREETING = "Hi team,"
ISSUES_HEADING = "Top 3 issues"
ISSUES_NOTE = "1–2★ reviews: previous 4 weeks → last 4 weeks"
QUOTES_HEADING = "What users say"
KEY_POINT_HEADING = "Key point"
ACTIONS_HEADING = "Suggested actions"
SNIPPET_HEADING = "Support snippet (ready to send to users)"
SNIPPET_QUESTION = "Why is my total charge more than brokerage?"
SOURCES_HEADING = "Sources:"
EMAIL_WORD_BUDGET = 230   # summary part only (greeting, snippet, Ref excluded)
SHORT_EXCERPT_WORDS = 15   # over budget: keep only a quote's LAST 15 words

# First half of the code-built fee line (insights stance/pattern counts).
FEE_LINE_PREFIX = "Charges beyond brokerage seen as hidden — top confusion pattern: "

# Same fixed title src/fee_explainer.py uses; only a fallback if the
# explainer markdown is missing.
FALLBACK_TITLE = "Why the total charge on a trade can be more than brokerage"

# Keys searched (in this order) for the draft id in a create_email_draft result.
DRAFT_ID_KEYS = ("draft_id", "draftid", "message_id", "messageid", "id")
DRAFT_ID_RE = re.compile(
    r"(?i)\b(?:draft|message)[ _]?id\b[\"'\s:=]+([A-Za-z0-9@._-]{4,})"
)

_APPROVAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
STEP_RESULT_LIMIT = 2000  # characters of tool output kept in the record

CONNECT_TIMEOUT = 30.0    # seconds allowed to open the HTTP connection
READ_TIMEOUT = 120.0      # seconds allowed to hear back from n8n
RETRY_DELAYS = (5.0, 10.0, 20.0)   # waits between connection attempts

# Set by _require_env() so error text can be scrubbed; never printed.
TOKEN = ""


class PublishError(Exception):
    """A failure the operator can act on; always safe to print."""


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------
def _rel(path):
    """Project-relative, forward-slash path (for readable messages)."""
    return os.path.relpath(str(path), str(ROOT)).replace(os.sep, "/")


def _clean(text):
    """Make sure the token can never leak into any printed message."""
    if TOKEN and TOKEN in text:
        return text.replace(TOKEN, "***")
    return text


def _now_iso():
    """Local time as ISO-8601, e.g. 2026-10-15T14:03:11+05:30."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short(text, limit=STEP_RESULT_LIMIT):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def _banner(title):
    line = "=" * 72
    print(line)
    print(title)
    print(line)


def _load_json(path):
    if not path.exists():
        raise PublishError(
            f"missing input file: {_rel(path)} — run the earlier pipeline "
            "steps first"
        )
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"cannot read {_rel(path)}: {_clean(str(exc))}")


def _week_label(insights):
    """insights period_end formatted 'DD Mon YYYY'."""
    period_end = str(insights.get("period_end") or "").strip()
    try:
        return date.fromisoformat(period_end).strftime("%d %b %Y")
    except ValueError:
        raise PublishError(
            f"{_rel(INSIGHTS_JSON)} has no usable period_end "
            f"(got {period_end!r})"
        )


def _fee_title(fee):
    """The explainer title: JSON title if present, else the md heading."""
    title = str(fee.get("title") or "").strip()
    if title:
        return title
    try:
        with open(FEE_MD, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#"):
                    heading = line.lstrip("#").strip()
                    if heading:
                        return heading
    except OSError:
        pass
    return FALLBACK_TITLE


# ---------------------------------------------------------------------------
# DOC + EMAIL PIECES (all code-built, no LLM)
# ---------------------------------------------------------------------------
def _int(value):
    """An int for counts; anything unusable becomes 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _comma(value):
    """Thousands separators for an int-like value."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _identified_fee_issue(insights):
    """Code-built fee line from the confusion-pattern + stance counts.

    Never uses what_confuses_them; the wording is fixed and only the
    pattern/stance numbers come from outputs/insights.json.
    """
    fee_issue = insights.get("fee_issue") or {}
    patterns = fee_issue.get("pattern_counts") or {}
    stances = fee_issue.get("stance_counts") or {}

    pairs = []
    for key, value in patterns.items():
        try:
            pairs.append((str(key), int(value)))
        except (TypeError, ValueError):
            continue
    if pairs:
        top_pattern, top_count = max(pairs, key=lambda pair: pair[1])
    else:
        top_pattern, top_count = "no clear pattern", 0

    confused = _int(stances.get("confused"))
    complaint_only = _int(stances.get("complaint_only"))
    return (
        f"{FEE_LINE_PREFIX}{top_pattern} "
        f"({_comma(top_count)} of {_comma(confused)} confused reviews); "
        f"a further {_comma(complaint_only)} reviews say charges are too high."
    )


def _short_theme_name(name):
    """"Customer Support & Trust" -> "Customer support" (and so on)."""
    head = str(name or "").split(" & ", 1)[0].strip()
    words = head.split()
    if not words:
        return head
    return " ".join([words[0]] + [word.lower() for word in words[1:]])


def _trend_label(direction):
    return {"up": "rising", "down": "falling"}.get(
        str(direction or "").strip().lower(), "flat"
    )


def _day_month(iso_date):
    """"2026-07-21" -> "21 Jul"."""
    try:
        parsed = date.fromisoformat(str(iso_date).strip())
    except ValueError:
        raise PublishError(f"bad date {iso_date!r} in the inputs")
    return f"{parsed.day} {parsed.strftime('%b')}"


def _day_month_year(iso_date):
    """"2026-10-14" -> "14 Oct 2026"."""
    try:
        parsed = date.fromisoformat(str(iso_date).strip())
    except ValueError:
        raise PublishError(f"bad date {iso_date!r} in the inputs")
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def _excerpt_plain(excerpt):
    """The quote excerpt without a leading/trailing ellipsis."""
    return str(excerpt or "").strip().strip("\u2026").strip()


def _short_excerpt(excerpt, limit=SHORT_EXCERPT_WORDS):
    """Keep only a quote's last `limit` words — never cut from the start.

    The result is an exact substring of the excerpt (no marks added).
    """
    plain = _excerpt_plain(excerpt)
    starts = [match.start() for match in re.finditer(r"\S+", plain)]
    if len(starts) <= limit:
        return plain
    return plain[starts[-limit]:]


def _html_escape(text):
    """HTML-escape a value for element text or attribute values."""
    return html.escape(str(text or ""), quote=True)


def _card_color(theme_name):
    """Left-border colour for an issue card, chosen by theme keyword."""
    name = str(theme_name or "").lower()
    if "support" in name:
        return "#2563eb"
    if "performance" in name or "speed" in name or "app" in name:
        return "#f59e0b"
    if "fee" in name or "charge" in name or "brokerage" in name:
        return "#dc2626"
    return "#d1d5db"


_TAG_RE = re.compile(r"<[^>]+>")


def _count_email_words(html_fragment):
    """Visible summary words of an HTML fragment, ignoring URL tokens."""
    text = html.unescape(_TAG_RE.sub(" ", str(html_fragment or "")))
    return sum(
        1
        for token in text.split()
        if "://" not in token and not token.startswith("www.")
    )


# ---------------------------------------------------------------------------
# PAYLOADS
# ---------------------------------------------------------------------------
def build_payloads():
    """Read the three inputs and build the payload parts.

    Refuses (PublishError) when any input file is missing/unreadable, when
    fee_explainer.json still has needs_human_review = true, or when
    pulse.json has no usable email_summary (re-run generate_pulse.py).

    Returns a dict with week_label, the display pieces, and the three hashed
    strings (doc_entry_body, email_subject, email_body_body).
    """
    pulse = _load_json(PULSE_JSON)
    fee = _load_json(FEE_JSON)
    insights = _load_json(INSIGHTS_JSON)

    if fee.get("needs_human_review"):
        raise PublishError(
            f"{_rel(FEE_JSON)} has needs_human_review = true — refusing to "
            "publish an unreviewed fee explanation. Fix the explainer (or "
            "re-run src/fee_explainer.py) first."
        )

    week_label = _week_label(insights)
    today = date.today().isoformat()

    pulse_markdown = str(pulse.get("weekly_pulse") or "").rstrip("\n")
    if not pulse_markdown:
        raise PublishError(f"{_rel(PULSE_JSON)} has no weekly_pulse text")

    top_themes = pulse.get("top_themes") or insights.get("top_themes") or []
    if len(top_themes) < 3:
        raise PublishError(f"{_rel(PULSE_JSON)} has fewer than 3 top_themes")

    # The Doc's WEEKLY PULSE section is rebuilt from the same approved
    # strings the pulse markdown was written from (words unchanged).
    theme_insights = [
        str(s).strip() for s in pulse.get("theme_insights") or []
    ]
    action_ideas = [str(s).strip() for s in pulse.get("action_ideas") or []]
    key_observation = str(pulse.get("key_observation") or "").strip()
    if len(theme_insights) < 3 or len(action_ideas) < 3 or not key_observation:
        raise PublishError(
            f"{_rel(PULSE_JSON)} has no usable theme_insights, "
            "key_observation or action_ideas — re-run src/generate_pulse.py"
        )

    # Fix 1: code-built from the stance / pattern counts, never
    # what_confuses_them.
    identified = _identified_fee_issue(insights)

    bullets = []
    for item in fee.get("explanation_bullets") or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            bullets.append(text)
    if not bullets:
        raise PublishError(f"{_rel(FEE_JSON)} has no explanation_bullets")

    link_lines = []
    link_items = []
    for item in fee.get("source_links") or []:
        title = str((item or {}).get("title") or "").strip()
        url = str((item or {}).get("url") or "").strip()
        if title and url:
            link_lines.append(f"{title} — {url}")
            link_items.append({"title": title, "url": url})

    last_checked = str(fee.get("last_checked") or today).strip()
    fee_title = _fee_title(fee)
    email_subject = (
        f"Weekly Product Pulse + Customer Clarification — {week_label}"
    )

    # The wording for the email's short lines comes from generate_pulse.py.
    email_summary = pulse.get("email_summary") or {}
    theme_lines = [
        str(s).strip() for s in email_summary.get("theme_lines") or []
    ]
    actions = [str(s).strip() for s in email_summary.get("actions") or []]
    key_point = str(email_summary.get("key_point") or "").strip()
    if len(theme_lines) < 3 or len(actions) < 3 or not key_point:
        raise PublishError(
            f"{_rel(PULSE_JSON)} has no usable email_summary (it needs 3 "
            "theme_lines, a key_point and 3 actions) — re-run "
            "src/generate_pulse.py"
        )

    # ---- Numbers from the data ----
    total_reviews = _comma(_int(insights.get("total_reviews")))
    reviews_span = (
        f"{_day_month(insights.get('period_start'))} – "
        f"{_day_month_year(insights.get('period_end'))}"
    )

    themes_by_name = {
        str(t.get("name") or ""): t for t in insights.get("themes") or []
    }
    theme_cards = []
    for i, name in enumerate(top_themes[:3], 1):
        theme = themes_by_name.get(str(name)) or {}
        trend = theme.get("trend") or {}
        theme_cards.append(
            {
                "n": i,
                "short": _short_theme_name(name),
                "full": str(name),
                "count": _int(theme.get("problem_count")),
                "trend": _trend_label(trend.get("direction")),
                "prev": _int(trend.get("previous_4_weeks")),
                "last": _int(trend.get("last_4_weeks")),
                "line": theme_lines[i - 1],
            }
        )

    # ---- Quotes: excerpt from pulse.json, rating/theme from insights.json ----
    pulse_quotes = (pulse.get("quotes") or [])[:3]
    rating_by_id = {}
    rating_by_text = {}
    theme_by_id = {}
    theme_by_text = {}
    for item in insights.get("quotes") or []:
        if item.get("review_id"):
            rating_by_id[str(item["review_id"])] = item.get("rating")
            theme_by_id[str(item["review_id"])] = item.get("theme")
        if item.get("text"):
            rating_by_text[str(item["text"])] = item.get("rating")
            theme_by_text[str(item["text"])] = item.get("theme")

    def rating_for(quote):
        review_id = str(quote.get("review_id") or "")
        if review_id and review_id in rating_by_id:
            return rating_by_id[review_id]
        return rating_by_text.get(str(quote.get("text") or ""))

    def theme_for(quote):
        review_id = str(quote.get("review_id") or "")
        if review_id and review_id in theme_by_id:
            return theme_by_id[review_id]
        return theme_by_text.get(str(quote.get("text") or ""))

    excerpts = [
        _excerpt_plain(q.get("excerpt") or q.get("text") or "")
        for q in pulse_quotes
    ]
    ratings = [rating_for(q) for q in pulse_quotes]

    try:
        last_checked_label = _day_month_year(last_checked)
    except PublishError:
        last_checked_label = last_checked

    # ---- The email body, as HTML with inline CSS ----
    subline = (
        f"Groww · week ending {_html_escape(week_label)} · "
        f"{total_reviews} Play Store reviews ({_html_escape(reviews_span)})"
    )

    def make_email(chosen_excerpts):
        """(summary HTML, full body HTML); the budget covers the summary."""
        issue_cards = []
        for card in theme_cards:
            border = _card_color(card["full"])
            issue_cards.append(
                '<div style="background:#f9fafb;'
                f"border-left:4px solid {border};"
                'padding:10px 14px;margin:8px 0;">'
                '<table role="presentation" width="100%" cellpadding="0" '
                'cellspacing="0" border="0"><tr>'
                f'<td style="font-weight:bold;">{card["n"]}. '
                f'{_html_escape(card["short"])}</td>'
                '<td align="right" style="color:#6b7280;">'
                f'{_comma(card["count"])} complaints · {card["trend"]} '
                f'({card["prev"]} → {card["last"]})</td>'
                "</tr>"
                f'<tr><td colspan="2" style="padding-top:4px;">'
                f'{_html_escape(card["line"])}</td></tr>'
                "</table></div>"
            )

        quote_blocks = []
        for plain, rating, quote in zip(
            chosen_excerpts, ratings, pulse_quotes
        ):
            attribution = "— "
            if rating is not None:
                attribution += f"{rating}★"
            theme_name = _short_theme_name(theme_for(quote) or "")
            if theme_name:
                attribution += f" · {_html_escape(theme_name)}"
            quote_blocks.append(
                '<div style="border-left:3px solid #9ca3af;'
                'padding-left:10px;margin:8px 0;font-style:italic;">'
                f"{_html_escape(plain)}"
                '<div style="font-style:normal;color:#6b7280;'
                f'font-size:12px;margin-top:4px;">{attribution}</div>'
                "</div>"
            )

        actions_html = "".join(
            f"<li>{_html_escape(action)}</li>" for action in actions[:3]
        )
        heading_style = (
            "font-size:16px;font-weight:bold;margin:16px 0 4px 0;"
        )
        summary = "".join(
            [
                f'<p style="{heading_style}">{ISSUES_HEADING}</p>',
                (
                    '<p style="color:#6b7280;font-size:13px;'
                    f'margin:0 0 4px 0;">{ISSUES_NOTE}</p>'
                ),
                *issue_cards,
                f'<p style="{heading_style}">{QUOTES_HEADING}</p>',
                *quote_blocks,
                f'<p style="{heading_style}">{KEY_POINT_HEADING}</p>',
                (
                    '<p style="margin:0 0 8px 0;">'
                    f"{_html_escape(key_point)}</p>"
                ),
                f'<p style="{heading_style}">{ACTIONS_HEADING}</p>',
                (
                    '<ol style="margin:0 0 8px 20px;padding:0;">'
                    f"{actions_html}</ol>"
                ),
            ]
        )

        bullets_html = "".join(
            f"<li>{_html_escape(text)}</li>" for text in bullets
        )
        if link_items:
            source_lis = "".join(
                (
                    f'<li><a href="{_html_escape(item["url"])}" '
                    f'style="color:#2563eb;">'
                    f'{_html_escape(item["title"])}</a></li>'
                )
                for item in link_items
            )
            sources_html = (
                f'<p style="margin:8px 0 2px 0;">{SOURCES_HEADING}</p>'
                f'<ul style="margin:0;padding-left:18px;">{source_lis}</ul>'
            )
        else:
            sources_html = (
                f'<p style="margin:8px 0 2px 0;">{SOURCES_HEADING}</p>'
            )
        snippet = "".join(
            [
                (
                    '<p style="font-size:16px;font-weight:bold;'
                    f'margin:16px 0 8px 0;">{SNIPPET_HEADING}</p>'
                ),
                (
                    '<div style="border:1px solid #e5e7eb;'
                    "border-radius:8px;padding:14px;background:#ffffff;"
                    'margin:0 0 8px 0;">'
                    '<p style="font-weight:bold;margin:0 0 8px 0;">'
                    f"{SNIPPET_QUESTION}</p>"
                ),
                (
                    '<ul style="margin:0 0 4px 0;padding-left:18px;">'
                    f"{bullets_html}</ul>"
                ),
                sources_html,
                (
                    '<p style="color:#6b7280;font-size:12px;'
                    'margin:8px 0 0 0;">'
                    f"Last checked: {_html_escape(last_checked_label)}"
                    "</p>"
                ),
                "</div>",
            ]
        )

        body = "".join(
            [
                (
                    '<div style="max-width:640px;margin:0 auto;'
                    "font-family:Arial, sans-serif;font-size:14px;"
                    'color:#1f2937;line-height:1.5;">'
                ),
                (
                    '<p style="font-size:20px;font-weight:bold;'
                    f'margin:0 0 4px 0;">{EMAIL_TITLE}</p>'
                ),
                f'<p style="color:#6b7280;font-size:13px;margin:0;">{subline}</p>',
                '<hr style="border:none;border-top:1px solid #e5e7eb;'
                'margin:12px 0 16px 0;">',
                f'<p style="margin:0 0 12px 0;">{GREETING}</p>',
                summary,
                snippet,
                "</div>",
            ]
        )
        # The budget covers the sub-line + the four summary sections only.
        return subline + summary, body

    summary_part, email_body_body = make_email(excerpts)
    if _count_email_words(summary_part) > EMAIL_WORD_BUDGET:
        excerpts = [_short_excerpt(excerpt) for excerpt in excerpts]
        summary_part, email_body_body = make_email(excerpts)
    email_words = _count_email_words(summary_part)
    print(f"Email summary words: {email_words} / {EMAIL_WORD_BUDGET}")
    if email_words > EMAIL_WORD_BUDGET:
        print(
            "Warning: summary still over budget with shortened quote "
            "excerpts."
        )

    # ---- The Doc entry: dividers, ▸ section markers, • bullets, 3-space ----
    top_theme_lines = [
        (
            f"   {card['n']}. {card['full']} — "
            f"{_comma(card['count'])} complaints · {card['trend']} "
            f"({card['prev']} → {card['last']})"
        )
        for card in theme_cards
    ]

    doc_quote_lines = []
    for quote in pulse_quotes:
        span = _excerpt_plain(quote.get("excerpt") or quote.get("text") or "")
        rating = rating_for(quote)
        theme_name = str(theme_for(quote) or "")
        parts = []
        if rating is not None:
            parts.append(f"{rating}★")
        if theme_name:
            parts.append(theme_name)
        doc_quote_lines.append(f'   • "{span}" — {", ".join(parts)}')

    pulse_section_lines = [
        (
            f"   Based on {total_reviews} Play Store reviews, "
            f"{str(insights.get('period_start') or '')} to "
            f"{str(insights.get('period_end') or '')}."
        ),
        "   Trend: 1–2★ reviews, previous 4 weeks → last 4 weeks.",
        "",
        "   Top themes",
        *(
            f"   • {name} — {insight}"
            for name, insight in zip(top_themes[:3], theme_insights)
        ),
        "   What users say",
        *doc_quote_lines,
        "   Key observation",
        f"   • {key_observation}",
        "   Action ideas",
        *(f"   {i}. {idea}" for i, idea in enumerate(action_ideas[:3], 1)),
    ]

    # Everything below the "===== end of entry =====" marker is the approval
    # footer, added at publish time, so the hash can be embedded safely.
    doc_entry_body = "\n".join(
        [
            "",
            "",
            DOC_DIVIDER,
            DOC_HEADER.format(week_label=week_label),
            DOC_DIVIDER,
            "",
            "▸ DATE",
            f"   {today}",
            "",
            "▸ TOP THEMES",
            *top_theme_lines,
            "",
            "▸ WEEKLY PULSE",
            *pulse_section_lines,
            "",
            "▸ IDENTIFIED FEE ISSUE",
            f"   {identified}",
            "",
            "▸ EXPLANATION BULLETS",
            *(f"   • {text}" for text in bullets),
            "",
            "▸ SOURCE LINKS",
            *(f"   • {line}" for line in link_lines),
            "",
            "▸ LAST CHECKED",
            f"   {last_checked}",
            "",
            "▸ APPROVAL",
            DOC_END,
        ]
    ) + "\n"

    return {
        "week_label": week_label,
        "date": today,
        "top_themes": [str(t) for t in top_themes],
        "weekly_pulse": "\n".join(pulse_section_lines),
        "identified_fee_issue": identified,
        "explanation_bullets": bullets,
        "source_links": link_items,
        "fee_title": fee_title,
        "last_checked": last_checked,
        "email_summary": {
            "theme_lines": theme_lines,
            "key_point": key_point,
            "actions": actions,
        },
        "doc_entry_body": doc_entry_body,
        "email_subject": email_subject,
        "email_body_body": email_body_body,
    }


def compute_hash(payloads):
    """SHA-256 over the canonical JSON of the three non-footer strings."""
    missing = [key for key in HASH_KEYS if key not in payloads]
    if missing:
        raise PublishError("payloads missing " + ", ".join(missing))
    canonical = json.dumps(
        {key: payloads[key] for key in HASH_KEYS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_doc_entry(payloads, approval_id, approved_at, payload_sha256):
    """Full Doc entry: the hashed body plus the (unhashed) approval line.

    The approval line keeps its exact format (read-back searches for the
    approval_id); only its 3-space indent follows the ▸ APPROVAL layout.
    """
    approval_line = (
        f"approval_id: {approval_id}   approved_at: {approved_at}   "
        f"payload_sha256: {payload_sha256}"
    )
    body = payloads["doc_entry_body"]
    if DOC_END in body:
        head, marker, tail = body.partition(DOC_END)
        return f"{head}   {approval_line}\n{marker}{tail}"
    return body.rstrip("\n") + "\n   " + approval_line + "\n" + DOC_END + "\n"


def render_email_body(payloads, approval_id, payload_sha256):
    """Full email HTML: the hashed body plus the (unhashed) footer line."""
    ref = (
        f"Ref: approval {approval_id[:8]} · check {payload_sha256[:12]} · "
        "Draft created after human approval. Not sent automatically."
    )
    footer = (
        '<p style="color:#9ca3af;font-size:12px;margin:16px 0 0 0;">'
        f"{_html_escape(ref)}</p>"
    )
    return payloads["email_body_body"].rstrip() + "\n" + footer + "\n"


# ---------------------------------------------------------------------------
# APPROVAL RECORD
# ---------------------------------------------------------------------------
def _new_steps():
    return {
        "duplicate_check": {
            "done": False, "already_logged": None, "new_entry": False,
            "error": None,
        },
        "hash_check": {"done": False, "recomputed": None},
        "doc_append": {
            "done": False, "ok": None, "skipped": False, "result": None,
            "error": None,
        },
        "read_back": {"done": False, "verified": None, "error": None},
        "email_draft": {
            "done": False, "ok": None, "draft_id": None, "response": None,
            "error": None,
        },
    }


def _merge_steps(steps):
    """Fill any missing step/field so old records still resume cleanly."""
    for name, default in _new_steps().items():
        current = steps.get(name)
        if not isinstance(current, dict):
            steps[name] = dict(default)
        else:
            for key, value in default.items():
                current.setdefault(key, value)
    return steps


def _approval_path(approval_id):
    return APPROVALS_DIR / f"{approval_id}.json"


def _save_approval(approval):
    path = _approval_path(approval["approval_id"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(approval, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise PublishError(
            f"cannot save approval record {_rel(path)}: {_clean(str(exc))}"
        )


def _load_approval(approval_id):
    approval_id = str(approval_id or "").strip()
    if not _APPROVAL_ID_RE.fullmatch(approval_id):
        raise PublishError(f"invalid approval id: {approval_id!r}")
    path = _approval_path(approval_id)
    if not path.exists():
        raise PublishError(
            f"no approval record at {_rel(path)} — nothing to resume"
        )
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"cannot read {_rel(path)}: {_clean(str(exc))}")
    required = (
        "approval_id", "approved_at", "payload_sha256", "week_label",
        "payloads",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise PublishError(
            f"{_rel(path)} is missing " + ", ".join(missing)
        )
    return record


def request_approval_cli(payloads, payload_sha256=None, input_fn=input):
    """Save the email preview, print the Doc entry and subject, ask for APPROVE.

    The final HTML goes to outputs/email_preview.html (the terminal never
    prints raw HTML). Only the exact word APPROVE proceeds; there is no --yes
    flag. On approve the record is saved to outputs/approvals/<id>.json and
    returned; on cancel nothing is saved and None is returned.
    """
    if payload_sha256 is None:
        payload_sha256 = compute_hash(payloads)
    approval_id = str(uuid.uuid4())
    approved_at = _now_iso()

    # The final email (with this approval's footer) as a browsable file;
    # the terminal only shows the Doc entry and the subject, never raw HTML.
    try:
        EMAIL_PREVIEW.parent.mkdir(parents=True, exist_ok=True)
        with open(EMAIL_PREVIEW, "w", encoding="utf-8") as fh:
            fh.write(render_email_body(payloads, approval_id, payload_sha256))
    except OSError as exc:
        print(
            f"Warning: could not write {_rel(EMAIL_PREVIEW)}: "
            f"{_clean(str(exc))}"
        )
    else:
        print(
            "Open outputs/email_preview.html in your browser to review "
            "the email before approving."
        )

    _banner("DOC ENTRY TO PUBLISH")
    print(render_doc_entry(payloads, approval_id, approved_at, payload_sha256))
    _banner("EMAIL SUBJECT")
    print(payloads["email_subject"])
    _banner(f"payload_sha256: {payload_sha256}")

    answer = input_fn("Type APPROVE to publish, anything else to cancel: ")
    if (answer or "").strip() != "APPROVE":
        print("Cancelled — nothing was published.")
        return None

    approval = {
        "approval_id": approval_id,
        "approved_at": approved_at,
        "payload_sha256": payload_sha256,
        "week_label": payloads["week_label"],
        "payloads": payloads,
        "steps": _new_steps(),
        "status": "approved",
    }
    _save_approval(approval)
    print(f"Approved. Saved to {_rel(_approval_path(approval_id))}.")
    return approval


# ---------------------------------------------------------------------------
# MCP CONNECTION (same approach as src/mcp_check.py)
# ---------------------------------------------------------------------------
class MCPConnection:
    """One live n8n MCP session; tool failures become PublishError."""

    def __init__(self, session, statuses, transport):
        self.session = session
        self.statuses = statuses
        self.transport = transport

    async def call(self, name, arguments=None):
        try:
            result = await self.session.call_tool(
                name, arguments if arguments is not None else {}
            )
        except Exception as exc:
            _log_traceback(f"publish tool {name}")
            inner = _unwrap_group(exc)
            raise PublishError(
                f"{name} failed: {_friendly_reason(inner, self.statuses)}"
            ) from None
        if getattr(result, "is_error", False):
            # The tool itself reported the error text (no traceback exists).
            raise PublishError(f"{name} failed: {_clean(_result_text(result))}")
        return result

    async def text(self, name, arguments=None):
        return _result_text(await self.call(name, arguments))


@asynccontextmanager
async def _streamable_streams(url, headers, statuses):
    http_client = create_mcp_http_client(
        headers=headers,
        timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )
    http_client.event_hooks = {"response": [_record_status(statuses)]}
    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            yield streams


@asynccontextmanager
async def _sse_streams(url, headers, statuses):
    def factory(headers=None, auth=None, timeout=None):
        client = create_mcp_http_client(headers=headers, auth=auth, timeout=timeout)
        client.event_hooks = {"response": [_record_status(statuses)]}
        return client

    async with sse_client(
        url,
        headers=headers,
        timeout=CONNECT_TIMEOUT,
        sse_read_timeout=READ_TIMEOUT,
        httpx_client_factory=factory,
    ) as streams:
        yield streams


@asynccontextmanager
async def _connect(url, token):
    """Streamable HTTP first, SSE as fallback; yields an MCPConnection.

    Network failures are retried with waits of 5s, 10s then 20s.
    """
    headers = {"Authorization": f"Bearer {token}"}
    transports = (
        ("Streamable HTTP", _streamable_streams),
        ("SSE", _sse_streams),
    )
    failures = []
    attempts = 1 + len(RETRY_DELAYS)
    async with AsyncExitStack() as stack:
        for transport_name, opener in transports:
            for attempt_no in range(attempts):
                if attempt_no:
                    delay = RETRY_DELAYS[attempt_no - 1]
                    print(
                        f"Retrying {transport_name} in {delay:.0f}s "
                        f"(attempt {attempt_no + 1} of {attempts})..."
                    )
                    await asyncio.sleep(delay)
                statuses = []
                attempt_stack = AsyncExitStack()
                try:
                    streams = await attempt_stack.enter_async_context(
                        opener(url, headers, statuses)
                    )
                    session = await attempt_stack.enter_async_context(
                        ClientSession(
                            streams[0], streams[1],
                            read_timeout_seconds=READ_TIMEOUT,
                        )
                    )
                    await session.initialize()
                except Exception as exc:
                    _log_traceback(f"publish connect via {transport_name}")
                    await attempt_stack.aclose()
                    reason = _friendly_reason(exc, statuses)
                    failures.append(reason)
                    if (
                        not _is_connection_error(exc)
                        or attempt_no >= len(RETRY_DELAYS)
                    ):
                        break  # not a network blip / retries exhausted
                    continue
                await stack.enter_async_context(attempt_stack)
                yield MCPConnection(session, statuses, transport_name)
                return
            if failures and failures[-1].startswith("Cannot reach"):
                break  # the server is unreachable either way
    reason = min(failures, key=_reason_rank) if failures else "could not connect"
    raise PublishError(f"cannot connect to the n8n MCP server: {reason}")


def _result_text(result):
    """All text blocks of a tool result (structured JSON as a fallback)."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""


def _find_id(value):
    """First non-empty id-ish scalar in a structured tool result."""
    if isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        for key in DRAFT_ID_KEYS:
            item = lowered.get(key)
            if isinstance(item, (str, int)) and str(item).strip():
                return str(item).strip()
        for item in value.values():
            found = _find_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_id(item)
            if found:
                return found
    return None


def _draft_id(result):
    """Best-effort draft/message id out of a create_email_draft result."""
    found = _find_id(getattr(result, "structured_content", None))
    if found:
        return found
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            found = _find_id(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            found = None
        if found:
            return found
        match = DRAFT_ID_RE.search(text)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# PUBLISH
# ---------------------------------------------------------------------------
def _run_async(coro):
    """asyncio.run, but safe when the caller (e.g. a UI) has a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    holder = {}

    def runner():
        try:
            holder["value"] = asyncio.run(coro)
        except BaseException as exc:
            holder["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in holder:
        raise holder["error"]
    return holder["value"]


def _require_env():
    global TOKEN
    load_dotenv(ROOT / ".env")
    url = (os.getenv("N8N_MCP_URL") or "").strip()
    token = (os.getenv("N8N_MCP_TOKEN") or "").strip()
    missing = [
        name
        for name, value in (("N8N_MCP_URL", url), ("N8N_MCP_TOKEN", token))
        if not value
    ]
    if missing:
        raise PublishError("missing " + ", ".join(missing) + " in .env")
    TOKEN = token
    mcp_check.TOKEN = token  # so the shared scrubber knows the secret too
    return url, token


def _ok_flag(step):
    if not step:
        return "-"
    if step.get("ok") is False:
        return "FAILED"
    if step.get("done") is True:
        return "OK"
    return "-"


def _summary(approval, status, detail=None):
    record = approval or {}
    steps = record.get("steps") or {}
    return {
        "status": status,
        "detail": detail,
        "approval_id": record.get("approval_id"),
        "payload_sha256": record.get("payload_sha256"),
        "week_label": record.get("week_label"),
        "doc_append": _ok_flag(steps.get("doc_append")),
        "read_back_verified": bool((steps.get("read_back") or {}).get("verified")),
        "email_draft": _ok_flag(steps.get("email_draft")),
        "draft_id": (steps.get("email_draft") or {}).get("draft_id"),
    }


def format_summary(summary):
    """The one-line final summary printed after every run."""
    digest = (summary.get("payload_sha256") or "-")[:12]
    verified = "YES" if summary.get("read_back_verified") else "NO"
    draft_id = summary.get("draft_id") or "-"
    return (
        f"Doc append: {summary.get('doc_append') or '-'} · "
        f"Read-back verified: {verified} · "
        f"Gmail draft: {summary.get('email_draft') or '-'} ({draft_id}) · "
        f"approval_id: {summary.get('approval_id') or '-'} · "
        f"hash: {digest}"
    )


def _blocked(approval, message):
    approval["status"] = "blocked"
    approval["blocked_reason"] = message
    _save_approval(approval)
    print(message)
    if message == "This week is already logged":
        print(
            "Re-run with --new-entry to log it again "
            "(typing APPROVE is still required)."
        )
    return _summary(approval, "blocked", message)


def _note_failure(approval, message):
    approval["status"] = "failed"
    approval["failed_reason"] = message
    try:
        _save_approval(approval)
    except PublishError:
        pass


def _append_log(summary):
    """One JSON line per publish run (a log failure never fails the run)."""
    try:
        PUBLISH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(PUBLISH_LOG, "a", encoding="utf-8") as fh:
            line = {"run_at": _now_iso(), **summary}
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Warning: could not write {_rel(PUBLISH_LOG)}: {_clean(str(exc))}")


async def _publish_steps(conn, approval, new_entry):
    """The ordered steps 1-6; the record is saved after each one."""
    payloads = approval["payloads"]
    approval_id = approval["approval_id"]
    week_needle = f"week ending {approval['week_label']}".lower()
    steps = _merge_steps(approval.get("steps") or {})
    approval["steps"] = steps

    # 1) duplicate check --------------------------------------------------
    doc_text = None
    if not steps["duplicate_check"]["done"]:
        print(f"Checking the Doc for an existing {approval['week_label']} entry...")
        try:
            doc_text = await conn.text("read_doc", {})
        except PublishError as exc:
            # Stays not-done, so --resume retries this step.
            steps["duplicate_check"]["error"] = _clean(str(exc))
            _save_approval(approval)
            raise
        already = week_needle in doc_text.lower()
        steps["duplicate_check"].update(
            done=True, already_logged=already, new_entry=new_entry
        )
        _save_approval(approval)
        if already and not new_entry:
            return _blocked(approval, "This week is already logged")
    elif steps["duplicate_check"].get("already_logged") and not new_entry:
        return _blocked(approval, "This week is already logged")

    # 2) payload hash -----------------------------------------------------
    if not steps["hash_check"]["done"]:
        recomputed = compute_hash(payloads)
        if recomputed != approval["payload_sha256"]:
            steps["hash_check"]["recomputed"] = recomputed
            return _blocked(approval, "Payload changed after approval — blocked")
        steps["hash_check"].update(done=True, recomputed=recomputed)
        _save_approval(approval)
        print("Payload hash matches the approval.")

    # 3) append -----------------------------------------------------------
    if not steps["doc_append"]["done"]:
        if doc_text is None:
            try:
                doc_text = await conn.text("read_doc", {})
            except PublishError as exc:
                steps["doc_append"]["error"] = _clean(str(exc))
                _save_approval(approval)
                raise
        if approval_id in doc_text:
            # Resume safety: never append the same approval twice.
            steps["doc_append"].update(
                done=True, ok=True, skipped=True,
                result="already present in the Doc",
            )
            _save_approval(approval)
            print("This approval_id is already in the Doc — skipping the append.")
        else:
            entry = render_doc_entry(
                payloads, approval_id,
                approval["approved_at"], approval["payload_sha256"],
            )
            print("Appending the entry to the Doc...")
            try:
                result = await conn.call("append_to_doc", {"entry": entry})
            except PublishError as exc:
                # Stays not-done (ok=False), so --resume retries the append.
                steps["doc_append"].update(
                    done=False, ok=False, result=_clean(str(exc)),
                    error=_clean(str(exc)),
                )
                _save_approval(approval)
                raise
            steps["doc_append"].update(
                done=True, ok=True, result=_short(_result_text(result))
            )
            _save_approval(approval)

    # 4) read back --------------------------------------------------------
    read_back = steps["read_back"]
    if not (read_back["done"] and read_back.get("verified") is True):
        print("Reading the Doc back to confirm the approval_id...")
        try:
            text = await conn.text("read_doc", {})
        except PublishError as exc:
            # Stays not-done, so --resume retries the read-back.
            read_back["error"] = _clean(str(exc))
            _save_approval(approval)
            raise
        read_back.update(done=True, verified=approval_id in text)
        _save_approval(approval)
        if not read_back["verified"]:
            print("Warning: approval_id not found in the Doc on read-back.")

    # 5) email draft ------------------------------------------------------
    draft = steps["email_draft"]
    if not draft["done"]:
        recomputed = compute_hash(payloads)
        if recomputed != approval["payload_sha256"]:
            return _blocked(approval, "Payload changed after approval — blocked")
        body = render_email_body(
            payloads, approval_id, approval["payload_sha256"]
        )
        print("Creating the Gmail draft...")
        try:
            result = await conn.call(
                "create_email_draft",
                {"subject": payloads["email_subject"], "body": body},
            )
        except PublishError as exc:
            # Stays not-done (ok=False), so --resume retries the draft.
            draft.update(
                done=False, ok=False, response=_clean(str(exc)),
                error=_clean(str(exc)),
            )
            _save_approval(approval)
            raise
        draft.update(
            done=True,
            ok=True,
            draft_id=_draft_id(result),
            response=_short(_result_text(result)),
        )
        _save_approval(approval)

    # 6) the record after each step already includes the final state ------
    approval["status"] = "published"
    _save_approval(approval)
    return _summary(approval, "published")


async def _run(url, token, approval, new_entry):
    async with _connect(url, token) as conn:
        print(f"Connected to n8n MCP via {conn.transport}.")
        return await _publish_steps(conn, approval, new_entry)


def publish(approval, new_entry=False):
    """Run the ordered MCP steps for an approved payload.

    `approval` is an approval_id (loads outputs/approvals/<id>.json) or the
    record dict itself — when the record is already on disk, the saved copy
    wins, so the published content is always the content that was approved.

    Returns the summary dict (see format_summary); prints friendly errors and
    never raises a traceback at the operator.
    """
    record = None
    try:
        if isinstance(approval, str):
            record = _load_approval(approval)
        else:
            record = approval
            approval_id = str(record.get("approval_id") or "")
            if approval_id and _approval_path(approval_id).exists():
                record = _load_approval(approval_id)
        required = (
            "approval_id", "approved_at", "payload_sha256", "week_label",
            "payloads",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise PublishError("approval record is missing " + ", ".join(missing))
        url, token = _require_env()
        summary = _run_async(_run(url, token, record, new_entry))
    except PublishError as exc:
        _log_traceback("publish (PublishError)")
        message = _clean(str(exc))
        if record is not None:
            _note_failure(record, message)
        print(f"Publish failed: {message}")
        summary = _summary(record, "failed", message)
    except Exception as exc:  # never crash with a raw traceback
        _log_traceback("publish (unexpected)")
        inner = _unwrap_group(exc)
        message = f"unexpected {type(inner).__name__}: {_clean(str(inner))}"
        if record is not None:
            _note_failure(record, message)
        print(f"Publish failed: {message}")
        summary = _summary(record, "failed", message)
    _append_log(summary)
    return summary


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Approval-gated publish of the weekly pulse + fee clarification "
            "to the shared Doc and a Gmail draft via the n8n MCP server."
        ),
    )
    parser.add_argument(
        "--new-entry",
        action="store_true",
        help=(
            "append even when this week is already logged "
            "(typing APPROVE is still required)"
        ),
    )
    parser.add_argument(
        "--resume",
        metavar="APPROVAL_ID",
        help="continue a saved approval from its unfinished steps",
    )
    args = parser.parse_args(argv)

    try:
        if args.resume:
            print(f"Resuming approval {args.resume}...")
            summary = publish(args.resume, new_entry=args.new_entry)
        else:
            try:
                payloads = build_payloads()
            except PublishError as exc:
                print(f"Publish refused: {_clean(str(exc))}")
                return 1
            payload_sha256 = compute_hash(payloads)
            approval = request_approval_cli(payloads, payload_sha256)
            if approval is None:
                return 1
            summary = publish(approval, new_entry=args.new_entry)
    except PublishError as exc:
        print(f"Publish failed: {_clean(str(exc))}")
        return 1
    except Exception as exc:
        _log_traceback("main (unexpected)")
        inner = _unwrap_group(exc)
        print(
            f"Publish failed: unexpected {type(inner).__name__}: "
            f"{_clean(str(inner))}"
        )
        return 1

    print()
    print(format_summary(summary))
    return 0 if summary.get("status") == "published" else 1


if __name__ == "__main__":
    sys.exit(main())
