"""Fetch public Groww reviews from the Google Play Store.

Downloads the last ~12 weeks of reviews, cleans them, saves the full cleaned
set to data/reviews_raw.csv and a week-balanced sample to data/reviews.csv.

Run with --resample to skip the Play Store entirely and just rebuild
data/reviews.csv from the existing data/reviews_raw.csv.
"""

import argparse
import re
import time
from datetime import date, timedelta

import pandas as pd
from google_play_scraper import Sort, reviews

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
APP_ID = "com.nextbillion.groww"
LANG = "en"
COUNTRY = "in"

BATCH_SIZE = 200          # reviews requested per API batch
LOOKBACK_DAYS = 84        # 12 weeks of history
MAX_REVIEWS = 60000       # safety cap so we never fetch forever
BATCH_PAUSE = 1.0         # seconds to sleep between batches
SAMPLE_PER_WEEK = 200     # max reviews kept per week in data/reviews.csv
RANDOM_STATE = 42

RAW_PATH = "data/reviews_raw.csv"
BALANCED_PATH = "data/reviews.csv"

# Columns we keep and how they are renamed (no user names / profile images).
COLUMN_RENAME = {
    "reviewId": "review_id",
    "at": "date",
    "score": "rating",
    "content": "text",
    "thumbsUpCount": "thumbs_up",
    "reviewCreatedVersion": "app_version",
}

# Keywords we count in the final summary (case-insensitive).
KEYWORDS = [
    "charge", "fee", "deducted", "exit load", "brokerage", "DP",
    "stamp duty", "hidden", "extra", "refund", "penalty", "mandate",
]


# ---------------------------------------------------------------------------
# 1. FETCH
# ---------------------------------------------------------------------------
def fetch_reviews():
    """Fetch reviews newest-first until they are older than the cutoff."""
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)

    collected = []
    token = None
    batch_no = 0
    oldest = None

    while len(collected) < MAX_REVIEWS:
        batch_no += 1

        # One batch of reviews; continuation_token resumes where we stopped.
        result, token = reviews(
            APP_ID,
            lang=LANG,
            country=COUNTRY,
            sort=Sort.NEWEST,
            count=BATCH_SIZE,
            continuation_token=token,
        )

        if not result:
            print(f"Batch {batch_no}: no more reviews returned.")
            break

        collected.extend(result)

        # Oldest date seen so far (reviews come back newest-first).
        dates = [r["at"].date() for r in result]
        batch_oldest = min(dates)
        oldest = batch_oldest if oldest is None else min(oldest, batch_oldest)

        print(
            f"Batch {batch_no:3d} | total fetched: {len(collected):6d} "
            f"| oldest date so far: {oldest}"
        )

        # Stop once we have gone past the 12-week window.
        if oldest <= cutoff:
            print(f"Reached {LOOKBACK_DAYS}-day cutoff ({cutoff}). Stopping.")
            break

        # Stop when the API has no further pages.
        if token is None:
            print("No continuation token left. Stopping.")
            break

        # Safety cap.
        if len(collected) >= MAX_REVIEWS:
            print(f"Hit safety cap of {MAX_REVIEWS} reviews. Stopping.")
            break

        # Be polite to the Play Store.
        time.sleep(BATCH_PAUSE)

    return collected


# ---------------------------------------------------------------------------
# 2. COLUMNS + week_start
# ---------------------------------------------------------------------------
def build_frame(records):
    """Turn raw records into a tidy DataFrame with only public review fields."""
    df = pd.DataFrame(records)
    df = df[list(COLUMN_RENAME.keys())].rename(columns=COLUMN_RENAME)

    # Date as YYYY-MM-DD.
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    # Monday of the week the review was posted (for weekly sampling).
    d = pd.to_datetime(df["date"])
    df["week_start"] = (d - pd.to_timedelta(d.dt.dayofweek, unit="D")).dt.strftime(
        "%Y-%m-%d"
    )

    return df


