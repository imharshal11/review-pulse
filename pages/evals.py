import streamlit as st
import ui
import kz_actions as act

ev = ui.evals_summary()
ui.context_bar("All checks passed" if ev and ev[0] == ev[1] else "Checks need attention", "verified" if ev and ev[0] == ev[1] else "failed")
ui.page_header("Quality · Automated checks against the project brief", "Evals")

c1, c2 = st.columns([3, 1])
with c1:
    if ev:
        color = "#16875A" if ev[0] == ev[1] else "#E04F35"
        ui.html_block(f'<div class="kz-card"><div style="font-size:46px;font-weight:800;color:{color};line-height:1">{ev[0]} / {ev[1]}</div>'
                      f'<div class="kz-muted" style="margin-top:6px">checks passed · no AI calls · runs in seconds</div>'
                      f'<div style="margin-top:12px">{ui.bar(ev[0] / max(ev[1], 1) * 100, color)}</div></div>')
    else:
        ui.html_block('<div class="kz-card"><b>No eval report yet.</b> Run the checks to see the scorecard.</div>')
with c2:
    if st.button("Run evals", type="primary", use_container_width=True):
        with st.spinner("Running checks…"):
            code, out = act.run_script(["src/evals.py"], timeout=300)
        st.session_state.kz_eval_out = out
        st.rerun()

out = st.session_state.get("kz_eval_out")
report = ui.read_text(ui.OUT / "eval_report.md")
if out:
    with st.container(border=True):
        st.code(out[-6000:], language=None)
elif report:
    rows = []
    for line in report.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    if len(rows) > 1:
        head = "".join(f"<th>{ui.esc(c)}</th>" for c in rows[0])
        body = ""
        for r in rows[1:]:
            tds = ""
            for i, c in enumerate(r):
                if c.upper() == "PASS":
                    c_html = '<span class="kz-chip" style="min-height:24px;padding:0 9px;font-size:12px;background:#E4F6EE;color:#16875A">PASS</span>'
                elif c.upper() == "FAIL":
                    c_html = '<span class="kz-chip" style="min-height:24px;padding:0 9px;font-size:12px;background:#FFF1EC;color:#E04F35">FAIL</span>'
                else:
                    c_html = ui.esc(c)
                tds += f"<td>{c_html}</td>"
            body += f"<tr>{tds}</tr>"
        gen = next((l.replace("Generated:", "").strip() for l in report.splitlines() if l.startswith("Generated")), "")
        ui.html_block(f'<div class="kz-card"><div class="kz-row"><h3 style="margin:0;font-size:17px">Evaluation report</h3>'
                      f'<span class="kz-small">{ui.esc(gen)}</span></div>'
                      f'<div class="kz-scroll" style="margin-top:10px"><table class="kz-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></div>')
    else:
        with st.container(border=True):
            st.markdown(report)
