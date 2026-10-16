"""Automatic evaluation of every project output against the project brief.

Runs a fixed list of offline checks over data/, outputs/, knowledge/ and src/.
No LLM calls and no MCP calls are ever made. Every check is wrapped in its own
error handling: an unexpected exception becomes a FAIL for that single check,
never a crash.

Each check prints one line - PASS/FAIL, the check name and a short detail -
and the run ends with "X/Y checks passed". Exit code 0 when every check
passed, 1 otherwise. The same table, with a timestamp, is written to
outputs/eval_report.md.

Usage:
  python src/evals.py
"""

import ast
import json
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DATA_CSV = ROOT / "data" / "reviews.csv"
INSIGHTS_JSON = ROOT / "outputs" / "insights.json"
THEMES_CSV = ROOT / "outputs" / "review_themes.csv"
PULSE_JSON = ROOT / "outputs" / "pulse.json"
PULSE_MD = ROOT / "outputs" / "weekly_pulse.md"
FEE_JSON = ROOT / "outputs" / "fee_explainer.json"
APPROVALS_DIR = ROOT / "outputs" / "approvals"
PUBLISH_LOG = ROOT / "outputs" / "publish_log.jsonl"
EMAIL_PREVIEW = ROOT / "outputs" / "email_preview.html"
KNOW_DIR = ROOT / "knowledge"
MCP_CHECK_PY = SRC / "mcp_check.py"
REPORT_MD = ROOT / "outputs" / "eval_report.md"

# Same required columns src/validate_csv.py enforces.
REQUIRED_COLUMNS = ("text", "date", "rating")
# Column names that would leak personal data (same idea as validate_csv).
PRIVATE_KEYWORDS = ("name", "user", "email")

SPAN_MIN_WEEKS = 8          # data must cover 8-12 weeks
SPAN_MAX_WEEKS = 12
MIN_THEMES, MAX_THEMES = 1, 5
TOP_THEMES = 3
MAX_PULSE_WORDS = 250
PULSE_SECTIONS = ("Top themes", "What users say", "Key observation",
                  "Action ideas")
ACTION_IDEAS = 3
MAX_EMAIL_LINE_WORDS = 12
MAX_FEE_BULLETS = 6
OFFICIAL_DOMAINS = ("groww.in", "915.groww.in", "nseindia.com")
SUBJECT_PREFIX = "Weekly Product Pulse + Customer Clarification \u2014"
EMAIL_FOOTER = "Not sent automatically"
SEND_IN_TOOL_NAME = "send"

# Words a customer-facing fee explanation may never use (fee_explainer.py).
FEE_BANNED_WORDS = (
    "cheap", "expensive", "best", "worst", "great", "unfortunately",
    "amazing", "hidden", "scam", "unfair",
)

# Copy of generate_pulse.EMAIL_SUMMARY_BANNED, used only when that module
# cannot be imported.
EMAIL_BANNED_FALLBACK = (
    "leverage", "holistic", "seamless", "robust", "synergy", "eroding",
    "undermine", "utilize", "streamline", "enhance", "optimize",
    "ecosystem", "stakeholders",
)

# Markdown symbols stripped before counting pulse words (generate_pulse.py).
MD_SYMBOLS = re.compile(r"[#*>\[\]`\"'\u201c\u201d\u2014\u2013\u2026\u2192\u2605()]")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
SOURCE_ID_RE = re.compile(r"^(S\d+)", re.IGNORECASE)

# Eight stable weeks for the synthetic edge-case CSVs.
SYNTHETIC_WEEKS = (
    "2026-07-20", "2026-07-27", "2026-08-03", "2026-08-10",
    "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07",
)
SYNTHETIC_TEXT = "this is a perfectly valid review text number {i} for testing"


