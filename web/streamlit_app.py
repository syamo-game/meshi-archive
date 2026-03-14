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

# Authentication gate (protects all pages)
WEB_PASSWORD = os.getenv("WEB_PASSWORD")
if WEB_PASSWORD:
    if not st.session_state.get("authenticated", False):
        st.title("Login")
        pwd = st.text_input("Password", type="password")
        if st.button("Login"):
            if pwd == WEB_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
        st.stop()

# Navigation
pg = st.navigation([
    st.Page("pages/home.py", title="Meshi Archive"),
    st.Page("pages/admin.py", title="Admin"),
])
pg.run()
