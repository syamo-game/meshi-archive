import os
import sys
import streamlit as st

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.database import init_db

init_db()

st.set_page_config(
    page_title="Meshi Archive",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
.stApp {
    background-color: #f5f7fa;
}
header[data-testid="stHeader"] {
    background-color: #ffffff;
    border-bottom: 1px solid #d3dae6;
}
[data-testid="stDataFrame"] > div {
    width: 100% !important;
}
[data-testid="stDataFrame"] iframe {
    min-height: 600px !important;
}
</style>
""", unsafe_allow_html=True)

pg = st.navigation([
    st.Page("_pages/home.py", title="Meshi Archive"),
    st.Page("_pages/admin.py", title="Admin"),
])
pg.run()
