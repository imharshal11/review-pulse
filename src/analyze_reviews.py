"""Review Intelligence Layer.

Reads data/reviews.csv, uses Groq LLM calls (JSON mode) to discover themes,
classify every review, rank themes, pick real quotes and analyse fee confusion.
Results are written to outputs/insights.json and outputs/review_themes.csv.
"""

import hashlib
import json
import math
import os
import re
import ssl
import time
from datetime import date

import httpx
import pandas as pd
from dotenv import load_dotenv
from groq import Groq

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
INPUT_CSV = "data/reviews.csv"
OUT_DIR = "outputs"
THEMES_CSV = "outputs/review_themes.csv"
INSIGHTS_JSON = "outputs/insights.json"
CACHE_DIR = "outputs/cache"

TRUNCATE = 300          # chars of review text sent to the LLM
CLASSIFY_BATCH = 40     # reviews per classification call
MAX_RETRIES = 5         # attempts per LLM call

# Reasoning models burn tokens "thinking" before they emit JSON, which can
# make JSON mode fail; give them headroom and a cheap reasoning budget.
FAST_KWARGS = {"reasoning_effort": "low", "max_completion_tokens": 16000}
BIG_KWARGS = {"max_completion_tokens": 8000}

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL")            # big model (themes, quotes, fees)
GROQ_MODEL_FAST = os.getenv("GROQ_MODEL_FAST")  # small model (classification)

_client = None


def _ssl_context():
    """Some machines (corporate SSL inspection) fail with the default bundle.

    We probe Groq once and fall back to the operating system trust store,
    which usually contains the extra root certificate.
    """
    try:
        httpx.get("https://api.groq.com/openai/v1/models", timeout=10)
        return None  # default certificates are fine
    except httpx.HTTPError as exc:
        print(f"  default TLS bundle failed ({type(exc).__name__}), "
              "using the OS trust store")
        return ssl.create_default_context()


def get_client():
    """Create the Groq client once and reuse it."""
    global _client
    if _client is None:
        if not GROQ_API_KEY:
            raise SystemExit("GROQ_API_KEY is missing from .env")
        ctx = _ssl_context()
        if ctx is None:
            _client = Groq(api_key=GROQ_API_KEY)
        else:
            _client = Groq(
                api_key=GROQ_API_KEY,
                http_client=httpx.Client(verify=ctx, timeout=60.0),
            )
    return _client