# ---------------------------------------------------------------------------
# 3. CLEAN
# ---------------------------------------------------------------------------
def clean(df):
    """Strip whitespace, drop empty/short texts and exact duplicates."""
    # Collapse newlines and extra spaces into single spaces.
    df = df.copy()
    df["text"] = (
        df["text"]
        .fillna("")
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    before = len(df)

    # Drop rows with empty text.
    df = df[df["text"] != ""]

    # Drop reviews with fewer than 5 words.
    df = df[df["text"].str.split().str.len() >= 5]

    # Drop exact duplicate texts (keep the first occurrence).
    df = df.drop_duplicates(subset="text", keep="first")

    print(f"Cleaning: {before} -> {len(df)} rows "
          f"(dropped {before - len(df)} empty/short/duplicate rows)")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. LOAD / SAVE RAW
# ---------------------------------------------------------------------------
def load_raw():
    """Load the already-cleaned reviews from data/reviews_raw.csv (--resample)."""
    df = pd.read_csv(RAW_PATH, encoding="utf-8", dtype={"text": str})
    print(f"Loaded {len(df)} reviews from {RAW_PATH} (no Play Store fetch).")
    return df


def save_raw(df):
    """Save every cleaned review."""
    df.to_csv(RAW_PATH, index=False, encoding="utf-8")
    print(f"Saved {len(df)} cleaned reviews to {RAW_PATH}")


# ---------------------------------------------------------------------------
# 4b. INCOMPLETE WEEK REMOVAL
# ---------------------------------------------------------------------------
def drop_incomplete_week(df):
    """Drop reviews from the week that has not finished yet (as of today)."""
    today = pd.Timestamp(date.today())
    # Monday of the current week (a week runs Mon -> Sun).
    current_week = (today - pd.to_timedelta(today.dayofweek, unit="D")).strftime(
        "%Y-%m-%d"
    )

    before = len(df)
    # week_start is YYYY-MM-DD, so string comparison is safe.
    df = df[df["week_start"] < current_week].reset_index(drop=True)

    removed = before - len(df)
    if removed:
        print(
            f"Removed {removed} reviews from the incomplete week "
            f"starting {current_week}."
        )
    else:
        print(f"No reviews found in the incomplete week starting {current_week}.")
    return df


# ---------------------------------------------------------------------------
# 5. BALANCED SAMPLE
# ---------------------------------------------------------------------------
def balanced_sample(df):
    """Take up to SAMPLE_PER_WEEK reviews per week (random, reproducible)."""
    parts = []
    for week, group in df.groupby("week_start"):
        if len(group) <= SAMPLE_PER_WEEK:
            parts.append(group)
        else:
            parts.append(
                group.sample(n=SAMPLE_PER_WEEK, random_state=RANDOM_STATE)
            )
    sample = pd.concat(parts).sort_values("date").reset_index(drop=True)
    sample.to_csv(BALANCED_PATH, index=False, encoding="utf-8")
    print(f"Saved {len(sample)} reviews to {BALANCED_PATH}")
    return sample


# ---------------------------------------------------------------------------
# 6. SUMMARY
# ---------------------------------------------------------------------------
def summarize(df):
    """Print coverage and content stats for the balanced file."""
    print("\n" + "=" * 60)
    print(f"SUMMARY: {BALANCED_PATH}")
    print("=" * 60)

    print(f"Total reviews: {len(df)}")

    dates = pd.to_datetime(df["date"])
    print(f"Date range: {dates.min().date()} to {dates.max().date()}")

    weeks = sorted(df["week_start"].unique())
    print(f"Weeks covered: {len(weeks)}")

    print("\nReviews per week:")
    per_week = df["week_start"].value_counts().sort_index()
    for week, n in per_week.items():
        print(f"  {week}: {n}")

    print("\nReviews per star rating:")
    ratings = df["rating"].value_counts().sort_index()
    for star in range(1, 6):
        print(f"  {star} star: {int(ratings.get(star, 0))}")

    print("\nKeyword counts (case-insensitive):")
    lowered = df["text"].str.lower()
    for kw in KEYWORDS:
        n = int(lowered.str.contains(re.escape(kw.lower()), regex=True).sum())
        print(f"  {kw}: {n}")

    if len(weeks) < 8:
        print("\n" + "!" * 60)
        print(
            f"WARNING: only {len(weeks)} week(s) covered - fewer than 8. "
            "The Play Store did not return enough history; widen the fetch "
            "or lower the per-week sample size."
        )
        print("!" * 60)

    print("=" * 60)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch Groww Play Store reviews (or resample without fetching)."
    )
    parser.add_argument(
        "--resample",
        action="store_true",
        help=f"do not contact the Play Store; rebuild {BALANCED_PATH} "
        f"from {RAW_PATH}",
    )
    args = parser.parse_args()

    if args.resample:
        # Resample-only: reuse the cleaned raw file, no fetching at all.
        df = load_raw()
    else:
        print(f"Fetching {APP_ID} reviews (last {LOOKBACK_DAYS} days)...")
        records = fetch_reviews()
        if not records:
            print("No reviews fetched.")
            return

        df = build_frame(records)
        df = clean(df)
        save_raw(df)

    # Both modes: drop the still-running week before sampling.
    df = drop_incomplete_week(df)

    sample = balanced_sample(df)
    summarize(sample)


if __name__ == "__main__":
    main()
