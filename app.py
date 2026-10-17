"""Kizuki — Streamlit entry point. Run: streamlit run app.py"""
import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

st.set_page_config(page_title="Kizuki", page_icon=str(ROOT / "assets" / "kizuki-mark.png"), layout="wide")

# Load secrets: Streamlit Cloud secrets first, then .env / environment. Never printed.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
for key in ("GROQ_API_KEY", "GROQ_MODEL", "GROQ_MODEL_FAST", "N8N_MCP_URL", "N8N_MCP_TOKEN", "APP_PASSWORD"):
    try:
        if key in st.secrets:
            os.environ[key] = str(st.secrets[key])
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import ui  # noqa: E402

ui.inject_css()
st.logo(str(ROOT / "assets" / "kizuki-wordmark.svg"), icon_image=str(ROOT / "assets" / "kizuki-mark.svg"), size="large")

pages = {
    "Analyse": [
        st.Page("pages/overview.py", title="Overview", icon=":material/space_dashboard:", default=True),
        st.Page("pages/upload.py", title="Upload reviews", icon=":material/upload:"),
        st.Page("pages/insights.py", title="Insights", icon=":material/insights:"),
    ],
    "Outputs": [
        st.Page("pages/pulse.py", title="Weekly pulse", icon=":material/article:"),
        st.Page("pages/fee.py", title="Fee explainer", icon=":material/receipt_long:"),
    ],
    "Publish": [
        st.Page("pages/approve.py", title="Approve & publish", icon=":material/task_alt:"),
        st.Page("pages/history.py", title="History", icon=":material/history:"),
    ],
    "Quality": [
        st.Page("pages/evals.py", title="Evals", icon=":material/fact_check:"),
        st.Page("pages/about.py", title="About Kizuki", icon=":material/info:"),
    ],
}
nav = st.navigation(pages)
ui.sidebar_footer()
nav.run()
