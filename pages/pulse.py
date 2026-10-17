import streamlit as st
import ui

md = ui.read_text(ui.OUT / "weekly_pulse.md")
pulse = ui.load_json(ui.OUT / "pulse.json") or {}
ui.context_bar("Ready for approval" if md else "No data yet", "ready" if md else "empty")
ui.page_header("Outputs · Internal note for the product team", "Weekly pulse")
if not md:
    ui.empty_state("No weekly pulse yet")
    st.stop()

wc = pulse.get("word_count", "—")
ui.html_block(ui.status_chip(f"{wc} / 250 words", "green") + "&nbsp;" + ui.status_chip("Awaiting approval", "violet"))
st.write("")
left, right = st.columns([2, 1], gap="medium")
with left:
    with st.container(border=True):
        st.markdown(md)
with right:
    ui.html_block('<div class="kz-card"><h3 style="margin:0 0 10px;font-size:16px">How this was written</h3>'
                  '<div style="font-size:14px;line-height:1.55"><p style="margin:0 0 10px">✓ <b>Numbers come from code.</b> The AI writes words only.</p>'
                  '<p style="margin:0 0 10px">✓ <b>Quotes are exact.</b> Each is matched to its review ID.</p>'
                  '<p style="margin:0">✓ <b>Length is enforced.</b> Over-long drafts are shortened automatically.</p></div></div>')
    st.download_button("Download .md", md, file_name="weekly_pulse.md", mime="text/markdown", use_container_width=True)
    st.page_link("pages/approve.py", label="Continue to approval", icon=":material/task_alt:")
