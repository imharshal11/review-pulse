import streamlit as st
import ui

ui.page_header("About", "Kizuki 気づき")
ui.html_block(
    f'<div class="kz-card" style="display:flex;gap:28px;align-items:center;flex-wrap:wrap;padding:32px">{ui.MARK_SVG.format(s=88)}'
    '<p style="flex:1;min-width:260px;font-size:17px;line-height:1.6;margin:0">Kizuki is Japanese for <i>noticing</i> — the moment something becomes clear. '
    'It turns app reviews into a weekly product pulse and a fact-checked support answer, and publishes nothing until a person approves it.</p></div>')
a, b = st.columns(2)
with a:
    ui.html_block('<div class="kz-card"><h3 style="margin:0 0 8px;font-size:18px">The problem</h3><p style="margin:0;line-height:1.6">Thousands of reviews arrive every week. '
                  'Product teams miss rising issues, and support teams answer the same fee question again and again because nobody wrote one clear, correct answer.</p></div>')
with b:
    ui.html_block('<div class="kz-card"><h3 style="margin:0 0 8px;font-size:18px">Who it\'s for</h3><p style="margin:0;line-height:1.6"><b>Product managers</b> get a 1-minute weekly pulse with evidence. '
                  '<b>Support leads</b> get a ready-to-send answer built only from official pages.</p></div>')
steps = [("Collect & clean", "12 weeks of reviews, balanced per week, personal data removed"),
         ("Find themes", "AI sorts reviews into up to 5 themes; code counts them"),
         ("Write the pulse", "≤ 250 words, real quotes, 3 actions"),
         ("Explain the fee", "RAG over official pages, checked by an AI judge"),
         ("You approve or reject", "Content is fingerprinted so it can't change after approval"),
         ("Publish via MCP", "Google Doc entry + Gmail draft, then read back to verify")]
cells = "".join(f'<div style="padding:16px;border-radius:14px;background:#F7F6FF"><b style="color:#5B4CDB">{i}</b><div style="font-weight:700;margin:4px 0">{t}</div>'
                f'<div class="kz-muted">{d}</div></div>' for i, (t, d) in enumerate(steps, 1))
ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 12px;font-size:18px">How it works</h3>'
              f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">{cells}</div></div>')
a, b = st.columns(2)
with a:
    trust = ["<b>AI judges, code counts.</b> Every number is computed, not generated.", "<b>Quotes are real.</b> Copied from the source by review ID.",
             "<b>Facts only from official sources,</b> each one checked.", "<b>A person approves or rejects every publish.</b> Email is draft-only by design.",
             "<b>Privacy first.</b> Names dropped; emails and phone numbers masked."]
    ui.html_block('<div class="kz-card"><h3 style="margin:0 0 10px;font-size:18px">Trust principles</h3>' + "".join(f'<p style="margin:0 0 8px;font-size:14px">— {t}</p>' for t in trust) + "</div>")
with b:
    stack = ["Python", "Groq · gpt-oss-120b", "Groq · gpt-oss-20b", "BM25 retrieval", "n8n MCP server", "Google Docs", "Gmail drafts", "Streamlit"]
    chips = "".join(f'<span style="display:inline-block;padding:6px 12px;border-radius:999px;border:1px solid #DAD8E8;font-size:13px;margin:0 6px 6px 0">{s}</span>' for s in stack)
    ui.html_block(f'<div class="kz-card"><h3 style="margin:0 0 10px;font-size:18px">Built with</h3>{chips}'
                  '<h4 style="margin:10px 0 6px;font-size:14px">Known limits</h4><ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.6">'
                  '<li>English Play Store reviews only</li><li>Fee explainer sources are Groww-specific</li><li>The fact-check judge uses the same model family as the writer</li>'
                  '<li>Google Doc entries are plain text, not styled</li></ul></div>')
