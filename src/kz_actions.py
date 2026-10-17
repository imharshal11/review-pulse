"""Kizuki UI actions: run pipeline scripts safely and record rejections."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"


def run_script(args: list[str], stdin_text: str | None = None, timeout: int = 1800) -> tuple[int, str]:
    """Run a project script with the same Python and return (exit_code, combined output)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run([sys.executable, *args], cwd=ROOT, input=stdin_text, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    except subprocess.TimeoutExpired:
        return 124, "Timed out. Nothing further was run."
    except Exception as e:  # never crash the UI
        return 1, f"Could not start the script: {type(e).__name__}"


def record_rejection(week_label: str, payload_hash: str, reason: str, note: str) -> dict:
    OUT.mkdir(exist_ok=True)
    entry = {"event": "rejected", "rejection_id": uuid.uuid4().hex[:8], "rejected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "week_label": week_label, "payload_sha256": payload_hash, "reason": reason, "note": note.strip()}
    with open(OUT / "rejections.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_rejections() -> list[dict]:
    p = OUT / "rejections.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows
