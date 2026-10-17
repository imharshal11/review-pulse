import io
import streamlit as st
import ui
import kz_actions as act

ui.context_bar("Add new reviews", "empty")
ui.page_header("Analyse · Step 1 of 3", "Upload reviews", "Use the same format as the template. Every file is checked before analysis starts.")

TEMPLATE = (
    "\ufeffreview_id,date,rating,text,thumbs_up,app_version,week_start\n"
    "r1,2026-10-05,2,Charges are not clear when I place an order,0,25.1.0,\n"
    "r2,2026-10-06,5,Clean app and easy to use for beginners,3,25.1.0,\n"
    "r3,2026-10-07,1,App keeps crashing when I open charts,1,25.1.0,\n"
)
left, right = st.columns(2)
with left:
    with st.container(border=True):
        st.markdown("#### Choose a file")
        up = st.file_uploader("CSV file", type=["csv"], label_visibility="collapsed")
        st.markdown("**Requirements**  \n- Columns `text`, `date`, `rating` are required  \n- Dates in `YYYY-MM-DD` format (e.g. 2026-10-05)  \n- 8–12 weeks of reviews (older ones are trimmed)  \n"
                    "- At least 100 usable reviews after cleaning  \n- Names, emails and phone numbers are removed or masked")
        st.download_button("Download CSV template", TEMPLATE.encode("utf-8"), file_name="kizuki_template.csv", mime="text/csv", use_container_width=True)
        st.caption("Opens correctly in Excel, Google Sheets and Numbers. review_id, thumbs_up, app_version and week_start can be left blank.")
    with st.container(border=True):
        st.markdown("#### Works with any review data")
        st.markdown("Real or test data, any app — Kizuki only checks the format. For a different app you still get "
                    "themes, verified quotes and the weekly pulse. The **fee explainer stays on Groww**, because its "
                    "facts come from Groww's own pricing pages.")

with right:
    if not up:
        ui.html_block('<div class="kz-card"><b>No file yet.</b><div class="kz-muted" style="margin-top:6px">Choose a CSV to check it. Nothing is analysed until you click Run analysis.</div></div>')
        st.stop()
    dest = ui.ROOT / "data" / "uploaded.csv"
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(up.getvalue())
    try:
        from validate_csv import validate_reviews_csv
        df, errors, warnings = validate_reviews_csv(str(dest))
    except Exception as e:
        df, errors, warnings = None, [f"The file could not be checked ({type(e).__name__})."], []
    with st.container(border=True):
        if errors:
            ui.html_block(f'<div class="kz-card" style="background:#FFF1EC;box-shadow:none;color:#B8361F"><b>This file can\'t be analysed yet</b><div class="kz-mono" style="font-size:13px">{ui.esc(up.name)}</div></div>')
            st.markdown("**Fix these to continue**")
            for e in errors:
                st.markdown(f"- ✕ {e}")
            for w in warnings:
                st.warning(w)
            st.caption("Nothing was analysed or stored. Your previous results are unchanged.")
        else:
            ui.html_block(f'<div class="kz-card" style="background:#E4F6EE;box-shadow:none;color:#0F5F3D"><b>Ready to analyse</b><div class="kz-mono" style="font-size:13px">{ui.esc(up.name)}</div></div>')
            n = len(df) if df is not None else 0
            weeks = df["week_start"].nunique() if df is not None and "week_start" in df else "—"
            a, b = st.columns(2)
            a.metric("Reviews kept", f"{n:,}")
            b.metric("Weeks", weeks)
            for w in warnings:
                st.warning(w)
            if st.button("Run analysis", type="primary", use_container_width=True):
                with st.status("Analysing — themes, quotes, fee issue, pulse, fee explainer…", expanded=True) as s:
                    code, out = act.run_script(["src/run_all.py", "--input", "data/uploaded.csv", "--skip-publish"], timeout=3600)
                    s.update(label="Analysis finished" if code == 0 else "Analysis stopped", state="complete" if code == 0 else "error")
                st.code("\n".join(out.splitlines()[-25:]), language=None)
                if code == 0:
                    st.page_link("pages/overview.py", label="See the new overview", icon=":material/space_dashboard:")
            st.caption("New data takes a few minutes. Repeat runs use the cache and finish in seconds.")
