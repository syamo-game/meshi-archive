import streamlit as st
import pandas as pd
import io
from sqlalchemy.orm import Session
from sqlalchemy import text

import sys
import os
# Add parent dir to PYTHONPATH for docker
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.database import SessionLocal, init_db
from db.models import Shop, Message

# Initialize tables just in case web container hits DB before bot
init_db()

# Page config
st.set_page_config(
    page_title="Meshi Discover",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Authentication Logic
WEB_PASSWORD = os.getenv("WEB_PASSWORD")
if WEB_PASSWORD:
    if not st.session_state.get("authenticated", False):
        st.title("🔒 Login Required")
        pwd = st.text_input("Please enter the password to access the database:", type="password")
        if st.button("Login"):
            if pwd == WEB_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
        st.stop()

# Custom CSS to make it look like Kibana/Elasticsearch
st.markdown("""
<style>
/* Base theme matching Kibana/Elastic style */
.stApp {
    background-color: #f5f7fa; 
}
header[data-testid="stHeader"] {
    background-color: #ffffff;
    border-bottom: 1px solid #d3dae6;
}
/* Title styling */
.main-title {
    font-size: 24px;
    font-weight: 500;
    color: #1a1c21;
    margin-bottom: 0px;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
</style>
""", unsafe_allow_html=True)


db = SessionLocal()

try:
    # -- DATA FETCHING --
    # Get all areas
    areas_query = db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
    all_areas = ["All Areas"] + [a[0] for a in areas_query if a[0]]

    # -- SIDEBAR (Filters) --
    st.sidebar.markdown("### ⚙️ Filter Configuration")

    selected_area = st.sidebar.selectbox("Filter by area", all_areas)

    visit_status = st.sidebar.radio(
        "Filter by status",
        ["All", "Unvisited (is_visited=false)", "Visited (is_visited=true)"]
    )

    # -- MAIN QUERY --
    query = db.query(Shop)

    if selected_area != "All Areas":
        query = query.filter(Shop.area == selected_area)

    if visit_status == "Unvisited (is_visited=false)":
        query = query.filter(Shop.is_visited == False)
    elif visit_status == "Visited (is_visited=true)":
        query = query.filter(Shop.is_visited == True)

    # Sort by newest first
    shops = query.order_by(Shop.created_at.desc()).all()

    # -- MAIN PANEL --
    st.title("Meshi Database")
    st.markdown(f"**{len(shops)}** locations found.")
    st.write("")  # Spacer

    if not shops:
        st.info("No results match your search criteria. Please adjust filters or wait for new data ingestion.")
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

        # Render interactive data table
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "_id": st.column_config.NumberColumn(
                    "ID",
                    help="Internal database row ID",
                    width="small"
                ),
                "@timestamp": st.column_config.DatetimeColumn(
                    "@timestamp",
                    format="YYYY-MM-DD HH:mm:ss",
                    width="medium"
                ),
                "message_id": st.column_config.TextColumn(
                    "message_id",
                    width="small"
                ),
                "shop.name": st.column_config.TextColumn(
                    "shop.name",
                    width="medium"
                ),
                "shop.area": st.column_config.TextColumn(
                    "shop.area",
                    width="small"
                ),
                "shop.category": st.column_config.TextColumn(
                    "shop.category",
                    width="small"
                ),
                "status.is_visited": st.column_config.CheckboxColumn(
                    "status.is_visited",
                    help="True if physically visited",
                    width="small"
                ),
                "url": st.column_config.LinkColumn(
                    "url",
                    help="Original Mention URL",
                    validate="^https?://",
                    max_chars=100,
                    width="large"
                )
            }
        )

        # Optional: Display raw JSON view of the latest document like Kibana
        if len(shops) > 0:
            with st.expander("Show Latest Document JSON"):
                st.json(data[0])

        csv_data = df.to_csv(index=False).encode('utf-8-sig')
        st.sidebar.markdown("---")
        st.sidebar.download_button(
            label="📥 Download Search Results (CSV)",
            data=csv_data,
            file_name='meshi_database.csv',
            mime='text/csv'
        )

except Exception as e:
    st.error(f"Database error: {e}")
finally:
    db.close()

# -- CSV IMPORT --
st.sidebar.markdown("---")
st.sidebar.markdown("### 📤 Import CSV (Master Sync)")
uploaded_file = st.sidebar.file_uploader(
    "Upload CSV to replace DB data",
    type=["csv"],
    help="CSV is treated as master. Existing rows are updated, rows missing from CSV are deleted."
)

if uploaded_file is not None:
    if st.sidebar.button("🔄 Run Import", type="primary"):
        try:
            csv_df = pd.read_csv(io.StringIO(uploaded_file.read().decode('utf-8-sig')))

            # Validate required columns
            required_cols = {"message_id", "shop.name"}
            if not required_cols.issubset(set(csv_df.columns)):
                st.sidebar.error(f"❌ CSV must contain columns: {required_cols}")
            else:
                # Normalize boolean column
                if "status.is_visited" in csv_df.columns:
                    csv_df["status.is_visited"] = csv_df["status.is_visited"].map(
                        lambda v: str(v).strip().lower() in ("true", "1", "yes")
                    )

                csv_message_ids = set(csv_df["message_id"].astype(str).tolist())

                import_db = SessionLocal()
                try:
                    updated = 0
                    inserted = 0
                    deleted = 0

                    for _, row in csv_df.iterrows():
                        mid = str(row["message_id"]).strip()

                        # Ensure matching Message row exists (FK requirement)
                        existing_msg = import_db.query(Message).filter_by(message_id=mid).first()
                        if not existing_msg:
                            import_db.add(Message(message_id=mid, is_target=True))

                        existing_shop = import_db.query(Shop).filter_by(message_id=mid).first()
                        if existing_shop:
                            existing_shop.shop_name = str(row.get("shop.name", "Unknown"))
                            existing_shop.area = row.get("shop.area") if pd.notna(row.get("shop.area")) else None
                            existing_shop.category = row.get("shop.category") if pd.notna(row.get("shop.category")) else None
                            existing_shop.url = row.get("url") if pd.notna(row.get("url")) else None
                            existing_shop.is_visited = bool(row.get("status.is_visited", False))
                            updated += 1
                        else:
                            import_db.add(Shop(
                                message_id=mid,
                                shop_name=str(row.get("shop.name", "Unknown")),
                                area=row.get("shop.area") if pd.notna(row.get("shop.area")) else None,
                                category=row.get("shop.category") if pd.notna(row.get("shop.category")) else None,
                                url=row.get("url") if pd.notna(row.get("url")) else None,
                                is_visited=bool(row.get("status.is_visited", False))
                            ))
                            inserted += 1

                    # Delete shops whose message_id is not in CSV
                    all_shops = import_db.query(Shop).all()
                    for shop in all_shops:
                        if shop.message_id not in csv_message_ids:
                            import_db.delete(shop)
                            deleted += 1

                    import_db.commit()
                    st.sidebar.success(f"✅ Import complete: {updated} updated, {inserted} inserted, {deleted} deleted")
                    st.rerun()
                except Exception as e:
                    import_db.rollback()
                    st.sidebar.error(f"❌ Import failed: {e}")
                finally:
                    import_db.close()
        except Exception as e:
            st.sidebar.error(f"❌ Failed to read CSV: {e}")
