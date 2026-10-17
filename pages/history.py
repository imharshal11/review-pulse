import streamlit as st
import ui
import kz_actions as act

ui.context_bar("Audit trail", "verified")
ui.page_header("Publish · Audit trail", "History", "Every approval and rejection, what it published, and whether it was verified.")

rows = []
d = ui.OUT / "approvals"
for f in (sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []):
    rec = ui.load_json(f)
    if not isinstance(rec, dict):
        continue
    ok = ui.steps_ok(rec)
    rows.append({"when": rec.get("approved_at", ""), "id": str(rec.get("approval_id", f.stem)), "week": rec.get("week_label", "—"),
                 "hash": str(rec.get("payload_sha256", ""))[:12], "status": "Verified" if ok else "Incomplete", "detail": "", "resume": not ok})
for r in act.read_rejections():
    rows.append({"when": r.get("rejected_at", ""), "id": r.get("rejection_id", "—"), "week": r.get("week_label", "—"), "hash": str(r.get("payload_sha256", ""))[:12],
                 "status": "Rejected", "detail": r.get("reason", "") + (f" — {r['note']}" if r.get("note") else ""), "resume": False})
rows.sort(key=lambda r: r["when"], reverse=True)

if not rows:
    ui.empty_state("No approvals or rejections yet")
    st.stop()

tone = {"Verified": ("#16875A", "#E4F6EE"), "Incomplete": ("#B07800", "#FFF6E0"), "Rejected": ("#E04F35", "#FFF1EC")}
trs = ""
for r in rows:
    fg, bg = tone[r["status"]]
    detail = f'<div class="kz-small">{ui.esc(r["detail"])}</div>' if r["detail"] else ""
    trs += (f'<tr><td class="kz-mono">{ui.esc(r["id"][:8])}</td><td>{ui.esc(r["week"])}</td><td>{ui.esc(str(r["when"])[:16].replace("T", " "))}</td>'
            f'<td class="kz-mono">{ui.esc(r["hash"])}</td><td><span class="kz-chip" style="min-height:26px;padding:0 10px;font-size:12px;background:{bg};color:{fg}">{r["status"]}</span>{detail}</td></tr>')
ui.html_block(f'<div class="kz-card"><div class="kz-scroll" style="overflow-x:auto"><table class="kz-table"><thead><tr><th>Approval</th><th>Week ending</th><th>When</th>'
              f'<th>Fingerprint</th><th>Status</th></tr></thead><tbody>{trs}</tbody></table></div>'
              '<div class="kz-small" style="margin-top:10px">Rejected = reviewed and stopped by a person; nothing was published. Incomplete = approved but not finished; resume below.</div></div>')

pending = [r for r in rows if r["resume"]]
if pending:
    with st.container(border=True):
        st.markdown("#### Resume an incomplete publish")
        pick = st.selectbox("Approval", [r["id"] for r in pending])
        if st.button("Resume publish", type="primary"):
            with st.spinner("Resuming — Kizuki will not create a duplicate entry…"):
                code, out = act.run_script(["src/publish.py", "--resume", pick], timeout=600)
            (st.success if "Gmail draft: OK" in out else st.error)("Resume finished." if "Gmail draft: OK" in out else "Resume stopped. See the log.")
            st.code("\n".join(out.splitlines()[-20:]), language=None)
