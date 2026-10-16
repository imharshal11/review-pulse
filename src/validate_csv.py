"""CSV intake checker for user-supplied review files.

Validates and cleans a reviews CSV in the SAME format as data/reviews.csv
(required columns: text, date, rating; optional: review_id, thumbs_up,
app_version, week_start) so it can be fed into the analysis pipeline.

The public entry point is validate_reviews_csv(path), which never raises:
it always returns (clean_df or None, errors, warnings).

Usage:
  python src/validate_csv.py <file.csv>
"""

import os
import re
import sys

import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
REQUIRED_COLUMNS = ("text", "date", "rating")
OPTIONAL_COLUMNS = ("review_id", "thumbs_up", "app_version", "week_start")

# Any column whose name contains one of these is dropped (privacy).
PRIVATE_KEYWORDS = ("name", "user", "email", "phone", "image", "avatar")

READ_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")

WINDOW_WEEKS = 12     # keep only the most recent weeks
MIN_WEEKS_WARN = 8    # warn when fewer weeks than this remain
MIN_REVIEWS = 100     # fail when fewer cleaned reviews than this remain
MIN_WORDS = 5         # reviews shorter than this are dropped

# Values that count as "no text / no id" in a user CSV.
BLANK_TEXT = ("", "nan", "none", "<na>", "null", "n/a")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?(?:\d[\s.-]?){9}\d(?!\d)")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def _norm(column):
    """Canonical column key: lower-case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(column).lower())


def _is_private(column):
    """True when a column name looks like it holds personal data."""
    text = str(column).lower()
    norm = _norm(column)
    return any(word in text or word in norm for word in PRIVATE_KEYWORDS)


def _read_csv(path, warnings):
    """Read the CSV, trying utf-8, utf-8-sig then latin-1."""
    last = None
    for encoding in READ_ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=encoding, dtype={"text": str})
        except Exception as exc:
            last = exc
            continue
        if encoding != "utf-8":
            warnings.append(f"file is not valid UTF-8; read it as {encoding}")
        return df
    raise RuntimeError(
        f"could not be read with any of {', '.join(READ_ENCODINGS)} "
        f"({type(last).__name__}: {last})"
    )


def _parse_dates(series):
    """Parse a date column; unparseable values become NaT."""
    try:
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    except (TypeError, ValueError):   # pandas < 2 has no format="mixed"
        parsed = pd.to_datetime(series, errors="coerce")
    missing = parsed.isna()
    if missing.any():                 # second pass for day-first formats
        try:
            retry = pd.to_datetime(
                series[missing], errors="coerce", dayfirst=True
            )
            parsed[missing] = retry
        except Exception:
            pass
    return parsed


def _blank_mask(series):
    """Boolean mask of values that are missing or only whitespace."""
    text = series.astype(str).str.strip()
    return series.isna() | text.str.lower().isin(BLANK_TEXT)


# ---------------------------------------------------------------------------
# MAIN VALIDATOR
# ---------------------------------------------------------------------------
def _validate(path, errors, warnings):
    if not str(path or "").strip():
        errors.append("no file path given")
        return None
    path = str(path).strip()
    if not os.path.isfile(path):
        errors.append(f"file not found: {path}")
        return None
    if os.path.getsize(path) == 0:
        errors.append(f"{path} is empty (0 bytes)")
        return None

    try:
        df = _read_csv(path, warnings)
    except Exception as exc:
        errors.append(str(exc))
        return None
    if df.empty:
        errors.append(f"{path} has a header but no data rows")
        return None

    # ---- required columns (case-insensitive, spaces ignored) ----
    found = {_norm(col): col for col in df.columns}
    missing = [col for col in REQUIRED_COLUMNS if _norm(col) not in found]
    if missing:
        errors.append("missing required column(s): " + ", ".join(missing))
        return None

    rename = {}
    for col in list(df.columns):
        target = next(
            (name for name in REQUIRED_COLUMNS + OPTIONAL_COLUMNS
             if _norm(name) == _norm(col)),
            None,
        )
        if target:
            rename[col] = target
    df = df.rename(columns=rename)

    # ---- privacy: drop personal-data columns ----
    private = [
        col for col in df.columns
        if col not in REQUIRED_COLUMNS and _is_private(col)
    ]
    if private:
        df = df.drop(columns=private)
        warnings.append("dropped private column(s): " + ", ".join(private))

    # ---- dates ----
    parsed = _parse_dates(df["date"])
    bad_dates = int(parsed.isna().sum())
    if bad_dates:
        warnings.append(f"dropped {bad_dates} row(s) with an unparseable date")
    df = df.assign(date=parsed).dropna(subset=["date"]).reset_index(drop=True)
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    # ---- ratings: integers 1-5 only ----
    rating = pd.to_numeric(df["rating"], errors="coerce")
    good_rating = (
        rating.notna() & (rating % 1 == 0) & rating.between(1, 5)
    )
    bad_ratings = int((~good_rating).sum())
    if bad_ratings:
        warnings.append(
            f"dropped {bad_ratings} row(s) with a rating that is not 1-5"
        )
    df = df[good_rating].copy()
    df["rating"] = rating[good_rating].astype(int)

    # ---- text: drop blank rows ----
    text = df["text"].fillna("").astype(str).str.strip()
    has_text = ~text.str.lower().isin(BLANK_TEXT)
    blanks = int((~has_text).sum())
    if blanks:
        warnings.append(f"dropped {blanks} row(s) with empty text")
    df = df[has_text].copy()
    df["text"] = text[has_text]

    # ---- exact duplicate texts ----
    dupes = df.duplicated(subset=["text"], keep="first")
    if dupes.any():
        warnings.append(f"dropped {int(dupes.sum())} exact duplicate review(s)")
        df = df[~dupes].copy()

    # ---- very short reviews ----
    words = df["text"].str.split().str.len()
    long_enough = words >= MIN_WORDS
    if (~long_enough).any():
        warnings.append(
            f"dropped {int((~long_enough).sum())} review(s) with fewer "
            f"than {MIN_WORDS} words"
        )
    df = df[long_enough].copy()

    # ---- privacy: mask emails and phone numbers inside the text ----
    masked = (
        df["text"]
        .str.replace(EMAIL_RE, "[email]", regex=True)
        .str.replace(PHONE_RE, "[phone]", regex=True)
    )
    changed = int((masked != df["text"]).sum())
    if changed:
        warnings.append(
            f"masked an email address or phone number in {changed} review(s)"
        )
    df["text"] = masked

    df = df.reset_index(drop=True)

    # ---- fill review_id / thumbs_up / app_version; compute week_start ----
    if "review_id" not in df.columns:
        df["review_id"] = [f"u-{i}" for i in range(len(df))]
    else:
        ids = df["review_id"]
        no_id = _blank_mask(ids)
        if no_id.any():
            # .where (not .loc) because an all-blank column is float64 and
            # cannot take string values by position.
            made_up = pd.Series(
                [f"u-{i}" for i in df.index], index=df.index, dtype="object"
            )
            df["review_id"] = ids.astype("object").where(~no_id, made_up)
            warnings.append(
                f"filled {int(no_id.sum())} missing review_id value(s)"
            )

    if "thumbs_up" not in df.columns:
        df["thumbs_up"] = 0
    else:
        thumbs = pd.to_numeric(df["thumbs_up"], errors="coerce")
        no_thumbs = int(thumbs.isna().sum())
        if no_thumbs:
            warnings.append(f"filled {no_thumbs} missing thumbs_up value(s) with 0")
        df["thumbs_up"] = thumbs.fillna(0).astype(int)

    if "app_version" not in df.columns:
        df["app_version"] = ""
        warnings.append("added an empty app_version column")

    parsed = pd.to_datetime(df["date"])
    df["week_start"] = (
        (parsed - pd.to_timedelta(parsed.dt.dayofweek, unit="D"))
        .dt.strftime("%Y-%m-%d")
    )

    # ---- time window: the latest WINDOW_WEEKS weeks only ----
    weeks = sorted(df["week_start"].unique())
    if len(weeks) > WINDOW_WEEKS:
        recent = set(weeks[-WINDOW_WEEKS:])
        outside = ~df["week_start"].isin(recent)
        if outside.any():
            warnings.append(
                f"dropped {int(outside.sum())} review(s) older than the "
                f"latest {WINDOW_WEEKS} weeks"
            )
        df = df[~outside].reset_index(drop=True)
    weeks_left = df["week_start"].nunique()
    if weeks_left < MIN_WEEKS_WARN:
        warnings.append(
            f"only {weeks_left} week(s) of reviews remain "
            f"(fewer than {MIN_WEEKS_WARN})"
        )

    # ---- minimum size ----
    if len(df) < MIN_REVIEWS:
        errors.append(
            f"only {len(df)} reviews remain after cleaning "
            f"(minimum {MIN_REVIEWS})"
        )
        return None
    return df


def validate_reviews_csv(path):
    """Validate and clean a reviews CSV. Never raises.

    Returns (clean_df or None, errors, warnings). clean_df is None whenever
    errors is non-empty; warnings never stop the file from being accepted.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        df = _validate(path, errors, warnings)
    except Exception as exc:   # this API must never raise
        df = None
        errors.append(f"unexpected {type(exc).__name__}: {exc}")
    if errors:
        return None, errors, warnings
    return df, errors, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("Usage: python src/validate_csv.py <file.csv>")
        return 2

    path = argv[0]
    print("=" * 60)
    print("REVIEW CSV INTAKE CHECK")
    print("=" * 60)
    print(f"File: {path}")

    df, errors, warnings = validate_reviews_csv(path)

    if errors:
        print("\nErrors:")
        for error in errors:
            print(f"  - {error}")
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")
    if df is not None:
        print(
            f"\nKept: {len(df)} review(s) | "
            f"{df['week_start'].nunique()} week(s) | "
            f"ratings {df['rating'].min()}-{df['rating'].max()}"
        )

    print("-" * 60)
    if errors:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
