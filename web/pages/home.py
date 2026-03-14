import streamlit as st
import pandas as pd
import io

from db.database import SessionLocal
from db.models import Shop

db = SessionLocal()

try:
    areas_query = db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
    all_areas = ["All"] + [a[0] for a in areas_query if a[0]]

    # -- SIDEBAR (Filters) --
    st.sidebar.markdown("### Filters")
    selected_area = st.sidebar.selectbox("Area", all_areas)
    visit_status = st.sidebar.radio("Status", ["All", "Unvisited", "Visited"])

    # -- MAIN QUERY --
    query = db.query(Shop)

    if selected_area != "All":
        query = query.filter(Shop.area == selected_area)

    if visit_status == "Unvisited":
        query = query.filter(Shop.is_visited == False)
    elif visit_status == "Visited":
        query = query.filter(Shop.is_visited == True)

    shops = query.order_by(Shop.created_at.desc()).all()

    # -- MAIN PANEL --
    st.markdown("## Meshi Archive")
    st.caption(f"{len(shops)} shops")

    if not shops:
        st.info("No results found.")
    else:
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
        display_df = df.drop(columns=["_id", "@timestamp", "message_id"])

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
                "shop.name": st.column_config.TextColumn("Name", width="large"),
                "shop.area": st.column_config.TextColumn("Area", width="medium"),
                "shop.category": st.column_config.TextColumn("Category", width="medium"),
                "status.is_visited": st.column_config.CheckboxColumn("Visited", width="small"),
                "url": st.column_config.LinkColumn(
                    "URL", validate="^https?://", max_chars=80, width="large"
                ),
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
