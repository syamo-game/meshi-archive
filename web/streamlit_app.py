import os
import sys
import streamlit as st

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.database import init_db

init_db()

st.set_page_config(
    page_title="Meshi Database",
    page_icon="🍜",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS: Digital Agency Design System
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=BIZ+UDPGothic:wght@400;700&display=swap');

/* Base font: BIZ UDPGothic (Digital Agency standard) */
html, body, .stApp, [class*="css"] {
    font-family: 'BIZ UDPGothic', 'Noto Sans JP', 'Hiragino Sans', 'Meiryo', sans-serif !important;
}

/* Background */
.stApp {
    background-color: #F5F5F5;
}

/* Header */
header[data-testid="stHeader"] {
    background-color: #ffffff;
    border-bottom: 2px solid #1D7EBB;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background-color: #ffffff;
    border-right: 1px solid #D9D9D9;
}
[data-testid="stSidebar"] > div:first-child {
    padding-top: 24px;
}

/* Section dividers in sidebar */
[data-testid="stSidebar"] hr {
    border-color: #D9D9D9;
    margin: 16px 0;
}

/* Main title */
.page-title {
    font-size: 24px;
    font-weight: 700;
    color: #1A1A1C;
    border-left: 4px solid #1D7EBB;
    padding-left: 12px;
    margin-bottom: 4px;
    line-height: 1.4;
}

/* Count badge */
.count-badge {
    display: inline-block;
    font-size: 14px;
    color: #595959;
    margin-bottom: 16px;
}

/* Section header in sidebar */
.sidebar-section {
    font-size: 13px;
    font-weight: 700;
    color: #1A1A1C;
    letter-spacing: 0.04em;
    margin-bottom: 8px;
}

/* Dataframe improvements */
[data-testid="stDataFrame"] > div {
    width: 100% !important;
}
[data-testid="stDataFrame"] {
    border: 1px solid #D9D9D9;
    border-radius: 4px;
    background: #ffffff;
}

/* Info / success / error messages */
[data-testid="stAlertContainer"] {
    border-radius: 4px;
}
</style>
""", unsafe_allow_html=True)

pg = st.navigation([
    st.Page("_pages/home.py", title="Meshi Database"),
    st.Page("_pages/admin.py", title="管理"),
])
pg.run()
