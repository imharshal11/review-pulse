import re
import streamlit as st
import ui

fee = ui.load_json(ui.OUT / "fee_explainer.json")
ins = ui.insights() or {}
ui.context_bar("Ready for approval" if fee else "No data yet", "ready" if fee else "empty")
ui.page_header("Outputs · Reusable support answer", "Fee explainer",
               "Written only from official sources, then checked bullet by bullet by a second AI pass.")
if not fee:
    ui.empty_state("No fee explainer yet")
    st.stop()

if fee.get("needs_human_review"):
    st.warning("A fee fact could not be confirmed from official sources, so this explainer is held for human review.")

fc = fee.get("fact_check") or {}
summary = fc.get("summary") or {}
checks = fc.get("bullets") or []
chips = ui.status_chip(f"{summary.get('supported', '—')} facts supported", "green") + "&nbsp;" + ui.status_chip(f"Last checked {ui.fmt_date(fee.get('last_checked'))}", "grey")
ui.html_block(chips)
st.write("")

left, right = st.columns([2, 1], gap="medium")
with left:
    items = ""
    bullets = fee.get("explanation_bullets") or []
    for i, b in enumerate(bullets):
        text = b.get("text") if isinstance(b, dict) else str(b)
        srcs = b.get("sources", []) if isinstance(b, dict) else []
        kind = b.get("type", "fact") if isinstance(b, dict) else "fact"
        status = ""
        if i < len(checks) and isinstance(checks[i], dict):
            status = checks[i].get("status") or ("supported" if checks[i].get("supported") else "")
        if kind == "summary":
            tag = '<span class="kz-chip" style="min-height:24px;padding:0 8px;font-size:12px;background:#F1F0F7;color:#5E5B76">Summary · no new facts</span>'
        elif status in ("supported", True) or status == "":
            tag = '<span class="kz-chip" style="min-height:24px;padding:0 8px;font-size:12px;background:#E4F6EE;color:#16875A">Supported</span>'
        else:
            tag = f'<span class="kz-chip" style="min-height:24px;padding:0 8px;font-size:12px;background:#FFF1EC;color:#E04F35">{ui.esc(status)}</span>'
        src_tags = "".join(f'<span class="kz-mono" style="font-size:12px;padding:2px 8px;border-radius:6px;background:#F4F5FB">{ui.esc(s)}</span>' for s in srcs)
        items += (f'<div style="display:flex;gap:14px;padding:14px;border-radius:14px;background:#F7F6FF;margin-bottom:10px">'
                  f'<b style="color:#5B4CDB">{i + 1}</b><div style="flex:1"><div style="font-size:15px;line-height:1.6">{ui.esc(text)}</div>'
                  f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">{tag}{src_tags}</div></div></div>')
    title = "Why the total charge on a trade can be more than brokerage"
    ui.html_block(f'<div class="kz-card"><h2 style="margin:0 0 14px;font-size:21px;font-weight:800">{title}</h2>{items}</div>')
    plain = title + "\n\n" + "\n".join(f"- {b.get('text') if isinstance(b, dict) else b}" for b in bullets)
    links = fee.get("source_links") or []
    plain += "\n\nSources:\n" + "\n".join(f"- {l.get('title')}: {l.get('url')}" for l in links if isinstance(l, dict))
    plain += f"\nLast checked: {fee.get('last_checked', '')}"
    with st.expander("Copy for support"):
        st.code(plain, language=None)
with right:
    fi = ins.get("fee_issue") or {}
    sc = fi.get("stance_counts") or {}
    total = sum(v for v in sc.values() if isinstance(v, (int, float)))
    ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 8px;font-size:16px">Why this fee</h3>'
                  f'<div style="font-size:14px;line-height:1.55">From {total} fee mentions: <b>{sc.get("confused", "—")}</b> users don\'t understand a charge and '
                  f'<b>{sc.get("complaint_only", "—")}</b> say charges are too high.</div></div>')
    srcmd = ui.read_text("knowledge/sources.md") or ""
    found = re.findall(r"(S\d)\D[^\n]*?(https?://\S+)", srcmd)
    lis = "".join(f'<div style="display:flex;gap:10px;margin-bottom:8px;font-size:14px"><span class="kz-mono" style="color:#5E5B76">{sid}</span>'
                  f'<a href="{ui.esc(url.rstrip(").,|"))}" target="_blank">{ui.esc(url.rstrip(").,|").split("//")[1][:42])}</a></div>' for sid, url in found)
    if not lis:
        lis = "".join(f'<div style="margin-bottom:8px;font-size:14px"><a href="{ui.esc(l.get("url"))}" target="_blank">{ui.esc(l.get("title"))}</a></div>'
                      for l in (fee.get("source_links") or []) if isinstance(l, dict))
    ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 10px;font-size:16px">Official sources</h3>{lis}'
                  '<div class="kz-small">Every number in a bullet must appear in its cited source.</div></div>')
    st.page_link("pages/approve.py", label="Continue to approval", icon=":material/task_alt:")
