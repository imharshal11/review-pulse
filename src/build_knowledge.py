"""Fetch official Groww / NSE sources and save clean text to knowledge/.

Each source is saved to knowledge/<id>.txt with a small header block so the
fee explainer can cite it. If a fetch fails or returns almost nothing, the
script tells you exactly which page to copy by hand into
knowledge/manual/<id>.txt (which is then used instead of fetching).
"""

import os
import re
from datetime import date

# Use the OS trust store so corporate/self-signed roots are accepted.
import truststore
truststore.inject_into_ssl()

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
KNOW_DIR = "knowledge"
MANUAL_DIR = os.path.join(KNOW_DIR, "manual")
SOURCES_MD = os.path.join(KNOW_DIR, "sources.md")

MIN_CHARS = 300          # shorter than this = treat the fetch as failed
TIMEOUT = 30

# A normal browser User-Agent, otherwise many sites refuse the request.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

# (id, title, url)
SOURCES = [
    (
        "S1",
        "Groww — Stocks pricing",
        "https://groww.in/pricing/stocks",
    ),
    (
        "S2",
        "Groww — Futures & Options pricing",
        "https://915.groww.in/pricing",
    ),
    (
        "S3",
        "Groww Help — How much does Groww charge for stocks?",
        "https://groww.in/help/stocks%2C-f%26o%2C-ipo-%26-mtf/"
        "sx-demat-account/how-much-does-groww-charge-for-stocks-1",
    ),
    (
        "S4",
        "NSE circular NSE/FA/73061 — Revision in Transaction Charges "
        "(27 Feb 2026)",
        "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf",
    ),
]


# ---------------------------------------------------------------------------
# FETCH + CLEAN
# ---------------------------------------------------------------------------
def clean_html(html):
    """Strip scripts/styles/chrome and collapse whitespace."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def fetch_pdf(url):
    """Extract text from every page of a PDF."""
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()

    # pypdf wants a file-like object, not raw bytes.
    from io import BytesIO

    reader = PdfReader(BytesIO(response.content))
    pages = [page.extract_text() or "" for page in reader.pages]
    return re.sub(r"\s+", " ", " ".join(pages)).strip()


def fetch_source(url):
    """Fetch one source, choosing HTML or PDF by the response type."""
    if url.lower().endswith(".pdf"):
        return fetch_pdf(url)

    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return clean_html(response.text)


def header_block(source_id, title, url, fetched_on, note=None):
    """The small metadata block saved at the top of every source file."""
    lines = [
        f"id: {source_id}",
        f"title: {title}",
        f"url: {url}",
        f"fetched_on: {fetched_on}",
    ]
    if note:
        lines.append(f"note: {note}")
    return "\n".join(lines) + "\n\n"


def manual_path(source_id):
    return os.path.join(MANUAL_DIR, f"{source_id}.txt")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    os.makedirs(KNOW_DIR, exist_ok=True)
    os.makedirs(MANUAL_DIR, exist_ok=True)

    today = date.today().isoformat()
    results = []          # (id, title, url, chars, status)
    saved = {}            # id -> dict for sources.md

    for source_id, title, url in SOURCES:
        manual = manual_path(source_id)

        # A hand-copied file always wins over fetching.
        if os.path.exists(manual):
            with open(manual, encoding="utf-8") as fh:
                text = re.sub(r"\s+", " ", fh.read()).strip()
            body = header_block(source_id, title, url, today,
                                note="source: manual copy") + text
            with open(os.path.join(KNOW_DIR, f"{source_id}.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
            saved[source_id] = {"title": title, "url": url,
                                "fetched_on": today}
            results.append((source_id, len(text), "OK (manual)"))
            print(f"{source_id}: {len(text)} chars saved (manual copy)")
            continue

        # Otherwise fetch it.
        try:
            text = fetch_source(url)
        except Exception as exc:
            text = ""
            print(f"{source_id}: fetch failed ({type(exc).__name__}: {exc})")

        if len(text) < MIN_CHARS:
            print(
                f"MANUAL NEEDED for {source_id}: open {url} in a browser, "
                f"copy all page text, and paste it into {manual}"
            )
            results.append((source_id, len(text), "MANUAL NEEDED"))
            continue

        body = header_block(source_id, title, url, today) + text
        with open(os.path.join(KNOW_DIR, f"{source_id}.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(body)

        saved[source_id] = {"title": title, "url": url, "fetched_on": today}
        results.append((source_id, len(text), "OK"))
        print(f"{source_id}: {len(text)} chars saved")

    # ---- knowledge/sources.md ----
    with open(SOURCES_MD, "w", encoding="utf-8") as fh:
        fh.write("# Sources\n\n")
        fh.write("| id | title | url | fetched_on |\n")
        fh.write("| --- | --- | --- | --- |\n")
        for source_id, title, url in SOURCES:
            info = saved.get(source_id)
            fetched = info["fetched_on"] if info else "—"
            fh.write(f"| {source_id} | {title} | {url} | {fetched} |\n")
    print(f"\nWrote {SOURCES_MD}")

    # ---- Summary ----
    print("\nSummary:")
    for source_id, chars, status in results:
        print(f"  {source_id}: {chars} chars - {status}")


if __name__ == "__main__":
    main()