# ---------------------------------------------------------------------------
# LLM HELPER (JSON mode + retry + cache)
# ---------------------------------------------------------------------------
def _parse_json(content):
    """Parse a JSON object, tolerating accidental markdown fences."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    return json.loads(text)


def _cache_file(model, system, user, extra):
    """Deterministic cache path for one exact LLM request."""
    key = model + "\n" + system + "\n" + user
    if extra:
        key += "\n" + json.dumps(extra, sort_keys=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.json")


def call_llm(model, system, user, **extra):
    """One JSON-mode chat call with caching and retries.

    Returns the parsed dict. Finished calls are stored in outputs/cache/ so
    re-running the script never repeats them.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_file(model, system, user, extra)

    # Cache hit: reuse the previous answer, no network call.
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            print(f"  cache hit ({os.path.basename(path)[:12]}...)")
            return json.load(fh)

    wait = 10
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = get_client().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                **extra,
            )
            data = _parse_json(resp.choices[0].message.content)

            # Save for next time.
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            return data

        except Exception as exc:  # rate limits, network, bad JSON, ...
            # Permanent client errors (e.g. model not found) will not fix
            # themselves, so fail fast instead of waiting.
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status != 429:
                print("  permanent API error, not retrying.")
                raise

            print(
                f"  LLM call failed (attempt {attempt}/{MAX_RETRIES}): "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt == MAX_RETRIES:
                raise
            print(f"  waiting {wait}s before retrying...")
            time.sleep(wait)
            wait *= 2


def short_ids(df):
    """Give every review a tiny id (r1, r2, ...) and keep the mapping."""
    ids = [f"r{i + 1}" for i in range(len(df))]
    mapping = dict(zip(ids, df["review_id"]))
    return ids, mapping


def clip(text):
    """Shorten text before sending it to the LLM."""
    return str(text)[:TRUNCATE]


# ---------------------------------------------------------------------------
# STAGE A - DISCOVER THEMES (big model, 1 call)
# ---------------------------------------------------------------------------
def pick_theme_sample(df, n_low, n_mid, n_high):
    """Sample 150 reviews across rating bands, preferring 10+ word texts."""
    words = df["text"].str.split().str.len()
    parts = []

    bands = [
        (df["rating"].isin([1, 2]), n_low),
        (df["rating"] == 3, n_mid),
        (df["rating"].isin([4, 5]), n_high),
    ]
    for mask, n in bands:
        band = df[mask]
        # Prefer longer, more informative reviews when there are enough.
        preferred = band[words[band.index] >= 10]
        pool = preferred if len(preferred) >= n else band
        parts.append(pool.sample(n=min(n, len(pool)), random_state=42))

    return pd.concat(parts).reset_index(drop=True)


def stage_a(df, ids, mapping):
    """Ask the big model for at most 5 themes."""
    sample = pick_theme_sample(df, 60, 30, 60)
    sample_idx = list(sample.index)

    numbered = "\n".join(
        f"{ids[i]}: {clip(df.at[i, 'text'])}" for i in sample_idx
    )

    system = (
        "You are a consumer-insights analyst for Indian stock and "
        "mutual-fund investing apps. You read Google Play reviews written in "
        "English or Hinglish. Always answer with a single valid JSON object "
        "and nothing else."
    )
    user = (
        "Below are Play Store reviews with short ids.\n\n"
        f"{numbered}\n\n"
        "Propose AT MOST 5 themes that together cover what these reviews talk "
        "about. Each theme must be distinct, have a name of 2-4 words and a "
        "description of exactly one sentence.\n"
        'Reply as JSON: {"themes": [{"name": "...", "description": "..."}]}'
    )

    result = call_llm(GROQ_MODEL, system, user)
    themes = result.get("themes", [])

    # Enforce the 5-theme limit: retry once, then keep the first 5.
    if len(themes) > 5:
        print(f"  got {len(themes)} themes, retrying once with a stricter ask")
        retry_user = user + (
            "\nYou returned too many themes. Return AT MOST 5 themes."
        )
        result = call_llm(GROQ_MODEL, system, retry_user)
        themes = result.get("themes", [])
    if len(themes) > 5:
        themes = themes[:5]

    themes = [
        {
            "name": str(t.get("name", f"Theme {i + 1}")).strip(),
            "description": str(t.get("description", "")).strip(),
        }
        for i, t in enumerate(themes)
    ]
    print(f"  discovered {len(themes)} themes:")
    for i, t in enumerate(themes, 1):
        print(f"    {i}. {t['name']} - {t['description']}")
    return themes


# ---------------------------------------------------------------------------
# STAGE B - CLASSIFY ALL REVIEWS (small fast model)
# ---------------------------------------------------------------------------
def classify_batch(batch_ids, texts, themes):
    """Classify one batch of reviews; returns {short_id: theme_number}."""
    system = (
        "You classify app reviews into the themes you are given. "
        "Always answer with a single valid JSON object and nothing else."
    )
    theme_lines = "\n".join(
        f"{i}. {t['name']} - {t['description']}" for i, t in enumerate(themes, 1)
    )
    payload = json.dumps(
        {sid: clip(txt) for sid, txt in zip(batch_ids, texts)},
        ensure_ascii=False,
    )
    user = (
        f"Themes:\n{theme_lines}\n\n"
        f"Reviews (short id -> text):\n{payload}\n\n"
        f"Map EVERY short id to ONE theme number from 1 to {len(themes)}. "
        "If a review fits no theme, use the closest one. "
        f'Reply as JSON: {{"r1": 3, "r2": 1, ...}}'
    )

    raw = call_llm(GROQ_MODEL_FAST, system, user, **FAST_KWARGS)
    mapping = {}
    valid = set(batch_ids)
    for sid, value in (raw or {}).items():
        if sid not in valid:
            continue
        try:
            num = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= num <= len(themes):
            mapping[sid] = num
    return mapping


def stage_b(df, ids, themes):
    """Classify every review, batch by batch."""
    system = (
        "You classify app reviews into the themes you are given. "
        "Always answer with a single valid JSON object and nothing else."
    )
    theme_lines = "\n".join(
        f"{i}. {t['name']} - {t['description']}" for i, t in enumerate(themes, 1)
    )
    id_to_row = {sid: i for i, sid in enumerate(ids)}

    assigned = {}
    counts = {}
    total_batches = math.ceil(len(ids) / CLASSIFY_BATCH)

    for b in range(total_batches):
        start = b * CLASSIFY_BATCH
        rows = range(start, min(start + CLASSIFY_BATCH, len(ids)))
        batch_ids = [ids[i] for i in rows]
        texts = [df.at[i, "text"] for i in rows]

        print(f"Batch {b + 1}/{total_batches} ({len(batch_ids)} reviews)")
        got = classify_batch(batch_ids, texts, themes)

        # Missing ids: retry that review once on its own.
        for sid in batch_ids:
            if sid in got:
                continue
            solo_user = (
                f"Themes:\n{theme_lines}\n\n"
                f'Review {sid}: "{clip(df.at[id_to_row[sid], "text"])}"\n'
                f'Reply as JSON: {{"{sid}": <theme number 1-{len(themes)}>}}'
            )
            solo = call_llm(GROQ_MODEL_FAST, system, solo_user, **FAST_KWARGS)
            picked = None
            if isinstance(solo, dict) and solo:
                # The solo call normally echoes the id back to us.
                value = solo.get(sid, next(iter(solo.values())))
                try:
                    num = int(value)
                    if 1 <= num <= len(themes):
                        picked = num
                except (TypeError, ValueError):
                    picked = None

            # Still nothing: fall back to the most common theme so far.
            if picked is None:
                picked = max(counts, key=counts.get) if counts else 1
                print(f"  {sid}: no answer, using most common theme ({picked})")
            got[sid] = picked

        assigned.update(got)
        for num in got.values():
            counts[num] = counts.get(num, 0) + 1

    # Anything never answered at all: most common theme (or 1).
    fallback = max(counts, key=counts.get) if counts else 1
    for sid in ids:
        assigned.setdefault(sid, fallback)

    out = df.copy()
    out["theme_number"] = [assigned[s] for s in ids]
    out["theme"] = [themes[n - 1]["name"] for n in out["theme_number"]]
    return out


# ---------------------------------------------------------------------------
# STAGE C - COUNT AND RANK (pure Python)
# ---------------------------------------------------------------------------
RANKING_RULE = (
    "Top 3 themes ranked by number of 1-2 star reviews (problem volume); "
    "ties broken by higher problem rate."
)


def problem_trend(problems, weeks):
    """Compare problem volume in the last 4 weeks vs the previous 4 weeks."""
    recent = weeks[-4:]
    previous = weeks[-8:-4]

    last = int(problems["week_start"].isin(recent).sum())
    prev = int(problems["week_start"].isin(previous).sum())

    if last > prev:
        direction = "up"
    elif last < prev:
        direction = "down"
    else:
        direction = "flat"

    return {
        "direction": direction,
        "last_4_weeks": last,
        "previous_4_weeks": prev,
        "delta": last - prev,
    }


def stage_c(df, themes):
    """Counts, problem volumes, trends and the issue-based ranking."""
    stats = []
    name_to_number = {}
    total = len(df)
    weeks = sorted(df["week_start"].unique())

    for i, theme in enumerate(themes, 1):
        group = df[df["theme_number"] == i]
        # A "problem" is a 1-2 star review inside this theme.
        problems = group[group["rating"].isin([1, 2])]
        name_to_number[theme["name"]] = i
        stats.append(
            {
                "name": theme["name"],
                "description": theme["description"],
                "count": int(len(group)),
                "share_pct": round(100.0 * len(group) / total, 1) if total else 0.0,
                "avg_rating": round(float(group["rating"].mean()), 2)
                if len(group)
                else 0.0,
                "problem_count": int(len(problems)),
                # Share of this theme's reviews that are 1-2 stars.
                "problem_rate": round(
                    100.0 * len(problems) / len(group), 1
                )
                if len(group)
                else 0.0,
                "weekly_counts": {
                    str(k): int(v)
                    for k, v in group["week_start"].value_counts().sort_index().items()
                },
            }
        )

    # Volume ranking (kept for reporting, not used for the top 3).
    themes_by_volume = sorted(
        stats, key=lambda s: s["count"], reverse=True
    )

    # Main ranking: most 1-2 star reviews first; ties broken by the higher
    # problem rate (a small theme that is mostly complaints ranks above a
    # large theme with the same problem count but fewer complaints per review).
    ranked = sorted(
        stats, key=lambda s: (s["problem_count"], s["problem_rate"]), reverse=True
    )
    top_names = [t["name"] for t in ranked[:3]]

    # Trend for the top themes only.
    for entry in stats:
        if entry["name"] in top_names:
            number = name_to_number[entry["name"]]
            problems = df[
                (df["theme_number"] == number) & (df["rating"].isin([1, 2]))
            ]
            entry["trend"] = problem_trend(problems, weeks)

    return stats, themes_by_volume, top_names


# ---------------------------------------------------------------------------
# STAGE D - PICK 3 REAL QUOTES (big model + code)
# ---------------------------------------------------------------------------
def pick_quote(df, ids, theme_number, theme_name):
    """Let the LLM pick one id, then copy the original text from the CSV."""
    group = df[df["theme_number"] == theme_number].copy()
    # Only unhappy customers (1-3 stars), and only quotes of a usable length.
    group = group[group["rating"].isin([1, 2, 3])]
    words = group["text"].str.split().str.len()
    group = group[(words >= 12) & (words <= 60)]
    if group.empty:
        return None
    group["_len"] = group["text"].str.len()
    group = group.sort_values(["thumbs_up", "_len"], ascending=False).head(25)

    pos = {i: ids[i] for i in group.index}          # row -> short id
    sid_to_row = {sid: row for row, sid in pos.items()}
    payload = json.dumps(
        {pos[i]: clip(group.at[i, "text"]) for i in group.index},
        ensure_ascii=False,
    )

    system = (
        "You pick the single review that best represents a theme. It must be "
        "clear, specific and contain no personal information. "
        "Always answer with a single valid JSON object and nothing else."
    )
    user = (
        f"Theme: {theme_name}\n\n"
        f"Candidate reviews:\n{payload}\n\n"
        'Reply with ONLY the id of the best review, as JSON: {"id": "r12"}'
    )
    result = call_llm(GROQ_MODEL, system, user)

    chosen = None
    if isinstance(result, dict):
        candidate = str(result.get("id", "")).strip()
        if candidate in sid_to_row:
            chosen = candidate
    if chosen is None:
        # Fall back to the top-ranked candidate.
        chosen = pos[group.index[0]]

    row = sid_to_row[chosen]
    # Copy the exact original text from the CSV, never LLM text.
    return {
        "theme": theme_name,
        "review_id": df.at[row, "review_id"],
        "rating": int(df.at[row, "rating"]),
        "date": str(df.at[row, "date"]),
        "text": df.at[row, "text"],
    }


def stage_d(df, ids, top_names, stats):
    quotes = []
    for name in top_names:
        number = next(
            i for i, s in enumerate(stats, 1) if s["name"] == name
        )
        quote = pick_quote(df, ids, number, name)
        if quote:
            quotes.append(quote)
    return quotes


# ---------------------------------------------------------------------------
# STAGE E - FEE CONFUSION (label first, then summarise)
# ---------------------------------------------------------------------------
# Whole-word, non-capturing regex so str.contains() never warns about groups.
FEE_PATTERN = re.compile(
    r"\b(?:charge|charges|charged|brokerage|brokrage|fee|fees|hidden|"
    r"deducted|deduction|dp charge|commission)\b",
    re.IGNORECASE,
)

FEE_LABEL_BATCH = 30   # reviews per labelling call in step E1

# The four stances the fast model must choose from.
STANCES = ("confused", "complaint_only", "praise", "unrelated")

# Confusion patterns the fast model must choose from when stance is confused.
CONFUSION_PATTERNS = (
    "charged on both buy and sell",
    "minimum charge on small trades",
    "other charges seen as hidden (taxes, DP, exchange)",
    "unexpected deduction or charge on failed or cancelled order",
    "other confusion",
)


def _normalise_stance(stance):
    """Map the model's wording onto one of the four known stances."""
    key = str(stance).strip().lower().replace(" ", "_").replace("-", "_")
    for known in STANCES:
        if key == known or known in key:
            return known
    # Closest guess; "complaint_only" is the safest non-confused default.
    return "complaint_only"


def _normalise_pattern(pattern):
    """Map the model's wording onto one of the known confusion patterns."""
    key = str(pattern).strip().lower()
    if not key or key in ("n/a", "na", "none", "no"):
        return "n/a"
    for known in CONFUSION_PATTERNS:
        if key == known.lower() or known.lower() in key or key in known.lower():
            return known
    return "other confusion"


def stage_e(df, ids, mapping):
    """Separate fee confusion from plain complaints.

    E1 labels every fee review with the fast model, E2 counts the labels in
    Python, E3 asks the big model to describe the confusion using only the
    reviews that were labelled "confused".
    """
    # Every review that mentions a fee/charge keyword.
    hits = df[df["text"].str.contains(FEE_PATTERN, regex=True)].copy()
    print(f"  {len(hits)} reviews mention a fee/charge keyword")
    if hits.empty:
        return None

    id_to_row = {sid: i for i, sid in enumerate(ids)}
    pos = {i: ids[i] for i in hits.index}          # row -> short id
    fee_ids = [pos[i] for i in hits.index]

    # --- Step E1: the fast model labels EVERY fee review ---
    label_system = (
        "You label Play Store reviews of a stock/mutual-fund app that mention "
        "fees or charges. Always answer with a single valid JSON object and "
        "nothing else."
    )
    stance_lines = "\n".join(
        f'- "{s}"' for s in STANCES
    )
    pattern_lines = "\n".join(f'- "{p}"' for p in CONFUSION_PATTERNS)

    labels = {}          # short id -> (stance, pattern)
    total_batches = math.ceil(len(fee_ids) / FEE_LABEL_BATCH)

    for b in range(total_batches):
        batch_ids = fee_ids[b * FEE_LABEL_BATCH : (b + 1) * FEE_LABEL_BATCH]
        payload = json.dumps(
            {sid: clip(hits.at[id_to_row[sid], "text"]) for sid in batch_ids},
            ensure_ascii=False,
        )
        print(f"  E1 batch {b + 1}/{total_batches} ({len(batch_ids)} reviews)")
        label_user = (
            "Reviews (short id -> text):\n"
            f"{payload}\n\n"
            "Label EVERY review with a stance and a pattern.\n"
            "stance must be exactly one of:\n"
            f"{stance_lines}\n"
            '  "confused" = the user does not understand what was charged, '
            "why, or when.\n"
            '  "complaint_only" = the user understands the charge but thinks '
            "it is too high.\n"
            '  "praise" = the user says charges are fine or low.\n'
            '  "unrelated" = the review is not about Groww fees.\n'
            "If stance is \"confused\", pattern must be exactly one of:\n"
            f"{pattern_lines}\n"
            "Otherwise pattern must be \"n/a\".\n"
            'Reply as JSON: {"r5": {"stance": "confused", "pattern": '
            '"<pattern>"}, "r6": {"stance": "complaint_only", "pattern": '
            '"n/a"}, ...}'
        )
        raw = call_llm(GROQ_MODEL_FAST, label_system, label_user, **FAST_KWARGS)

        for sid in batch_ids:
            if sid not in (raw or {}):
                continue
            entry = raw[sid]
            if not isinstance(entry, dict):
                continue
            stance = _normalise_stance(entry.get("stance", ""))
            pattern = (
                _normalise_pattern(entry.get("pattern", "n/a"))
                if stance == "confused"
                else "n/a"
            )
            labels[sid] = (stance, pattern)

        # Missing ids: retry that review once on its own.
        for sid in batch_ids:
            if sid in labels:
                continue
            solo_user = (
                f'Review {sid}: "{clip(hits.at[id_to_row[sid], "text"])}"\n\n'
                "Label this review with a stance and a pattern.\n"
                "stance must be exactly one of:\n"
                f"{stance_lines}\n"
                "If stance is \"confused\", pattern must be exactly one of:\n"
                f"{pattern_lines}\n"
                "Otherwise pattern must be \"n/a\".\n"
                f'Reply as JSON: {{"{sid}": {{"stance": "<stance>", '
                '"pattern": "<pattern or n/a>"}}}}'
            )
            solo = call_llm(GROQ_MODEL_FAST, label_system, solo_user, **FAST_KWARGS)
            entry = (solo or {}).get(sid)
            if isinstance(entry, dict):
                stance = _normalise_stance(entry.get("stance", ""))
                pattern = (
                    _normalise_pattern(entry.get("pattern", "n/a"))
                    if stance == "confused"
                    else "n/a"
                )
                labels[sid] = (stance, pattern)
            else:
                # No answer: count as a plain complaint (not confusion).
                labels[sid] = ("complaint_only", "n/a")

    # --- Step E2: Python counts the stances and the confusion patterns ---
    stance_counts = {s: 0 for s in STANCES}
    pattern_counts = {}
    for sid in fee_ids:
        stance, pattern = labels.get(sid, ("complaint_only", "n/a"))
        stance_counts[stance] = stance_counts.get(stance, 0) + 1
        if stance == "confused":
            pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

    confused_ids = [sid for sid in fee_ids if labels.get(sid, ("", ""))[0] == "confused"]
    print(
        f"  stances: {stance_counts} | confused: {len(confused_ids)} "
        f"of {len(fee_ids)}"
    )

    if not confused_ids:
        # Nothing was labelled confused; still return a usable result.
        return {
            "fee_name": "",
            "what_users_think": "",
            "what_confuses_them": "",
            "confusion_patterns": [],
            "mention_count": 0,
            "fee_reviews_total": int(len(hits)),
            "stance_counts": stance_counts,
            "pattern_counts": {},
            "examples": [],
        }

    # --- Step E3: the big model describes the confusion (confused reviews only) ---
    confused_payload = json.dumps(
        {sid: clip(hits.at[id_to_row[sid], "text"]) for sid in confused_ids},
        ensure_ascii=False,
    )
    system = (
        "You are a consumer-insights analyst. You describe only what users "
        "say or believe. Neutral tone. Always answer with a single valid "
        "JSON object and nothing else."
    )
    user = (
        "These Play Store reviews show users who are confused about a fee or "
        "charge on a stock/mutual-fund app:\n\n"
        f"{confused_payload}\n\n"
        "Write the fee_name (a short name for the fee these users are "
        "confused about), what_users_think (one sentence) and "
        "what_confuses_them (one or two sentences).\n"
        "Describe ONLY what the users say or believe in these reviews.\n"
        "Do NOT state any actual fee amounts, rules, or which segments are "
        "free. Use no facts from outside the reviews.\n"
        'Reply as JSON: {"fee_name": "...", "what_users_think": "...", '
        '"what_confuses_them": "..."}'
    )
    judged = call_llm(GROQ_MODEL, system, user, **BIG_KWARGS)

    # Example quotes: up to 3 confused reviews that are long enough to quote,
    # most upvoted first, text copied exactly from the CSV.
    examples = []
    confused_rows = [id_to_row[sid] for sid in confused_ids]
    cand = df.loc[confused_rows].copy()
    words = cand["text"].str.split().str.len()
    cand = cand[(words >= 12) & (words <= 60)]
    cand = cand.sort_values("thumbs_up", ascending=False).head(3)
    for row in cand.index:
        examples.append(
            {
                "review_id": df.at[row, "review_id"],
                "rating": int(df.at[row, "rating"]),
                "date": str(df.at[row, "date"]),
                "text": df.at[row, "text"],
            }
        )

    return {
        "fee_name": str(judged.get("fee_name", "")).strip(),
        "what_users_think": str(judged.get("what_users_think", "")).strip(),
        "what_confuses_them": str(judged.get("what_confuses_them", "")).strip(),
        "confusion_patterns": [
            p for p in CONFUSION_PATTERNS if pattern_counts.get(p)
        ],
        # Counted by Python from the per-review labels, not by the LLM.
        "mention_count": len(confused_ids),
        "fee_reviews_total": int(len(hits)),
        "stance_counts": stance_counts,
        "pattern_counts": pattern_counts,
        "examples": examples,
    }


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------
def print_summary(insights):
    """Print a clean human-readable summary."""
    print("\n" + "=" * 64)
    print("REVIEW INTELLIGENCE SUMMARY")
    print("=" * 64)
    print(
        f"Period {insights['period_start']} to {insights['period_end']} "
        f"| {insights['total_reviews']} reviews"
    )

    # Volume table (with problem_count and problem_rate).
    print("\nThemes by volume:")
    print(
        f"  {'theme':<24} {'count':>5} {'share':>6} {'avg':>4} "
        f"{'problems':>8} {'prob%':>6}"
    )
    for theme in insights["themes_by_volume"]:
        print(
            f"  {theme['name']:<24} {theme['count']:>5} "
            f"{theme['share_pct']:>5}% {theme['avg_rating']:>4} "
            f"{theme['problem_count']:>8} {theme['problem_rate']:>5}%"
        )

    print(f"\nRanking rule: {insights['ranking_rule']}")

    # Top 3 with trend.
    by_name = {t["name"]: t for t in insights["themes"]}
    print("\nTop 3 themes (by problem volume):")
    for i, name in enumerate(insights["top_themes"], 1):
        trend = by_name.get(name, {}).get("trend", {})
        arrow = {"up": "up", "down": "down", "flat": "flat"}[
            trend.get("direction", "flat")
        ]
        print(
            f"  {i}. {name} "
            f"(problems: {by_name.get(name, {}).get('problem_count', 0)}; "
            f"last 4 wks {trend.get('last_4_weeks', 0)} vs "
            f"prev 4 wks {trend.get('previous_4_weeks', 0)} -> {arrow})"
        )

    print("\nQuotes:")
    for quote in insights["quotes"]:
        print(f"  [{quote['rating']} stars, {quote['date']}] {quote['theme']}")
        print(f"    \"{quote['text']}\"")

    fee = insights.get("fee_issue")
    if fee:
        print("\nFee confusion:")
        print(f"  Fee: {fee['fee_name']}")
        print(f"  Users think: {fee['what_users_think']}")
        print(f"  What confuses them: {fee['what_confuses_them']}")
        print(
            f"  Confused reviews: {fee['mention_count']} "
            f"(of {fee['fee_reviews_total']} fee reviews)"
        )
        print("  Stance counts (code-counted):")
        for stance in ("confused", "complaint_only", "praise", "unrelated"):
            print(f"    {stance}: {fee['stance_counts'].get(stance, 0)}")
        print("  Confusion pattern counts (code-counted):")
        for pattern, count in sorted(
            fee["pattern_counts"].items(), key=lambda kv: kv[1], reverse=True
        ):
            print(f"    {pattern}: {count}")
    print("=" * 64)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    if not GROQ_MODEL or not GROQ_MODEL_FAST:
        raise SystemExit(
            "Set GROQ_MODEL (big model) and GROQ_MODEL_FAST (small model) in .env"
        )
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    df = pd.read_csv(INPUT_CSV, encoding="utf-8", dtype={"text": str})
    ids, mapping = short_ids(df)
    print(f"Loaded {len(df)} reviews from {INPUT_CSV}")

    # STAGE A - discover up to 5 themes with the big model.
    print("\nSTAGE A: discovering themes...")
    themes = stage_a(df, ids, mapping)

    # STAGE B - classify every review with the small fast model.
    print(f"\nSTAGE B: classifying {len(df)} reviews in batches of "
          f"{CLASSIFY_BATCH}...")
    themed = stage_b(df, ids, themes)
    themed[
        [
            "review_id",
            "date",
            "rating",
            "thumbs_up",
            "app_version",
            "week_start",
            "text",
            "theme_number",
            "theme",
        ]
    ].to_csv(THEMES_CSV, index=False, encoding="utf-8")
    print(f"Saved per-review themes to {THEMES_CSV}")

    # STAGE C - counts, problem ranking and trends, no LLM.
    print("\nSTAGE C: counting, ranking by problem volume, trends...")
    stats, themes_by_volume, top_names = stage_c(themed, themes)

    # STAGE D - three real quotes for the top themes, text from the CSV.
    print("\nSTAGE D: picking quotes for the top 3 themes...")
    quotes = stage_d(themed, ids, top_names, stats)

    # STAGE E - the most misunderstood fee (LLM judges, Python counts).
    print("\nSTAGE E: analysing fee confusion...")
    fee_issue = stage_e(themed, ids, mapping)

    # Assemble the final insights file.
    dates = pd.to_datetime(themed["date"])
    insights = {
        "generated_on": date.today().isoformat(),
        "period_start": str(dates.min().date()),
        "period_end": str(dates.max().date()),
        "total_reviews": int(len(themed)),
        "themes": stats,
        "themes_by_volume": themes_by_volume,
        "top_themes": top_names,
        "ranking_rule": RANKING_RULE,
        "quotes": quotes,
        "fee_issue": fee_issue,
    }
    with open(INSIGHTS_JSON, "w", encoding="utf-8") as fh:
        json.dump(insights, fh, ensure_ascii=False, indent=2)
    print(f"\nSaved insights to {INSIGHTS_JSON}")

    print_summary(insights)


if __name__ == "__main__":
    main()
