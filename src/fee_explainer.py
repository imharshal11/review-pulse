"""Mini RAG fee explainer.

Loads the fee confusion found in outputs/insights.json, retrieves the most
relevant chunks from the official sources in knowledge/ with BM25, and asks
the big model for a short, facts-only explanation. Every bullet is validated
in code (sources, numbers, tone) before it is kept.
"""

import json
import os
import re
from datetime import date

from rank_bm25 import BM25Okapi

# Same retry + cache helper used by the analysis script.
from analyze_reviews import call_llm

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
INSIGHTS_JSON = "outputs/insights.json"
KNOW_DIR = "knowledge"
SOURCES_MD = os.path.join(KNOW_DIR, "sources.md")

OUT_MD = "outputs/fee_explainer.md"
OUT_JSON = "outputs/fee_explainer.json"
OUT_DRAFT_MD = "outputs/fee_explainer_DRAFT.md"

CHUNK_SIZE = 700      # approximate chunk length in characters
CHUNK_OVERLAP = 100   # characters of overlap between chunks
TOP_K = 10            # chunks handed to the model

MAX_BULLETS = 6
MAX_BULLET_WORDS = 30
SUMMARY_MAX_WORDS = 25
MIN_FACT_BULLETS = 3  # below this, only a DRAFT explainer is saved
MAX_REPAIRS = 3

# The title is fixed so the document is always findable under one name.
FIXED_TITLE = "Why the total charge on a trade can be more than brokerage"

# Extra retrieval terms layered on top of the fee-issue text.
EXTRA_TERMS = (
    "brokerage per executed order minimum DP charges sell STT stamp duty "
    "exchange transaction charges SEBI turnover IPFT GST regulatory "
    "statutory levied by exchange F&O"
)

# Words we never want in a customer-facing explanation.
BANNED_WORDS = (
    "cheap", "expensive", "best", "worst", "great", "unfortunately",
    "amazing", "hidden", "scam", "unfair",
)

# Domains we treat as official for the two source links.
OFFICIAL_DOMAINS = ("groww.in", "915.groww.in", "nseindia.com")

PULSE_KWARGS = {"max_completion_tokens": 4000}


# ---------------------------------------------------------------------------
# LOAD KNOWLEDGE
# ---------------------------------------------------------------------------
def read_header(path):
    """Read the id/title/url/fetched_on block at the top of a source file."""
    info = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                break  # blank line ends the header block
            if ":" in line:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
    return info