class CheckError(Exception):
    """A missing or unreadable input; reported as a FAIL, never raised."""


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------
def _rel(path):
    try:
        return Path(path).relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _norm(column):
    """Canonical column key: lower-case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(column).lower())


def _short(text, limit=160):
    """One safe single-line detail string for the terminal and the report."""
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "\u2026"


def _pandas():
    try:
        import pandas as pd
    except Exception as exc:      # noqa: BLE001 - reported, never raised
        raise CheckError(f"pandas is unavailable: {exc}") from None
    return pd


def _validate_csv_module():
    try:
        import validate_csv
    except Exception as exc:      # noqa: BLE001 - reported, never raised
        raise CheckError(f"cannot import src/validate_csv.py: {exc}") from None
    return validate_csv


_PULSE = {"module": None, "tried": False}


def _pulse_module():
    """generate_pulse.py for the shared word/banned-word rules, if importable."""
    if not _PULSE["tried"]:
        _PULSE["tried"] = True
        try:
            import generate_pulse
            _PULSE["module"] = generate_pulse
        except Exception:         # noqa: BLE001 - a local copy is used instead
            _PULSE["module"] = None
    return _PULSE["module"]


_PUBLISH = {"module": None, "tried": False}


def _publish_module(required=False):
    """publish.py for compute_hash / render_email_body, if importable."""
    if not _PUBLISH["tried"]:
        _PUBLISH["tried"] = True
        try:
            import publish
            _PUBLISH["module"] = publish
        except Exception as exc:  # noqa: BLE001
            _PUBLISH["error"] = f"{type(exc).__name__}: {exc}"
        else:
            _PUBLISH["error"] = None
    if _PUBLISH["module"] is None and required:
        raise CheckError(f"cannot import src/publish.py: {_PUBLISH.get('error')}")
    return _PUBLISH["module"]


def _text(path):
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CheckError(f"missing {_rel(path)}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise CheckError(f"cannot read {_rel(path)}: {exc}") from None


def _json(path):
    path = Path(path)
    raw = _text(path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckError(f"{_rel(path)} is not valid JSON: {exc}") from None


_REVIEWS = {"frame": None, "error": None}


def _reviews():
    """data/reviews.csv as a DataFrame, read once for every check that needs it."""
    if _REVIEWS["frame"] is None and _REVIEWS["error"] is None:
        try:
            if not DATA_CSV.is_file():
                raise CheckError(f"missing {_rel(DATA_CSV)}")
            frame = _pandas().read_csv(DATA_CSV, dtype=str)
        except CheckError as exc:
            _REVIEWS["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            _REVIEWS["error"] = f"cannot read {_rel(DATA_CSV)}: {exc}"
        else:
            _REVIEWS["frame"] = frame
    if _REVIEWS["frame"] is None:
        raise CheckError(_REVIEWS["error"] or f"cannot read {_rel(DATA_CSV)}")
    return _REVIEWS["frame"]


def _review_index():
    """{review_id: text} from data/reviews.csv."""
    frame = _reviews()
    columns = {_norm(col): col for col in frame.columns}
    if "reviewid" not in columns or "text" not in columns:
        raise CheckError("data/reviews.csv needs review_id and text columns")
    ids, texts = columns["reviewid"], columns["text"]
    return {str(i): str(t) for i, t in zip(frame[ids], frame[texts])}


def _latest_approval():
    """The newest approval record (by approved_at, then file mtime)."""
    if not APPROVALS_DIR.is_dir():
        raise CheckError(f"missing {_rel(APPROVALS_DIR)}")
    best = None
    for path in sorted(APPROVALS_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        key = (str(record.get("approved_at") or ""), path.stat().st_mtime_ns)
        if best is None or key > best[0]:
            best = (key, record, path)
    if best is None:
        raise CheckError(f"no readable approval record in {_rel(APPROVALS_DIR)}")
    return best[1], best[2]


def _knowledge_sources():
    """{source_id: body} for every knowledge/S*.txt file."""
    sources = {}
    for path in sorted(KNOW_DIR.glob("S*.txt")):
        if not re.fullmatch(r"S\d+\.txt", path.name):
            continue
        raw = _text(path)
        # The id/title/url header block ends at the first blank line.
        body = raw.split("\n\n", 1)[1] if "\n\n" in raw else raw
        sources[path.stem.upper()] = body
    if not sources:
        raise CheckError(f"no knowledge/S*.txt files in {_rel(KNOW_DIR)}")
    return sources


def _source_id(citation):
    match = SOURCE_ID_RE.match(str(citation).strip())
    return match.group(1).upper() if match else ""


def _numbers_in(text):
    """Numbers that must be backed by a source (single digits are ignored)."""
    return [n for n in NUM_RE.findall(str(text)) if len(n) > 1 or "." in n]


def count_words(markdown):
    """Same word-count rule as src/generate_pulse.py."""
    module = _pulse_module()
    if module is not None:
        return int(module.count_words(markdown))
    cleaned = MD_SYMBOLS.sub(" ", str(markdown))
    return len([w for w in cleaned.split() if w])


def _email_banned_words():
    module = _pulse_module()
    banned = getattr(module, "EMAIL_SUMMARY_BANNED", None) if module else None
    return tuple(banned) if banned else EMAIL_BANNED_FALLBACK


def _banned_hits(text, words):
    lowered = str(text).lower()
    return [w for w in words if re.search(rf"\b{re.escape(w)}\b", lowered)]


@contextmanager
def _temp_folder():
    folder = Path(tempfile.mkdtemp(prefix="evals-"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _validate_file(path):
    """(clean_df or None, errors, warnings) - validate_csv never raises."""
    return _validate_csv_module().validate_reviews_csv(str(path))


def _synthetic_rows(count, weeks=SYNTHETIC_WEEKS, start=0):
    return [
        {
            "date": weeks[i % len(weeks)],
            "rating": (i % 5) + 1,
            "text": SYNTHETIC_TEXT.format(i=start + i),
        }
        for i in range(count)
    ]


def _write_frame(folder, name, rows, columns=None):
    pd = _pandas()
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[columns]
    path = Path(folder) / name
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def _without_folder(errors, folder):
    return [str(e).replace(str(folder), "") for e in errors]


# ---------------------------------------------------------------------------
# DATA CHECKS
# ---------------------------------------------------------------------------
def check_data_file_exists():
    if not DATA_CSV.is_file():
        return False, f"missing {_rel(DATA_CSV)}"
    if DATA_CSV.stat().st_size == 0:
        return False, f"{_rel(DATA_CSV)} is empty (0 bytes)"
    frame = _reviews()
    return True, f"{_rel(DATA_CSV)}: {len(frame)} rows, {len(frame.columns)} columns"


def check_data_required_columns():
    columns = {_norm(col) for col in _reviews().columns}
    missing = [c for c in REQUIRED_COLUMNS if _norm(c) not in columns]
    if missing:
        return False, "missing column(s): " + ", ".join(missing)
    return True, "required columns present: " + ", ".join(REQUIRED_COLUMNS)


def check_data_no_private_columns():
    columns = list(_reviews().columns)
    offenders = []
    for col in columns:
        lowered = str(col).lower()
        norm = _norm(col)
        if any(word in lowered or word in norm for word in PRIVATE_KEYWORDS):
            offenders.append(str(col))
    if offenders:
        return False, "personal-data column(s): " + ", ".join(offenders)
    return True, f"{len(columns)} columns, no name/user/email column"


def check_data_date_span():
    pd = _pandas()
    frame = _reviews()
    columns = {_norm(col): col for col in frame.columns}
    if "date" not in columns:
        return False, "no date column"
    parsed = pd.to_datetime(frame[columns["date"]], errors="coerce").dropna()
    if parsed.empty:
        return False, "no parseable dates"
    first, last = parsed.min(), parsed.max()
    weeks = (last - first).days / 7.0
    detail = f"{first.date()} to {last.date()} = {weeks:.1f} weeks"
    if not (SPAN_MIN_WEEKS <= weeks <= SPAN_MAX_WEEKS):
        return False, f"{detail} (need {SPAN_MIN_WEEKS}-{SPAN_MAX_WEEKS} weeks)"
    return True, detail


# ---------------------------------------------------------------------------
# INSIGHTS CHECKS
# ---------------------------------------------------------------------------
def check_insights_theme_counts():
    insights = _json(INSIGHTS_JSON)
    themes = insights.get("themes") or []
    top = insights.get("top_themes") or []
    themes = themes if isinstance(themes, list) else []
    top = top if isinstance(top, list) else []
    problems = []
    if not (MIN_THEMES <= len(themes) <= MAX_THEMES):
        problems.append(f"{len(themes)} themes (need {MIN_THEMES}-{MAX_THEMES})")
    if len(top) != TOP_THEMES:
        problems.append(f"{len(top)} top_themes (need exactly {TOP_THEMES})")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(themes)} themes, exactly {len(top)} top_themes"


def check_insights_problem_counts():
    pd = _pandas()
    insights = _json(INSIGHTS_JSON)
    themes = insights.get("themes") or []
    if not themes:
        return False, "insights.json has no themes"
    frame = pd.read_csv(THEMES_CSV)
    columns = {_norm(col): col for col in frame.columns}
    if "theme" not in columns or "rating" not in columns:
        return False, f"{_rel(THEMES_CSV)} needs theme and rating columns"
    rating = pd.to_numeric(frame[columns["rating"]], errors="coerce")
    theme_col = frame[columns["theme"]].astype(str)
    recomputed = theme_col[rating <= 2].value_counts().to_dict()
    mismatched = []
    for theme in themes:
        name = str(theme.get("name") or "")
        try:
            stated = int(theme.get("problem_count"))
        except (TypeError, ValueError):
            mismatched.append(f"{name}: unusable problem_count")
            continue
        found = int(recomputed.get(name, 0))
        if stated != found:
            mismatched.append(f"{name} {stated}!={found}")
    if mismatched:
        return False, "; ".join(mismatched[:4])
    return True, f"{len(themes)} problem_count values match {_rel(THEMES_CSV)}"


def check_insights_quotes_exact():
    insights = _json(INSIGHTS_JSON)
    quotes = insights.get("quotes") or []
    if len(quotes) != TOP_THEMES:
        return False, f"{len(quotes)} quotes (need exactly {TOP_THEMES})"
    index = _review_index()
    for i, quote in enumerate(quotes, 1):
        review_id = str(quote.get("review_id") or "")
        text = str(quote.get("text") or "")
        row = index.get(review_id)
        if row is None:
            return False, f"quote {i}: review_id {review_id[:8]} not in data/reviews.csv"
        if text not in row:
            return False, f"quote {i}: text is not in review {review_id[:8]}"
    return True, f"{len(quotes)} quotes found verbatim in data/reviews.csv"


def check_insights_fee_issue():
    insights = _json(INSIGHTS_JSON)
    fee = insights.get("fee_issue")
    if not isinstance(fee, dict) or not fee:
        return False, "fee_issue missing or empty"
    problems = []
    for key in ("stance_counts", "pattern_counts"):
        value = fee.get(key)
        if not isinstance(value, dict) or not value:
            problems.append(f"{key} missing or empty")
    if problems:
        return False, "; ".join(problems)
    return True, (
        f"stance_counts {len(fee['stance_counts'])} keys, "
        f"pattern_counts {len(fee['pattern_counts'])} keys"
    )


# ---------------------------------------------------------------------------
# PULSE CHECKS
# ---------------------------------------------------------------------------
def check_pulse_word_count():
    words = count_words(_text(PULSE_MD))
    if words > MAX_PULSE_WORDS:
        return False, f"{words} words (limit {MAX_PULSE_WORDS})"
    return True, f"{words}/{MAX_PULSE_WORDS} words"


def check_pulse_sections():
    lines = [line.lstrip("#").strip().lower() for line in _text(PULSE_MD).splitlines()]
    missing = [s for s in PULSE_SECTIONS if s.lower() not in lines]
    if missing:
        return False, "missing section(s): " + ", ".join(missing)
    return True, "all 4 sections present in weekly_pulse.md"


def check_pulse_action_ideas():
    pulse = _json(PULSE_JSON)
    ideas = pulse.get("action_ideas")
    ideas = ideas if isinstance(ideas, list) else []
    in_markdown = 0
    in_section = False
    for line in _text(PULSE_MD).splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == "action ideas"
            continue
        if in_section and re.match(r"^\s*\d+\.\s", line):
            in_markdown += 1
    problems = []
    if len(ideas) != ACTION_IDEAS:
        problems.append(f"pulse.json has {len(ideas)} action_ideas")
    if in_markdown != ACTION_IDEAS:
        problems.append(f"weekly_pulse.md shows {in_markdown} numbered ideas")
    if problems:
        return False, "; ".join(problems)
    return True, f"exactly {ACTION_IDEAS} action ideas in pulse.json and the markdown"


def check_pulse_excerpts_exact():
    pulse = _json(PULSE_JSON)
    quotes = pulse.get("quotes") or []
    if not quotes:
        return False, "pulse.json has no quotes"
    index = _review_index()
    for i, quote in enumerate(quotes, 1):
        excerpt = str(quote.get("excerpt") or "").strip()
        text = str(quote.get("text") or "")
        plain = excerpt.strip("\u2026").strip()
        if not (excerpt in text or (plain and plain in text)):
            return False, f"excerpt {i} is not an exact substring of its review"
        review_id = str(quote.get("review_id") or "")
        row = index.get(review_id)
        if row is None or text not in row:
            return False, f"excerpt {i}: review {review_id[:8]} not in data/reviews.csv"
    return True, f"{len(quotes)} excerpts are exact substrings of their reviews"


def check_pulse_email_summary():
    pulse = _json(PULSE_JSON)
    summary = pulse.get("email_summary")
    if not isinstance(summary, dict) or not summary:
        return False, "email_summary missing"
    theme_lines = summary.get("theme_lines") or []
    key_point = summary.get("key_point") or ""
    actions = summary.get("actions") or []
    theme_lines = theme_lines if isinstance(theme_lines, list) else []
    actions = actions if isinstance(actions, list) else []
    key_point = str(key_point).strip()
    if len(theme_lines) < TOP_THEMES or len(actions) < TOP_THEMES or not key_point:
        return False, (
            f"need {TOP_THEMES} theme_lines, a key_point and {TOP_THEMES} "
            f"actions (got {len(theme_lines)}, {int(bool(key_point))}, {len(actions)})"
        )
    lines = [str(x) for x in theme_lines] + [key_point] + [str(x) for x in actions]
    banned = _email_banned_words()
    problems = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            problems.append(f"line {i} is empty")
            continue
        if any(ch.isdigit() for ch in line):
            problems.append(f"line {i} contains a digit")
        words = len(line.split())
        if words > MAX_EMAIL_LINE_WORDS:
            problems.append(f"line {i} has {words} words (max {MAX_EMAIL_LINE_WORDS})")
        hits = _banned_hits(line, banned)
        if hits:
            problems.append(f"line {i} uses banned word(s) {', '.join(hits)}")
    if problems:
        return False, "; ".join(problems[:3])
    return True, f"{len(lines)} lines, <= {MAX_EMAIL_LINE_WORDS} words, no digits/banned words"


# ---------------------------------------------------------------------------
# FEE EXPLAINER CHECKS
# ---------------------------------------------------------------------------
def _fee_bullets():
    fee = _json(FEE_JSON)
    bullets = fee.get("explanation_bullets")
    if not isinstance(bullets, list) or not bullets:
        raise CheckError(f"{_rel(FEE_JSON)} has no explanation_bullets")
    return fee, bullets


def _bullet_text(bullet):
    if isinstance(bullet, dict):
        return str(bullet.get("text") or "").strip()
    return str(bullet).strip()


def check_fee_bullets_and_review():
    fee, bullets = _fee_bullets()
    problems = []
    if not (1 <= len(bullets) <= MAX_FEE_BULLETS):
        problems.append(f"{len(bullets)} bullets (need 1-{MAX_FEE_BULLETS})")
    if "needs_human_review" not in fee:
        problems.append("needs_human_review missing")
    elif fee["needs_human_review"] is not False:
        problems.append(f"needs_human_review is {fee['needs_human_review']!r}")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(bullets)} bullets, needs_human_review is false"


def check_fee_source_links():
    fee = _json(FEE_JSON)
    links = fee.get("source_links")
    if not isinstance(links, list):
        return False, "source_links missing"
    if len(links) != 2:
        return False, f"{len(links)} source_links (need exactly 2)"
    for i, link in enumerate(links, 1):
        if not isinstance(link, dict):
            return False, f"link {i} is not an object"
        url = str(link.get("url") or "")
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if not host:
            return False, f"link {i} has no usable url"
        if not any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS):
            return False, f"link {i} is not an official domain: {host}"
    return True, "2 source_links, both on official domains"


def check_fee_last_checked():
    fee = _json(FEE_JSON)
    value = fee.get("last_checked")
    if not str(value or "").strip():
        return False, "last_checked missing or empty"
    return True, f"last_checked = {value}"


def check_fee_numbers_in_sources():
    _, bullets = _fee_bullets()
    sources = _knowledge_sources()
    problems = []
    fact_count = 0
    for i, bullet in enumerate(bullets, 1):
        text = _bullet_text(bullet)
        is_summary = (
            isinstance(bullet, dict)
            and str(bullet.get("type") or "").strip().lower() == "summary"
        )
        if is_summary:
            continue
        fact_count += 1
        if isinstance(bullet, dict):
            cites = [_source_id(c) for c in (bullet.get("sources") or [])]
        else:
            cites = []
        valid = [c for c in cites if c in sources]
        if not valid:
            problems.append(f"bullet {i} cites no readable source")
            continue
        for number in _numbers_in(text):
            if not any(number in sources[c] for c in valid):
                problems.append(f"bullet {i}: {number} not in {'/'.join(valid)}")
    if not fact_count:
        return False, "no fact bullets to check"
    if problems:
        return False, "; ".join(problems[:3])
    return True, f"{fact_count} fact bullets, every number found in its cited S*.txt"


def check_fee_fact_check_summary():
    fee = _json(FEE_JSON)
    summary = (fee.get("fact_check") or {}).get("summary")
    if not isinstance(summary, dict):
        return False, "fact_check.summary missing"
    problems = []
    for key in ("not_supported", "unverified"):
        try:
            value = int(summary.get(key))
        except (TypeError, ValueError):
            problems.append(f"{key} is {summary.get(key)!r}")
            continue
        if value != 0:
            problems.append(f"{key} = {value}")
    if problems:
        return False, "; ".join(problems)
    return True, "fact_check: not_supported = 0, unverified = 0"


def check_fee_no_banned_words():
    _, bullets = _fee_bullets()
    problems = []
    for i, bullet in enumerate(bullets, 1):
        hits = _banned_hits(_bullet_text(bullet), FEE_BANNED_WORDS)
        if hits:
            problems.append(f"bullet {i}: {', '.join(hits)}")
    if problems:
        return False, "; ".join(problems[:3])
    return True, f"no banned words in {len(bullets)} bullets"


# ---------------------------------------------------------------------------
# APPROVAL AND MCP SAFETY CHECKS
# ---------------------------------------------------------------------------
def check_approval_record_fields():
    record, path = _latest_approval()
    required = ("approval_id", "approved_at", "payload_sha256")
    missing = [k for k in required if not str(record.get(k) or "").strip()]
    if missing:
        return False, f"{_rel(path)} is missing " + ", ".join(missing)
    return True, f"latest record {_rel(path)} has approval_id/approved_at/payload_sha256"


def check_approval_payload_hash():
    publish = _publish_module(required=True)
    record, path = _latest_approval()
    payloads = record.get("payloads")
    if not isinstance(payloads, dict):
        return False, f"{_rel(path)} has no payloads to hash"
    recomputed = publish.compute_hash(payloads)
    stored = str(record.get("payload_sha256") or "")
    if recomputed != stored:
        return False, f"hash mismatch: recomputed {recomputed[:12]} != {stored[:12]}"
    return True, f"payload_sha256 recomputes to {recomputed[:12]} (publish.compute_hash)"


def check_publish_log_steps():
    lines = [l for l in _text(PUBLISH_LOG).splitlines() if l.strip()]
    if not lines:
        return False, f"{_rel(PUBLISH_LOG)} is empty"
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return False, f"last log line is not valid JSON: {exc}"
    problems = []
    if str(last.get("doc_append") or "").upper() != "OK":
        problems.append(f"doc_append={last.get('doc_append')!r}")
    if last.get("read_back_verified") is not True:
        problems.append(f"read_back_verified={last.get('read_back_verified')!r}")
    if str(last.get("email_draft") or "").upper() != "OK":
        problems.append(f"email_draft={last.get('email_draft')!r}")
    if problems:
        return False, "; ".join(problems)
    return True, "latest log line: doc append OK, read-back verified, email draft OK"


def check_publish_email_subject():
    record, _ = _latest_approval()
    subject = str((record.get("payloads") or {}).get("email_subject") or "")
    if not subject.startswith(SUBJECT_PREFIX):
        return False, f"subject does not start with {SUBJECT_PREFIX!r}"
    return True, f"subject starts with {SUBJECT_PREFIX!r}"


def check_publish_email_body_footer():
    record, path = _latest_approval()
    payloads = record.get("payloads") or {}
    body, source = None, None
    publish = _publish_module(required=False)
    if publish is not None:
        try:
            body = publish.render_email_body(
                payloads,
                str(record.get("approval_id") or ""),
                str(record.get("payload_sha256") or ""),
            )
            source = "rendered email body"
        except Exception:        # noqa: BLE001 - fall back to the saved preview
            body = None
    if body is None and EMAIL_PREVIEW.is_file():
        body, source = _text(EMAIL_PREVIEW), _rel(EMAIL_PREVIEW)
    if body is None:
        return False, f"no email body to inspect (approval {_rel(path)})"
    if EMAIL_FOOTER not in body:
        return False, f"{EMAIL_FOOTER!r} missing from {source}"
    return True, f"{EMAIL_FOOTER!r} present in {source}"


def check_mcp_no_send_tools():
    source = _text(MCP_CHECK_PY)
    tools = None
    error = None
    try:
        for node in ast.parse(source).body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(t, ast.Name) and t.id == "REQUIRED_TOOLS"
                   for t in node.targets):
                tools = tuple(ast.literal_eval(node.value))
                break
    except (ValueError, SyntaxError) as exc:
        error = str(exc)
    if tools is None:
        try:
            import mcp_check
            tools = tuple(mcp_check.REQUIRED_TOOLS)
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot read REQUIRED_TOOLS from src/mcp_check.py: {error or exc}"
    names = [str(t) for t in tools]
    senders = [n for n in names if SEND_IN_TOOL_NAME in n.lower()]
    if senders:
        return False, "tool name(s) containing 'send': " + ", ".join(senders)
    return True, "no 'send' tool in REQUIRED_TOOLS (" + ", ".join(names) + ")"


# ---------------------------------------------------------------------------
# EDGE CASES (validate_csv.py on synthetic files in a temp folder)
# ---------------------------------------------------------------------------
def check_edge_empty_file():
    with _temp_folder() as folder:
        path = folder / "empty.csv"
        path.write_bytes(b"")
        frame, errors, _ = _validate_file(path)
    if frame is not None:
        return False, "an empty file was accepted"
    if not errors:
        return False, "an empty file produced no error"
    return True, f"empty file rejected: {_short(_without_folder(errors, folder)[0])}"


def check_edge_missing_text_column():
    with _temp_folder() as folder:
        path = _write_frame(folder, "no_text.csv", _synthetic_rows(120),
                            columns=["date", "rating"])
        frame, errors, _ = _validate_file(path)
        clean = _without_folder(errors, folder)
    if frame is not None:
        return False, "a CSV without a text column was accepted"
    if not any("text" in str(e) for e in clean):
        return False, "expected a missing-column error, got: " + _short("; ".join(clean))
    return True, f"missing text column rejected: {_short(clean[0])}"


def check_edge_too_few_rows():
    with _temp_folder() as folder:
        path = _write_frame(folder, "few_rows.csv", _synthetic_rows(20))
        frame, errors, _ = _validate_file(path)
        clean = _without_folder(errors, folder)
    if frame is not None:
        return False, "20 valid rows were accepted"
    if not any("100" in str(e) for e in clean):
        return False, "expected a 'fewer than 100' error, got: " + _short("; ".join(clean))
    return True, f"20 rows rejected: {_short(clean[0])}"


def check_edge_bad_ratings_dropped():
    rows = _synthetic_rows(120) + [
        {"date": "2026-07-21", "rating": 0, "text": SYNTHETIC_TEXT.format(i=900)},
        {"date": "2026-07-21", "rating": 7, "text": SYNTHETIC_TEXT.format(i=901)},
        {"date": "2026-07-21", "rating": "good", "text": SYNTHETIC_TEXT.format(i=902)},
    ]
    with _temp_folder() as folder:
        path = _write_frame(folder, "bad_ratings.csv", rows)
        frame, errors, warnings = _validate_file(path)
    if frame is None:
        return False, "file rejected: " + _short("; ".join(_without_folder(errors, folder)))
    if len(frame) != 120:
        return False, f"kept {len(frame)} rows, expected 120"
    if not any("rating" in str(w) for w in warnings):
        return False, "no warning about the dropped ratings"
    return True, "3 bad ratings (0, 7, 'good') dropped with a warning, 120 kept"


def check_edge_pii_masked():
    rows = _synthetic_rows(119) + [{
        "date": "2026-07-21",
        "rating": 3,
        "text": "reach me at user@example.com or +919876543210 for feedback",
    }]
    with _temp_folder() as folder:
        path = _write_frame(folder, "pii.csv", rows)
        frame, errors, warnings = _validate_file(path)
    if frame is None:
        return False, "file rejected: " + _short("; ".join(_without_folder(errors, folder)))
    texts = [str(t) for t in frame["text"].tolist()]
    has_email = any("[email]" in t for t in texts)
    has_phone = any("[phone]" in t for t in texts)
    if not (has_email and has_phone):
        return False, f"masking incomplete (email={has_email}, phone={has_phone})"
    if not any("masked" in str(w) for w in warnings):
        return False, "no warning about the masked values"
    return True, "email masked as [email] and phone number as [phone]"


def check_edge_private_column_dropped():
    rows = _synthetic_rows(120)
    for row in rows:
        row["user_name"] = "person"
    with _temp_folder() as folder:
        path = _write_frame(folder, "user_name.csv", rows)
        frame, errors, warnings = _validate_file(path)
    if frame is None:
        return False, "file rejected: " + _short("; ".join(_without_folder(errors, folder)))
    if "user_name" in {str(c) for c in frame.columns}:
        return False, "the user_name column was kept"
    if not any("private column" in str(w) for w in warnings):
        return False, "no warning about the dropped private column"
    return True, "user_name column dropped with a warning"


def check_edge_twenty_weeks_trimmed():
    start = datetime(2026, 3, 2)
    rows = []
    for n in range(20):
        day = (start + timedelta(days=7 * n)).date().isoformat()
        for k in range(10):
            rows.append({
                "date": day,
                "rating": ((n * 10 + k) % 5) + 1,
                "text": SYNTHETIC_TEXT.format(i=n * 10 + k),
            })
    with _temp_folder() as folder:
        path = _write_frame(folder, "span20.csv", rows)
        frame, errors, warnings = _validate_file(path)
    if frame is None:
        return False, "file rejected: " + _short("; ".join(_without_folder(errors, folder)))
    weeks_left = int(frame["week_start"].nunique())
    if weeks_left != SPAN_MAX_WEEKS:
        return False, f"{weeks_left} weeks kept, expected {SPAN_MAX_WEEKS}"
    if not any("older than" in str(w) for w in warnings):
        return False, "no warning about the trimmed older reviews"
    return True, "20 weeks of reviews trimmed to the latest 12 weeks"


# ---------------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------------
CHECKS = (
    ("data.file_exists", "Data", check_data_file_exists),
    ("data.required_columns", "Data", check_data_required_columns),
    ("data.no_private_columns", "Data", check_data_no_private_columns),
    ("data.date_span_weeks", "Data", check_data_date_span),
    ("insights.theme_counts", "Insights", check_insights_theme_counts),
    ("insights.problem_counts", "Insights", check_insights_problem_counts),
    ("insights.quotes_exact", "Insights", check_insights_quotes_exact),
    ("insights.fee_issue", "Insights", check_insights_fee_issue),
    ("pulse.word_count", "Pulse", check_pulse_word_count),
    ("pulse.sections", "Pulse", check_pulse_sections),
    ("pulse.action_ideas", "Pulse", check_pulse_action_ideas),
    ("pulse.excerpts_exact", "Pulse", check_pulse_excerpts_exact),
    ("pulse.email_summary", "Pulse", check_pulse_email_summary),
    ("fee.bullets_and_review", "Fee explainer", check_fee_bullets_and_review),
    ("fee.source_links", "Fee explainer", check_fee_source_links),
    ("fee.last_checked", "Fee explainer", check_fee_last_checked),
    ("fee.numbers_in_sources", "Fee explainer", check_fee_numbers_in_sources),
    ("fee.fact_check_summary", "Fee explainer", check_fee_fact_check_summary),
    ("fee.no_banned_words", "Fee explainer", check_fee_no_banned_words),
    ("approval.record_fields", "Approval/MCP", check_approval_record_fields),
    ("approval.payload_hash", "Approval/MCP", check_approval_payload_hash),
    ("publish.log_steps_ok", "Approval/MCP", check_publish_log_steps),
    ("publish.email_subject", "Approval/MCP", check_publish_email_subject),
    ("publish.email_body_footer", "Approval/MCP", check_publish_email_body_footer),
    ("mcp.no_send_tools", "Approval/MCP", check_mcp_no_send_tools),
    ("edge.empty_file", "Edge cases", check_edge_empty_file),
    ("edge.missing_text_column", "Edge cases", check_edge_missing_text_column),
    ("edge.too_few_rows", "Edge cases", check_edge_too_few_rows),
    ("edge.bad_ratings_dropped", "Edge cases", check_edge_bad_ratings_dropped),
    ("edge.pii_masked", "Edge cases", check_edge_pii_masked),
    ("edge.private_column_dropped", "Edge cases", check_edge_private_column_dropped),
    ("edge.twenty_weeks_trimmed", "Edge cases", check_edge_twenty_weeks_trimmed),
)


def run_checks():
    """Run every check; an exception is a FAIL for that check only."""
    results = []
    for name, group, check in CHECKS:
        try:
            ok, detail = check()
        except (Exception, SystemExit) as exc:   # never crash the run
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({
            "name": name,
            "group": group,
            "ok": bool(ok),
            "detail": _short(detail),
        })
    return results


def write_report(results, when, passed, total):
    """outputs/eval_report.md: the same table plus the timestamp."""
    lines = [
        "# Evaluation report",
        "",
        f"Generated: {when}",
        "",
        "| # | Status | Check | Group | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, row in enumerate(results, 1):
        status = "PASS" if row["ok"] else "FAIL"
        detail = row["detail"].replace("|", "\\|")
        lines.append(f"| {i} | {status} | {row['name']} | {row['group']} | {detail} |")
    lines += ["", f"**{passed}/{total} checks passed**", ""]
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def _utf8_stdout():
    """Print safely even on a cp1252 Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv=None):
    _utf8_stdout()
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    results = run_checks()
    for row in results:
        status = "PASS" if row["ok"] else "FAIL"
        print(f"{status}  {row['name']:<30} {row['detail']}")
    passed = sum(1 for row in results if row["ok"])
    total = len(results)
    try:
        write_report(results, when, passed, total)
    except OSError as exc:
        print(f"WARN  could not write {_rel(REPORT_MD)}: {exc}")
    print()
    print(f"{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
