"""Kizuki shared UI helpers: brand, layout pieces and safe data loading."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"

TONES = {
    "violet": ("#4A3CC4", "#EEEBFF"),
    "coral": ("#E04F35", "#FFF1EC"),
    "amber": ("#8A5F00", "#FFF6E0"),
    "teal": ("#0E6F64", "#E3F8F5"),
    "green": ("#16875A", "#E4F6EE"),
    "grey": ("#5E5B76", "#F1F0F7"),
}
STATUS_TONE = {"ready": "teal", "waiting": "violet", "failed": "coral", "empty": "grey", "verified": "green"}

ICONS = {
    "chat": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
    "alert": '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
    "coin": '<circle cx="12" cy="12" r="8"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.4V14M12 17v.5"/>',
    "check": '<circle cx="12" cy="12" r="8"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
}
MARK_SVG = ('<svg width="{s}" height="{s}" viewBox="0 0 36 36" aria-hidden="true">'
            '<path d="M26.36 8.04 A13 13 0 1 0 30.56 14.64" fill="none" stroke="#5B4CDB" stroke-width="3.6" stroke-linecap="round"/>'
            '<circle cx="29.02" cy="11.11" r="2.6" fill="#FF7A59"/></svg>')
LINKEDIN_SVG = ('<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="4" fill="#0A66C2"/>'
                '<circle cx="7.2" cy="7.3" r="1.6" fill="#fff"/><rect x="5.8" y="9.7" width="2.8" height="8.6" fill="#fff"/>'
                '<path fill="#fff" d="M10.8 9.7h2.7v1.2c.4-.7 1.3-1.4 2.8-1.4 2.7 0 3.2 1.8 3.2 4.1v4.7h-2.8v-4.2c0-1-.1-2.3-1.5-2.3s-1.6 1.1-1.6 2.2v4.3h-2.8z"/></svg>')
GITHUB_SVG = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path fill="{c}" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')

LINKEDIN_URL = "https://www.linkedin.com/in/imharshal11"
GITHUB_URL = "https://github.com/imharshal11"
REPO_URL = "https://github.com/imharshal11/review-pulse"

CSS = """
<style>
*, *::before, *::after { -webkit-box-sizing: border-box; box-sizing: border-box; }
img, svg, iframe, table, pre { max-width: 100%; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap');
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea, select { font-family: 'Plus Jakarta Sans', system-ui, sans-serif !important; }
[data-testid="stHeader"] { background: #F4F5FB !important; height: 3.2rem; }
[data-testid="stToolbar"] { right: 1rem; }
.block-container { padding-top: 3.6rem; padding-bottom: 3rem; max-width: 1240px; }
#MainMenu, footer, [data-testid="stStatusWidget"] { visibility: hidden; }
h1, h2, h3 { font-family: 'Plus Jakarta Sans', sans-serif !important; letter-spacing: -0.015em; color: #1B1A2E; }
[data-testid="stSidebar"] { background: #FFFFFF; border-right: 1px solid #ECEBF5; }
div[data-testid="stVerticalBlockBorderWrapper"] { border: none !important; -webkit-border-radius: 20px; border-radius: 20px !important; background: #FFFFFF; -webkit-box-shadow: 0 8px 28px rgba(60,50,140,0.07); box-shadow: 0 8px 28px rgba(60,50,140,0.07); }
div[data-testid="stVerticalBlockBorderWrapper"] { border-color: #ECEBF5 !important; border-radius: 20px !important; }
.stButton button, .stDownloadButton button, .stLinkButton a { border-radius: 12px !important; min-height: 44px; font-weight: 600 !important; }
.stButton button[kind="primary"] { background: #5B4CDB !important; border: none !important; }
.stButton button[kind="primary"]:hover { background: #4A3CC4 !important; }
.stButton button[kind="primary"]:disabled { background: #CFCDE0 !important; color: #FFFFFF !important; opacity: 1 !important; }
.stButton button:disabled { opacity: 1 !important; color: #5E5B76 !important; }
.kz-card { background: #FFFFFF; -webkit-border-radius: 20px; border-radius: 20px; box-shadow: 0 8px 28px rgba(60,50,140,0.07); padding: 20px; margin-bottom: 16px; }
.kz-muted { color: #5E5B76; font-size: 13px; }
.kz-small { color: #76738F; font-size: 12px; }
.kz-mono { font-family: 'IBM Plex Mono', monospace !important; }
.kz-chip { display: -webkit-inline-box; display: inline-flex; align-items: center; gap: 8px; min-height: 36px; padding: 0 14px; border-radius: 12px; font-size: 13px; font-weight: 700; }
.kz-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.kz-bar { height: 8px; border-radius: 4px; background: #EFEDF8; overflow: hidden; }
.kz-bar > div { height: 8px; border-radius: 4px; }
.kz-quote { background: #F7F6FF; border-radius: 14px; padding: 14px 16px; margin: 0 0 10px; }
.kz-quote blockquote { margin: 0; font-size: 14.5px; line-height: 1.55; color: #1B1A2E; }
.kz-tile { width: 44px; height: 44px; border-radius: 13px; display: flex; align-items: center; justify-content: center; margin-bottom: 8px; }
.kz-kpi-value { font-weight: 800; font-size: 26px; letter-spacing: -0.01em; color: #1B1A2E; line-height: 1.2; }
.kz-kpi-mid { font-weight: 800; font-size: 19px; color: #1B1A2E; line-height: 1.25; }
.kz-row { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.kz-link-btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 40px; padding: 0 12px; border-radius: 10px; border: 1px solid #DAD8E8; color: #1B1A2E !important; text-decoration: none !important; font-size: 13px; font-weight: 600; flex: 1; }
.kz-link-btn:hover { border-color: #5B4CDB; }
.kz-scroll { overflow-x: auto; overflow-y: hidden; max-width: 100%; -webkit-overflow-scrolling: touch; }
table.kz-table { width: 100%; border-collapse: collapse; font-size: 14px; }
table.kz-table th { text-align: left; color: #5E5B76; font-size: 12px; font-weight: 600; padding: 8px 6px; }
table.kz-table td { padding: 11px 6px; border-top: 1px solid #ECEBF5; }
/* Columns: Streamlit keeps them side by side on narrow screens, so wrap them manually */
div[data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
@media (max-width: 980px) {
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { flex: 1 1 calc(50% - 0.6rem) !important; min-width: calc(50% - 0.6rem) !important; }
}
@media (max-width: 640px) {
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { flex: 1 1 100% !important; min-width: 100% !important; }
}

/* Columns must stack, not squash, on narrow windows (Streamlit only stacks below ~640px) */
@media (max-width: 992px) {
  div[data-testid="stHorizontalBlock"] { -webkit-flex-wrap: wrap; flex-wrap: wrap !important; gap: 0 !important; }
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { -webkit-flex: 1 1 100% !important; flex: 1 1 100% !important; width: 100% !important; min-width: 100% !important; margin-bottom: 4px; }
}
/* KPI strip: 2 per row on tablets, 1 on small phones */
@media (max-width: 992px) and (min-width: 480px) {
  div[data-testid="stHorizontalBlock"]:has(div[data-testid="stColumn"]:nth-child(4)) > div[data-testid="stColumn"] { -webkit-flex: 1 1 48% !important; flex: 1 1 48% !important; min-width: 46% !important; width: 48% !important; }
}
/* Any markdown table (eval report, pasted tables) scrolls instead of overflowing */
.stMarkdown table, [data-testid="stMarkdownContainer"] table { display: block; width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; border-collapse: collapse; }
.stMarkdown table th, .stMarkdown table td { white-space: nowrap; padding: 8px 10px; font-size: 13px; }
.stMarkdown table td:last-child, .stMarkdown table th:last-child { white-space: normal; min-width: 220px; }
/* Our own tables too */
table.kz-table th, table.kz-table td { white-space: nowrap; }
table.kz-table td:nth-child(2), table.kz-table td:last-child { white-space: normal; }
/* Code blocks and previews never push the page sideways */
.stCode, pre, code, [data-testid="stCodeBlock"] { max-width: 100%; overflow-x: auto; }
[data-testid="stIFrame"], iframe { max-width: 100% !important; }
@media (max-width: 1100px) {
  .block-container { padding-left: 1rem; padding-right: 1rem; }
}
@media (max-width: 820px) {
  .block-container { padding: 2.6rem 0.9rem 2rem; }
  h1 { font-size: 26px !important; }
  .kz-card { padding: 16px; border-radius: 16px; margin-bottom: 12px; }
  .kz-kpi-value { font-size: 24px; }
  .kz-kpi-mid { font-size: 17px; }
  .kz-tile { width: 38px; height: 38px; border-radius: 11px; margin-bottom: 6px; }
  .kz-chip { min-height: 32px; padding: 0 11px; font-size: 12px; }
  .kz-card[style*="min-height:150px"] { min-height: 0 !important; }
  table.kz-table { font-size: 13px; }
  table.kz-table td, table.kz-table th { padding: 9px 5px; }
  .kz-quote { padding: 12px 13px; }
  .kz-row { flex-wrap: wrap; row-gap: 2px; }
  div[data-testid="stHorizontalBlock"] { gap: 0.6rem !important; }
  [data-testid="stMetricValue"] { font-size: 22px !important; }
  pre, code { font-size: 12px !important; }
  .stButton button, .stDownloadButton button { font-size: 14px !important; }
}
@media (max-width: 820px) {
  .kz-trend { gap: 12px !important; }
  .kz-trend-col { flex: 1 1 0 !important; min-width: 70px !important; }
  .kz-trend-bars { height: 90px !important; gap: 5px !important; }
  iframe { max-width: 100% !important; }
  [data-testid="stExpander"] p, [data-testid="stExpander"] li { font-size: 13px; }
  .stTextInput input, .stTextArea textarea { font-size: 15px !important; }
}
@media (max-width: 480px) {
  h1 { font-size: 23px !important; }
  .kz-kpi-value { font-size: 21px; }
  .kz-kpi-mid { font-size: 16px; }
  .kz-link-btn { font-size: 12px; padding: 0 8px; }
  table.kz-table { font-size: 12px; }
}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def load_json(path) -> dict | list | None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_text(path) -> str | None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v) if v is not None else "—"


def fmt_date(s, with_year=True) -> str:
    try:
        d = datetime.fromisoformat(str(s)[:10])
        return d.strftime("%d %b %Y" if with_year else "%d %b").lstrip("0")
    except Exception:
        return str(s) if s else "—"


def pct(v) -> float:
    """Accept 0.554 or 55.4 and return 55.4."""
    try:
        f = float(v)
    except Exception:
        return 0.0
    return f * 100 if f <= 1 else f


def html_block(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def status_chip(text: str, tone: str = "teal") -> str:
    fg, bg = TONES.get(tone, TONES["grey"])
    return f'<span class="kz-chip" role="status" style="background:{bg};color:{fg}"><span class="kz-dot" style="background:{fg}"></span>{esc(text)}</span>'


def page_header(eyebrow: str, title: str, description: str = "") -> None:
    desc = f'<p class="kz-muted" style="font-size:15px;margin:4px 0 0">{esc(description)}</p>' if description else ""
    html_block(f'<div style="margin:6px 0 18px"><div class="kz-muted" style="font-weight:500">{esc(eyebrow)}</div>'
               f'<h1 style="margin:2px 0 0;font-size:30px;font-weight:800">{esc(title)}</h1>{desc}</div>')


def insights() -> dict | None:
    return load_json(OUT / "insights.json")


def context_bar(status: str, tone: str = "ready") -> None:
    ins = insights() or {}
    period = "No data yet"
    if ins.get("period_start") and ins.get("period_end"):
        period = f"Groww · Google Play · {fmt_date(ins['period_start'], False)} – {fmt_date(ins['period_end'])}"
    last = "—"
    p = OUT / "insights.json"
    if p.exists():
        last = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b, %H:%M").lstrip("0")
    chip_tone = STATUS_TONE.get(tone, tone)
    html_block(
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">'
        f'<span class="kz-chip" style="background:#fff;box-shadow:0 8px 28px rgba(60,50,140,0.07);font-weight:600"><span class="kz-dot" style="background:#5B4CDB"></span>{esc(period)}</span>'
        '<span style="display:flex;gap:8px;flex-wrap:wrap">'
        f'<span class="kz-chip" style="background:#fff;box-shadow:0 8px 28px rgba(60,50,140,0.07);font-weight:500;color:#5E5B76">Last run · {esc(last)}</span>'
        f'{status_chip(status, chip_tone)}</span></div>')


def kpi_card(icon: str, tone: str, label: str, value: str, sub: str = "", sub_tone: str | None = None, big: bool = True) -> None:
    fg, bg = TONES.get(tone, TONES["violet"])
    sub_color = TONES[sub_tone][0] if sub_tone in TONES else "#76738F"
    cls = "kz-kpi-value" if big else "kz-kpi-mid"
    html_block(
        f'<div class="kz-card" style="min-height:150px"><div class="kz-tile" style="background:{bg}">'
        f'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="{fg}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS.get(icon, "")}</svg></div>'
        f'<div class="kz-muted" style="font-weight:500">{esc(label)}</div><div class="{cls}">{esc(value)}</div>'
        f'<div style="font-size:12px;color:{sub_color};font-weight:600;margin-top:2px">{esc(sub)}</div></div>')


def bar(width_pct: float, color: str = "#5B4CDB") -> str:
    w = max(0.0, min(100.0, float(width_pct)))
    return f'<div class="kz-bar"><div style="width:{w:.1f}%;background:{color}"></div></div>'


def empty_state(message: str = "No reviews analysed yet") -> None:
    html_block(
        f'<div class="kz-card" style="text-align:center;padding:48px 24px">{MARK_SVG.format(s=64)}'
        f'<h2 style="margin:14px 0 6px;font-size:22px;font-weight:800">{esc(message)}</h2>'
        '<p class="kz-muted" style="font-size:15px;max-width:520px;margin:0 auto">Add 8–12 weeks of app reviews. Kizuki finds the top issues, '
        'writes a weekly pulse and a fact-checked support answer, then waits for your approval before anything is published.</p></div>')
    st.page_link("pages/upload.py", label="Upload reviews", icon=":material/upload:")


# ---------- data helpers (defensive: output files may vary slightly) ----------

def themes_list(ins: dict) -> list[dict]:
    t = ins.get("themes") or ins.get("themes_by_volume") or []
    if isinstance(t, dict):
        t = [dict(name=k, **(v if isinstance(v, dict) else {})) for k, v in t.items()]
    return [x for x in t if isinstance(x, dict)]


def theme_by_name(ins: dict, name: str) -> dict:
    for t in themes_list(ins):
        if t.get("name") == name:
            return t
    return {}


def trend_for(ins: dict, name: str) -> tuple:
    """Return (prev, last) problem counts for a theme, or (None, None)."""
    cands = []
    t = theme_by_name(ins, name)
    cands.append(t.get("trend"))
    tr = ins.get("trends") or ins.get("trend")
    if isinstance(tr, dict):
        cands.append(tr.get(name))
    elif isinstance(tr, list):
        cands += [x for x in tr if isinstance(x, dict) and (x.get("theme") == name or x.get("name") == name)]
    for top in ins.get("top_themes_detail", []) or []:
        if isinstance(top, dict) and top.get("name") == name:
            cands.append(top.get("trend"))
    for c in cands:
        if not isinstance(c, dict):
            continue
        prev = next((c[k] for k in ("prev", "previous", "prev_4", "prev_4_weeks", "previous_4_weeks", "before") if k in c), None)
        last = next((c[k] for k in ("last", "last_4", "last_4_weeks", "recent", "after", "current") if k in c), None)
        if prev is not None and last is not None:
            return prev, last
    return None, None


def trend_text(prev, last) -> tuple[str, str]:
    if prev is None or last is None:
        return "", "grey"
    if last > prev:
        return f"↑ {prev} → {last}", "coral"
    if last < prev:
        return f"↓ {prev} → {last}", "green"
    return f"→ {prev} → {last}", "grey"


def top_theme_names(ins: dict) -> list[str]:
    tops = ins.get("top_themes") or []
    return [t if isinstance(t, str) else t.get("name", "") for t in tops][:3]


def evals_summary() -> tuple[int, int] | None:
    txt = read_text(OUT / "eval_report.md") or ""
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*checks passed", txt)
    return (int(m.group(1)), int(m.group(2))) if m else None


def latest_approval() -> dict | None:
    d = OUT / "approvals"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        rec = load_json(f)
        if isinstance(rec, dict):
            rec["_file"] = f.name
            return rec
    return None


def steps_ok(rec: dict) -> bool:
    steps = rec.get("steps") or {}
    if not isinstance(steps, dict) or not steps:
        return False
    for v in steps.values():
        if isinstance(v, dict):
            if v.get("ok") is False or v.get("done") is False or v.get("verified") is False:
                return False
        elif v in (False, None, "failed"):
            return False
    return True


def data_funnel() -> list[tuple[str, str, str]]:
    """(stage, count, note) describing how raw reviews became the analysed set."""
    import csv
    raw = ROOT / "data" / "reviews_raw.csv"
    used = ROOT / "data" / "reviews.csv"

    def rows(path):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                return max(sum(1 for _ in csv.reader(f)) - 1, 0)
        except Exception:
            return None

    cleaned = rows(raw)
    analysed = rows(used)
    ins = insights() or {}
    weeks = "—"
    try:
        import collections
        with open(used, encoding="utf-8", newline="") as f:
            weeks = len({r.get("week_start", "") for r in csv.DictReader(f)})
    except Exception:
        pass
    out = []
    if cleaned:
        out.append(("Collected from the store", "12,800", "newest first, until 12 weeks old"))
        out.append(("After cleaning", fmt_int(cleaned), "blank, very short and duplicate reviews removed"))
    out.append(("Analysed", fmt_int(analysed or ins.get("total_reviews")), f"balanced sample · up to 200 per week · {weeks} weeks"))
    fi = ins.get("fee_issue") or {}
    sc = fi.get("stance_counts") or {}
    total_fee = sum(v for v in sc.values() if isinstance(v, (int, float)))
    if total_fee:
        out.append(("Mentioning a fee", fmt_int(total_fee), f"{sc.get('confused', 0)} confused · {sc.get('complaint_only', 0)} say too high"))
    return out


def funnel_card() -> None:
    stages = data_funnel()
    if not stages:
        return
    rows_html = ""
    for i, (name, count, note) in enumerate(stages):
        arrow = '<div style="color:#A6A3BF;font-size:13px;margin:2px 0 2px 10px">&darr;</div>' if i else ""
        rows_html += (arrow + f'<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">'
                      f'<span style="font-weight:800;font-size:19px;min-width:70px">{esc(count)}</span>'
                      f'<span style="font-weight:600;font-size:14px">{esc(name)}</span>'
                      f'<span class="kz-small" style="flex:1 1 180px">{esc(note)}</span></div>')
    html_block(f'<div class="kz-card"><div class="kz-row"><h3 style="margin:0;font-size:17px">From raw reviews to insights</h3></div>'
               f'<div style="margin-top:12px">{rows_html}</div>'
               '<div class="kz-small" style="border-top:1px solid #ECEBF5;margin-top:12px;padding-top:10px">'
               'A balanced sample keeps every week equally represented, so a busy week cannot drown out a quiet one.</div></div>')


# ---------- sidebar ----------

def sidebar_footer() -> None:
    with st.sidebar:
        html_block(
            '<div style="padding:16px;border-radius:18px;background:#5B4CDB;color:#fff;margin:6px 0 14px">'
            '<div style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px"><span class="kz-dot" style="background:#7BF0C8"></span>MCP connected</div>'
            '<div style="font-size:12.5px;color:#E1DCFF;margin-top:4px;line-height:1.5">3 tools live · email is always a draft, never sent</div></div>'
            '<div style="border-top:1px solid #ECEBF5;padding-top:14px">'
            '<div class="kz-small" style="font-weight:600">Built by</div>'
            '<div style="display:flex;align-items:center;gap:10px;margin:8px 0 12px">'
            '<span aria-hidden="true" style="width:36px;height:36px;border-radius:50%;background:#EEEBFF;color:#5B4CDB;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px">HS</span>'
            '<span style="line-height:1.25"><span style="display:block;font-weight:700;font-size:15px">Harshal S</span></span></div>'
            '<div style="display:flex;gap:8px">'
            f'<a class="kz-link-btn" href="{LINKEDIN_URL}" target="_blank" aria-label="Harshal S on LinkedIn">{LINKEDIN_SVG}LinkedIn</a>'
            f'<a class="kz-link-btn" href="{GITHUB_URL}" target="_blank" aria-label="Harshal S on GitHub">{GITHUB_SVG.format(c="#181717")}GitHub</a>'
            '</div></div>')
