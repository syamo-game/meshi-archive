import re
import os
import streamlit as st
import pandas as pd
import io

from db.database import SessionLocal
from db.models import Shop, Message

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    st.error("ADMIN_PASSWORD が設定されていません。管理ページは無効です。")
    st.stop()

if not st.session_state.get("admin_authenticated", False):
    st.markdown("""
    <style>
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
        <div class="login-title">管理者ログイン</div>
        <div class="login-desc">管理ページにアクセスするには管理者パスワードが必要です。</div>
    </div>
    """, unsafe_allow_html=True)
    pwd = st.text_input("管理者パスワード", type="password", placeholder="パスワードを入力してください")
    if st.button("ログイン", type="primary"):
        if pwd == ADMIN_PASSWORD:
            st.session_state["admin_authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが正しくありません。")
    st.stop()

st.markdown('<div class="page-title">管理</div>', unsafe_allow_html=True)
st.write("")

_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_ROWS = 5_000
_SAFE_URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)
_CSV_INJECT_CHARS = ('=', '+', '-', '@', '\t', '\r')


def _sanitize_text(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    while s and s[0] in _CSV_INJECT_CHARS:
        s = s[1:].strip()
    return s or None


def _sanitize_url(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s if _SAFE_URL_PATTERN.match(s) else None


def normalize_message_id(val: str) -> str:
    """Convert scientific notation ID (e.g. '1.47725e+18') to full integer string."""
    s = str(val).strip()
    if 'e' in s.lower() or (s.replace('.', '', 1).isdigit() and '.' in s):
        try:
            return str(int(float(s)))
        except ValueError:
            pass
    return s


# -- CSV IMPORT --
st.markdown("### CSV インポート")
st.caption("アップロードした CSV をマスターとして扱います。CSV に存在しないレコードはデータベースから削除されます。")

uploaded_file = st.file_uploader("CSV ファイルをアップロード", type=["csv"])

if uploaded_file is not None:
    raw_bytes = uploaded_file.read()

    if len(raw_bytes) > _MAX_FILE_BYTES:
        st.error(f"ファイルサイズが上限を超えています（最大 {_MAX_FILE_BYTES // 1024 // 1024} MB）。")
    else:
        try:
            csv_df = pd.read_csv(
                io.StringIO(raw_bytes.decode('utf-8-sig')),
                dtype={"message_id": str}
            )

            st.dataframe(csv_df.head(5), use_container_width=True)
            st.caption(f"{len(csv_df):,} 行")

            if len(csv_df) > _MAX_ROWS:
                st.error(f"行数が上限を超えています（最大 {_MAX_ROWS:,} 行）。")
            else:
                required_cols = {"message_id", "shop.name"}
                if not required_cols.issubset(set(csv_df.columns)):
                    st.error(f"CSV に必須列が不足しています: {required_cols}")
                else:
                    st.warning("この操作は既存データを上書きします。削除されたレコードは復元できません。")
                    if st.button("インポートを実行", type="primary"):
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
                                shop_name = _sanitize_text(row.get("shop.name")) or "Unknown"
                                area = _sanitize_text(row.get("shop.area"))
                                category = _sanitize_text(row.get("shop.category"))
                                url = _sanitize_url(row.get("url"))
                                is_visited = bool(row.get("status.is_visited", False))

                                existing_msg = import_db.query(Message).filter_by(message_id=mid).first()
                                if not existing_msg:
                                    import_db.add(Message(message_id=mid, is_target=True))

                                existing_shop = import_db.query(Shop).filter_by(message_id=mid).first()
                                if existing_shop:
                                    existing_shop.shop_name = shop_name
                                    existing_shop.area = area
                                    existing_shop.category = category
                                    existing_shop.url = url
                                    existing_shop.is_visited = is_visited
                                    updated += 1
                                else:
                                    import_db.add(Shop(
                                        message_id=mid,
                                        shop_name=shop_name,
                                        area=area,
                                        category=category,
                                        url=url,
                                        is_visited=is_visited
                                    ))
                                    inserted += 1

                            all_shops = import_db.query(Shop).all()
                            for shop in all_shops:
                                if shop.message_id not in csv_message_ids:
                                    import_db.delete(shop)
                                    deleted += 1

                            import_db.commit()
                            st.success(
                                f"インポート完了: {updated} 件更新、{inserted} 件追加、{deleted} 件削除"
                            )
                        except Exception as e:
                            import_db.rollback()
                            st.error(f"インポートに失敗しました: {e}")
                        finally:
                            import_db.close()

        except Exception as e:
            st.error(f"CSV の読み込みに失敗しました: {e}")
