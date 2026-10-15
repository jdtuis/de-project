"""Hàm dùng chung cho các trang: kết nối ClickHouse, tìm bảng và cột."""

import os

import clickhouse_connect
import pandas as pd
import streamlit as st


def clean_host(h: str) -> str:
    h = (h or "").strip()
    for prefix in ("jdbc:clickhouse://", "https://", "http://"):
        if h.startswith(prefix):
            h = h[len(prefix):]
    return h.split("/")[0].split(":")[0]


def _defaults() -> dict:
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


def get_creds() -> tuple:
    """Lấy thông tin kết nối từ secrets / biến môi trường, nếu thiếu thì hỏi ở thanh bên."""
    d = _defaults()
    if d["host"] and d["password"]:
        return (clean_host(d["host"]), d["user"], d["password"], d["database"])

    saved = st.session_state.get("_creds", {})
    st.sidebar.title("Kết nối")
    host = clean_host(st.sidebar.text_input("Host", saved.get("host", ""), placeholder="xxxx.clickhouse.cloud"))
    user = st.sidebar.text_input("User", saved.get("user", "default"))
    password = st.sidebar.text_input("Password", saved.get("password", ""), type="password")
    database = st.sidebar.text_input("Database", saved.get("database", "dwh"))
    st.session_state["_creds"] = {"host": host, "user": user, "password": password, "database": database}
    if not host or not password:
        st.info("Nhập host và password ClickHouse Cloud ở thanh bên trái để bắt đầu.")
        st.stop()
    return (host, user, password, database)


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
        send_receive_timeout=120,
    )


def _query(sql: str, creds: tuple) -> pd.DataFrame:
    return get_client(*creds).query_df(sql)


@st.cache_data(ttl=10, show_spinner=False)
def run_query(sql: str, creds: tuple) -> pd.DataFrame:
    """Cache ngắn (10 giây), dùng cho dữ liệu gần realtime."""
    return _query(sql, creds)


@st.cache_data(ttl=300, show_spinner="Đang truy vấn dữ liệu...")
def run_query_slow(sql: str, creds: tuple) -> pd.DataFrame:
    """Cache 5 phút, dùng cho báo cáo tổng hợp theo tháng."""
    return _query(sql, creds)


@st.cache_data(ttl=300, show_spinner=False)
def _list_tables(creds: tuple) -> list:
    df = get_client(*creds).query_df(
        "SELECT name FROM system.tables WHERE database = {db:String}",
        parameters={"db": creds[3]},
    )
    return df["name"].tolist()


@st.cache_data(ttl=300, show_spinner=False)
def table_columns(creds: tuple, table: str) -> list:
    df = get_client(*creds).query_df(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tb:String} ORDER BY position",
        parameters={"db": creds[3], "tb": table},
    )
    return df["name"].tolist()


def qi(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def resolve_table(creds: tuple, name: str, required: bool = True):
    """Tìm bảng `name`. Ưu tiên view v_<name> (gộp batch + streaming), nếu không có thì dùng bảng thường.
    Trả về (tên đầy đủ đã quote, tên ngắn)."""
    names = set(_list_tables(creds))
    for cand in (f"v_{name}", name):
        if cand in names:
            return f"{qi(creds[3])}.{qi(cand)}", cand
    if required:
        st.error(f"Không tìm thấy bảng `{name}` (hoặc `v_{name}`) trong database `{creds[3]}`.")
        st.write("Các bảng hiện có:")
        st.code(", ".join(sorted(names)) or "(trống)")
        st.stop()
    return None, None


def pick(cols: list, *candidates: str):
    """Trả về tên cột đầu tiên có trong `cols` (không phân biệt hoa thường), hoặc None."""
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def require(found: dict, available: dict | None = None):
    """Dừng trang với thông báo rõ ràng nếu thiếu cột bắt buộc."""
    missing = [k for k, v in found.items() if v is None]
    if missing:
        st.error("Không tìm thấy các cột: " + ", ".join(missing))
        for table, cols in (available or {}).items():
            st.write(f"Các cột của `{table}`:")
            st.code(", ".join(cols))
        st.info("Mở file của trang này và sửa danh sách tên cột trong các lệnh pick(...) cho khớp với bảng của bạn.")
        st.stop()


def dt(col: str) -> str:
    """Biểu thức đọc cột ngày giờ an toàn (cả khi cột đang là String)."""
    return f"parseDateTimeBestEffortOrNull(toString({qi(col)}))"


def num(col: str) -> str:
    """Biểu thức đọc cột số an toàn (cả khi cột đang là String)."""
    return f"toFloat64OrNull(toString({qi(col)}))"
