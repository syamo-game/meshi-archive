import streamlit as st
import pandas as pd
import io

import sys
import os
# Add parent dir to PYTHONPATH for docker
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.database import SessionLocal, init_db
from db.models import Shop

# Initialize tables just in case web container hits DB before bot
init_db()

# Page config
st.set_page_config(
    page_title="Meshi Archive",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Authentication Logic
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


db = SessionLocal()

try:
    # -- DATA FETCHING --
    areas_query = db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
    all_areas = ["All"] + [a[0] for a in areas_query if a[0]]

    # -- SIDEBAR (Filters) --
    st.sidebar.markdown("### Filters")

    selected_area = st.sidebar.selectbox("Area", all_areas)

    visit_status = st.sidebar.radio(
        "Status",
        ["All", "Unvisited", "Visited"]
    )

    # -- MAIN QUERY --
    query = db.query(Shop)

    if selected_area != "All":
        query = query.filter(Shop.area == selected_area)

    if visit_status == "Unvisited":
        query = query.filter(Shop.is_visited == False)
    elif visit_status == "Visited":
        query = query.filter(Shop.is_visited == True)

    # Sort by newest first
    shops = query.order_by(Shop.created_at.desc()).all()

    # -- MAIN PANEL --
    st.markdown("## Meshi Archive")
    st.caption(f"{len(shops)} shops")

    if not shops:
        st.info("No results found.")
    else:
        # Convert ORM objects to structured list of dicts for the dataframe
        data = []
        for shop in shops:
            data.append({
                "_id": shop.id,
                "@timestamp": shop.created_at.strftime('%Y-%m-%d %H:%M:%S') if shop.created_at else None,
                "message_id": shop.message_id,
                "shop.name": shop.shop_name,
                "shop.area": shop.area,
                "shop.category": shop.category,
                "status.is_visited": shop.is_visited,
                "url": shop.url
            })

        df = pd.DataFrame(data)

        # Display table: hide _id, @timestamp, message_id (kept in CSV only)
        display_df = df.drop(columns=["_id", "@timestamp", "message_id"])

        # Show all rows up to a max height; each row ~35px + 38px header
        row_height = 35
        header_height = 38
        max_height = 720
        table_height = min(len(display_df) * row_height + header_height, max_height)

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=table_height,
            column_config={
                "shop.name": st.column_config.TextColumn(
                    "Name",
                    width="large"
                ),
                "shop.area": st.column_config.TextColumn(
                    "Area",
                    width="medium"
                ),
                "shop.category": st.column_config.TextColumn(
                    "Category",
                    width="medium"
                ),
                "status.is_visited": st.column_config.CheckboxColumn(
                    "Visited",
                    width="small"
                ),
                "url": st.column_config.LinkColumn(
                    "URL",
                    validate="^https?://",
                    max_chars=80,
                    width="large"
                )
            }
        )

        with st.expander("Latest record (JSON)"):
            st.json(data[0])

        csv_data = df.to_csv(index=False).encode('utf-8-sig')
        st.sidebar.markdown("---")
        st.sidebar.download_button(
            label="Download CSV",
            data=csv_data,
            file_name='meshi_archive.csv',
            mime='text/csv'
        )

except Exception as e:
    st.error(f"Database error: {e}")
finally:
    db.close()
