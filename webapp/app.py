"""
Logistics DWH Explorer - Streamlit app đọc dữ liệu từ ClickHouse Cloud (database `dwh`).

Chạy:
    pip install streamlit clickhouse-connect
    streamlit run app.py

Cấu hình kết nối (ưu tiên từ trên xuống):
    1. .streamlit/secrets.toml  ->  [clickhouse] host / user / password / database
    2. Biến môi trường          ->  CH_HOST, CH_USER, CH_PASSWORD, CH_DATABASE
    3. Nhập tay ở thanh bên trái của app
"""

import os

import clickhouse_connect
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Logistics DWH Explorer", layout="wide")

# ---------------------------------------------------------------------------
# Kết nối
# ---------------------------------------------------------------------------


def clean_host(h: str) -> str:
    """Bỏ https://, cổng, dấu / để chỉ còn tên host."""
    h = (h or "").strip()
    for prefix in ("https://", "http://", "jdbc:clickhouse://"):
        if h.startswith(prefix):
            h = h[len(prefix):]
    return h.split("/")[0].split(":")[0]


def load_default_creds() -> dict:
    cfg = {}
    try:
        cfg = dict(st.secrets["clickhouse"])
    except Exception:
        pass
    return {
        "host": cfg.get("host") or os.environ.get("CH_HOST", ""),
        "user": cfg.get("user") or os.environ.get("CH_USER", "default"),
        "password": cfg.get("password") or os.environ.get("CH_PASSWORD", ""),
        "database": cfg.get("database") or os.environ.get("CH_DATABASE", "dwh"),
    }


@st.cache_resource
def get_client(host: str, user: str, password: str, database: str):
    return clickhouse_connect.get_client(
        host=host,
        port=8443,
        username=user,
        password=password,
        secure=True,
        database=database,
        connect_timeout=15,
        send_receive_timeout=60,
    )


@st.cache_data(ttl=10, show_spinner=False)
def run_query(sql: str, creds: tuple, params: dict | None = None) -> pd.DataFrame:
    client = get_client(*creds)
    return client.query_df(sql, parameters=params or {})


def qi(name: str) -> str:
    """Quote tên cột / bảng cho ClickHouse."""
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------------------
# Thanh bên: kết nối + chọn bảng
# ---------------------------------------------------------------------------

defaults = load_default_creds()

st.sidebar.title("Kết nối")
host = clean_host(st.sidebar.text_input("Host", defaults["host"], placeholder="xxxx.clickhouse.cloud"))
user = st.sidebar.text_input("User", defaults["user"])
password = st.sidebar.text_input("Password", defaults["password"], type="password")
database = st.sidebar.text_input("Database", defaults["database"])

if not host or not password:
    st.title("Logistics DWH Explorer")
    st.info("Nhập host và password ClickHouse Cloud ở thanh bên trái để bắt đầu.")
    st.stop()

creds = (host, user, password, database)

try:
    tables_df = run_query(
        "SELECT name, engine FROM system.tables WHERE database = {db:String} ORDER BY name",
        creds,
        {"db": database},
    )
except Exception as e:
    st.title("Logistics DWH Explorer")
    st.error(
        "Không kết nối được ClickHouse. Kiểm tra host, user, password, và danh sách IP "
        "được phép truy cập (IP Access List) trong ClickHouse Cloud."
    )
    st.code(str(e))
    st.stop()

if tables_df.empty:
    st.title("Logistics DWH Explorer")
    st.warning(f"Database `{database}` chưa có bảng nào.")
    st.stop()

# Ưu tiên view v_* lên đầu vì đó là chỗ gộp batch + streaming
names = tables_df["name"].tolist()
names.sort(key=lambda n: (not n.startswith("v_"), n))