def load_sources():
    """Return {source_id: {"title","url","fetched_on","text"}}."""
    sources = {}
    for name in sorted(os.listdir(KNOW_DIR)):
        if not re.fullmatch(r"S\d+\.txt", name):
            continue
        path = os.path.join(KNOW_DIR, name)
        info = read_header(path)
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        # Drop the header block; the body is everything after its blank line.
        body = raw.split("\n\n", 1)[1] if "\n\n" in raw else raw
        sources[name[:-4]] = {
            "title": info.get("title", name[:-4]),
            "url": info.get("url", ""),
            "fetched_on": info.get("fetched_on", ""),
            "text": body.strip(),
        }
    return sources


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping character chunks."""
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def build_chunks(sources):
    """All chunks as (chunk_id, source_id, text)."""
    chunks = []
    for source_id in sorted(sources):
        for i, piece in enumerate(chunk_text(sources[source_id]["text"])):
            if piece.strip():
                chunks.append((f"{source_id}-c{i}", source_id, piece))
    return chunks


# ---------------------------------------------------------------------------
# RETRIEVAL (BM25)
# ---------------------------------------------------------------------------
def retrieve(chunks, query, k=TOP_K):
    """Top-k chunks for the query; returns the chosen chunk ids in order."""
    tokenised = [chunk[2].lower().split() for chunk in chunks]
    bm25 = BM25Okapi(tokenised)
    scores = bm25.get_scores(query.lower().split())

    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    chosen = ranked[:k]
    return [chunks[i][0] for i in chosen]


def pin_key_chunks(chunks, chosen_ids):
    """Always include S3 chunks containing 'levied' (appended if missing)."""
    pinned = [
        cid for cid, sid, text in chunks
        if sid == "S3" and "levied" in text.lower()
    ]
    for cid in pinned:
        if cid not in chosen_ids:
            chosen_ids.append(cid)
    return pinned


# ---------------------------------------------------------------------------
# VALIDATION (the strict checker)
# ---------------------------------------------------------------------------
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text):
    """All numbers in a bullet, ignoring 1-digit list markers."""
    return [n for n in NUM_RE.findall(text) if len(n) > 1 or "." in n]


def source_text(source_id, sources):
    return sources.get(source_id, {}).get("text", "")


def normalise_source_id(citation):
    """'S1-c3' -> 'S1'; accepts bare 'S1' too. Returns '' if unusable."""
    citation = str(citation).strip()
    match = re.match(r"^(S\d+)", citation, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def bullet_type(bullet):
    """'summary' if the writer tagged the closing bullet, else 'fact'."""
    return ("summary"
            if str(bullet.get("type", "")).strip().lower() == "summary"
            else "fact")


def validate(bullets, sources):
    """Return a list of human-readable problems (empty list = all good)."""
    problems = []

    if not (1 <= len(bullets) <= MAX_BULLETS):
        problems.append(f"bullet count {len(bullets)} is not 1-{MAX_BULLETS}")

    for i, bullet in enumerate(bullets, 1):
        text = str(bullet.get("text", "")).strip()
        # Accept both "S1" and "S1-c3"; keep only the source id part.
        cites = [
            sid for sid in (normalise_source_id(c)
                            for c in bullet.get("sources", [])) if sid
        ]
        btype = bullet_type(bullet)

        if not text:
            problems.append(f"bullet {i}: empty text")
            continue

        # Banned tone words.
        lowered = text.lower()
        hits = [w for w in BANNED_WORDS if re.search(rf"\b{w}\b", lowered)]
        if hits:
            problems.append(f"bullet {i}: banned words {hits}")

        if btype == "summary":
            # Closing summary: a recap only - no digits, tight word limit.
            if re.search(r"\d", text):
                problems.append(
                    f"bullet {i} (summary): must contain no digits"
                )
            if len(text.split()) > SUMMARY_MAX_WORDS:
                problems.append(
                    f"bullet {i} (summary): {len(text.split())} words "
                    f"(max {SUMMARY_MAX_WORDS})"
                )
            continue

        # ---- Fact bullets ----
        # Every fact bullet must cite at least one real source.
        valid_cites = [c for c in cites if c in sources]
        if not valid_cites:
            problems.append(
                f"bullet {i}: cites no valid source (got {cites})"
            )

        # Every number must appear in the text of a cited source.
        for number in numbers_in(text):
            if not any(number in source_text(c, sources)
                       for c in valid_cites):
                problems.append(
                    f"bullet {i}: number '{number}' not found in "
                    f"sources {valid_cites}"
                )

        # Word limit.
        if len(text.split()) > MAX_BULLET_WORDS:
            problems.append(
                f"bullet {i}: {len(text.split())} words "
                f"(max {MAX_BULLET_WORDS})"
            )

        # Bullet c) (3rd bullet: regulatory/statutory charges).
        if i == 3:
            if "S3" not in valid_cites:
                problems.append(
                    "bullet 3 (regulatory/statutory): must cite S3"
                )
            if re.search(r"\d", text) or "%" in text:
                problems.append(
                    "bullet 3 (regulatory/statutory): must contain no "
                    "numbers or percentages"
                )

    return problems


# ---------------------------------------------------------------------------
# LLM CALL
# ---------------------------------------------------------------------------
SYSTEM = (
    "You write short, neutral, facts-only explanations of trading charges "
    "for an investing app's support team. You only state facts that appear "
    "in the provided source chunks. Always answer with a single valid JSON "
    "object and nothing else."
)


def build_user_prompt(fee_issue, chunks, chosen_ids, extra=""):
    """The user message: the fee issue plus the retrieved chunks."""
    by_id = {cid: (sid, text) for cid, sid, text in chunks}
    lines = []
    for cid in chosen_ids:
        sid, text = by_id[cid]
        lines.append(f"[{cid}] (source {sid}) {text}")
    chunk_block = "\n\n".join(lines)

    return (
        "Users of a stock/mutual-fund app are confused about charges. "
        "Here is the confusion:\n"
        f"- Fee: {fee_issue.get('fee_name', '')}\n"
        f"- What confuses them: {fee_issue.get('what_confuses_them', '')}\n\n"
        f"Official source chunks:\n{chunk_block}\n\n"
        "Explain the charges using ONLY facts from these chunks.\n"
        "Write the bullets so they cover ONLY these points, in this order:\n"
        "a) what brokerage is and that it applies per executed order. If "
        "stocks (equity) and F&O have different brokerage rules in the "
        "chunks, state them separately in this bullet and never merge them "
        "into one rule: give the equity rule and the F&O rule as found in "
        "the chunks.\n"
        "b) the DP charge on selling shares\n"
        "c) regulatory and statutory charges (STT, stamp duty, exchange "
        "transaction charges, SEBI turnover charge, IPFT) and who levies "
        "them. This bullet MUST cite S3, MUST use S3's own wording about "
        "who levies these charges and that they are not from Groww, and "
        "MUST contain NO numbers or percentages (the rate tables are "
        "flattened and segment-specific rates are ambiguous).\n"
        "d) GST added on top of certain charges\n"
        "e) one closing summary bullet: the total cost of a trade = "
        "brokerage + the charges above, so it can be higher than "
        "brokerage alone. Tag it \"type\": \"summary\". No numbers, no "
        f"charge names not already mentioned in bullets a-d, max "
        f"{SUMMARY_MAX_WORDS} words.\n"
        "Do NOT include penalties, auto square-off, delayed payment "
        "charges, pledge, API, MTF, or account-related fees.\n"
        "Keep only the most important numbers; avoid long lists inside one "
        "bullet.\n"
        "Cite source ids like \"S1\", not chunk ids like \"S1-c3\".\n"
        "The title must be exactly: "
        f"\"{FIXED_TITLE}\"\n"
        f"Return JSON: {{\"title\": \"{FIXED_TITLE}\", "
        "\"bullets\": [{\"text\": \"...\", \"type\": \"fact\", "
        "\"sources\": [\"S1\"]}]}}\n"
        "Tag every bullet a-d as \"type\": \"fact\" and bullet e as "
        "\"type\": \"summary\".\n"
        f"Rules: at most {MAX_BULLETS} bullets; each fact bullet at most "
        f"{MAX_BULLET_WORDS} words and the summary at most "
        f"{SUMMARY_MAX_WORDS} words; neutral, facts-only tone; every fact "
        "must come ONLY from the given chunks; never merge equity and F&O "
        "brokerage rules into one rule; every fact bullet must list the "
        "source ids it used; do not give advice or opinions; do not invent "
        "numbers."
        + (f"\n{extra}" if extra else "")
    )


# ---------------------------------------------------------------------------
# LLM-AS-JUDGE FACT CHECK
# ---------------------------------------------------------------------------
JUDGE_SYSTEM = (
    "You are a strict fact-checker for an investing app's support team. "
    "You verify that every claim in a bullet (numbers AND what they apply "
    "to) is directly stated in the provided source text. You never infer, "
    "merge, or add claims. Always answer with a single valid JSON object "
    "and nothing else."
)

# Cap on how much retrieved chunk text the judge sees per bullet.
JUDGE_CONTEXT_CAP = 6000  # characters


def is_too_large_error(exc):
    """True if the error looks like a 413 / request-too-large failure."""
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("413", "too large", "request_too_large",
                      "reduce the length", "context length")
    )


def build_judge_context(cites, chunks_by_id, chosen_ids, pinned_ids=(),
                        limit=JUDGE_CONTEXT_CAP, max_chunks=None):
    """Only the retrieved chunks (in BM25 rank order) from the cited sources.

    Pinned chunks (S3 'levied') are always included first when their source
    is cited. Highest-ranked chunks are kept next; total size is capped at
    `limit` characters (optionally also at `max_chunks` pieces).
    """
    parts = []
    used = 0
    seen = set()

    def add(cid, force=False):
        nonlocal used
        if cid in seen or cid not in chunks_by_id:
            return True
        sid, text = chunks_by_id[cid]
        if sid not in cites:
            return True
        piece = f"[{cid}]\n{text}"
        if not force and used + len(piece) > limit:
            return False
        parts.append(piece)
        seen.add(cid)
        used += len(piece)
        return True

    for cid in pinned_ids:
        add(cid, force=True)
    for cid in chosen_ids:  # chosen_ids is already in BM25 rank order
        if max_chunks is not None and len(parts) >= max_chunks:
            break
        if not add(cid):
            break
    return "\n\n".join(parts)


def judge_bullet(bullet, chunks_by_id, chosen_ids, pinned_ids=()):
    """Ask the big model whether one bullet is fully supported.

    Returns {"bullet": <text>, "status": "supported"|"not_supported"|
    "unverified"|"summary", "reason": str, "needs_human_review": bool}.
    """
    text = str(bullet.get("text", "")).strip()
    if bullet_type(bullet) == "summary":
        return {
            "bullet": text,
            "status": "summary",
            "reason": "summary (not fact-checked)",
            "needs_human_review": False,
        }
    cites = {
        sid for sid in (normalise_source_id(c)
                        for c in bullet.get("sources", [])) if sid
    }
    context = build_judge_context(cites, chunks_by_id, chosen_ids,
                                  pinned_ids=pinned_ids)
    if not context:
        return {
            "bullet": text,
            "status": "unverified",
            "reason": "no retrieved chunks for the cited sources",
            "needs_human_review": True,
        }

    def ask(context_text):
        user = (
            "Claim to check:\n"
            f"\"{text}\"\n\n"
            "Retrieved source chunks to check against:\n"
            f"{context_text}\n\n"
            "Return JSON: {\"supported\": true/false, \"reason\": \"<one "
            "sentence>\"}.\n"
            "\"supported\" is true only if EVERY claim in the bullet "
            "(numbers AND what they apply to) is directly stated in the "
            "provided chunks. Mixing rules from different segments (for "
            "example equity vs F&O) or adding claims not in the chunks "
            "makes it false."
        )
        return call_llm_from_analyze(user, system=JUDGE_SYSTEM)

    try:
        result = ask(context)
    except Exception as exc:
        if not is_too_large_error(exc):
            print(f"    judge call failed: {exc}")
            return {
                "bullet": text,
                "status": "unverified",
                "reason": f"judge unavailable: {exc}",
                "needs_human_review": True,
            }
        # Retry once with only the top 2 chunks.
        try:
            result = ask(
                build_judge_context(cites, chunks_by_id, chosen_ids,
                                    pinned_ids=pinned_ids, max_chunks=2)
            )
        except Exception as exc2:
            print(f"    judge call failed: {exc2}")
            return {
                "bullet": text,
                "status": "unverified",
                "reason": f"judge unavailable: {exc2}",
                "needs_human_review": True,
            }

    raw = result.get("supported", False)
    supported = raw if isinstance(raw, bool) else str(raw).strip().lower() in (
        "true", "yes", "1"
    )
    reason = str(result.get("reason", "")).strip()
    return {
        "bullet": text,
        "status": "supported" if supported else "not_supported",
        "reason": reason,
        "needs_human_review": False,
    }


def print_judge_report(judge_results):
    print("  Fact-check judge report:")
    for i, verdict in enumerate(judge_results, 1):
        status = verdict["status"]
        if status == "summary":
            print(f"    bullet {i}: summary (not fact-checked)")
            continue
        note = " (needs human review)" if status == "unverified" else ""
        print(f"    bullet {i}: {status}{note} - {verdict['reason']}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    model_name = os.getenv("GROQ_MODEL", "")
    print(f"Model: {model_name}")

    with open(INSIGHTS_JSON, encoding="utf-8") as fh:
        insights = json.load(fh)
    fee_issue = insights.get("fee_issue") or {}

    sources = load_sources()
    if not sources:
        raise SystemExit(
            "No knowledge/S*.txt files. Run src/build_knowledge.py first."
        )

    chunks = build_chunks(sources)
    chunks_by_id = {cid: (sid, text) for cid, sid, text in chunks}
    print(f"Loaded {len(sources)} sources, {len(chunks)} chunks")

    # ---- Retrieval ----
    query = " ".join(
        [
            str(fee_issue.get("fee_name", "")),
            str(fee_issue.get("what_confuses_them", "")),
            EXTRA_TERMS,
        ]
    )
    chosen_ids = retrieve(chunks, query)
    pinned_ids = pin_key_chunks(chunks, chosen_ids)
    print(f"Retrieved chunks: {', '.join(chosen_ids)}")
    if pinned_ids:
        print(f"Pinned S3 'levied' chunks: {', '.join(pinned_ids)}")

    # ---- LLM call, with up to MAX_REPAIRS retries of ALL checks ----
    user = build_user_prompt(fee_issue, chunks, chosen_ids)
    result = call_llm_from_analyze(user)
    bullets = result.get("bullets", [])

    passed_on = None
    judge_results = []

    for attempt in range(MAX_REPAIRS):
        problems = validate(bullets, sources)
        if not problems:
            # Code checks passed; now the LLM-as-judge fact check.
            judge_results = [
                judge_bullet(b, chunks_by_id, chosen_ids, pinned_ids)
                for b in bullets
            ]
            print_judge_report(judge_results)
            failing = [j for j in judge_results
                       if j["status"] == "not_supported"]
            if not failing:
                passed_on = attempt + 1
                break
            problems = [
                f"bullet \"{j['bullet'][:60]}...\": not supported - "
                f"{j['reason']}"
                for j in failing
            ]

        print(f"  validation failed (attempt {attempt + 1}):")
        for problem in problems:
            print(f"    - {problem}")
        repair = (
            "\nYour previous answer had problems:\n- "
            + "\n- ".join(problems)
            + "\nReturn a corrected FULL JSON with bullets that fix every "
            "problem. Re-check every claim against the chunks before "
            "answering. The title must stay exactly: " + FIXED_TITLE
        )
        result = call_llm_from_analyze(user + repair)
        bullets = result.get("bullets", [])
    else:
        # Still failing: drop the bad bullets, keep the good ones.
        print("  WARNING: validation still failing; dropping bad bullets")
        good = []
        for bullet in bullets:
            if not validate([bullet], sources):
                good.append(bullet)
        bullets = good[:MAX_BULLETS]
        # Judge whatever survived, so the report matches the saved output.
        judge_results = [
            judge_bullet(b, chunks_by_id, chosen_ids, pinned_ids)
            for b in bullets
        ]
        print_judge_report(judge_results)

    if passed_on is not None:
        print(f"  validation passed on attempt {passed_on}")

    # ---- Final-output guarantee ----
    # Never keep a fact bullet whose LAST judge verdict is not_supported.
    last_verdict = {j["bullet"]: j["status"] for j in judge_results}
    kept = []
    kept_judges = []
    for bullet in bullets:
        text = str(bullet.get("text", "")).strip()
        status = last_verdict.get(text)
        if bullet_type(bullet) != "summary" and status == "not_supported":
            print(f"  dropping not-supported bullet: \"{text[:60]}...\"")
            continue
        kept.append(bullet)
        if status is not None:
            kept_judges.append(
                next(j for j in judge_results if j["bullet"] == text)
            )
    dropped = len(bullets) - len(kept)
    bullets, judge_results = kept, kept_judges
    if dropped:
        print(f"  dropped {dropped} bullet(s) with not-supported verdict")

    n_fact = sum(1 for b in bullets if bullet_type(b) != "summary")
    is_draft = n_fact < MIN_FACT_BULLETS
    if is_draft:
        print(
            f"  WARNING: only {n_fact} fact bullet(s) remain "
            f"(need {MIN_FACT_BULLETS}); saving DRAFT only - "
            "NEEDS HUMAN REVIEW"
        )

    n_supported = sum(1 for j in judge_results
                      if j["status"] == "supported")
    n_not_supported = sum(1 for j in judge_results
                          if j["status"] == "not_supported")
    n_unverified = sum(1 for j in judge_results
                       if j["status"] == "unverified")
    n_summary = sum(1 for j in judge_results
                    if j["status"] == "summary")

    # Store only source ids (S1..S4) in the output, never chunk ids.
    for bullet in bullets:
        bullet["sources"] = [
            sid for sid in (normalise_source_id(c)
                            for c in bullet.get("sources", [])) if sid
        ]

    problems = validate(bullets, sources)
    number_check = not any("number" in p for p in problems)
    banned_check = not any("banned" in p for p in problems)

    # ---- Source links: the 2 most-cited official sources ----
    citation_counts = {}
    for bullet in bullets:
        for cid in bullet.get("sources", []):
            citation_counts[cid] = citation_counts.get(cid, 0) + 1

    official = [
        (cid, n) for cid, n in citation_counts.items()
        if cid in sources and any(
            d in sources[cid]["url"] for d in OFFICIAL_DOMAINS
        )
    ]
    official.sort(key=lambda pair: pair[1], reverse=True)
    link_ids = [cid for cid, _ in official[:2]]

    last_checked = next(
        (sources[cid]["fetched_on"] for cid in link_ids
         if sources[cid]["fetched_on"]),
        date.today().isoformat(),
    )

    # ---- Build the markdown ----
    md_lines = [f"## {FIXED_TITLE}", ""]
    if is_draft:
        md_lines.append(
            "> **NEEDS HUMAN REVIEW** - draft only; too few fact bullets "
            "survived validation."
        )
        md_lines.append("")
    md_lines.append(
        "_For support: reusable explanation for users confused about "
        "charges beyond brokerage._"
    )
    md_lines.append("")
    for bullet in bullets:
        md_lines.append(f"- {bullet['text']}")

    links = " · ".join(
        f"[{sources[cid]['title']}]({sources[cid]['url']})" for cid in link_ids
    )
    md_lines.append("")
    md_lines.append(f"**Sources:** {links}")
    md_lines.append(f"**Last checked:** {last_checked}")
    markdown = "\n".join(md_lines) + "\n"

    # ---- Outputs ----
    out_md = OUT_DRAFT_MD if is_draft else OUT_MD
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write(markdown)

    payload = {
        "identified_fee_issue": {
            "fee_name": fee_issue.get("fee_name", ""),
            "what_confuses_them": fee_issue.get("what_confuses_them", ""),
        },
        "explanation_bullets": [
            {
                "text": b["text"],
                "type": bullet_type(b),
                "sources": b.get("sources", []),
            }
            for b in bullets
        ],
        "source_links": [
            {"title": sources[cid]["title"], "url": sources[cid]["url"]}
            for cid in link_ids
        ],
        "fact_check": {
            "summary": {
                "supported": n_supported,
                "not_supported": n_not_supported,
                "unverified": n_unverified,
                "summary_bullets": n_summary,
            },
            "bullets": judge_results,
        },
        "needs_human_review": is_draft,
        "last_checked": last_checked,
        "all_sources": read_sources_md(),
    }
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print()
    print(markdown)
    print("Validation report (final saved bullets only):")
    print(f"  bullets: {len(bullets)} ({n_fact} fact, {n_summary} summary)")
    print(f"  number check passed: {number_check}")
    print(f"  banned words check passed: {banned_check}")
    print(
        f"  fact check (LLM judge): {n_supported} supported, "
        f"{n_not_supported} not supported, {n_unverified} unverified"
    )
    if is_draft:
        print(
            f"  WARNING: DRAFT only - NEEDS HUMAN REVIEW. "
            f"Saved to {OUT_DRAFT_MD} and {OUT_JSON} (not {OUT_MD})."
        )
    else:
        print(f"\nSaved to {OUT_MD} and {OUT_JSON}")


def call_llm_from_analyze(user, system=SYSTEM):
    """Thin wrapper so the model/env come from analyze_reviews."""
    import os
    from dotenv import load_dotenv
    load_dotenv()
    model = os.getenv("GROQ_MODEL")
    return call_llm(model, system, user, **PULSE_KWARGS)


def read_sources_md():
    """Return the raw text of knowledge/sources.md (or '')."""
    if not os.path.exists(SOURCES_MD):
        return ""
    with open(SOURCES_MD, encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    main()
