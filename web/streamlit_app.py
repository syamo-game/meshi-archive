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
    page_title="Meshi Database",
    page_icon="🍜",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Authentication Logic
WEB_PASSWORD = os.getenv("WEB_PASSWORD")
if WEB_PASSWORD:
    if not st.session_state.get("authenticated", False):
        # Login page with Digital Agency style
        st.markdown("""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=BIZ+UDPGothic:wght@400;700&display=swap');
        html, body, .stApp {
            font-family: 'BIZ UDPGothic', 'Noto Sans JP', 'Hiragino Sans', 'Meiryo', sans-serif;
        }
        .login-container {
            max-width: 400px;
            margin: 80px auto;
            padding: 40px;
            background: #ffffff;
            border: 1px solid #D9D9D9;
            border-radius: 4px;
        }
        .login-title {
            font-size: 20px;
            font-weight: 700;
            color: #1A1A1C;
            margin-bottom: 8px;
        }
        .login-desc {
            font-size: 14px;
            color: #595959;
            margin-bottom: 24px;
        }
        </style>
        <div class="login-container">
            <div class="login-title">ログイン</div>
            <div class="login-desc">このページにアクセスするにはパスワードが必要です。</div>
        </div>
        """, unsafe_allow_html=True)
        pwd = st.text_input("パスワード", type="password", placeholder="パスワードを入力してください")
        if st.button("ログイン", type="primary"):
            if pwd == WEB_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("パスワードが正しくありません。")
        st.stop()

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


db = SessionLocal()

try:
    # -- DATA FETCHING --
    areas_query = db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
    all_areas = ["すべてのエリア"] + [a[0] for a in areas_query if a[0]]

    # -- SIDEBAR --
    st.sidebar.markdown('<div class="sidebar-section">絞り込み</div>', unsafe_allow_html=True)

    selected_area = st.sidebar.selectbox("エリア", all_areas)

    visit_status = st.sidebar.radio(
        "訪問状況",
        ["すべて", "未訪問", "訪問済み"]
    )

    # -- MAIN QUERY --
    query = db.query(Shop)

    if selected_area != "すべてのエリア":
        query = query.filter(Shop.area == selected_area)

    if visit_status == "未訪問":
        query = query.filter(Shop.is_visited == False)
    elif visit_status == "訪問済み":
        query = query.filter(Shop.is_visited == True)

    # Sort by newest first
    shops = query.order_by(Shop.created_at.desc()).all()

    # -- MAIN PANEL --
    st.markdown('<div class="page-title">Meshi Database</div>', unsafe_allow_html=True)
    st.write("")  # Spacer
    count_text = f"{len(shops):,} 件の結果"
    st.markdown(f'<div class="count-badge">{count_text}</div>', unsafe_allow_html=True)

    if not shops:
        st.info("絞り込み条件に一致するデータがありません。条件を変更するか、新しいデータが登録されるまでお待ちください。")
    else:
        # Convert ORM objects to structured list of dicts
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

        # Display table: hide internal columns (kept in CSV only)
        display_df = df.drop(columns=["_id", "@timestamp", "message_id"])
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "店名": st.column_config.TextColumn(
                    "店名",
                    width="medium"
                ),
                "エリア": st.column_config.TextColumn(
                    "エリア",
                    width="small"
                ),
                "カテゴリ": st.column_config.TextColumn(
                    "カテゴリ",
                    width="small"
                ),
                "訪問済み": st.column_config.CheckboxColumn(
                    "訪問済み",
                    help="実際に訪問した場合にチェックが入ります",
                    width="small"
                ),
                "URL": st.column_config.LinkColumn(
                    "URL",
                    help="Discord 上のメンション元 URL",
                    validate="^https?://",
                    max_chars=100,
                    width="large"
                )
            }
        )

        # Latest document JSON viewer
        with st.expander("最新レコードの詳細を表示"):
            st.json(data[0])

        # CSV export (sidebar)
        # Build CSV with internal columns for round-trip compatibility
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

# -- CSV IMPORT --
st.sidebar.markdown("---")
st.sidebar.markdown('<div class="sidebar-section">CSV インポート（マスター同期）</div>', unsafe_allow_html=True)
uploaded_file = st.sidebar.file_uploader(
    "CSV ファイルをアップロード",
    type=["csv"],
    help="アップロードした CSV をマスターとして扱います。既存レコードは更新され、CSV に存在しないレコードは削除されます。"
)

if uploaded_file is not None:
    if st.sidebar.button("インポートを実行", type="primary"):
        try:
            csv_df = pd.read_csv(
                io.StringIO(uploaded_file.read().decode('utf-8-sig')),
                dtype={"message_id": str}  # Prevent pandas from converting large IDs to float
            )

            def normalize_message_id(val: str) -> str:
                """Convert scientific notation ID (e.g. '1.47725e+18') to full integer string."""
                s = str(val).strip()
                if 'e' in s.lower() or (s.replace('.', '', 1).isdigit() and '.' in s):
                    try:
                        return str(int(float(s)))
                    except ValueError:
                        pass
                return s

            # Validate required columns
            required_cols = {"message_id", "shop.name"}
            if not required_cols.issubset(set(csv_df.columns)):
                st.sidebar.error(f"CSV に必須列が不足しています: {required_cols}")
            else:
                # Normalize boolean column
                if "status.is_visited" in csv_df.columns:
                    csv_df["status.is_visited"] = csv_df["status.is_visited"].map(
                        lambda v: str(v).strip().lower() in ("true", "1", "yes")
                    )

                csv_message_ids = set(
                    normalize_message_id(v) for v in csv_df["message_id"].astype(str)
                )

                import_db = SessionLocal()
                try:
                    updated = 0
                    inserted = 0
                    deleted = 0

                    for _, row in csv_df.iterrows():
                        mid = normalize_message_id(str(row["message_id"]))

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
                    st.sidebar.success(
                        f"インポート完了: {updated} 件更新、{inserted} 件追加、{deleted} 件削除"
                    )
                    st.rerun()
                except Exception as e:
                    import_db.rollback()
                    st.sidebar.error(f"インポートに失敗しました: {e}")
                finally:
                    import_db.close()
        except Exception as e:
            st.sidebar.error(f"CSV の読み込みに失敗しました: {e}")
