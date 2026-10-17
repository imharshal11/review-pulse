import streamlit as st
import ui

ins = ui.insights()
ui.context_bar("Ready for approval" if ins else "No data yet", "ready" if ins else "empty")
ui.page_header("Analyse", "Insights", "AI groups reviews into themes. Code does every count, so the numbers can be checked.")
if not ins:
    ui.empty_state()
    st.stop()

ui.funnel_card()

themes = ui.themes_list(ins)
tops = ui.top_theme_names(ins)
order = tops + [t.get("name") for t in themes if t.get("name") not in tops]
rows = ""
for rank, name in enumerate(order, 1):
    t = ui.theme_by_name(ins, name)
    rate = ui.pct(t.get("problem_rate", (t.get("problem_count", 0) or 0) / max(t.get("count", 1) or 1, 1)))
    is_top = name in tops
    badge = (f'<span style="display:inline-flex;width:24px;height:24px;border-radius:7px;align-items:center;justify-content:center;font-size:12px;font-weight:700;'
             + ('background:#5B4CDB;color:#fff">' if is_top else 'border:1px solid #DAD8E8;color:#5E5B76">') + f'{rank}</span>')
    color = "#E04F35" if is_top else "#A6A3BF"
    rows += (f'<tr><td>{badge}</td><td style="font-weight:{700 if is_top else 400}">{ui.esc(name)}</td>'
             f'<td style="text-align:right">{ui.fmt_int(t.get("count"))}</td><td style="text-align:right">{ui.pct(t.get("share_pct", 0)):.1f}%</td>'
             f'<td style="text-align:right">{t.get("avg_rating", "—")}</td><td style="text-align:right;font-weight:700">{ui.fmt_int(t.get("problem_count"))}</td>'
             f'<td style="min-width:160px"><div style="display:flex;gap:8px;align-items:center"><div style="flex:1">{ui.bar(rate, color)}</div>'
             f'<span style="font-size:13px;width:46px;text-align:right">{rate:.1f}%</span></div></td></tr>')
rule = ins.get("ranking_rule") or "Top 3 themes ranked by number of 1–2★ reviews."
ui.html_block(f'<div class="kz-card"><div class="kz-row"><h3 style="margin:0;font-size:17px">All themes</h3><span class="kz-small">{len(themes)} themes · max 5 allowed</span></div>'
              f'<div class="kz-scroll" style="overflow-x:auto;margin-top:8px"><table class="kz-table"><thead><tr><th>Rank</th><th>Theme</th><th style="text-align:right">Reviews</th>'
              f'<th style="text-align:right">Share</th><th style="text-align:right">Avg ★</th><th style="text-align:right">1–2★</th><th>Problem rate</th></tr></thead>'
              f'<tbody>{rows}</tbody></table></div><div class="kz-card" style="background:#F7F6FF;box-shadow:none;margin:12px 0 0;padding:12px 14px;font-size:13px"><b>Ranking rule:</b> {ui.esc(rule)}</div></div>')

left, right = st.columns(2)
with left:
    cols = ""
    vals = [ui.trend_for(ins, n) for n in tops]
    mx = max([v for pair in vals for v in pair if v is not None] + [1])
    for n, (p, l) in zip(tops, vals):
        short = n.split(" ")[0]
        if p is None:
            cols += f'<div style="flex:1 1 120px"><div class="kz-small">{ui.esc(short)}</div><div class="kz-muted">No trend data</div></div>'
            continue
        change = f"{(l - p) / p * 100:+.0f}%" if p else ""
        tone = "#E04F35" if l > p else "#16875A"
        cols += (f'<div style="flex:1 1 120px;max-width:200px;min-width:0">'
                 f'<div style="display:flex;align-items:flex-end;justify-content:center;gap:10px;height:120px;border-bottom:1px solid #ECEBF5">'
                 f'<div style="width:34px;height:{p / mx * 100:.0f}%;background:#D9D4FF;border-radius:6px 6px 0 0"></div>'
                 f'<div style="width:34px;height:{l / mx * 100:.0f}%;background:#5B4CDB;border-radius:6px 6px 0 0"></div></div>'
                 f'<div style="font-size:13px;font-weight:700;margin-top:8px">{ui.esc(short)}</div>'
                 f'<div style="font-size:13px;color:{tone};font-weight:600">{p} &rarr; {l} ({change})</div></div>')
    ui.html_block(f'<div class="kz-card"><div class="kz-row"><h3 style="margin:0;font-size:17px">Trend &mdash; 1&ndash;2&#9733; reviews</h3>'
                  f'<span class="kz-small"><span style="color:#D9D4FF">&#9632;</span> prev 4 wks &nbsp;<span style="color:#5B4CDB">&#9632;</span> last 4 wks</span></div>'
                  f'<div style="display:flex;gap:14px;margin-top:14px;flex-wrap:wrap">{cols}</div></div>')

with right:
    fi = ins.get("fee_issue") or {}
    sc = fi.get("stance_counts") or {}
    total = sum(v for v in sc.values() if isinstance(v, (int, float))) or 1
    seg = [("complaint_only", "#5E5B76", "say charges are too high"), ("confused", "#E04F35", "don't understand a charge"),
           ("praise", "#16875A", "say charges are fine"), ("unrelated", "#CFCDE0", "not about Groww fees")]
    barhtml = "".join(f'<div style="width:{(sc.get(k, 0) or 0) / total * 100:.1f}%;background:{c}"></div>' for k, c, _ in seg)
    legend = "".join(f'<div style="font-size:14px"><b style="color:{c if k == "confused" else "#1B1A2E"}">{sc.get(k, 0)}</b> {lbl}</div>' for k, c, lbl in seg)
    pc = fi.get("pattern_counts") or {}
    top_pat = max(((k, v) for k, v in pc.items() if k not in ("none", "n/a")), key=lambda kv: kv[1], default=None)
    pat = f'{ui.esc(top_pat[0])} — <b>{top_pat[1]} of {sc.get("confused", "—")}</b>' if top_pat else "—"
    ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 12px;font-size:17px">{total} fee mentions — confused or complaining?</h3>'
                  f'<div style="display:flex;height:16px;border-radius:8px;overflow:hidden" role="img" aria-label="Fee mention breakdown">{barhtml}</div>'
                  f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin-top:12px">{legend}</div>'
                  f'<div style="border-top:1px solid #ECEBF5;margin-top:12px;padding-top:10px"><div class="kz-small" style="font-weight:600">Top confusion</div>'
                  f'<div style="font-size:14px;margin-top:4px">{pat}</div></div></div>')
