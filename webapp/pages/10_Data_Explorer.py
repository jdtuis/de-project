"""Khám phá dữ liệu: xem nhanh bất kỳ bảng hoặc view nào trong warehouse, vẽ biểu đồ và chạy SQL đọc dữ liệu.
Đây là bản chuyển từ app.py cũ sang một trang trong menu."""

import streamlit as st

from common import get_creds, qi, run_query

creds = get_creds()
DB = creds[3]

st.title("Khám phá dữ liệu")

tables = run_query(
    f"SELECT name FROM system.tables WHERE database = '{DB.replace(chr(39), '')}' ORDER BY name", creds
)["name"].tolist()
if not tables:
    st.warning(f"Database `{DB}` chưa có bảng nào.")
    st.stop()
tables.sort(key=lambda n: (not n.startswith("v_"), n))

c1, c2 = st.columns([3, 1])
table = c1.selectbox("Bảng hoặc view", tables, help="Các view v_* gồm cả dữ liệu batch và streaming.")
limit = c2.number_input("Số dòng", 10, 5000, 500, step=100)
full = f"{qi(DB)}.{qi(table)}"

tab_data, tab_chart, tab_sql, tab_schema = st.tabs(["Dữ liệu", "Biểu đồ nhanh", "SQL tự do", "Cấu trúc"])

df = run_query(f"SELECT * FROM {full} LIMIT {int(limit)}", creds)
total = run_query(f"SELECT count() AS n FROM {full}", creds)

with tab_data:
    st.metric("Tổng số dòng", f"{int(total['n'].iloc[0]):,}" if len(total) else "n/a")
    st.dataframe(df, width="stretch", hide_index=True)
    st.download_button("Tải CSV", df.to_csv(index=False).encode("utf-8"), file_name=f"{table}.csv", mime="text/csv")

with tab_chart:
    nums = df.select_dtypes("number").columns.tolist()
    if not nums:
        st.info("Bảng này không có cột số để vẽ.")
    else:
        x = st.selectbox("Nhóm theo", df.columns.tolist())
        y = st.selectbox("Cộng tổng cột", nums)
        st.bar_chart(df.groupby(x)[y].sum().rename(f"Tổng {y}").head(30))
        st.caption(f"Biểu đồ chỉ dựa trên {len(df):,} dòng đã tải ở trên.")

with tab_sql:
    st.caption("Chỉ cho phép SELECT, WITH, SHOW, DESCRIBE.")
    sql = st.text_area("Câu truy vấn", f"SELECT * FROM {full} LIMIT 100", height=140)
    if st.button("Chạy"):
        first = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if first not in {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"}:
            st.error("Chỉ cho phép câu lệnh đọc dữ liệu.")
        else:
            try:
                st.dataframe(run_query(sql, creds), width="stretch", hide_index=True)
            except Exception as e:
                st.error(str(e))

with tab_schema:
    st.dataframe(run_query(f"DESCRIBE TABLE {full}", creds)[["name", "type"]], width="stretch", hide_index=True)
