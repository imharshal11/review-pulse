import streamlit as st
import ui

ins = ui.insights()
evals = ui.evals_summary()
fee = ui.load_json(ui.OUT / "fee_explainer.json") or {}
pulse = ui.load_json(ui.OUT / "pulse.json") or {}
appr = ui.latest_approval()

if not ins:
    ui.context_bar("No data yet", "empty")
    ui.page_header("Groww · Google Play", "Overview")
    ui.empty_state()
    st.stop()

all_pass = bool(evals and evals[0] == evals[1])
ui.context_bar("Ready for approval" if all_pass and not fee.get("needs_human_review") else "Needs review", "ready" if all_pass else "waiting")
ui.page_header(f"Week ending {ui.fmt_date(ins.get('period_end'))}", "Welcome back, Harshal",
               "What Groww users are telling you this week, and what is ready to publish.")

if all_pass:
    ui.html_block(f'<div class="kz-card" style="background:#EEEBFF;box-shadow:none;padding:14px 18px"><b>Ready for approval.</b> '
                  f'All {evals[1]} automated checks passed and every fee fact is supported by an official source.</div>')

tops = ui.top_theme_names(ins)
top1 = tops[0] if tops else "—"
p1, l1 = ui.trend_for(ins, top1)
t1 = ui.theme_by_name(ins, top1)
fi = ins.get("fee_issue") or {}
sc = fi.get("stance_counts") or {}

c = st.columns(4)  # Streamlit stacks these on narrow screens
with c[0]:
    ui.kpi_card("chat", "violet", "Reviews analysed", ui.fmt_int(ins.get("total_reviews")), "Balanced · up to 200 per week")
with c[1]:
    sub = f"{ui.fmt_int(t1.get('problem_count'))} complaints"
    tt, tone = ui.trend_text(p1, l1)
    ui.kpi_card("alert", "coral", "Top issue", top1, f"{sub} · {tt}" if tt else sub, "coral", big=False)
with c[2]:
    ui.kpi_card("coin", "amber", "Fee confusion", "Charges beyond brokerage",
                f"{sc.get('confused', '—')} confused · {sc.get('complaint_only', '—')} say too high", big=False)
with c[3]:
    if appr:
        ok = ui.steps_ok(appr)
        ui.kpi_card("check", "teal", "Last publish", "Verified" if ok else "Incomplete",
                    f"{str(appr.get('approval_id', ''))[:8]} · {ui.fmt_date(appr.get('approved_at'), False)}", "green" if ok else "coral", big=False)
    else:
        ui.kpi_card("check", "teal", "Last publish", "Not yet", "Waiting for your approval", big=False)

left, right = st.columns(2)
with left:
    rows = ""
    maxp = max([ui.theme_by_name(ins, n).get("problem_count", 0) or 0 for n in tops] + [1])
    for i, n in enumerate(tops, 1):
        t = ui.theme_by_name(ins, n)
        pc = t.get("problem_count", 0) or 0
        tt, tone = ui.trend_text(*ui.trend_for(ins, n))
        rows += (f'<div style="margin:14px 0"><div class="kz-row" style="font-size:14px"><b>{i}. {ui.esc(n)}</b>'
                 f'<span><b>{pc}</b> <span style="color:{ui.TONES[tone][0]};font-weight:600">{ui.esc(tt)}</span></span></div>'
                 f'<div style="margin-top:6px">{ui.bar(pc / maxp * 100)}</div></div>')
    ui.html_block(f'<div class="kz-card"><div class="kz-row"><h3 style="margin:0;font-size:17px">Top 3 issues</h3></div>{rows}'
                  '<div class="kz-small" style="border-top:1px solid #ECEBF5;padding-top:10px">Ranked by 1–2★ reviews · trend compares the previous 4 weeks with the last 4</div></div>')
with right:
    fc = (fee.get("fact_check") or {}).get("summary") or {}
    steps = [
        ("Reviews collected & cleaned", f"{ui.fmt_int(ins.get('total_reviews'))} kept", True),
        ("Themes found & counted", f"{len(ui.themes_list(ins))} themes", True),
        ("Weekly pulse written", f"{pulse.get('word_count', '—')} / 250 words", bool(pulse)),
        ("Fee explainer fact-checked", f"{fc.get('supported', '—')} supported", bool(fee) and not fee.get("needs_human_review")),
        ("Human approval", "Done" if appr else "Waiting for you", bool(appr)),
        ("Google Doc entry + Gmail draft", "Verified" if appr and ui.steps_ok(appr) else "After approval", bool(appr and ui.steps_ok(appr))),
    ]
    lis = ""
    for name, meta, done in steps:
        mark = ('<span style="width:22px;height:22px;border-radius:50%;background:#E4F6EE;color:#16875A;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700">✓</span>'
                if done else '<span style="width:22px;height:22px;border-radius:50%;border:1.5px solid #CFCDE0;display:inline-block"></span>')
        lis += (f'<div style="display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #F2F1F8;font-size:14px">{mark}'
                f'<span style="flex:1">{ui.esc(name)}</span><span class="kz-small">{ui.esc(meta)}</span></div>')
    ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 6px;font-size:17px">This week\'s run</h3>{lis}</div>')

left, right = st.columns(2)
with left:
    qs = ""
    pq = pulse.get("quotes") or []
    src = pq if pq else (ins.get("quotes") or [])
    for q in src[:3]:
        text = (q.get("excerpt") or q.get("text") or "").strip("…").strip()
        meta = f"{q.get('rating', '')}★ · {q.get('theme', '')}" if q.get("rating") else ui.esc(q.get("theme", ""))
        qs += f'<div class="kz-quote"><blockquote>“{ui.esc(text)}”</blockquote><div class="kz-small" style="margin-top:6px">{ui.esc(meta)} · verified word-for-word</div></div>'
    ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 12px;font-size:17px">What users say</h3>{qs or "<span class=kz-muted>No quotes yet.</span>"}</div>')
with right:
    bullets = fee.get("explanation_bullets") or []
    lis = "".join(f'<li style="margin-bottom:8px">{ui.esc(b.get("text") if isinstance(b, dict) else b)}</li>' for b in bullets[:4])
    sup = (fee.get("fact_check") or {}).get("summary") or {}
    ui.html_block(f'<div class="kz-card"><div class="kz-row"><h3 style="margin:0;font-size:17px">Support answer</h3>'
                  f'{ui.status_chip(str(sup.get("supported", "—")) + " facts supported", "green")}</div>'
                  f'<p style="font-weight:700;margin:12px 0 8px">Why is my total charge more than brokerage?</p>'
                  f'<ul style="padding-left:18px;margin:0;font-size:14px;line-height:1.55">{lis}</ul></div>')
    st.page_link("pages/fee.py", label="Open the full explainer and sources", icon=":material/arrow_forward:")