st.sidebar.divider()
st.sidebar.title("Dữ liệu")
table = st.sidebar.selectbox(
    "Bảng / view",
    names,
    help="Các view v_* gồm cả dữ liệu batch và streaming. Nên dùng chúng cho bảng fact.",
)
live = st.sidebar.toggle("Tự làm mới (30 giây)", value=False)
st.sidebar.caption("Kết quả truy vấn được cache 10 giây.")

full_name = f"{qi(database)}.{qi(table)}"

# ---------------------------------------------------------------------------
# Nội dung chính
# ---------------------------------------------------------------------------


def classify_columns(schema: pd.DataFrame):
    date_cols, num_cols, cat_cols = [], [], []
    for _, row in schema.iterrows():
        t = row["type"]
        if "Date" in t:
            date_cols.append(row["name"])
        elif any(k in t for k in ("Int", "Float", "Decimal")):
            num_cols.append(row["name"])
        elif any(k in t for k in ("String", "LowCardinality", "Enum", "FixedString")):
            cat_cols.append(row["name"])
    return date_cols, num_cols, cat_cols


def render_body():
    schema = run_query(f"DESCRIBE TABLE {full_name}", creds)[["name", "type"]]
    date_cols, num_cols, cat_cols = classify_columns(schema)

    st.title(table)
    st.caption(f"{database}.{table}")

    total = int(run_query(f"SELECT count() AS n FROM {full_name}", creds)["n"][0])

    k1, k2, k3 = st.columns(3)
    k1.metric("Tổng số dòng", f"{total:,}")
    k2.metric("Số cột", len(schema))
    if date_cols:
        rng = run_query(
            f"SELECT min({qi(date_cols[0])}) AS lo, max({qi(date_cols[0])}) AS hi FROM {full_name}",
            creds,
        )
        k3.metric(f"Mới nhất ({date_cols[0]})", str(rng["hi"][0])[:19])
    else:
        k3.metric("Cột ngày", "không có")

    tab_chart, tab_cat, tab_data, tab_sql, tab_schema = st.tabs(
        ["Xu hướng theo thời gian", "Phân bố theo nhóm", "Dữ liệu", "SQL tự do", "Cấu trúc bảng"]
    )

    # --- Xu hướng theo thời gian ------------------------------------------
    with tab_chart:
        if not date_cols:
            st.info("Bảng này không có cột ngày, hãy xem tab Phân bố theo nhóm hoặc Dữ liệu.")
        else:
            c1, c2, c3, c4 = st.columns(4)
            date_col = c1.selectbox("Cột ngày", date_cols, key="t_date")
            agg = c2.selectbox("Phép tính", ["Đếm dòng", "Tổng", "Trung bình"], key="t_agg")
            gran = c3.selectbox("Gộp theo", ["Ngày", "Tuần", "Tháng"], index=2, key="t_gran")
            window = c4.selectbox(
                "Khoảng thời gian",
                ["Tất cả", "30 ngày gần nhất", "90 ngày gần nhất", "1 năm gần nhất"],
                key="t_win",
            )

            metric_col = None
            if agg != "Đếm dòng":
                if not num_cols:
                    st.warning("Bảng không có cột số, chuyển sang Đếm dòng.")
                    agg = "Đếm dòng"
                else:
                    metric_col = st.selectbox("Cột số", num_cols, key="t_metric")

            d = qi(date_col)
            bucket = {
                "Ngày": f"toDate({d})",
                "Tuần": f"toStartOfWeek(toDate({d}))",
                "Tháng": f"toStartOfMonth(toDate({d}))",
            }[gran]
            value = {
                "Đếm dòng": "count()",
                "Tổng": f"sum({qi(metric_col)})" if metric_col else "count()",
                "Trung bình": f"avg({qi(metric_col)})" if metric_col else "count()",
            }[agg]
            days = {"Tất cả": None, "30 ngày gần nhất": 30, "90 ngày gần nhất": 90, "1 năm gần nhất": 365}[window]
            where = f"WHERE {d} >= (SELECT max({d}) FROM {full_name}) - INTERVAL {days} DAY" if days else ""

            sql = (
                f"SELECT {bucket} AS period, {value} AS value FROM {full_name} "
                f"{where} GROUP BY period ORDER BY period LIMIT 5000"
            )
            ts = run_query(sql, creds)
            if ts.empty:
                st.info("Không có dữ liệu trong khoảng này.")
            else:
                st.line_chart(ts.set_index("period")["value"])
                with st.expander("Xem câu SQL đã chạy"):
                    st.code(sql, language="sql")

    # --- Phân bố theo nhóm -------------------------------------------------
    with tab_cat:
        if not cat_cols:
            st.info("Bảng này không có cột dạng chữ để nhóm.")
        else:
            c1, c2, c3 = st.columns(3)
            cat = c1.selectbox("Nhóm theo", cat_cols, key="c_cat")
            agg2 = c2.selectbox("Phép tính", ["Đếm dòng", "Tổng", "Trung bình"], key="c_agg")
            top_n = c3.slider("Hiển thị top", 5, 50, 15, key="c_top")

            metric2 = None
            if agg2 != "Đếm dòng":
                if not num_cols:
                    st.warning("Bảng không có cột số, chuyển sang Đếm dòng.")
                    agg2 = "Đếm dòng"
                else:
                    metric2 = st.selectbox("Cột số", num_cols, key="c_metric")

            value2 = {
                "Đếm dòng": "count()",
                "Tổng": f"sum({qi(metric2)})" if metric2 else "count()",
                "Trung bình": f"avg({qi(metric2)})" if metric2 else "count()",
            }[agg2]

            sql2 = (
                f"SELECT {qi(cat)} AS grp, {value2} AS value FROM {full_name} "
                f"GROUP BY grp ORDER BY value DESC LIMIT {int(top_n)}"
            )
            cat_df = run_query(sql2, creds)
            st.bar_chart(cat_df.set_index("grp")["value"])
            st.dataframe(cat_df, use_container_width=True, hide_index=True)

    # --- Dữ liệu -----------------------------------------------------------
    with tab_data:
        c1, c2 = st.columns([1, 2])
        n = c1.number_input("Số dòng tải về", 10, 20000, 500, step=100, key="d_n")
        order_opts = ["(không sắp xếp)"] + schema["name"].tolist()
        order_col = c2.selectbox("Sắp xếp giảm dần theo", order_opts, key="d_order")
        order_sql = "" if order_col == order_opts[0] else f"ORDER BY {qi(order_col)} DESC"
        data = run_query(f"SELECT * FROM {full_name} {order_sql} LIMIT {int(n)}", creds)
        st.dataframe(data, use_container_width=True, hide_index=True)
        st.download_button(
            "Tải CSV",
            data.to_csv(index=False).encode("utf-8"),
            file_name=f"{table}.csv",
            mime="text/csv",
        )

    # --- SQL tự do ---------------------------------------------------------
    with tab_sql:
        st.caption("Chỉ cho phép SELECT, WITH, SHOW, DESCRIBE.")
        user_sql = st.text_area(
            "Câu truy vấn",
            f"SELECT * FROM {full_name} LIMIT 100",
            height=140,
            key="s_sql",
        )
        if st.button("Chạy", key="s_run"):
            first = user_sql.strip().split(None, 1)[0].upper() if user_sql.strip() else ""
            if first not in {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"}:
                st.error("Chỉ cho phép câu lệnh đọc dữ liệu.")
            else:
                try:
                    st.dataframe(run_query(user_sql, creds), use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(str(e))

    # --- Cấu trúc bảng -----------------------------------------------------
    with tab_schema:
        st.dataframe(schema, use_container_width=True, hide_index=True)


try:
    if live:
        st.fragment(render_body, run_every=30)()
    else:
        render_body()
except Exception as e:
    st.error("Có lỗi khi truy vấn dữ liệu.")
    st.code(str(e))
