import os
import re
import streamlit as st
try:
    _html_frame = st.html_frame
except AttributeError:
    import streamlit.components.v1 as _components
    _html_frame = _components.html
import ui
import kz_actions as act

ui.context_bar("Awaiting your approval", "waiting")
ui.page_header("Publish · Human approval required", "Approve & publish",
               "Check exactly what will be written. Nothing is logged or drafted until you approve.")

fee = ui.load_json(ui.OUT / "fee_explainer.json")
if not fee or not ui.insights():
    ui.empty_state("Nothing to approve yet")
    st.stop()

try:
    import publish
    payloads = publish.build_payloads()
    phash = publish.compute_hash(payloads)
except SystemExit:
    st.error("The outputs are not ready to publish. Run the analysis again, then come back.")
    st.stop()
except Exception as e:
    st.error(f"Could not prepare the preview ({type(e).__name__}). Re-run the analysis, then try again.")
    st.stop()

def pget(*keys, default=""):
    for k in keys:
        if isinstance(payloads, dict) and payloads.get(k):
            return payloads[k]
    return default

subject = pget("email_subject", default="Weekly Product Pulse + Customer Clarification")
email_html = pget("email_body_body", "email_body", "email_html")
doc_text = pget("doc_entry_body", "doc_entry", "doc_text")
week = pget("week_label", default=ui.fmt_date((ui.insights() or {}).get("period_end")))
evals = ui.evals_summary()
summary = (fee.get("fact_check") or {}).get("summary") or {}
pulse = ui.load_json(ui.OUT / "pulse.json") or {}

left, right = st.columns([3, 2], gap="medium")
with left:
    with st.container(border=True):
        st.caption("GMAIL DRAFT · TO YOU · NOT SENT")
        st.markdown(f"**{subject}**")
        if email_html and "<" in str(email_html):
            _html_frame(str(email_html), height=640, scrolling=True)
        else:
            st.text(str(email_html)[:4000])
        links = [l for l in (fee.get("source_links") or []) if isinstance(l, dict) and l.get("url")]
        if links:
            st.caption("Sources in this email (links open normally in the real Gmail draft):")
            cols = st.columns(len(links))
            for col, l in zip(cols, links):
                col.link_button(l.get("title", "Source"), l["url"], use_container_width=True)
    with st.container(border=True):
        st.caption("GOOGLE DOC ENTRY · REVIEW PULSE — WEEKLY LOG")
        st.code(str(doc_text)[:6000], language=None)

with right:
    with st.container(border=True):
        st.markdown("#### Approval gate")
        ui.html_block(f'<div class="kz-card" style="background:#F7F6FF;box-shadow:none;padding:14px"><div class="kz-small">Content fingerprint (SHA-256)</div>'
                      f'<div class="kz-mono" style="font-size:16px;font-weight:500">{ui.esc(phash[:12])}…</div>'
                      '<div class="kz-small">If anything changes after approval, the fingerprint won\'t match and publishing is blocked.</div></div>')
        checks = [("Automated checks", f"{evals[0]} / {evals[1]} passed" if evals else "Not run"),
                  ("Fee facts supported", str(summary.get("supported", "—"))),
                  ("Pulse length", f"{pulse.get('word_count', '—')} / 250 words")]
        ui.html_block("".join(f'<div class="kz-row" style="font-size:14px;padding:6px 0"><span>{a}</span><b>{b}</b></div>' for a, b in checks))

        pw = os.environ.get("APP_PASSWORD", "")
        if pw and not st.session_state.get("kz_unlocked"):
            st.info("Publishing is locked on this link. Enter the owner password to approve or reject.")
            entered = st.text_input("Owner password", type="password")
            if st.button("Unlock", use_container_width=True):
                if entered == pw:
                    st.session_state.kz_unlocked = True
                    st.rerun()
                else:
                    st.error("Wrong password. Nothing was changed.")
            st.stop()

        mode = st.session_state.get("kz_mode", "pending")
        if mode == "pending":
            typed = st.text_input("Type APPROVE to confirm", placeholder="APPROVE")
            if st.button("Approve & publish", type="primary", disabled=typed != "APPROVE", use_container_width=True):
                with st.status("Publishing — duplicate check, fingerprint, Doc entry, read-back, Gmail draft…", expanded=True) as s:
                    code, out = act.run_script(["src/publish.py"], stdin_text="APPROVE\n", timeout=600)
                    st.session_state.kz_out = out
                    m = re.search(r"approvals[\\/]([0-9a-f-]{36})", out)
                    st.session_state.kz_last_id = m.group(1) if m else ""
                    ok = "Read-back verified: YES" in out and "Gmail draft: OK" in out
                    dup = "already logged" in out
                    st.session_state.kz_mode = "done" if ok else ("duplicate" if dup else "failed")
                    s.update(label="Done" if ok else "Stopped", state="complete" if ok else "error")
                st.rerun()
            if st.button("Reject", use_container_width=True):
                st.session_state.kz_mode = "rejecting"
                st.rerun()
            st.caption("Approve creates one Doc entry and one Gmail draft. Kizuki has no tool that sends email.")

        elif mode == "rejecting":
            st.markdown("**Why are you rejecting this?** Your reason is saved to History so the next run can be fixed.")
            reason = st.radio("Reason", ["Fee issue is wrong or unclear", "A quote is cut or misleading", "Numbers look wrong",
                                         "Wording is not ready to share", "Other"], index=None, label_visibility="collapsed")
            note = st.text_area("Note (optional)", placeholder="What should change before the next approval?")
            c1, c2 = st.columns(2)
            if c1.button("Confirm reject", type="primary", disabled=reason is None, use_container_width=True):
                act.record_rejection(str(week), phash, reason, note)
                st.session_state.kz_mode = "rejected"
                st.session_state.kz_reason = reason
                st.rerun()
            if c2.button("Back", use_container_width=True):
                st.session_state.kz_mode = "pending"
                st.rerun()

        elif mode == "rejected":
            ui.html_block(f'<div class="kz-card" style="background:#FFF1EC;box-shadow:none;color:#B8361F"><b>Rejected — nothing was published.</b><br>'
                          f'Reason: {ui.esc(st.session_state.get("kz_reason", ""))}</div>')
            st.markdown("✓ No Doc entry written  \n✓ No Gmail draft created  \n✓ Rejection saved to History")
            if st.button("Start over", use_container_width=True):
                st.session_state.kz_mode = "pending"
                st.rerun()

        else:
            out = st.session_state.get("kz_out", "")
            if mode == "done":
                st.success(f"Published and verified · approval {st.session_state.get('kz_last_id', '')[:8]}")
            elif mode == "duplicate":
                st.warning("This week is already logged in the Doc. Nothing new was published.")
            else:
                st.error("Publishing stopped before finishing. Your approval is saved — resume it from History.")
            with st.expander("Activity log", expanded=mode != "done"):
                lines = [l for l in out.splitlines() if l.strip() and not l.startswith("=")]
                st.code("\n".join(lines[-30:]) or "No output", language=None)
            c1, c2 = st.columns(2)
            c1.page_link("pages/history.py", label="View history", icon=":material/history:")
            if c2.button("Start over", use_container_width=True):
                st.session_state.kz_mode = "pending"
                st.rerun()
