import streamlit as st
import pandas as pd

from db.database import SessionLocal
from db.models import Shop

db = SessionLocal()

try:
    areas_query = db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
    all_areas = ["すべてのエリア"] + [a[0] for a in areas_query if a[0]]

    # -- SIDEBAR --
    st.sidebar.markdown('<div class="sidebar-section">絞り込み</div>', unsafe_allow_html=True)
    selected_area = st.sidebar.selectbox("エリア", all_areas)
    visit_status = st.sidebar.radio("訪問状況", ["すべて", "未訪問", "訪問済み"])

    # -- MAIN QUERY --
    query = db.query(Shop)

    if selected_area != "すべてのエリア":
        query = query.filter(Shop.area == selected_area)

    if visit_status == "未訪問":
        query = query.filter(Shop.is_visited == False)
    elif visit_status == "訪問済み":
        query = query.filter(Shop.is_visited == True)

    shops = query.order_by(Shop.created_at.desc()).all()

    # -- MAIN PANEL --
    st.markdown('<div class="page-title">Meshi Database</div>', unsafe_allow_html=True)
    st.write("")  # Spacer
    count_text = f"{len(shops):,} 件の結果"
    st.markdown(f'<div class="count-badge">{count_text}</div>', unsafe_allow_html=True)

    if not shops:
        st.info("絞り込み条件に一致するデータがありません。条件を変更するか、新しいデータが登録されるまでお待ちください。")
    else:
        data = []
        for shop in shops:
            data.append({
                "_id": shop.id,
                "@timestamp": shop.created_at.strftime('%Y-%m-%d %H:%M:%S') if shop.created_at else None,
                "message_id": shop.message_id,
                "店名": shop.shop_name,
                "エリア": shop.area,
                "カテゴリ": shop.category,
                "訪問済み": shop.is_visited,
                "URL": shop.url
            })

        df = pd.DataFrame(data)
        display_df = df.drop(columns=["_id", "@timestamp", "message_id"])

        row_height = 35
        header_height = 38
        max_height = 1200
        table_height = min(len(display_df) * row_height + header_height, max_height)

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            height=table_height,
            column_config={
                "店名": st.column_config.TextColumn("店名", width="large"),
                "エリア": st.column_config.TextColumn("エリア", width="medium"),
                "カテゴリ": st.column_config.TextColumn("カテゴリ", width="medium"),
                "訪問済み": st.column_config.CheckboxColumn(
                    "訪問済み",
                    help="実際に訪問した場合にチェックが入ります",
                    width="small"
                ),
                "URL": st.column_config.LinkColumn(
                    "URL",
                    help="Discord 上のメンション元 URL",
                    validate="^https?://",
                    max_chars=80,
                    width="large"
                ),
            }
        )

        with st.expander("最新レコードの詳細を表示"):
            st.json(data[0])

        # Build CSV with original English column names for import compatibility
        csv_df = df.rename(columns={
            "店名": "shop.name",
            "エリア": "shop.area",
            "カテゴリ": "shop.category",
            "訪問済み": "status.is_visited",
            "URL": "url"
        })
        csv_data = csv_df.to_csv(index=False).encode('utf-8-sig')

        st.sidebar.markdown("---")
        st.sidebar.markdown('<div class="sidebar-section">ダウンロード</div>', unsafe_allow_html=True)
        st.sidebar.download_button(
            label="検索結果を CSV でダウンロード",
            data=csv_data,
            file_name='meshi_database.csv',
            mime='text/csv'
        )

except Exception as e:
    st.error(f"データベースエラーが発生しました: {e}")
finally:
    db.close()
