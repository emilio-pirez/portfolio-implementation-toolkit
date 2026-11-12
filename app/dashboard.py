"""Streamlit dashboard (placeholder - the six pages are built in Phase 6).

Run with: ``streamlit run app/dashboard.py``
"""

import streamlit as st

from pit import __version__
from pit.config import load_config

st.set_page_config(page_title="Portfolio Implementation Toolkit", layout="wide")

cfg = load_config()
st.title("Portfolio Implementation Toolkit")
st.caption(f"v{__version__} - {cfg.fund.name}")
st.info("Dashboard under construction. Pages will be added in Phase 6.")
