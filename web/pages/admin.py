import re
import streamlit as st
import pandas as pd
import io

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from db.database import SessionLocal, init_db
from db.models import Shop, Message

init_db()

st.set_page_config(
    page_title="Meshi Archive - Admin",
    page_icon="🔎",
    layout="wide",
)

# Admin auth (separate from WEB_PASSWORD)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    st.error("ADMIN_PASSWORD is not configured. Admin page is disabled.")
    st.stop()

if not st.session_state.get("admin_authenticated", False):
    st.title("Admin Login")
    pwd = st.text_input("Admin password", type="password")
    if st.button("Login"):
        if pwd == ADMIN_PASSWORD:
            st.session_state["admin_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()

st.markdown("## Admin")

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
    s = str(val).strip()
    if 'e' in s.lower() or (s.replace('.', '', 1).isdigit() and '.' in s):
        try:
            return str(int(float(s)))
        except ValueError:
            pass
    return s


# -- CSV IMPORT --
st.markdown("### Import CSV")
st.caption("CSV is treated as master. Rows missing from the CSV will be deleted from the database.")

uploaded_file = st.file_uploader("Upload CSV", type=["csv"])

if uploaded_file is not None:
    raw_bytes = uploaded_file.read()

    if len(raw_bytes) > _MAX_FILE_BYTES:
        st.error(f"File too large (max {_MAX_FILE_BYTES // 1024 // 1024} MB).")
    else:
        try:
            csv_df = pd.read_csv(
                io.StringIO(raw_bytes.decode('utf-8-sig')),
                dtype={"message_id": str}
            )

            st.dataframe(csv_df.head(5), use_container_width=True)
            st.caption(f"{len(csv_df):,} rows")

            if len(csv_df) > _MAX_ROWS:
                st.error(f"Too many rows (max {_MAX_ROWS:,}).")
            else:
                required_cols = {"message_id", "shop.name"}
                if not required_cols.issubset(set(csv_df.columns)):
                    st.error(f"CSV must contain: {required_cols}")
                else:
                    st.warning("This will overwrite existing data. Deleted rows cannot be recovered.")
                    if st.button("Confirm and import", type="primary"):
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
                            st.success(f"Done: {updated} updated, {inserted} added, {deleted} removed")
                        except Exception as e:
                            import_db.rollback()
                            st.error(f"Import failed: {e}")
                        finally:
                            import_db.close()

        except Exception as e:
            st.error(f"Failed to read CSV: {e}")
