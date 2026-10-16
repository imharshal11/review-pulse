"""One-command pipeline for the weekly Review Pulse.

Runs every step as a subprocess of the same Python interpreter, in order:

    analyze_reviews.py -> generate_pulse.py -> [build_knowledge.py]
    -> fee_explainer.py -> mcp_check.py -> publish.py

Usage:
    python src/run_all.py [--input <csv>] [--skip-publish] [--refresh-sources]

    --input <csv>        validate a user CSV first (see src/validate_csv.py),
                         then analyse the cleaned copy at data/input_reviews.csv
    --skip-publish       stop after mcp_check.py; publish.py is never reached
                         (publishing stays interactive: type APPROVE)
    --refresh-sources    also re-fetch the knowledge sources with
                         src/build_knowledge.py before the fee explainer

The first failing step stops the run; every step prints a fix hint and the
exact command to re-run. Set PYTHONIOENCODING=utf-8 for all children so the
terminal output is safe on Windows.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
CLEANED_CSV = ROOT / "data" / "input_reviews.csv"
FEE_JSON = ROOT / "outputs" / "fee_explainer.json"

# What to do (and how to re-run) when a step fails.
HINTS = {
    "validate_csv.py": (
        "fix the reported CSV problems (columns, dates, ratings, too few "
        "reviews) and re-run"
    ),
    "analyze_reviews.py": (
        "check GROQ_API_KEY, GROQ_MODEL and GROQ_MODEL_FAST in .env and that "
        "the network can reach api.groq.com"
    ),
    "generate_pulse.py": (
        "it needs outputs/insights.json from analyze_reviews.py - finish a "
        "successful analysis first"
    ),
    "build_knowledge.py": (
        "a source page could not be fetched; copy it by hand into "
        "knowledge/manual/ as the script says, then re-run"
    ),
    "fee_explainer.py": (
        "it needs outputs/insights.json and the pages in knowledge/; read the "
        "reported fact-check reason, fix the sources, then re-run"
    ),
    "mcp_check.py": (
        "check N8N_MCP_URL and N8N_MCP_TOKEN in .env and that the n8n MCP "
        "server is reachable"
    ),
    "publish.py": (
        "publishing is interactive: type APPROVE to publish, anything else "
        "cancels; use --skip-publish to stop before it"
    ),
}


def rerun_command():
    """The exact command the user just ran, for the failure hints."""
    return "python " + " ".join(sys.argv)


def print_summary(results):
    """Final table of every step, its status and how long it took."""
    print()
    print("=" * 56)
    print("PIPELINE SUMMARY")
    print("=" * 56)
    width = max(len(row["step"]) for row in results)
    print(f"  {'step':<{width}}  {'status':<8}  {'time':>8}")
    for row in results:
        print(
            f"  {row['step']:<{width}}  {row['status']:<8}  "
            f"{row['seconds']:>7.1f}s"
        )
    print("=" * 56)


def run_step(script, env, results):
    """Run one script as a subprocess; records ✓/✗ and the elapsed time."""
    print()
    print(f"----- {script} " + "-" * max(0, 44 - len(script)))
    started = time.perf_counter()
    code = subprocess.call(
        [sys.executable, str(SRC / script)], cwd=str(ROOT), env=env
    )
    seconds = time.perf_counter() - started
    if code == 0:
        print(f"\n✓ {script} ({seconds:.1f}s)")
        results.append({"step": script, "status": "OK", "seconds": seconds})
        return True
    print(f"\n✗ {script} failed")
    results.append({"step": script, "status": "FAILED", "seconds": seconds})
    print(f"  Fix: {HINTS.get(script, 'read the output above')}.")
    print(f"  Then re-run: {rerun_command()}")
    return False


def validate_input(path, env, results):
    """Validate the user CSV, save the cleaned copy, point REVIEWS_CSV at it.

    Returns True on success; on failure the errors are printed and the
    pipeline must stop.
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from validate_csv import validate_reviews_csv

    print(f"----- validate_csv.py ({path}) " + "-" * 20)
    started = time.perf_counter()
    df, errors, warnings = validate_reviews_csv(path)
    seconds = time.perf_counter() - started

    for warning in warnings:
        print(f"  warning: {warning}")
    if errors or df is None:
        for error in errors:
            print(f"  error: {error}")
        print("\n✗ validate_csv.py failed")
        results.append(
            {"step": "validate_csv.py", "status": "FAILED", "seconds": seconds}
        )
        print(f"  Fix: {HINTS['validate_csv.py']}.")
        print(f"  Then re-run: {rerun_command()}")
        return False

    try:
        df.to_csv(CLEANED_CSV, index=False, encoding="utf-8")
    except OSError as exc:
        print(f"  error: cannot write {CLEANED_CSV}: {exc}")
        print("\n✗ validate_csv.py failed")
        results.append(
            {"step": "validate_csv.py", "status": "FAILED", "seconds": seconds}
        )
        print(f"  Then re-run: {rerun_command()}")
        return False

    print(f"\n✓ validate_csv.py ({seconds:.1f}s)")
    results.append({"step": "validate_csv.py", "status": "OK", "seconds": seconds})
    print(f"  Cleaned copy: data/input_reviews.csv ({len(df)} reviews)")
    env["REVIEWS_CSV"] = str(CLEANED_CSV)
    os.environ["REVIEWS_CSV"] = str(CLEANED_CSV)
    return True


def fee_needs_human_review():
    """True when outputs/fee_explainer.json still asks for a human review."""
    try:
        with open(FEE_JSON, encoding="utf-8") as fh:
            return bool(json.load(fh).get("needs_human_review"))
    except (OSError, json.JSONDecodeError):
        return False   # missing/broken: let the publish step report it


def _utf8_stdout():
    """Print ✓/✗ safely even on a cp1252 Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv=None):
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Run the whole Review Pulse pipeline in order."
    )
    parser.add_argument(
        "--input",
        metavar="CSV",
        help="user CSV to validate and analyse instead of data/reviews.csv",
    )
    parser.add_argument(
        "--skip-publish",
        action="store_true",
        help="stop after mcp_check.py; publish.py is never run",
    )
    parser.add_argument(
        "--refresh-sources",
        action="store_true",
        help="also re-fetch the knowledge sources (build_knowledge.py)",
    )
    args = parser.parse_args(argv)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    results = []

    if args.input and not validate_input(args.input, env, results):
        print_summary(results)
        return 1

    steps = ["analyze_reviews.py", "generate_pulse.py"]
    if args.refresh_sources:
        steps.append("build_knowledge.py")
    steps += ["fee_explainer.py", "mcp_check.py"]
    if not args.skip_publish:
        steps.append("publish.py")

    for script in steps:
        # The publish step stops here when the fee explainer still needs a
        # human; everything before it has already run successfully.
        if script == "publish.py" and fee_needs_human_review():
            print()
            print("----- publish.py " + "-" * 31)
            print(
                "✗ publish skipped: outputs/fee_explainer.json still has "
                "needs_human_review = true"
            )
            print("  Fix: review outputs/fee_explainer_DRAFT.md against the "
                  "sources, then re-run src/fee_explainer.py.")
            print(f"  Then re-run: {rerun_command()}")
            results.append(
                {"step": "publish.py", "status": "BLOCKED", "seconds": 0.0}
            )
            print_summary(results)
            return 1
        if not run_step(script, env, results):
            print_summary(results)
            return 1

    if args.skip_publish:
        results.append(
            {"step": "publish.py", "status": "SKIPPED", "seconds": 0.0}
        )
        print("\nPublish skipped (--skip-publish). Re-run without it to "
              "publish (typing APPROVE is still required).")

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
